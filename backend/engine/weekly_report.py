"""Weekly Report：每周一 9:00 生成并邮件推送。

v4 结构（2026-04-22 第三轮调整：加 HF 板块 + 重新排序）：
1. 头部：周数 + 本周事件/Release/榜单快照 统计
2. 🔴 本周关键信号（change_events P0/P1；含厂商博客 new_blog_post）
3. 📦 本周新模型/开源发布（按 repo 列 + LLM 参数变化 + 突破点 + 论文链接）
4. 📊 榜单变化（按领域聚合 LMArena/AA/SuperCLUE Top 5 + LLM 跨平台一句话）
5. 🤗 HuggingFace 趋势 Top 10（社区下载/讨论热度，NEW 徽标跨周对比）
6. 💬 社区声音（按 matched_model 聚合 + LLM 提炼用户观点 + 原帖链接）
7. 💭 本周社区热议（Reddit Top 帖按 LLM 归纳 3-5 个主题，兜底 alias 匹配不到的新话题）

砍掉：🔥 热度 Top 10（绝对分无对比参照信息量低，留到热度维度齐全后再加"本周上升最快"视图）。

任何一块 LLM 或数据挂了就跳过该块，不让整封邮件挂掉。
归档到 weekly_reports 表，前端 Dashboard 可以回看历史。
"""
import json
import logging
from datetime import datetime, timedelta
from html import escape

from backend.db import get_conn, record_status
from backend.engine import (
    alias_learner,
    hf_digest,
    openrouter_digest,
    leaderboard_digest,
    reddit_opinions,
    reddit_themes,
    release_digest,
)
from backend.utils.email_sender import send_email

logger = logging.getLogger(__name__)


# --------------------------- utils ---------------------------

def _iso_week(dt: datetime | None = None) -> str:
    dt = dt or datetime.now()
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


SEV_COLOR = {"P0": "#b91c1c", "P1": "#d97706", "P2": "#6b7280"}
CHANGE_COLOR = {"NEW": "#22c55e"}  # ↑/↓ 动态着色在渲染时处理


def _change_style(change: str | None) -> str:
    if not change:
        return ""
    if change == "NEW":
        return "background:#22c55e;color:white;"
    if change == "—":
        return "background:#e5e7eb;color:#6b7280;"
    if change.startswith("↑"):
        return "background:#dcfce7;color:#166534;"
    if change.startswith("↓"):
        return "background:#fee2e2;color:#b91c1c;"
    return "background:#e5e7eb;color:#6b7280;"


# --------------------------- 数据采集（保留原有）---------------------------

def _gather_events(period_start_iso: str, limit: int = 20) -> list[dict]:
    """拉 P0/P1 事件。**冷启动过滤**：
    当 event_type 是 new_repo/new_release 时，要检查这个 repo 所属 org 是否在该 event 之前已经被扫过。
    如果没有——说明这是系统第一次扫到这个 org 时把所有存量 repo/release 都当"新增"产生的 bootstrap 误报，
    周报里要过滤掉。真正"本周新增"是指该 org 已经扫过若干次之后才出现的 repo/release。
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, event_type, severity, source, title, detail_json, model_name, created_at
            FROM change_events
            WHERE created_at >= ? AND severity IN ('P0','P1')
            ORDER BY
                CASE severity WHEN 'P0' THEN 0 ELSE 1 END,
                created_at DESC
            LIMIT ?
            """,
            (period_start_iso, limit * 3),  # 多取一些，过滤后截断
        ).fetchall()
        events = [dict(r) for r in rows]

        # 预计算每个 org 的首次扫描时间
        scan_rows = conn.execute(
            "SELECT org, MIN(scraped_at) AS first_at FROM github_snapshots GROUP BY org"
        ).fetchall()
        first_scan = {r["org"]: r["first_at"] for r in scan_rows}

    filtered = []
    bootstrap_skipped = 0
    for e in events:
        if e["event_type"] in ("new_repo", "new_release"):
            # 从 detail_json 里拿 org
            org = None
            try:
                detail = json.loads(e.get("detail_json") or "{}")
                org = detail.get("org")
            except Exception:
                detail = {}
            if org:
                org_first = first_scan.get(org)
                # 该事件创建时间 ≤ org 首次扫描时间 + 10 分钟 → bootstrap 扫描期的误报
                if org_first and e["created_at"] <= org_first:
                    bootstrap_skipped += 1
                    continue
                # created_at 与 org_first 差 <5min 说明还是同一次 bootstrap 扫描
                try:
                    t1 = datetime.fromisoformat(e["created_at"])
                    t2 = datetime.fromisoformat(org_first) if org_first else None
                    if t2 and abs((t1 - t2).total_seconds()) < 300:
                        bootstrap_skipped += 1
                        continue
                except Exception:
                    pass
        filtered.append(e)
        if len(filtered) >= limit:
            break

    if bootstrap_skipped:
        logger.info("[Weekly] 冷启动过滤：跳过 %d 条 bootstrap 扫描产生的 new_repo/new_release 事件",
                    bootstrap_skipped)
    return filtered


