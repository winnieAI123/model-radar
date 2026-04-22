"""HuggingFace Digest：给周报产出"HF 趋势 Top N" + LLM 一句话总结。

为什么要有这个板块：
- 榜单（LMArena/AA/SuperCLUE）是"官方评测里谁更强"的信号，更新慢
- HF trending 是"社区当下在下载谁"的信号，更新快、能第一时间捕捉新开源
- 两者一起看，才能把"榜单上升慢但社区已经在疯抢"这种隐形信号挑出来

输出：
{
  "top":           [{rank, model_id, author, pipeline_tag, likes, downloads,
                     matched_model, change, hf_url}],
  "as_of":         "2026-04-22 02:23:06",   # 最新快照时间
  "any_baseline":  bool,                    # 有没有 ~7 天前对比基线
  "summary_md":    "……",                    # LLM 归纳一句：主导玩家 + 值得关注的冷门
  "used_llm":      bool,
}

change 字段：
- "NEW"   = 本周新进 trending 榜
- "↓"    = 上周在榜、本周跌出（不出现在 top 列表里，只影响 summary 描述）
- None    = 上周也在
（冷启动没有基线时全部留 None，UI 侧不渲染徽标）
"""
import logging
from datetime import datetime, timedelta

from backend.db import get_conn
from backend.utils import llm_client

logger = logging.getLogger(__name__)


HUMANIZER_PRINCIPLES = """你在给一个技术团队写周报，读者是工程师和产品经理。
写作原则：
- 陈述事实，不情绪化。别用"展现了卓越能力"、"令人瞩目"、"里程碑"、"革命性"。
- 短句，信息密度优先。
- 指名具体模型 / 具体 pipeline_tag，不用"众多大模型"这种虚指。

‼️ 绝对禁区：
- **不要推断 vendor 归属或"家族系列"**。只有当 model_id 前缀明显一致
  （例如 Qwen/Qwen3.6-35B-A3B 和 unsloth/Qwen3.6-35B-A3B-GGUF 都含 Qwen3.6）
  才可以说"Qwen3 系列占几席"。名字不一样的模型不能归一起。
- 不要编造没给你的数据（例如不要说"下载量环比涨 X%"，除非 change 字段写明）。"""


def _latest_snapshot_time(conn, list_type: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(scraped_at) AS t FROM hf_snapshots WHERE list_type=?",
        (list_type,),
    ).fetchone()
    return row["t"] if row and row["t"] else None


