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
- 聚类以**模型 / 产品 / 工具**为主轴。同一模型（如 Claude Opus 4.7）下的所有文章 —— 包括产品发布、特点解读、上手教程、深度评测、用户吐槽 —— 都归到一个主题下。
- 事件标题要具体。"大模型最新进展" 太虚；"GPT-Image-2 文生图新模型发布" 就具体；"Claude Opus 4.7 实测与反馈" 也算具体。
- summary 30-80 字，说清楚这个主题的核心（模型是什么 + 本周围绕它的主要讨论点），不要营销话术。
- angle 每篇 15-40 字，说清楚这篇文章相比其他文章的独特角度（深度测评 / 官方通稿翻译 / 用户实测 / 上手教程 / 对比某竞品 / 行业影响分析 / 个人吐槽等）。
- **维度标签**（可选，仅当正文中明确涉及时）：价格 / 体验 / 场景 / 对比。任何一个维度若正文有实质内容，就在 dimensions 数组里加上对应 key；没提到就别硬凑，让 summary 自己发挥。
- 允许深度评测、上手教程、使用反馈作为有效文章 —— 只要围绕一个具体的模型/产品/工具即可。
- 同一事件如果多篇文章角度完全相同（都是搬运官方通稿），只保留最具体的那篇。"""


# 正文投喂 LLM 的长度上限。太长 token 浪费；太短丢失"价格/体验/场景/对比"这类埋在正文里的细节。
# 2500 字约覆盖绝大多数公众号长文的前 60-80%（文尾的推广/免责声明不需要）。
BODY_SNIPPET_MAX = 2500


def _fetch_posts(conn, days: int, limit: int = 150) -> list[dict]:
    """取过去 N 天所有 wechat_* 源的文章。按 published_at 倒序。
    body_full 是 2026-04-24 新加的列，旧文章可能为 NULL，用 summary 作 fallback。
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """
        SELECT url, source, title, summary, body_full, published_at, matched_model
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
    # body_full 优先（新数据），退化到 summary（历史数据或非微信源）
    raw_body = p.get("body_full") or p.get("summary") or ""
    body = raw_body.replace("\n", " ").replace("  ", " ")[:BODY_SNIPPET_MAX]
    author = _author_of(p.get("source") or "")
    matched = f" · matched={p['matched_model']}" if p.get("matched_model") else ""
    pub = (p.get("published_at") or "")[:10]
    return (
        f"[{i}] {author} · {pub}{matched}\n"
        f"   标题: {title}\n"
        f"   正文: {body or '(无正文)'}"
    )


def _build_prompt(posts: list[dict]) -> list[dict]:
    posts_lines = "\n\n".join(_format_post(p, i + 1) for i, p in enumerate(posts))
    user_content = (
        f"{HUMANIZER_PRINCIPLES}\n\n"
        f"任务：下面是本周中文技术公众号按发布时间倒序的 {len(posts)} 篇文章（含正文前 {BODY_SNIPPET_MAX} 字）。\n"
        f"请按**模型/产品/工具**为主轴聚合，归纳最多 20 个主题，每个主题给一个 JSON 对象：\n"
        f'  - "title":      中文主题标题，25 字以内，具体（带型号/产品名，如"Claude Opus 4.7 实测与反馈"）\n'
        f'  - "summary":    中文一句话说清楚这个主题的核心（模型是什么 + 本周围绕它有哪些讨论），30-80 字\n'
        f'  - "dimensions": 可选数组，仅当正文实质涉及时才加入，元素取值为 "价格"/"体验"/"场景"/"对比" 中的一个或多个。没提到的维度不要放，没有涉及的维度整个数组为 [] 即可\n'
        f'  - "post_ids":   数组，挑 1-N 条属于该主题的文章编号（上面 [N] 里的 N）\n'
        f'  - "angles":     数组，每个元素是字符串，和 post_ids 顺序一致，说明该篇文章的独特角度（15-40 字）\n\n'
        f"要求：\n"
        f"- **合并同一模型/产品的所有文章**到一个主题下。例如不同公众号都在讲 Claude Opus 4.7（一篇是官方发布解读、一篇是实测吐槽、一篇是对比测评），合成 1 个主题，articles 里列 3 篇各自的角度。\n"
        f"- 允许「深度评测 / 上手教程 / 使用反馈 / 个人吐槽」作为有效文章 —— 只要围绕一个具体的模型/产品/工具即可。\n"
        f"- 过滤完全离题的内容：纯商业八卦、纯招聘、纯活动预告、纯金融投融资（不涉及具体模型/产品）。\n"
        f"- 单篇文章不涉及具体模型/产品但有技术价值（如「怎么写 Prompt」「Agent 设计方法论」等方法论文章）的，可以单独成一个主题。\n"
        f"- **维度标签不强制**：只在文章真提到价格（token 单价 / 订阅费）/体验（响应速度 / UI / bug）/场景（代码 / 写作 / 长文本 / Agent）/对比（vs 其他模型）时标。没提就别凑。\n"
        f"- 主题数量上限 20 个，按数据实际情况出，不硬凑。\n"
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

    raw = llm_client.chat(_build_prompt(posts), temperature=0.3, max_tokens=3000)
    items = _parse_json(raw)

    VALID_DIMS = {"价格", "体验", "场景", "对比"}
    themes: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title   = (it.get("title")   or "").strip()
        summary = (it.get("summary") or "").strip()
        ids     = it.get("post_ids") or []
        angles  = it.get("angles")   or []
        # dimensions 可选：过滤非法值、去重、保持顺序
        dims_raw = it.get("dimensions") or []
        dimensions: list[str] = []
        if isinstance(dims_raw, list):
            for d in dims_raw:
                if isinstance(d, str) and d.strip() in VALID_DIMS and d.strip() not in dimensions:
                    dimensions.append(d.strip())
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
        themes.append({"title": title, "summary": summary,
                       "dimensions": dimensions, "articles": article_refs})

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
