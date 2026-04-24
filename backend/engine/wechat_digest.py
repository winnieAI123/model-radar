"""WeChat Digest：把本周公众号文章按标题先分类，每类写综合总结段落。

和 wechat_themes 的区别：
- wechat_themes 是"每个事件一个主题 + 每篇文章一句角度"，阅读体验像目录。
- wechat_digest 是"先分 2-4 类，每类 100-250 字连贯段落 + 文章用 [1][2] 脚注式引用"，
  阅读体验像综述文章，读者不用在 20 个标题里跳来跳去。
- 维度（价格/体验/场景/对比）若文章实质涉及则必须写进段落里（带具体数字）。

输出：
{
  "categories": [
    {
      "name": "模型发布与实测",
      "summary": "连贯段落，含 [1][2][3] 引用",
      "refs": [
        {"n": 1, "url": "...", "source": "赛博禅心"},
        ...
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
- 忠实综述，不营销。禁用："震撼发布"/"颠覆行业"/"王炸"/"碾压"/"一文读懂"/"史诗级"/"重磅"。
- 段落要连贯，像一名编辑读完 N 篇文章后写出的综述段落，不是罗列。
- **必须具体**：从文章正文里挖具体数字、榜单分数、价格、对比、开放范围、短板。越具体越好，不要写泛泛定性。
  示例（这是段落该有的样子）：
    「OpenAI 发布 GPT-5.5，核心卖点是"用更少 token 完成更难任务"。API 定价 $5/$30 每百万 token，
     是 5.4 的两倍，但官方称实际 token 消耗更低。编码方面 Terminal-Bench 2.0 达 82.7%（5.4 为 75.1%），
     SWE-Bench Pro 58.6%，但仍低于 Claude Opus 4.7 的 64.3%。上下文窗口 400K。知识工作方面 OSWorld 78.7%，
     Tau2-bench 98.0%。短板：SWE-Bench Pro 落后 Claude、MCP Atlas 不及 Claude/Gemini、长上下文 256K 以上
     Claude 仍占优。目前向 ChatGPT 付费用户开放，同步推出首个通用越狱奖金 $25,000 的赏金计划。」
  具体维度至少覆盖：价格 / 榜单分数 / 体验场景 / 竞品对比 / 短板 / 开放范围（文章提到哪些就写哪些）。
- **允许并鼓励直接写博主名**（公众号名），例如"赛博禅心介绍…""数字生命卡兹克给出…""另有作者提到…"。
  我们接入的公众号不多，署名比匿名更直观。避免"有文章提到"/"某作者"这种纯匿名措辞。
- 引用文章用 [1] [2] [3] 形式，编号对应输入列表的 [N]。哪篇文章说的数字/观点就引哪篇，编号要精准。
- 不要写文章标题（标题仅你在分类时内部使用）。博主名可以写。"""


BODY_SNIPPET_MAX = 4500


def _fetch_posts(conn, days: int, limit: int = 150) -> list[dict]:
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
    if source and source.startswith("wechat_"):
        return source[len("wechat_"):] or source
    return source or ""


def _format_post(p: dict, i: int) -> str:
    title = (p["title"] or "")[:120]
    raw_body = p.get("body_full") or p.get("summary") or ""
    body = raw_body.replace("\n", " ").replace("  ", " ")[:BODY_SNIPPET_MAX]
    author = _author_of(p.get("source") or "")
    pub = (p.get("published_at") or "")[:10]
    return (
        f"[{i}] {author} · {pub}\n"
        f"   标题: {title}\n"
        f"   正文: {body or '(无正文)'}"
    )


def _build_prompt(posts: list[dict]) -> list[dict]:
    posts_lines = "\n\n".join(_format_post(p, i + 1) for i, p in enumerate(posts))
    user_content = (
        f"{HUMANIZER_PRINCIPLES}\n\n"
        f"任务：下面是本周中文技术公众号按发布时间倒序的 {len(posts)} 篇文章（含正文前 {BODY_SNIPPET_MAX} 字）。\n\n"
        f"步骤一：**先按标题将文章分成 2-4 类**，分类名自定。常见分类（参考，不强制）：\n"
        f"  - 模型发布与实测\n"
        f"  - 技术方法论 / Agent 与工程\n"
        f"  - 新应用参考 / 落地案例\n"
        f"  - 行业观察 / 商业动态\n"
        f"  - 综述与盘点\n"
        f"  若文章数少于 6 篇，只分 1-2 类也可以，不强凑。\n\n"
        f"步骤二：**每类写一段 150-350 字的综合总结**：\n"
        f"  - 读完该类所有文章后融合多位博主观点，段落自然流畅\n"
        f"  - **必须挖具体**：从原文抽出具体数字、榜单分数（含对比基准）、API 价格、上下文长度、开放范围、短板、赏金/免费政策等。\n"
        f"    禁止写「效果优异」「表现出色」「大幅提升」这种无数字的泛泛描述。\n"
        f"  - **博主名可以直接写**（例：「赛博禅心给出了 50+ 生图实测」、「数字生命卡兹克的教程涵盖 XX」），不要用「有文章」「某作者」这种匿名措辞\n"
        f"  - 用 [1] [2] [3] 形式引用文章编号（对应输入 [N]），**不要写文章标题**\n"
        f"  - 不要罗列式「第一篇讲…第二篇讲…」\n\n"
        f"输出 JSON：\n"
        f'{{\n'
        f'  "categories": [\n'
        f'    {{\n'
        f'      "name":    "分类名（中文，6-12 字）",\n'
        f'      "summary": "150-350 字综合段落，含具体数字/对比/博主名，[1][2] 引用",\n'
        f'      "refs":    [1, 2, 3]   // 该段落引用到的文章编号（去重后的有序数组）\n'
        f'    }}\n'
        f'  ]\n'
        f'}}\n\n'
        f"--- 文章 ---\n"
        f"{posts_lines}\n\n"
        f"直接输出 JSON 对象，不要加代码块、不要加解释。"
    )
    return [
        {"role": "system", "content": "你是一名中文技术周报主编，擅长把一堆公众号文章归类后写成综述段落。"},
        {"role": "user",   "content": user_content},
    ]


