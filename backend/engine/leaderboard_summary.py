"""排名变动摘要：纯模板生成，不用 LLM。
被 weekly_report 和 alert 邮件使用。MVP 版。
"""
import json
from collections import defaultdict


def summarize_events(events: list[dict]) -> str:
    """把一批 change_events 里的榜单事件拼成一段中文摘要。

    events: list of dict-like rows from change_events 表，需含
            event_type / title / detail_json
    """
    buckets: dict[str, list[str]] = defaultdict(list)
    for e in events:
        t = e["event_type"]
        if t == "rank_crowned":
            buckets["登顶"].append(e["title"])
        elif t == "rank_change":
            buckets["上升"].append(e["title"])
        elif t == "new_model_on_board":
            buckets["首次上榜"].append(e["title"])
        elif t == "new_release":
            buckets["新 Release"].append(e["title"])
        elif t == "new_repo":
            buckets["新仓库"].append(e["title"])
        elif t == "star_surge":
            buckets["Star 飙升"].append(e["title"])

    if not buckets:
        return "（本次周期无显著变动）"

    order = ["登顶", "上升", "首次上榜", "新仓库", "新 Release", "Star 飙升"]
    lines = []
    for cat in order:
        items = buckets.get(cat, [])
        if not items:
            continue
        lines.append(f"【{cat}】")
        for t in items:
            lines.append(f"  → {t}")
        lines.append("")
    return "\n".join(lines).strip()