def _gather_source_stats(period_start_iso: str, events: list[dict] | None = None) -> dict:
    """榜单行数 / 新 release 数 / 过滤后的事件数。事件数用上游已过滤好的列表计数，避免把冷启动误报算进去。"""
    with get_conn() as conn:
        lb = conn.execute(
            "SELECT COUNT(*) FROM leaderboard_snapshots WHERE scraped_at >= ?",
            (period_start_iso,),
        ).fetchone()[0]
        gh_releases = conn.execute(
            "SELECT COUNT(*) FROM github_releases WHERE scraped_at >= ?",
            (period_start_iso,),
        ).fetchone()[0]
    return {"leaderboard_rows": lb, "new_releases": gh_releases,
            "total_events": len(events) if events is not None else 0}


# --------------------------- HTML 渲染 ---------------------------

def _render_event_row(e: dict) -> str:
    detail = {}
    try:
        detail = json.loads(e.get("detail_json") or "{}")
    except Exception:
        pass
    link = detail.get("url") or detail.get("html_url") or ""
    link_html = (
        f'<div style="margin-top:4px;"><a href="{escape(link)}" '
        f'style="color:#1a7fd4;text-decoration:none;font-size:12px;">🔗 查看</a></div>'
        if link else ""
    )
    sev = e.get("severity") or "P2"
    color = SEV_COLOR.get(sev, "#6b7280")
    return f"""
    <tr><td style="padding:12px 18px;border-bottom:1px solid #eee;">
      <span style="display:inline-block;background:{color};color:white;font-size:11px;
                   padding:2px 8px;border-radius:3px;font-weight:700;margin-right:8px;">{escape(sev)}</span>
      <span style="color:#666;font-size:12px;">[{escape(e.get('event_type', ''))}] · {escape(e.get('source', ''))}</span>
      <div style="font-size:14px;color:#111;margin-top:4px;font-weight:500;">{escape(e.get('title', ''))}</div>
      {link_html}
    </td></tr>
    """


def _render_events_section(events: list[dict]) -> str:
    if not events:
        return "<div style='padding:18px;color:#999;text-align:center;'>本周 P0/P1 事件：无</div>"
    return (
        "<table style='width:100%;border-collapse:collapse;'>"
        + "".join(_render_event_row(e) for e in events)
        + "</table>"
    )


SOURCE_META = {
    "lmarena":   {"label": "LMArena",             "color": "#6366f1", "bg": "#eef2ff"},
    "aa":        {"label": "Artificial Analysis", "color": "#0891b2", "bg": "#ecfeff"},
    "superclue": {"label": "SuperCLUE",           "color": "#db2777", "bg": "#fdf2f8"},
}


