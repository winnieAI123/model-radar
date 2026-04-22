"""Reddit Opinions：按 matched_model 聚合用户观点。

周报"💬 社区声音"段用。输出结构：
{
  "models": [
    {
      "model": "Claude Opus 4.7",
      "post_count": 12,
      "opinions": [
        {"quote": "一位开发者表示...", "url": "https://reddit.com/r/..."},
        ...
      ],
      "used_llm": True,
    },
    ...
  ],
  "fallback_md": "(LLM 全挂时的兜底文字)",
}

每个热门模型给 LLM 2-3 条"有开发者表示..."的观点，带原帖链接引用。未匹配到模型的 meme 帖直接忽略。
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from backend.db import get_conn
from backend.utils import llm_client

logger = logging.getLogger(__name__)


HUMANIZER_PRINCIPLES = """写作原则：
- 忠实复述用户原帖核心观点，不夸大。禁用："展现了卓越能力"/"令人瞩目"/"里程碑式"/"彻底改变"/"引领潮流"。
- 短句优先。每条观点 30-80 字。
- 具体 > 抽象：原帖提到什么场景、什么对比、什么 bug，就说什么；别说"反响热烈"这种空话。
- 使用"有开发者表示"/"一位用户分享"/"多人提到"这类自然引用式措辞。
- 如果原帖只是 meme/玩笑/情绪宣泄没有技术观点，跳过，不强行总结。"""


def _fetch_posts_for_model(conn, model: str, days: int) -> list[dict]:
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    rows = conn.execute(
        """
        SELECT post_id, subreddit, title, selftext, url, score,
               num_comments, created_utc
        FROM reddit_posts
        WHERE matched_model = ? AND created_utc >= ?
        ORDER BY score DESC
        LIMIT 20
        """,
        (model, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def _top_models_by_posts(conn, days: int, limit: int) -> list[tuple[str, int]]:
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    rows = conn.execute(
        """
        SELECT matched_model, COUNT(*) AS cnt
        FROM reddit_posts
        WHERE matched_model IS NOT NULL AND matched_model != '' AND created_utc >= ?
        GROUP BY matched_model
        ORDER BY cnt DESC, MAX(score) DESC
        LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()
    return [(r["matched_model"], r["cnt"]) for r in rows]


def _format_post_for_prompt(p: dict, i: int) -> str:
    title = (p["title"] or "")[:140]
    body = (p.get("selftext") or "").replace("\n", " ")[:400]
    return (
        f"[{i}] r/{p['subreddit']} · score={p['score']} · {p['num_comments']} 评论 · url={p['url']}\n"
        f"   标题: {title}\n"
        f"   正文: {body or '(无正文)'}"
    )


def _build_prompt(model: str, posts: list[dict]) -> list[dict]:
    posts_lines = "\n\n".join(_format_post_for_prompt(p, i + 1) for i, p in enumerate(posts))
    user_content = (
        f"{HUMANIZER_PRINCIPLES}\n\n"
        f"任务：下面是过去 7 天 Reddit 上关于模型『{model}』的相关帖子。\n"
        f"请挑出 2-3 条有实质技术观点的用户反馈，每条给我一个 JSON 对象：\n"
        f'  - "quote": 中文一句话转述（别照抄原英文），以"有开发者表示"/"一位用户分享"/"多人提到"开头，30-80 字\n'
        f'  - "url":   对应原帖的 url（必须从输入里选一条）\n\n'
        f"要求：\n"
        f"- 如果所有帖子都是 meme / 玩笑 / 无实质观点，返回空数组 []\n"
        f"- 优先挑 score 高、带正文的帖子\n"
        f"- 不同观点之间要有差异（别三条都说同一个意思）\n\n"
        f"--- 帖子（按 score 倒序）---\n"
        f"{posts_lines}\n\n"
        f"直接输出 JSON 数组，不要加代码块、不要加解释。"
    )
    return [
        {"role": "system", "content": "你是一名中文技术周报作者，擅长从英文论坛帖子里提炼技术观点。"},
        {"role": "user",   "content": user_content},
    ]


def _parse_json_array(raw: str | None) -> list[dict]:
    if not raw:
        return []
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
    except Exception as e:
        logger.warning("[Opinions] JSON 解析失败: %s", e)
        return []
    if isinstance(obj, dict) and "items" in obj:
        obj = obj["items"]
    return obj if isinstance(obj, list) else []


def _opinions_for_model(conn, model: str, days: int) -> dict:
    posts = _fetch_posts_for_model(conn, model, days)
    if not posts:
        return {"model": model, "post_count": 0, "opinions": [], "used_llm": False}

    raw = llm_client.chat(
        _build_prompt(model, posts),
        temperature=0.4,
        max_tokens=600,
    )
    items = _parse_json_array(raw)

    # 清洗：必须含 quote + url，且 url 得在输入 posts 的 url 列表里
    valid_urls = {p["url"] for p in posts if p.get("url")}
    cleaned: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        q = (it.get("quote") or "").strip()
        u = (it.get("url") or "").strip()
        if q and u and u in valid_urls:
            cleaned.append({"quote": q, "url": u})

    return {
        "model":      model,
        "post_count": len(posts),
        "opinions":   cleaned,
        "used_llm":   bool(raw),
    }


def generate(days: int = 7, top_models: int = 5) -> dict:
    """聚合 Top N 最热议模型 × 每个模型 2-3 条观点。"""
    with get_conn() as conn:
        model_counts = _top_models_by_posts(conn, days=days, limit=top_models)
        if not model_counts:
            return {"models": [], "fallback_md": "本周 Reddit 未匹配到具体模型相关的帖子。"}

        logger.info("[Opinions] 期 %dd Top %d 模型: %s", days, top_models,
                    [f"{m}({c})" for m, c in model_counts])

        results = []
        for model, _cnt in model_counts:
            r = _opinions_for_model(conn, model, days=days)
            if r["opinions"]:  # 跳过没观点（全是 meme）的模型
                results.append(r)

    if not results:
        return {
            "models":      [],
            "fallback_md": "本周 Reddit 匹配到相关模型，但讨论大多为 meme 或情绪化表达，未提炼出具体技术观点。",
        }

    return {"models": results, "fallback_md": ""}


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    r = generate()
    if r["fallback_md"]:
        print("FALLBACK:", r["fallback_md"])
    for m in r["models"]:
        print(f"\n== {m['model']} ({m['post_count']} 帖) ==")
        for op in m["opinions"]:
            print(f"  • {op['quote']}")
            print(f"    {op['url']}")
