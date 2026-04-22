"""按"领域"聚合 LMArena / AA / SuperCLUE 本周榜单 + LLM 跨平台一句话总结。

用于周报的"📊 榜单变化"段。输出结构：
{
  "text":           {"title": "LLM 对话",  "platforms": [...], "summary_md": "...", "used_llm": bool},
  "text_to_image":  {"title": "文生图",    ...},
  "text_to_video":  {"title": "文生视频",  ...},
  "image_to_video": {"title": "图生视频",  ...},
}

每个 domain 下 platforms 是列表，元素含 {source, top_n: [{rank, model_name, canonical, score, change}], has_baseline}。
冷启动期（7 天前没快照）change=None、has_baseline=False，LLM prompt 会说明不做对比。

中文特色榜（image_edit / text_to_speech / ref_to_video）按用户决策不进入周报。
"""
import logging
from datetime import datetime, timedelta

from backend.db import get_conn
from backend.utils import llm_client
from backend.utils.model_alias import normalize

logger = logging.getLogger(__name__)

DOMAINS: dict[str, str] = {
    "text":           "LLM 对话",
    "text_to_image":  "文生图",
    "text_to_video":  "文生视频",
    "image_to_video": "图生视频",
}

DOMAIN_SOURCES: dict[str, list[str]] = {
    "text":           ["lmarena"],
    "text_to_image":  ["lmarena", "aa", "superclue"],
    "text_to_video":  ["lmarena", "aa", "superclue"],
    "image_to_video": ["lmarena", "aa", "superclue"],
}

SOURCE_LABEL: dict[str, str] = {
    "lmarena":   "LMArena",
    "aa":        "Artificial Analysis",
    "superclue": "SuperCLUE",
}

# 平台徽章点击去向。优先 (source, domain) 精确匹配，fallback 到 source 默认 URL。
# 新增精确路径时在 _BY_DOMAIN 里加；保守起见，未验证的路径先用 _DEFAULT 的首页。
_PUBLIC_URL_DEFAULT: dict[str, str] = {
    "lmarena":   "https://lmarena.ai/leaderboard",
    "aa":        "https://artificialanalysis.ai/leaderboards/models",
    "superclue": "https://www.superclueai.com/",
}
_PUBLIC_URL_BY_DOMAIN: dict[tuple[str, str], str] = {
    # 目前未填具体 domain 路径。后续校验到某平台稳定 URL 再加进来。
}


def _public_url(source: str, domain: str) -> str | None:
    return _PUBLIC_URL_BY_DOMAIN.get((source, domain)) or _PUBLIC_URL_DEFAULT.get(source)


HUMANIZER_PRINCIPLES = """你在给一个技术团队写周报，读者是工程师和产品经理，不喜欢营销式表达。

写作原则：
- 陈述事实，不做情绪渲染。别用"展现了卓越能力"、"充分体现了"、"令人瞩目"、"里程碑式"、"革命性"、"引领了行业潮流"。
- 别用三段并列的总结性结尾（例如"它不仅……还……更……"）。
- 短句，信息密度优先。能用一句话说完的不分两句。
- 具体 > 抽象：指名模型、指名平台、指名变化方向。
- 如果某领域数据稀疏，直接写"讨论量少"或"仅 X 平台覆盖"，不要硬凑。

‼️ 绝对禁区（最重要）：
- **不要推断模型的厂商归属或"系列"**。你不知道 muse-spark 是谁家的、也不知道 Riverflow 是谁家的。
  禁止写"Claude 系列"、"OpenAI 系列"、"Meta 旗下"之类的归属词。
  榜单给你什么名字，你就用什么名字。只讲排名事实（谁在前、谁上升、谁跨平台一致），不讲出身。
- 如果要强调"同一厂家多个模型霸榜"，必须从模型名里明显能看出同名前缀（例如 claude-opus-4-7-thinking 和 claude-opus-4-6 同属 claude-opus 系列）才可以说；
  名字完全不同的模型（例如 muse-spark）绝对不能归到某个系列里。"""


def _latest_scraped_at(conn, source: str, before: str | None = None) -> str | None:
    if before:
        row = conn.execute(
            "SELECT MAX(scraped_at) AS d FROM leaderboard_snapshots WHERE source=? AND scraped_at < ?",
            (source, before),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(scraped_at) AS d FROM leaderboard_snapshots WHERE source=?",
            (source,),
        ).fetchone()
    return row["d"] if row else None


def _top_n_at(conn, source: str, category: str, scraped_at: str, n: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT rank, model_name, score FROM leaderboard_snapshots "
        "WHERE source=? AND category=? AND scraped_at=? AND rank<=? ORDER BY rank",
        (source, category, scraped_at, n),
    ).fetchall()
    return [dict(r) for r in rows]


def _full_snapshot_at(conn, source: str, category: str, scraped_at: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT rank, model_name FROM leaderboard_snapshots "
        "WHERE source=? AND category=? AND scraped_at=? ORDER BY rank",
        (source, category, scraped_at),
    ).fetchall()
    return {r["model_name"]: r["rank"] for r in rows}


def _compute_change(model_name: str, current_rank: int,
                    previous_map: dict[str, int] | None) -> str | None:
    """'NEW' / '↑3' / '↓2' / '—' / None（冷启动期无 baseline）。"""
    if not previous_map:
        return None
    if model_name not in previous_map:
        return "NEW"
    prev = previous_map[model_name]
    delta = prev - current_rank
    if delta > 0:
        return f"↑{delta}"
    if delta < 0:
        return f"↓{-delta}"
    return "—"