def _render_leaderboard_platform(platform: dict) -> str:
    meta = SOURCE_META.get(platform["source"],
                           {"label": platform["source"], "color": "#6b7280", "bg": "#f3f4f6"})
    label, color, bg = meta["label"], meta["color"], meta["bg"]
    url = platform.get("public_url")

    badge_inner = f"""
      <span style="display:inline-block;padding:3px 10px;background:{bg};color:{color};
                   border-radius:4px;font-size:11px;font-weight:700;letter-spacing:0.5px;
                   margin-bottom:8px;">{escape(label)}{' →' if url else ''}</span>"""
    header = (
        f'<a href="{escape(url)}" style="text-decoration:none;" target="_blank" rel="noopener">{badge_inner}</a>'
        if url else f'<div>{badge_inner}</div>'
    )

    if not platform["top_n"]:
        return f"""
        <div style="flex:1;min-width:220px;">
          {header}
          <div style="color:#9ca3af;font-size:12px;padding:6px 0;">无数据</div>
        </div>"""

    # 计算最大分数用于画条
    scores = [it["score"] for it in platform["top_n"] if it.get("score") is not None]
    max_score = max(scores) if scores else None

    rows = []
    for it in platform["top_n"]:
        change = it.get("change")
        change_html = ""
        if change:
            change_html = (
                f'<span style="display:inline-block;{_change_style(change)}'
                f'font-size:10px;padding:1px 5px;border-radius:3px;margin-left:6px;font-weight:700;'
                f'vertical-align:middle;">{escape(change)}</span>'
            )
        # 分数条
        score = it.get("score")
        if score is not None and max_score:
            pct = max(8, int(score / max_score * 100))
            score_block = f"""
            <div style="display:flex;align-items:center;gap:6px;min-width:72px;">
              <div style="width:40px;height:3px;background:#f3f4f6;border-radius:2px;overflow:hidden;">
                <div style="width:{pct}%;height:100%;background:{color};"></div>
              </div>
              <span style="color:{color};font-weight:600;font-size:11px;font-variant-numeric:tabular-nums;">{score:.1f}</span>
            </div>"""
        else:
            score_block = '<div style="min-width:72px;"></div>'

        # rank 圆点
        rank_color = color if it["rank"] <= 3 else "#9ca3af"
        rows.append(f"""
        <div style="display:flex;align-items:center;gap:8px;padding:5px 0;
                    border-bottom:1px dashed #f3f4f6;">
          <span style="display:inline-flex;align-items:center;justify-content:center;
                       width:20px;height:20px;border-radius:50%;background:{rank_color};color:white;
                       font-size:10px;font-weight:700;flex-shrink:0;">{it['rank']}</span>
          <span style="flex:1;font-size:12px;color:#1f2937;overflow:hidden;text-overflow:ellipsis;
                       white-space:nowrap;">{escape(it['model_name'])}{change_html}</span>
          {score_block}
        </div>""")

    return f"""
    <div style="flex:1;min-width:240px;">
      {header}
      <div>{"".join(rows)}</div>
    </div>"""


def _render_leaderboard_section(leaderboards: dict) -> str:
    if not leaderboards:
        return "<div style='padding:18px;color:#999;text-align:center;'>榜单数据未就绪</div>"

    blocks = []
    for domain_key, d in leaderboards.items():
        platforms_html = "".join(_render_leaderboard_platform(p) for p in d["platforms"])
        baseline_note = (
            '' if d.get("any_baseline") else
            '<span style="color:#9ca3af;font-size:11px;font-weight:400;margin-left:8px;">· 冷启动期，暂无上周对比</span>'
        )
        blocks.append(f"""
        <div style="padding:18px 22px;border-bottom:1px solid #eef2f7;">
          <div style="font-size:15px;font-weight:700;color:#0f172a;margin-bottom:14px;">
            {escape(d['title'])}{baseline_note}
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:22px;">{platforms_html}</div>
          <div style="padding:10px 14px;margin-top:14px;background:#f8fafc;border-left:3px solid #3b82f6;
                      border-radius:4px;font-size:13px;color:#1f2937;line-height:1.65;">
            <strong style="color:#3b82f6;">本周总结 · </strong>{escape(d['summary_md'])}
          </div>
        </div>""")
    return "".join(blocks)


KIND_LABEL = {
    "model":     ("🧠 模型", "#7c3aed"),
    "eval":      ("📊 评测", "#0891b2"),
    "framework": ("⚙️ 框架", "#059669"),
    "tool":      ("🛠️ 工具", "#d97706"),
    "other":     ("📦 其他", "#6b7280"),
}