def _latest_rows(conn, list_type: str, scraped_at: str, top_n: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT rank, model_id, author, pipeline_tag, likes, downloads, matched_model
        FROM hf_snapshots
        WHERE list_type=? AND scraped_at=?
        ORDER BY rank ASC
        LIMIT ?
        """,
        (list_type, scraped_at, top_n),
    ).fetchall()
    return [dict(r) for r in rows]


def _baseline_ids(conn, list_type: str, days_ago: int) -> set[str]:
    """取 ~days_ago 天前最接近的那一次快照的 model_id 集合。没有就返回空 set。"""
    cutoff_early = (datetime.now() - timedelta(days=days_ago + 2)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_late  = (datetime.now() - timedelta(days=days_ago - 2)).strftime("%Y-%m-%d %H:%M:%S")

    # 在 [days_ago-2, days_ago+2] 窗口里找最晚的一个快照时间
    row = conn.execute(
        """
        SELECT MAX(scraped_at) AS t FROM hf_snapshots
        WHERE list_type=? AND scraped_at BETWEEN ? AND ?
        """,
        (list_type, cutoff_early, cutoff_late),
    ).fetchone()
    if not row or not row["t"]:
        return set()

    ids = conn.execute(
        "SELECT model_id FROM hf_snapshots WHERE list_type=? AND scraped_at=?",
        (list_type, row["t"]),
    ).fetchall()
    return {r["model_id"] for r in ids}


def _dropouts(current_ids: set[str], baseline_ids: set[str], limit: int = 5) -> list[str]:
    """上周在榜 Top N 范围内、本周跌出 Top N 的 model_id。只取 limit 条，避免 prompt 太长。"""
    dropped = [m for m in baseline_ids if m not in current_ids]
    return dropped[:limit]


def _format_for_prompt(rows: list[dict], dropped: list[str]) -> str:
    """把 top 列表和 dropouts 捏成给 LLM 的数据块。"""
    lines = ["### 本周 Trending Top N"]
    for it in rows:
        parts = [f"#{it['rank']} {it['model_id']}"]
        if it.get("pipeline_tag"):
            parts.append(f"tag={it['pipeline_tag']}")
        if it.get("likes") is not None:
            parts.append(f"likes={it['likes']}")
        if it.get("downloads") is not None:
            parts.append(f"dl={it['downloads']}")
        if it.get("matched_model"):
            parts.append(f"canonical={it['matched_model']}")
        if it.get("change"):
            parts.append(f"change={it['change']}")
        lines.append("- " + " · ".join(parts))

    if dropped:
        lines.append("\n### 上周在榜 · 本周跌出")
        for mid in dropped:
            lines.append(f"- {mid}")
    return "\n".join(lines)


def _llm_summary(rows: list[dict], dropped: list[str], any_baseline: bool) -> tuple[str, bool]:
    """调 LLM 归纳一句/两句总结。失败走模板降级。"""
    if not rows:
        return "本周 HuggingFace 趋势榜无数据。", False

    if any_baseline:
        baseline_note = (
            "数据带 change 字段（NEW=本周新进；没标=上周也在），另外还给了本周跌出 Top 榜的 id。"
            "请同时覆盖：① 当前谁在主导；② 哪些模型本周新进值得关注；③ 哪些跌出了。"
        )
    else:
        baseline_note = (
            "目前没有上周基准（系统刚上线），只描述『当前格局』——"
            "主要是什么类型（text-generation / image-to-video / image-to-3d 等）、"
            "有哪些相对冷门但值得关注的条目（例如非常规 pipeline、名字陌生的新厂家）。"
            "下周起会开始跑对比。"
        )

    user_content = (
        f"{HUMANIZER_PRINCIPLES}\n\n"
        f"任务：根据下面本周 HuggingFace trending Top 榜，给一个 60-120 字的中文总结。\n"
        f"{baseline_note}\n"
        f"不要罗列每一行，抓关键信号即可。\n\n"
        f"--- 数据 ---\n{_format_for_prompt(rows, dropped)}\n\n"
        f"直接输出纯文本（不要 Markdown、不要列表、不要代码块）。"
    )

    summary = llm_client.chat(
        [
            {"role": "system", "content": "你是一名中文技术周报作者。"},
            {"role": "user",   "content": user_content},
        ],
        temperature=0.3, max_tokens=400,
    )
    if summary:
        return summary.strip(), True

    # 降级：纯模板
    top_pipes: dict[str, int] = {}
    for r in rows:
        p = r.get("pipeline_tag") or "其他"
        top_pipes[p] = top_pipes.get(p, 0) + 1
    pipe_desc = "、".join(f"{k}×{v}" for k, v in sorted(top_pipes.items(), key=lambda x: -x[1])[:3])
    top1 = rows[0]["model_id"]
    return f"Top 榜领头：{top1}；Top 10 按 pipeline 分布为 {pipe_desc}。", False


def generate(days: int = 7, top_n: int = 10) -> dict:
    """产出 HF trending Top N + 跨周对比 + LLM 一句话总结。"""
    with get_conn() as conn:
        latest_t = _latest_snapshot_time(conn, "trending")
        if not latest_t:
            return {"top": [], "as_of": None, "any_baseline": False,
                    "summary_md": "本周 HuggingFace 数据未就绪。", "used_llm": False}

        rows = _latest_rows(conn, "trending", latest_t, top_n)
        baseline = _baseline_ids(conn, "trending", days_ago=days)

    current_ids = {r["model_id"] for r in rows}
    for r in rows:
        mid = r["model_id"]
        r["hf_url"] = f"https://huggingface.co/{mid}"
        if baseline:
            r["change"] = "NEW" if mid not in baseline else None
        else:
            r["change"] = None

    dropped = _dropouts(current_ids, baseline) if baseline else []
    summary_md, used_llm = _llm_summary(rows, dropped, any_baseline=bool(baseline))

    logger.info("[HF Digest] trending %d 条 · as_of=%s · baseline=%s · LLM=%s",
                len(rows), latest_t, "有" if baseline else "无", used_llm)
    return {
        "top":          rows,
        "as_of":        latest_t,
        "any_baseline": bool(baseline),
        "summary_md":   summary_md,
        "used_llm":     used_llm,
    }


if __name__ == "__main__":
    import json as _j
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(_j.dumps(generate(), indent=2, ensure_ascii=False))