def _gather_one_platform(conn, source: str, category: str,
                         baseline_cutoff_iso: str, top_n: int = 5) -> dict:
    public_url = _public_url(source, category)
    latest = _latest_scraped_at(conn, source)
    if not latest:
        return {"source": source, "top_n": [], "has_baseline": False, "scraped_at": None,
                "public_url": public_url}

    baseline = _latest_scraped_at(conn, source, before=baseline_cutoff_iso)
    previous_map = _full_snapshot_at(conn, source, category, baseline) if baseline else None
    has_baseline = bool(previous_map)

    top_rows = _top_n_at(conn, source, category, latest, n=top_n)
    items = []
    for r in top_rows:
        items.append({
            "rank":       r["rank"],
            "model_name": r["model_name"],
            "canonical":  normalize(r["model_name"]),
            "score":      r["score"],
            "change":     _compute_change(r["model_name"], r["rank"], previous_map),
        })
    return {
        "source":       source,
        "top_n":        items,
        "has_baseline": has_baseline,
        "scraped_at":   latest,
        "public_url":   public_url,
    }


def _format_platforms_for_prompt(platforms: list[dict]) -> str:
    lines = []
    for p in platforms:
        src_label = SOURCE_LABEL.get(p["source"], p["source"])
        if not p["top_n"]:
            lines.append(f"### {src_label}: 无数据")
            continue
        lines.append(f"### {src_label}")
        for it in p["top_n"]:
            name = it["model_name"]
            can = f" [canonical={it['canonical']}]" if it["canonical"] and it["canonical"] != name else ""
            score = f" · score={it['score']:.1f}" if it["score"] is not None else ""
            change = f" · {it['change']}" if it["change"] else ""
            lines.append(f"- #{it['rank']} {name}{can}{score}{change}")
    return "\n".join(lines)


def _llm_domain_summary(domain_title: str, platforms: list[dict]) -> tuple[str, bool]:
    any_baseline = any(p["has_baseline"] for p in platforms)
    if any_baseline:
        baseline_note = "数据包含上周基准，着重讲『本周』相比上周的变化（新登顶 / 跌出 Top 5 / 排名大幅变动）。"
    else:
        baseline_note = "目前没有上周基准快照（系统刚上线），本次只讲『当前』格局：谁在领先、谁在咬尾。下周起会开始对比变化。"

    if all(not p["top_n"] for p in platforms):
        return f"{domain_title}本周无榜单数据。", False

    user_content = (
        f"{HUMANIZER_PRINCIPLES}\n\n"
        f"任务：请针对『{domain_title}』这个领域，读下面各平台的 Top 5 榜单，"
        f"写一句或两句（总计 40-80 字）中文总结，告诉工程师读者当前格局的关键信号。\n\n"
        f"说明：\n"
        f"- 同一个模型在不同平台名字可能略有差异（canonical 标签帮你对齐，例如 `Nano Banana 2` 和 "
        f"`Gemini-3.1-Flash-Image-Preview(Nano Banana 2)` 是同一个）。\n"
        f"- {baseline_note}\n"
        f"- 如果某模型在多家平台都登顶，要点出『跨平台登顶』。\n"
        f"- 如果只有 1 家平台覆盖这个领域，直接讲该平台 Top 1-2 的对比即可。\n"
        f"- ⚠️ 再次强调：只讲名字+排名事实，不要推断厂商归属（除非模型名前缀本身一模一样）。\n\n"
        f"--- 数据 ---\n"
        f"{_format_platforms_for_prompt(platforms)}\n\n"
        f"直接输出一段纯文本（不要加标题、不要 Markdown 列表、不要代码块），40-80 字。"
    )

    summary = llm_client.chat(
        [
            {"role": "system", "content": "你是一名中文技术周报作者。"},
            {"role": "user",   "content": user_content},
        ],
        temperature=0.3,
        max_tokens=300,
    )
    if summary:
        return summary.strip(), True

    # 降级
    parts = []
    for p in platforms:
        if p["top_n"]:
            top1 = p["top_n"][0]
            parts.append(f"{SOURCE_LABEL.get(p['source'], p['source'])} 第一：{top1['model_name']}")
    return f"{domain_title}当前格局：{'；'.join(parts) if parts else '数据缺失'}。", False


def generate(days: int = 7, top_n: int = 5) -> dict:
    baseline_cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    out: dict[str, dict] = {}

    with get_conn() as conn:
        for domain_key, domain_title in DOMAINS.items():
            sources = DOMAIN_SOURCES.get(domain_key, [])
            platforms = [
                _gather_one_platform(conn, src, domain_key, baseline_cutoff, top_n=top_n)
                for src in sources
            ]
            summary_md, used_llm = _llm_domain_summary(domain_title, platforms)
            out[domain_key] = {
                "title":        domain_title,
                "platforms":    platforms,
                "summary_md":   summary_md,
                "used_llm":     used_llm,
                "any_baseline": any(p["has_baseline"] for p in platforms),
            }
            logger.info("[LeaderboardDigest] %s: %d platforms, LLM=%s", domain_title,
                        len([p for p in platforms if p["top_n"]]), used_llm)

    return out


if __name__ == "__main__":
    import json as _json
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    r = generate()
    for k, v in r.items():
        print(f"\n===== {v['title']} =====")
        for p in v["platforms"]:
            if p["top_n"]:
                print(f"  [{p['source']}] top1 = {p['top_n'][0]['model_name']}")
        print(f"  LLM 总结: {v['summary_md']}")