def _render_release_item(r: dict) -> str:
    kind = r.get("kind", "other")
    label, color = KIND_LABEL.get(kind, KIND_LABEL["other"])
    pub = (r.get("published_at") or "").split("T")[0]
    paper = r.get("paper_url") or ""
    paper_html = f' · <a href="{escape(paper)}" style="color:#1a7fd4;">📄 论文</a>' if paper else ""
    return f"""
    <div style="padding:12px 18px;border-bottom:1px solid #eee;">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px;">
        <span style="display:inline-block;background:{color};color:white;font-size:10px;
                     padding:2px 7px;border-radius:3px;font-weight:700;">{escape(label)}</span>
        <span style="font-size:14px;font-weight:600;color:#111;">{escape(r['org'])}/{escape(r['repo_name'])}</span>
        <span style="color:#666;font-size:12px;">{escape(r.get('tag_name') or '')}</span>
        <span style="color:#999;font-size:11px;margin-left:auto;">{escape(pub)}</span>
      </div>
      <div style="font-size:13px;color:#222;line-height:1.6;margin-top:4px;">{escape(r.get('one_liner') or '')}</div>
      <div style="margin-top:6px;font-size:12px;">
        <a href="{escape(r.get('html_url') or '')}" style="color:#1a7fd4;text-decoration:none;">🔗 Release</a>{paper_html}
      </div>
    </div>"""


def _render_release_section(releases: dict) -> str:
    items = releases.get("items") or []
    if not items:
        return "<div style='padding:18px;color:#999;text-align:center;'>本周无新 Release</div>"
    return "".join(_render_release_item(r) for r in items)


def _render_opinions_section(opinions: dict) -> str:
    models = opinions.get("models") or []
    if not models:
        fb = opinions.get("fallback_md") or "本周社区数据不足。"
        return f"<div style='padding:18px;color:#666;font-size:13px;line-height:1.6;'>{escape(fb)}</div>"

    blocks = []
    for m in models:
        quotes_html = "".join(f"""
        <div style="padding:8px 12px;margin:6px 0;background:#f8fafc;border-left:3px solid #5fd1b5;
                    border-radius:3px;font-size:13px;color:#222;line-height:1.6;">
          {escape(op['quote'])}
          <div style="margin-top:4px;"><a href="{escape(op['url'])}" style="color:#1a7fd4;font-size:11px;">原帖 →</a></div>
        </div>""" for op in m["opinions"])
        blocks.append(f"""
        <div style="padding:12px 18px;border-bottom:1px solid #eee;">
          <div style="font-size:14px;font-weight:700;color:#111;margin-bottom:4px;">
            {escape(m['model'])}
            <span style="color:#999;font-size:11px;font-weight:400;margin-left:6px;">{m['post_count']} 帖</span>
          </div>
          {quotes_html}
        </div>""")
    return "".join(blocks)


def _render_hf_section(hf_data: dict) -> str:
    top = hf_data.get("top") or []
    if not top:
        return "<div style='padding:18px;color:#999;text-align:center;'>HuggingFace 数据未就绪</div>"

    as_of = (hf_data.get("as_of") or "").split(".")[0]
    baseline_note = (
        '' if hf_data.get("any_baseline") else
        '<span style="color:#9ca3af;font-size:11px;font-weight:400;margin-left:8px;">· 冷启动期，暂无上周对比</span>'
    )

    rows = []
    for it in top:
        rank = it["rank"]
        rank_color = "#f97316" if rank <= 3 else "#9ca3af"

        change = it.get("change")
        change_html = ""
        if change == "NEW":
            change_html = (
                '<span style="display:inline-block;background:#22c55e;color:white;'
                'font-size:10px;padding:1px 5px;border-radius:3px;margin-left:6px;'
                'font-weight:700;vertical-align:middle;">NEW</span>'
            )

        pipe = it.get("pipeline_tag") or ""
        pipe_html = (
            f'<span style="display:inline-block;background:#eef2ff;color:#6366f1;'
            f'font-size:10px;padding:1px 6px;border-radius:3px;margin-left:6px;'
            f'font-weight:600;">{escape(pipe)}</span>' if pipe else ""
        )

        matched = it.get("matched_model")
        matched_html = (
            f'<span style="color:#0891b2;font-size:11px;margin-left:6px;">→ {escape(matched)}</span>'
            if matched else ""
        )

        likes = it.get("likes") or 0
        dls   = it.get("downloads") or 0
        stats_html = (
            f'<span style="color:#64748b;font-size:11px;font-variant-numeric:tabular-nums;'
            f'white-space:nowrap;">❤ {likes} · ⬇ {_fmt_downloads(dls)}</span>'
        )

        rows.append(f"""
        <div style="display:flex;align-items:center;gap:8px;padding:7px 0;
                    border-bottom:1px dashed #f3f4f6;">
          <span style="display:inline-flex;align-items:center;justify-content:center;
                       width:22px;height:22px;border-radius:50%;background:{rank_color};color:white;
                       font-size:11px;font-weight:700;flex-shrink:0;">{rank}</span>
          <span style="flex:1;font-size:13px;color:#1f2937;overflow:hidden;
                       text-overflow:ellipsis;white-space:nowrap;">
            <a href="{escape(it['hf_url'])}" target="_blank" rel="noopener"
               style="color:#1f2937;text-decoration:none;font-weight:500;">{escape(it['model_id'])}</a>
            {change_html}{pipe_html}{matched_html}
          </span>
          {stats_html}
        </div>""")

    summary_md = (hf_data.get("summary_md") or "").strip()
    summary_html = (
        f"""
      <div style="padding:10px 14px;margin-top:14px;background:#f8fafc;border-left:3px solid #f97316;
                  border-radius:4px;font-size:13px;color:#1f2937;line-height:1.65;">
        <strong style="color:#f97316;">本周总结 · </strong>{escape(summary_md)}
      </div>"""
        if summary_md else ""
    )

    return f"""
    <div style="padding:14px 22px;">
      <div style="font-size:11px;color:#94a3b8;margin-bottom:10px;">
        快照时间 {escape(as_of)}{baseline_note}
      </div>
      <div>{"".join(rows)}</div>
      {summary_html}
    </div>"""


