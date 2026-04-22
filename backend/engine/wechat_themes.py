"""WeChat Themes：把本周公众号文章喂给 LLM，按事件/话题聚合归纳。

与 reddit_themes 同构（复用 humanizer 原则、同样的 JSON 解析逻辑），区别：
- 数据源：blog_posts 表中 source LIKE 'wechat_%' 的近 7d 文章
- 聚合单位：同一"事件/产品"（例如 GPT-Image-2 / 豆包更新 / Claude Opus 4.7）不同公众号的不同角度
- 输出：每个事件 1 个 theme，articles 列表里保留每篇文章的标题、公众号、角度一句话

输出：
{
  "themes": [
    {
      "title": "事件/话题标题",
      "summary": "一句话说清楚这个事件在讲什么",
      "articles": [
        {"title": "...", "url": "...", "source": "公众号名", "angle": "这篇讲了什么角度"}
      ]
    }
  ],
  "article_count": N,
  "used_llm": True,
  "fallback_md": ""
}
"""
import json
import logging
import re
from datetime import datetime, timedelta

from backend.db import get_conn
from backend.utils import llm_client

logger = logging.getLogger(__name__)


HUMANIZER_PRINCIPLES = """写作原则：
- 忠实归纳，不渲染。禁用："震撼发布"/"颠覆行业"/"王炸"/"碾压"/"一文读懂"/"史诗级"。
- 事件标题要具体。"大模型最新进展" 太虚；"GPT-Image-2 文生图新模型发布" 就具体。
- summary 30-80 字，说清楚这个事件本身（谁做了什么，关键参数/特点是什么），不要营销话术。
- angle 每篇 15-40 字，说清楚这篇文章相比其他文章的独特角度（深度测评 / 官方通稿翻译 / 用户实测 / 对比某竞品 / 行业影响分析等）。
- 同一事件如果多篇文章角度完全相同（都是搬运官方通稿），只保留最具体的那篇。"""


def _fetch_posts(conn, days: int, limit: int = 150) -> list[dict]:
    """取过去 N 天所有 wechat_* 源的文章。按 published_at 倒序。"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """
        SELECT url, source, title, summary, published_at, matched_model
        FROM blog_posts
        WHERE source LIKE 'wechat_%'
          AND published_at >= ?
          AND title IS NOT NULL
          AND title <> ''
        ORDER BY published_at DESC
        LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _author_of(source: str) -> str:
    """'wechat_赛博禅心' -> '赛博禅心'"""
    if source and source.startswith("wechat_"):
        return source[len("wechat_"):] or source
    return source or ""


def _format_post(p: dict, i: int) -> str:
    title = (p["title"] or "")[:120]
    body = (p.get("summary") or "").replace("\n", " ")[:400]
    author = _author_of(p.get("source") or "")
    matched = f" · matched={p['matched_model']}" if p.get("matched_model") else ""
    pub = (p.get("published_at") or "")[:10]
    return (
        f"[{i}] {author} · {pub}{matched}\n"
        f"   标题: {title}\n"
        f"   正文片段: {body or '(无摘要)'}"
    )


def _build_prompt(posts: list[dict]) -> list[dict]:
    posts_lines = "\n\n".join(_format_post(p, i + 1) for i, p in enumerate(posts))
    user_content = (
        f"{HUMANIZER_PRINCIPLES}\n\n"
        f"任务：下面是本周中文技术公众号按发布时间倒序的 {len(posts)} 篇文章。\n"
        f"请按**事件/产品/话题**聚合，归纳 3-6 个事件，每个事件给一个 JSON 对象：\n"
        f'  - "title":    中文事件标题，25 字以内，具体（带型号/产品名/动作）\n'
        f'  - "summary":  中文一句话说清楚这个事件本身，30-80 字\n'
        f'  - "post_ids": 数组，挑 1-4 条属于该事件的文章编号（上面 [N] 里的 N）\n'
        f'  - "angles":   数组，每个元素是字符串，和 post_ids 顺序一致，说明该篇文章的独特角度（15-40 字，例：『CSDN 翻译官方公告』『用户实测对比 Flux』）\n\n'
        f"要求：\n"
        f"- 合并同一事件的不同文章。例如有 3 篇都在讲 GPT-Image-2，那就合成 1 个 theme，articles 里列 3 篇各自的角度。\n"
        f"- 只聚类**技术事件 / 产品发布 / 行业动态 / 深度分析**，过滤纯榜单搬运、纯营销广告、活动预告、招聘、投融资八卦。\n"
        f"- 宁可归 3 个实打实的事件，也不硬凑到 6 个。\n"
        f"- post_ids 和 angles 数组长度必须一致。\n\n"
        f"--- 文章 ---\n"
        f"{posts_lines}\n\n"
        f"直接输出 JSON 数组，不要加代码块、不要加解释。"
    )
    return [
        {"role": "system", "content": "你是一名中文技术周报作者，擅长从一堆公众号文章里识别出真正的技术事件并合并同主题报道。"},
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
        logger.warning("[WeChatThemes] JSON 解析失败: %s", e)
        return []
    if isinstance(obj, dict) and "items" in obj:
        obj = obj["items"]
    return obj if isinstance(obj, list) else []


def generate(days: int = 7, top_posts: int = 150) -> dict:
    """归纳本周公众号文章涉及的事件/话题。"""
    with get_conn() as conn:
        posts = _fetch_posts(conn, days=days, limit=top_posts)

    if not posts:
        return {"themes": [], "article_count": 0, "used_llm": False,
                "fallback_md": "本周无公众号文章数据。"}

    # 输入规模控制：>80 篇时按 published_at 近→远截断（近的更可能是本周事件）
    if len(posts) > 80:
        logger.info("[WeChatThemes] 文章过多 (%d)，截断到最近 80 篇", len(posts))
        posts = posts[:80]

    raw = llm_client.chat(_build_prompt(posts), temperature=0.3, max_tokens=1800)
    items = _parse_json(raw)

    themes: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title   = (it.get("title")   or "").strip()
        summary = (it.get("summary") or "").strip()
        ids     = it.get("post_ids") or []
        angles  = it.get("angles")   or []
        if not title or not summary:
            continue
        article_refs = []
        for idx_pos, pid in enumerate(ids):
            try:
                idx = int(pid) - 1
            except Exception:
                continue
            if not (0 <= idx < len(posts)):
                continue
            p = posts[idx]
            angle = ""
            if idx_pos < len(angles) and isinstance(angles[idx_pos], str):
                angle = angles[idx_pos].strip()
            article_refs.append({
                "title":  p["title"],
                "url":    p["url"],
                "source": _author_of(p.get("source") or ""),
                "angle":  angle,
            })
        if not article_refs:
            continue
        themes.append({"title": title, "summary": summary, "articles": article_refs})

    logger.info("[WeChatThemes] 归纳 %d 个事件（基于 %d 篇文章）", len(themes), len(posts))
    return {
        "themes":        themes,
        "article_count": len(posts),
        "used_llm":      bool(raw),
        "fallback_md":   "" if themes else "本周公众号文章未归纳出明显技术事件。",
    }


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    r = generate()
    print(f"\narticle_count={r['article_count']} used_llm={r['used_llm']} themes={len(r['themes'])}")
    for t in r["themes"]:
        print(f"\n【{t['title']}】")
        print(f"  {t['summary']}")
        for a in t["articles"]:
            print(f"    · {a['source']}: {a['title'][:60]}")
            print(f"      {a.get('angle') or ''}")
            print(f"      {a['url']}")