def _parse_json_obj(raw: str | None) -> dict:
    if not raw:
        return {}
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
    except Exception as e:
        logger.warning("[WeChatDigest] JSON 解析失败: %s", e)
        return {}
    return obj if isinstance(obj, dict) else {}


_REF_RE = re.compile(r"\[(\d+)\]")


def _resolve_refs(summary: str, ref_ids: list, posts: list[dict]) -> tuple[str, list[dict]]:
    """返回 (清洗后的 summary, refs 列表)。
    - refs 按在 summary 中首次出现顺序重排编号（1..K），避免 LLM 给的数字断开
    - 无效编号（不在 posts 范围内）的 [N] 原样删除
    """
    ids_in_summary: list[int] = []
    seen = set()
    for m in _REF_RE.finditer(summary):
        try:
            n = int(m.group(1))
        except Exception:
            continue
        if n in seen:
            continue
        if 1 <= n <= len(posts):
            ids_in_summary.append(n)
            seen.add(n)

    # 加上 ref_ids 里但没出现在段落里的（LLM 可能把某些引用漏标在段落但列在 refs）
    if isinstance(ref_ids, list):
        for r in ref_ids:
            try:
                n = int(r)
            except Exception:
                continue
            if 1 <= n <= len(posts) and n not in seen:
                ids_in_summary.append(n)
                seen.add(n)

    # 旧编号 → 新编号映射
    remap = {old: new for new, old in enumerate(ids_in_summary, start=1)}

    def _sub(m):
        try:
            n = int(m.group(1))
        except Exception:
            return ""
        new = remap.get(n)
        return f"[{new}]" if new else ""

    cleaned = _REF_RE.sub(_sub, summary)

    refs: list[dict] = []
    for old in ids_in_summary:
        p = posts[old - 1]
        refs.append({
            "n":      remap[old],
            "url":    p.get("url") or "",
            "source": _author_of(p.get("source") or ""),
            "title":  p.get("title") or "",
        })
    return cleaned, refs


def generate(days: int = 7, top_posts: int = 150) -> dict:
    with get_conn() as conn:
        posts = _fetch_posts(conn, days=days, limit=top_posts)

    if not posts:
        return {"categories": [], "article_count": 0, "used_llm": False,
                "fallback_md": "本周无公众号文章数据。"}

    # 和 wechat_themes 一致：>80 篇截断到最近 80 篇
    if len(posts) > 80:
        logger.info("[WeChatDigest] 文章过多 (%d)，截断到最近 80 篇", len(posts))
        posts = posts[:80]

    raw = llm_client.chat(_build_prompt(posts), temperature=0.3, max_tokens=6000)
    obj = _parse_json_obj(raw)
    raw_cats = obj.get("categories") or []

    categories: list[dict] = []
    for c in raw_cats:
        if not isinstance(c, dict):
            continue
        name    = (c.get("name")    or "").strip()
        summary = (c.get("summary") or "").strip()
        refs_in = c.get("refs") or []
        if not name or not summary:
            continue
        cleaned, refs = _resolve_refs(summary, refs_in, posts)
        if not refs:  # 段落里一个有效引用都没有，跳过
            continue
        categories.append({"name": name, "summary": cleaned, "refs": refs})

    logger.info("[WeChatDigest] %d 类 / %d 篇文章", len(categories), len(posts))
    return {
        "categories":    categories,
        "article_count": len(posts),
        "used_llm":      bool(raw),
        "fallback_md":   "" if categories else "本周公众号文章未归纳出有效分类。",
    }


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    r = generate()
    print(f"\narticle_count={r['article_count']} used_llm={r['used_llm']} categories={len(r['categories'])}")
    for c in r["categories"]:
        print(f"\n【{c['name']}】")
        print(f"  {c['summary']}")
        for ref in c["refs"]:
            print(f"  [{ref['n']}] {ref['source']} · {ref['title'][:40]} · {ref['url']}")