def _render_openrouter_section(or_data: dict) -> str:
    """OpenRouter 周榜：比 HF/榜单都"硬"——这是真金白银的 API token 消耗。"""
    top = or_data.get("top") or []
    if not top:
        return "<div style='padding:18px;color:#999;text-align:center;'>OpenRouter 数据未就绪</div>"

    week = or_data.get("week_date") or ""
    rows = []
    for it in top:
        rank = it["rank"]
        rank_color = "#0ea5e9" if rank <= 3 else "#9ca3af"  # 天蓝色区分于 HF 的橙色

        chg = it.get("change_pct")
        is_new = it.get("is_new")
        if is_new:
            chg_html = (
                '<span style="display:inline-block;background:#22c55e;color:white;'
                'font-size:10px;padding:1px 5px;border-radius:3px;margin-left:6px;'
                'font-weight:700;vertical-align:middle;">NEW</span>'
            )
        elif chg is not None and chg >= 1.0:  # 暴涨 ≥ 100%
            chg_html = (
                f'<span style="display:inline-block;background:#fef3c7;color:#b45309;'
                f'font-size:10px;padding:1px 5px;border-radius:3px;margin-left:6px;'
                f'font-weight:700;vertical-align:middle;">🔥 +{chg*100:.0f}%</span>'
            )
        elif chg is not None and chg > 0:
            chg_html = (
                f'<span style="color:#16a34a;font-size:11px;margin-left:6px;font-weight:600;'
                f'font-variant-numeric:tabular-nums;">▲ {chg*100:.0f}%</span>'
            )
        elif chg is not None and chg < 0:
            chg_html = (
                f'<span style="color:#dc2626;font-size:11px;margin-left:6px;font-weight:600;'
                f'font-variant-numeric:tabular-nums;">▼ {chg*100:.0f}%</span>'
            )
        else:
            chg_html = ""

        matched = it.get("matched_model")
        matched_html = (
            f'<span style="color:#0891b2;font-size:11px;margin-left:6px;">→ {escape(matched)}</span>'
            if matched else ""
        )

        tokens_html = (
            f'<span style="color:#0f172a;font-size:12px;font-weight:600;'
            f'font-variant-numeric:tabular-nums;white-space:nowrap;">{escape(it["tokens_display"])} tokens</span>'
        )

        author_html = (
            f'<span style="color:#94a3b8;font-size:11px;">by {escape(it["author"] or "")}</span>'
        )

        rows.append(f"""
        <div style="display:flex;align-items:center;gap:8px;padding:7px 0;
                    border-bottom:1px dashed #f3f4f6;">
          <span style="display:inline-flex;align-items:center;justify-content:center;
                       width:22px;height:22px;border-radius:50%;background:{rank_color};color:white;
                       font-size:11px;font-weight:700;flex-shrink:0;">{rank}</span>
          <span style="flex:1;font-size:13px;color:#1f2937;overflow:hidden;
                       text-overflow:ellipsis;white-space:nowrap;">
            <a href="{escape(it['url'])}" target="_blank" rel="noopener"
               style="color:#1f2937;text-decoration:none;font-weight:500;">{escape(it['name'])}</a>
            {author_html}
            {chg_html}{matched_html}
          </span>
          {tokens_html}
        </div>""")

    summary_md = (or_data.get("summary_md") or "").strip()
    summary_html = (
        f"""
      <div style="padding:10px 14px;margin-top:14px;background:#f0f9ff;border-left:3px solid #0ea5e9;
                  border-radius:4px;font-size:13px;color:#1f2937;line-height:1.65;">
        <strong style="color:#0ea5e9;">本周总结 · </strong>{escape(summary_md)}
      </div>"""
        if summary_md else ""
    )

    return f"""
    <div style="padding:14px 22px;">
      <div style="font-size:11px;color:#94a3b8;margin-bottom:10px;">
        周榜日期 {escape(week)} · OpenRouter 自聚合 · change% 为周环比
      </div>
      <div>{"".join(rows)}</div>
      {summary_html}
    </div>"""


