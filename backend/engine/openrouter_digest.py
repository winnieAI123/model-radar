"""OpenRouter Digest：给周报产出 "🔌 OpenRouter 真实调用量 Top N" + LLM 总结。

为什么要有这个板块：
- LMArena/AA 榜单是"评委觉得谁强"
- HF trending 是"社区在围观谁"
- OpenRouter 是"开发者真金白银在调用谁"——最硬的生产信号

每行信号：
- rank / model / author
- total_tokens（周 token 总量）+ request_count
- change_pct：周环比（+42% / -26%）; None 表示本周新进榜
- matched_model：canonical 对齐（便于和其他板块互链）

输出：
{
  "top":          [{rank, permaslug, name, author, total_tokens, tokens_display,
                    request_count, change_pct, change_label, is_new, matched_model}],
  "week_date":    "2026-04-21",
  "any_previous": bool,                  # 有没有上一次快照可以做内部 diff（目前只看 OR 自己给的 change）
  "summary_md":   "……",
  "used_llm":     bool,
}
"""
import logging

from backend.db import get_conn
from backend.engine.hf_digest import HUMANIZER_PRINCIPLES
from backend.utils import llm_client

logger = logging.getLogger(__name__)


def _latest_snapshot(conn) -> str | None:
    row = conn.execute(
        "SELECT MAX(scraped_at) AS t FROM openrouter_rankings"
    ).fetchone()
    return row["t"] if row and row["t"] else None


def _latest_rows(conn, scraped_at: str, top_n: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT rank, model_permaslug, author, total_tokens, completion_tokens,
               prompt_tokens, request_count, change_pct, matched_model, week_date
        FROM openrouter_rankings
        WHERE scraped_at = ?
        ORDER BY rank ASC
        LIMIT ?
        """,
        (scraped_at, top_n),
    ).fetchall()
    return [dict(r) for r in rows]


def _tokens_display(n: int) -> str:
    """1,416,041,000,000 → '1.42T'；1B 量级走 'B tokens' 更符合 OR 页面习惯。"""
    if n is None:
        return "-"
    if n >= 1_000_000_000_000:
        return f"{n/1_000_000_000_000:.2f}T"
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.0f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.0f}M"
    return str(n)


def _model_name(permaslug: str) -> str:
    """anthropic/claude-4.6-sonnet-20260217 → claude-4.6-sonnet
    只去掉尾部 YYYYMMDD 戳，保留 OR 原始命名（他们已经是 kebab-case，瞎大小写反而认不出）。
    """
    if not permaslug:
        return permaslug
    tail = permaslug.split("/", 1)[-1]
    import re
    return re.sub(r"-\d{8}$", "", tail)


def _change_label(chg: float | None) -> str:
    if chg is None:
        return "NEW"
    pct = chg * 100
    if pct >= 100:
        return f"🔥 +{pct:.0f}%"
    if pct > 0:
        return f"+{pct:.0f}%"
    if pct == 0:
        return "±0%"
    return f"{pct:.0f}%"


def _format_for_prompt(rows: list[dict]) -> str:
    lines = ["### 本周 OpenRouter API 调用量 Top 榜"]
    for it in rows:
        parts = [f"#{it['rank']} {it['name']}", f"by {it['author']}"]
        parts.append(f"tokens={it['tokens_display']}")
        parts.append(f"requests={it['request_count']:,}")
        parts.append(f"change={it['change_label']}")
        if it.get("matched_model"):
            parts.append(f"canonical={it['matched_model']}")
        lines.append("- " + " · ".join(parts))
    return "\n".join(lines)


def _llm_summary(rows: list[dict]) -> tuple[str, bool]:
    if not rows:
        return "本周 OpenRouter 无数据。", False

    user_content = (
        f"{HUMANIZER_PRINCIPLES}\n\n"
        f"任务：根据下面 OpenRouter 本周 API 真实调用量 Top 榜，给一个 80-140 字的中文总结。\n"
        f"OpenRouter 是一个 LLM API 聚合平台，token 数代表真实被生产环境调用的量级——这是最硬的使用信号。\n"
        f"请覆盖：① 本周谁在主导（top 1-3 的量级对比）；② 哪些是 NEW 进榜（非常值得关注，代表刚上线就被调起来）；"
        f"③ 哪些是 🔥 超 100% 暴涨；④ 哪些明显下滑或排名倒退。\n"
        f"不要罗列每一行，抓结构性信号即可。\n\n"
        f"--- 数据 ---\n{_format_for_prompt(rows)}\n\n"
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

    # 降级模板
    top1 = rows[0]
    new_rows = [r for r in rows if r.get("is_new")]
    surge_rows = [r for r in rows if r.get("change_pct") and r["change_pct"] >= 1.0]
    bits = [f"Top 1：{top1['name']}（{top1['tokens_display']} tokens）"]
    if new_rows:
        bits.append("NEW 进榜 " + "、".join(r["name"] for r in new_rows[:3]))
    if surge_rows:
        bits.append("🔥 环比翻倍 " + "、".join(
            f"{r['name']}(+{r['change_pct']*100:.0f}%)" for r in surge_rows[:3]))
    return "；".join(bits) + "。", False


def generate(top_n: int = 20) -> dict:
    """产出 OpenRouter Top N + 一句话总结。"""
    with get_conn() as conn:
        latest = _latest_snapshot(conn)
        if not latest:
            return {"top": [], "week_date": None, "any_previous": False,
                    "summary_md": "本周 OpenRouter 数据未就绪。", "used_llm": False}
        raw_rows = _latest_rows(conn, latest, top_n)

    if not raw_rows:
        return {"top": [], "week_date": None, "any_previous": False,
                "summary_md": "本周 OpenRouter 数据未就绪。", "used_llm": False}

    week_date = raw_rows[0].get("week_date")
    top_rows = []
    for r in raw_rows:
        permaslug = r["model_permaslug"]
        chg = r["change_pct"]
        top_rows.append({
            "rank":            r["rank"],
            "permaslug":       permaslug,
            "name":            _model_name(permaslug),
            "author":          r["author"],
            "total_tokens":    r["total_tokens"],
            "tokens_display":  _tokens_display(r["total_tokens"]),
            "request_count":   r["request_count"],
            "change_pct":      chg,
            "change_label":    _change_label(chg),
            "is_new":          chg is None,
            "matched_model":   r["matched_model"],
            "url":             f"https://openrouter.ai/{permaslug}",
        })

    summary_md, used_llm = _llm_summary(top_rows)

    logger.info(
        "[OpenRouter Digest] week=%s · Top %d · NEW=%d · 暴涨=%d · LLM=%s",
        week_date, len(top_rows),
        sum(1 for r in top_rows if r["is_new"]),
        sum(1 for r in top_rows if r["change_pct"] and r["change_pct"] >= 1.0),
        used_llm,
    )
    return {
        "top":          top_rows,
        "week_date":    week_date,
        "any_previous": True,  # OR 自己给的 change 字段就是内建基线
        "summary_md":   summary_md,
        "used_llm":     used_llm,
    }


if __name__ == "__main__":
    import json as _j
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(_j.dumps(generate(), indent=2, ensure_ascii=False))
