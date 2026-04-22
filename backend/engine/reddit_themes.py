"""Reddit Themes：把本周 Reddit 高分帖（不限 matched_model）喂给 LLM，归纳 3-5 个热议主题。

与 reddit_opinions 的区别：
- reddit_opinions 按 matched_model 聚合，要求精确匹配到某 canonical 型号，产出"谁怎么评价 XX 模型"
- reddit_themes 不要求 matched_model，按 score 取本周 Top 30 帖，让 LLM 归纳"开发者在聊什么话题"

用途：alias 匹配永远跟不上新模型和新话题，用主题归纳兜底，避免漏掉热议讨论。

输出：
{
  "themes": [
    {"title": "xxx", "summary": "xxx", "posts": [{"title":..., "url":...}]},
    ...
  ],
  "post_count": 30,
  "used_llm": True,
  "fallback_md": "",
}
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from backend.db import get_conn
from backend.engine import alias_learner
from backend.utils import llm_client

logger = logging.getLogger(__name__)


HUMANIZER_PRINCIPLES = """写作原则：
- 忠实归纳，不渲染。禁用："展现了卓越能力"/"令人瞩目"/"里程碑式"/"革命性"/"引领潮流"。
- 主题标题要具体。"大家在聊新模型"太虚；"Claude Opus 4.7 的工具调用稳定性争议" 就具体。
- summary 30-80 字，说清楚讨论的核心问题 / 对比 / 吐槽点。
- 别拼凑主题。宁可只归 2 个实打实的主题，也不硬凑到 5 个。meme / 纯情绪帖不成主题。"""


def _fetch_top_posts(conn, days: int, limit: int = 30) -> list[dict]:
    """按 score 倒序取本周 Top N 帖（不限 matched_model，包含所有 sub）。"""
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    rows = conn.execute(
        """
        SELECT post_id, subreddit, title, selftext, url, score,
               num_comments, created_utc, matched_model
        FROM reddit_posts
        WHERE created_utc >= ? AND score >= 5
        ORDER BY score DESC
        LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _format_post(p: dict, i: int) -> str:
    title = (p["title"] or "")[:180]
    body = (p.get("selftext") or "").replace("\n", " ")[:300]
    matched = f" · matched={p['matched_model']}" if p.get("matched_model") else ""
    return (
        f"[{i}] r/{p['subreddit']} · score={p['score']} · {p['num_comments']} 评论{matched}\n"
        f"   标题: {title}\n"
        f"   正文: {body or '(无正文)'}\n"
        f"   url: {p['url']}"
    )


def _build_prompt(posts: list[dict]) -> list[dict]:
    posts_lines = "\n\n".join(_format_post(p, i + 1) for i, p in enumerate(posts))
    user_content = (
        f"{HUMANIZER_PRINCIPLES}\n\n"
        f"任务：下面是本周 Reddit（r/LocalLLaMA、r/ChatGPT、r/StableDiffusion、r/singularity 等）按 score 排序的 Top {len(posts)} 帖。\n"
        f"请归纳出 3-5 个**开发者社区本周在真正讨论的话题**，每个话题给一个 JSON 对象：\n"
        f'  - "title":   中文主题标题，20 字以内，要具体（指名模型/技术点/争议点）\n'
        f'  - "summary": 中文一句话说清楚这个话题的核心——有人吐槽/对比/求助/晒效果/什么争议，30-80 字\n'
        f'  - "post_ids": 数组，挑 1-3 条最代表该主题的帖子编号（上面 [N] 里的 N）\n'
        f'  - "models":   数组，列出这个主题里被明确提到的**具体模型名**（带版本号的，例如 "Gemini 3.1 Flash"、"muse-spark"、"Qwen3-Coder"），没有就给空数组。\n'
        f'                 注意：只列真·模型名，不要把"LLM"、"reasoning model"、"open-source"这种通用词当模型。\n\n'
        f"要求：\n"
        f"- 主题之间内容应有差异，别三个主题都在讲 Sonnet。\n"
        f"- 如果 Top {len(posts)} 帖大部分是 meme / 情绪 / 求助 / 广告，哪怕只能归出 2 个真正的话题也可以，不要硬凑。\n"
        f"- 主题可以覆盖：模型对比、具体模型的 bug 或亮点、工具链讨论（CLI/Agent/RAG）、行业动态（价格/政策）、技术争议等。\n\n"
        f"--- 帖子 ---\n"
        f"{posts_lines}\n\n"
        f"直接输出 JSON 数组，不要加代码块、不要加解释。"
    )
    return [
        {"role": "system", "content": "你是一名中文技术周报作者，擅长从英文论坛上归纳开发者社区在讨论的话题。"},
        {"role": "user",   "content": user_content},
    ]


def _parse_json(raw: str | None) -> list[dict]:
    if not raw:
        return []
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
    except Exception as e:
        logger.warning("[Themes] JSON 解析失败: %s", e)
        return []
    if isinstance(obj, dict) and "items" in obj:
        obj = obj["items"]
    return obj if isinstance(obj, list) else []


def generate(days: int = 7, top_posts: int = 30) -> dict:
    """归纳本周 Reddit 热议主题。"""
    with get_conn() as conn:
        posts = _fetch_top_posts(conn, days=days, limit=top_posts)

    if not posts:
        return {"themes": [], "post_count": 0, "used_llm": False,
                "fallback_md": "本周 Reddit 帖子过少，无法归纳热议主题。"}

    raw = llm_client.chat(_build_prompt(posts), temperature=0.4, max_tokens=1200)
    items = _parse_json(raw)

    # 后处理：把 post_ids 映射回帖子实体；过滤格式不对的
    themes: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title   = (it.get("title")   or "").strip()
        summary = (it.get("summary") or "").strip()
        ids     = it.get("post_ids") or []
        models  = it.get("models")   or []
        if not title or not summary:
            continue
        post_refs = []
        for pid in ids:
            try:
                idx = int(pid) - 1
            except Exception:
                continue
            if 0 <= idx < len(posts):
                p = posts[idx]
                post_refs.append({
                    "title": p["title"],
                    "url":   p["url"],
                    "subreddit": p["subreddit"],
                    "score": p["score"],
                })
        # LLM 识别的模型名喂给 alias_learner，不在表里的会进 pending_model_aliases
        if isinstance(models, list):
            sample_url = post_refs[0]["url"] if post_refs else None
            try:
                alias_learner.record_llm_candidates(
                    [m for m in models if isinstance(m, str)],
                    sample_url=sample_url,
                )
            except Exception as e:
                logger.warning("[Themes] alias_learner.record_llm_candidates 失败: %s", e)
        themes.append({"title": title, "summary": summary, "posts": post_refs})

    logger.info("[Themes] 归纳 %d 个主题（基于 %d 个帖子）", len(themes), len(posts))
    return {
        "themes":      themes,
        "post_count":  len(posts),
        "used_llm":    bool(raw),
        "fallback_md": "" if themes else "本周 Reddit 热门帖多为 meme / 求助，未归纳出明显技术主题。",
    }


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    r = generate()
    print(f"\npost_count={r['post_count']} used_llm={r['used_llm']} themes={len(r['themes'])}")
    for t in r["themes"]:
        print(f"\n【{t['title']}】")
        print(f"  {t['summary']}")
        for p in t["posts"]:
            print(f"    · r/{p['subreddit']} · score={p['score']} · {p['title'][:70]}")
            print(f"      {p['url']}")