def _fmt_downloads(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _render_themes_section(themes_data: dict) -> str:
    themes = themes_data.get("themes") or []
    if not themes:
        fb = themes_data.get("fallback_md") or "本周 Reddit 数据不足，未归纳出热议主题。"
        return f"<div style='padding:18px;color:#666;font-size:13px;line-height:1.6;'>{escape(fb)}</div>"

    blocks = []
    for t in themes:
        posts = t.get("posts") or []
        links_html = " · ".join(
            f'<a href="{escape(p.get("url") or "")}" '
            f'style="color:#64748b;text-decoration:none;font-size:11px;" '
            f'title="{escape((p.get("title") or "")[:120])}">'
            f'r/{escape(p.get("subreddit") or "")} {p.get("score", 0)}↑</a>'
            for p in posts
        )
        posts_html = (
            f'<div style="margin-top:6px;color:#94a3b8;font-size:11px;">代表帖：{links_html}</div>'
            if links_html else ""
        )
        blocks.append(f"""
        <div style="padding:12px 18px;border-bottom:1px solid #eee;">
          <div style="font-size:14px;font-weight:700;color:#111;margin-bottom:4px;">
            {escape(t.get('title') or '')}
          </div>
          <div style="font-size:13px;color:#334155;line-height:1.65;">
            {escape(t.get('summary') or '')}
          </div>
          {posts_html}
        </div>""")
    return "".join(blocks)


def _render_html(data: dict) -> str:
    week   = data["week_number"]
    stats  = data["stats"]
    events_html       = _render_events_section(data["events"])
    leaderboard_html  = _render_leaderboard_section(data["leaderboards"])
    hf_html           = _render_hf_section(data["hf"])
    openrouter_html   = _render_openrouter_section(data["openrouter"])
    releases_html     = _render_release_section(data["releases"])
    opinions_html     = _render_opinions_section(data["opinions"])
    themes_html       = _render_themes_section(data["themes"])

    return f"""\
<!doctype html>
<html><body style="margin:0;padding:24px;background:#f5f5f7;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;">
<div style="max-width:720px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06);">

  <div style="padding:24px 28px;background:linear-gradient(135deg,#0f172a,#1e3a8a);color:white;">
    <div style="font-size:12px;opacity:0.7;letter-spacing:1px;">MODELRADAR WEEKLY</div>
    <div style="font-size:22px;font-weight:700;margin-top:6px;">{escape(week)} · AI 模型周报</div>
    <div style="font-size:12px;opacity:0.75;margin-top:8px;">
      本周事件 <strong>{stats['total_events']}</strong> 条 ·
      新 Release <strong>{stats['new_releases']}</strong> 个 ·
      榜单快照 <strong>{stats['leaderboard_rows']}</strong> 行
    </div>
  </div>

  <div style="padding:18px 24px;border-bottom:1px solid #eee;">
    <h3 style="margin:0 0 10px 0;font-size:14px;color:#111;">🔴 本周关键信号</h3>
    {events_html}
  </div>

  <div style="padding:4px 6px;border-bottom:1px solid #eee;">
    <h3 style="margin:14px 18px 4px;font-size:14px;color:#111;">
      📦 本周新模型 / 开源发布
      <span style="font-weight:400;color:#999;font-size:11px;margin-left:6px;">按 repo，含参数变化 + 突破点</span>
    </h3>
    {releases_html}
  </div>

  <div style="padding:4px 6px;border-bottom:1px solid #eee;">
    <h3 style="margin:14px 18px 10px;font-size:14px;color:#111;">📊 榜单变化</h3>
    {leaderboard_html}
  </div>

  <div style="padding:4px 6px;border-bottom:1px solid #eee;">
    <h3 style="margin:14px 18px 4px;font-size:14px;color:#111;">
      🤗 HuggingFace 趋势 Top 10
      <span style="font-weight:400;color:#999;font-size:11px;margin-left:6px;">社区下载/讨论热度实时信号</span>
    </h3>
    {hf_html}
  </div>

  <div style="padding:4px 6px;border-bottom:1px solid #eee;">
    <h3 style="margin:14px 18px 4px;font-size:14px;color:#111;">
      🔌 OpenRouter 真实调用量
      <span style="font-weight:400;color:#999;font-size:11px;margin-left:6px;">开发者生产环境 token 消耗 · 周粒度</span>
    </h3>
    {openrouter_html}
  </div>

  <div style="padding:4px 6px;border-bottom:1px solid #eee;">
    <h3 style="margin:14px 18px 4px;font-size:14px;color:#111;">
      💬 社区声音
      <span style="font-weight:400;color:#999;font-size:11px;margin-left:6px;">Reddit 按模型聚合</span>
    </h3>
    {opinions_html}
  </div>

  <div style="padding:4px 6px;border-bottom:1px solid #eee;">
    <h3 style="margin:14px 18px 4px;font-size:14px;color:#111;">
      💭 本周社区热议
      <span style="font-weight:400;color:#999;font-size:11px;margin-left:6px;">LLM 归纳 · 基于 Reddit Top {data['themes'].get('post_count', 0)} 帖</span>
    </h3>
    {themes_html}
  </div>

  <div style="padding:16px 24px;background:#fafafa;color:#999;font-size:11px;text-align:center;">
    ModelRadar 周报 · 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} · 数据源 LMArena / AA / SuperCLUE / HuggingFace / OpenRouter / GitHub / 厂商博客 / Reddit
  </div>
</div>
</body></html>"""


# --------------------------- 持久化 & 入口 ---------------------------

def _persist(data: dict, html: str) -> None:
    stats_json = json.dumps({
        "stats":          data["stats"],
        "events_count":   len(data["events"]),
        "leaderboards":   {k: {"any_baseline": v.get("any_baseline"),
                               "used_llm":     v.get("used_llm")}
                           for k, v in data["leaderboards"].items()},
        "hf":             {"top_n":         len((data.get("hf") or {}).get("top") or []),
                           "any_baseline":  (data.get("hf") or {}).get("any_baseline", False),
                           "as_of":         (data.get("hf") or {}).get("as_of")},
        "openrouter":     {"top_n":         len((data.get("openrouter") or {}).get("top") or []),
                           "week_date":     (data.get("openrouter") or {}).get("week_date"),
                           "used_llm":      (data.get("openrouter") or {}).get("used_llm")},
        "releases":       {"kept":      data["releases"].get("kept_count", 0),
                           "noise":     data["releases"].get("noise_count", 0),
                           "dedup":     data["releases"].get("dedup_count", 0),
                           "used_llm":  data["releases"].get("used_llm", False)},
        "opinions":       {"model_count": len(data["opinions"].get("models") or [])},
        "themes":         {"theme_count": len(data["themes"].get("themes") or []),
                           "post_count":  data["themes"].get("post_count", 0),
                           "used_llm":    data["themes"].get("used_llm", False)},
        "alias_learner":  {"auto_accepted": len((data.get("alias_stats") or {}).get("auto_accepted") or []),
                           "pending_added": (data.get("alias_stats") or {}).get("pending_added", 0)},
    }, ensure_ascii=False)

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO weekly_reports(week_number, period_start, period_end, html, stats_json, sent_at)
            VALUES (?, ?, ?, ?, ?, NULL)
            ON CONFLICT(week_number) DO UPDATE SET
                html = excluded.html,
                stats_json = excluded.stats_json,
                period_start = excluded.period_start,
                period_end = excluded.period_end
            """,
            (
                data["week_number"],
                data["period_start"],
                data["period_end"],
                html,
                stats_json,
            ),
        )


def _mark_sent(week: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE weekly_reports SET sent_at=CURRENT_TIMESTAMP WHERE week_number=?",
            (week,),
        )


def _safe_call(name: str, fn, *args, **kwargs):
    """任何一个子模块挂了都不能让整封周报挂。"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.exception("[Weekly] 子模块 %s 失败: %s", name, e)
        return None


def generate(days: int = 7) -> dict:
    """拼数据。不发邮件也不入库。"""
    now = datetime.now()
    period_start = now - timedelta(days=days)
    period_start_iso = period_start.strftime("%Y-%m-%d %H:%M:%S")
    week = _iso_week(now)

    events            = _safe_call("events",       _gather_events, period_start_iso) or []
    stats             = _safe_call("stats",        _gather_source_stats, period_start_iso, events) or {"leaderboard_rows": 0, "new_releases": 0, "total_events": 0}
    leaderboards      = _safe_call("leaderboards", leaderboard_digest.generate, days=days) or {}
    hf                = _safe_call("hf",           hf_digest.generate, days=days) or {"top": [], "as_of": None, "any_baseline": False}
    openrouter        = _safe_call("openrouter",   openrouter_digest.generate) or {"top": [], "week_date": None, "any_previous": False, "summary_md": "", "used_llm": False}
    releases          = _safe_call("releases",     release_digest.generate, days=days) or {"items": [], "used_llm": False, "kept_count": 0, "noise_count": 0, "dedup_count": 0}
    opinions          = _safe_call("opinions",     reddit_opinions.generate, days=days) or {"models": [], "fallback_md": "(opinions 模块异常)"}
    # alias 自愈：先扫未匹配高分帖把新模型名自动加进 learned_aliases，
    # 再跑 opinions（虽然本轮已跑过，但下一次 Reddit 采集会受益）和 themes。
    # themes 过程里 LLM 识别的模型名也会走自动接受。
    alias_stats       = _safe_call("alias_learner", alias_learner.learn_from_reddit, days=days) or {}
    themes            = _safe_call("themes",        reddit_themes.generate, days=days) or {"themes": [], "post_count": 0, "used_llm": False, "fallback_md": "(themes 模块异常)"}

    return {
        "week_number":   week,
        "period_start":  period_start.strftime("%Y-%m-%d %H:%M:%S"),
        "period_end":    now.strftime("%Y-%m-%d %H:%M:%S"),
        "events":        events,
        "stats":         stats,
        "leaderboards":  leaderboards,
        "hf":            hf,
        "openrouter":    openrouter,
        "releases":      releases,
        "opinions":      opinions,
        "themes":        themes,
        "alias_stats":   alias_stats,
    }


def generate_and_send(days: int = 7, dry_run: bool = False) -> dict:
    """完整流程：拼数据 → 渲染 HTML → 入库 → 发邮件。"""
    try:
        data = generate(days=days)
        html = _render_html(data)
        _persist(data, html)

        result = {"week": data["week_number"], "sent": False, "html_bytes": len(html)}
        if dry_run:
            logger.info("[Weekly] dry_run 模式：不发邮件，已归档到 weekly_reports")
            record_status("weekly_report", success=True)
            return result

        subject = f"ModelRadar 周报 · {data['week_number']} · {data['stats']['total_events']} 条事件"
        ok = send_email(subject, html)
        if ok:
            _mark_sent(data["week_number"])
            result["sent"] = True
            logger.info("[Weekly] 周报已发送，周=%s", data["week_number"])
        else:
            logger.error("[Weekly] 邮件发送失败，已归档未发送")

        record_status("weekly_report", success=ok)
        return result
    except Exception as e:
        logger.exception("[Weekly] 生成失败: %s", e)
        record_status("weekly_report", success=False, error=str(e))
        raise


if __name__ == "__main__":
    import logging as _l
    import sys
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    dry = "--send" not in sys.argv
    print(generate_and_send(dry_run=dry))
