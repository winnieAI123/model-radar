"""Community Digest：把过去 N 天的 reddit_posts 汇总成一段中文小结。

输出给周报拼装用。失败时降级到纯模板（统计数字），不会让整个周报挂掉。

核心 prompt 里内嵌 humanizer 原则：
- 禁止"展现了卓越能力"/"充分体现了"/"令人瞩目"等套话
- 禁止三段并列总结结尾
- 当陈述事实，不做营销包装
- 中文、短句
"""
import logging
from datetime import datetime, timedelta, timezone
from collections import Counter

from backend.db import get_conn
from backend.utils import llm_client

logger = logging.getLogger(__name__)


HUMANIZER_PRINCIPLES = """你在给一个技术团队写周报，读者是工程师和产品经理，不喜欢营销式表达。

写作原则：
- 陈述事实，不做情绪渲染。别用"展现了卓越能力"、"充分体现了"、"令人瞩目"、"里程碑式"、"革命性"、"引领了行业潮流"。
- 别用三段并列的总结性结尾（例如"它不仅……还……更……"）。
- 短句，信息密度优先。能用一句话说完的不分两句。
- 具体 > 抽象。别说"反响热烈"，说"某贴收到 500+ 评论、多人抱怨延迟"。
- 如果某模型信息稀少，直接写"讨论量少"，不要硬凑。"""


def _fetch_recent_posts(days: int = 7) -> list[dict]:
    """过去 N 天的 reddit_posts，按 score 倒序。"""
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT post_id, subreddit, title, selftext, url, score,
                   num_comments, created_utc, matched_model
            FROM reddit_posts
            WHERE created_utc >= ?
            ORDER BY score DESC
            """,
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def _model_stats(posts: list[dict]) -> list[tuple[str, int, int]]:
    """按 matched_model 统计 (model, post_count, total_score)。"""
    c = Counter()
    s = Counter()
    for p in posts:
        m = p.get("matched_model")
        if not m:
            continue
        c[m] += 1
        s[m] += p.get("score") or 0
    return [(m, c[m], s[m]) for m in sorted(c, key=lambda k: (-c[k], -s[k]))]


def _format_post_for_prompt(p: dict, i: int) -> str:
    title = p["title"][:120]
    body = (p.get("selftext") or "")[:300].replace("\n", " ")
    tag = f"[模型: {p['matched_model']}] " if p.get("matched_model") else ""
    return (
        f"{i}. {tag}r/{p['subreddit']} · score={p['score']} · {p['num_comments']} 评论\n"
        f"   标题: {title}\n"
        f"   正文: {body or '(无正文)'}"
    )


def _template_digest(posts: list[dict], stats: list[tuple[str, int, int]]) -> str:
    """LLM 挂了时的降级版本：纯数字。"""
    if not posts:
        return "本周 Reddit 无相关帖子。"
    parts = [f"本周共抓到 **{len(posts)}** 条帖子（LocalLLaMA / StableDiffusion / singularity / ChatGPT 等 sub）。"]
    if stats:
        parts.append("\n**被提及最多的模型 Top 5：**")
        for m, cnt, score in stats[:5]:
            parts.append(f"- `{m}` — {cnt} 帖，累计 {score} 分")
    parts.append("\n**最热帖 Top 3：**")
    for p in posts[:3]:
        parts.append(f"- [r/{p['subreddit']}] {p['title'][:80]} · {p['score']} 分 · [链接]({p['url']})")
    return "\n".join(parts)


def _build_llm_prompt(posts: list[dict], stats: list[tuple[str, int, int]]) -> list[dict]:
    top_posts = posts[:20]
    stats_lines = [f"- `{m}`: {cnt} 帖 / {score} 分" for m, cnt, score in stats[:8]]

    user_content = (
        f"{HUMANIZER_PRINCIPLES}\n\n"
        f"任务：把下面这批 Reddit 热帖（按 score 倒序）总结成一份 200-400 字的「本周社区声音」中文小结，"
        f"重点讲模型/产品/工具层面的讨论趋势，不要流水账复述标题。\n\n"
        f"--- 模型被提及统计 ---\n"
        + "\n".join(stats_lines) + "\n\n"
        f"--- 热帖明细 ---\n"
        + "\n\n".join(_format_post_for_prompt(p, i + 1) for i, p in enumerate(top_posts)) + "\n\n"
        f"输出纯 Markdown 正文（别加标题，别加 ```），一段或两段即可。"
    )

    return [
        {"role": "system", "content": "你是一名中文技术周报作者，服务对象是云服务团队的工程师。"},
        {"role": "user",   "content": user_content},
    ]


def generate(days: int = 7) -> dict:
    """生成社区摘要。返回 {summary_md, post_count, model_stats, used_llm, period_days}."""
    posts = _fetch_recent_posts(days=days)
    stats = _model_stats(posts)

    if not posts:
        return {
            "summary_md":  "本周 Reddit 无相关帖子（可能是代理/限流问题，检查 `system_status` 表里 reddit 采集器状态）。",
            "post_count":  0,
            "model_stats": [],
            "used_llm":    False,
            "period_days": days,
        }

    logger.info("[Digest] 期 %dd 帖数=%d 命中模型=%d", days, len(posts), len(stats))

    summary = llm_client.chat(_build_llm_prompt(posts, stats), temperature=0.4, max_tokens=1200)
    used_llm = bool(summary)
    if not summary:
        logger.warning("[Digest] LLM 失败，降级到模板")
        summary = _template_digest(posts, stats)

    return {
        "summary_md":  summary.strip(),
        "post_count":  len(posts),
        "model_stats": stats[:10],
        "used_llm":    used_llm,
        "period_days": days,
    }


if __name__ == "__main__":
    import json as _json
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    r = generate(days=7)
    print(_json.dumps({k: v for k, v in r.items() if k != "summary_md"}, ensure_ascii=False, indent=2))
    print("\n--- Summary MD ---")
    print(r["summary_md"])
