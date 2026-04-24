"""Weekly Report：每周一 9:00 生成并邮件推送。

v4 结构（2026-04-22 第三轮调整：加 HF 板块 + 重新排序）：
1. 头部：周数 + 本周事件/Release/榜单快照 统计
2. 🔴 本周关键信号（change_events P0/P1；含厂商博客 new_blog_post）
3. 📦 本周新模型/开源发布（按 repo 列 + LLM 参数变化 + 突破点 + 论文链接）
4. 📊 榜单变化（按领域聚合 LMArena/AA/SuperCLUE Top 20 + LLM 跨平台一句话）
5. 🤗 HuggingFace 趋势 Top 10（社区下载/讨论热度，NEW 徽标跨周对比）
6. 💬 社区声音（按 matched_model 聚合 + LLM 提炼用户观点 + 原帖链接）
7. 💭 本周社区热议（Reddit Top 帖按 LLM 归纳 3-5 个主题，兜底 alias 匹配不到的新话题）
8. 📮 中文社区观察（过去 7d 公众号文章按事件聚合 + 每篇文章的独特角度）

砍掉：🔥 热度 Top 10（绝对分无对比参照信息量低，留到热度维度齐全后再加"本周上升最快"视图）。

任何一块 LLM 或数据挂了就跳过该块，不让整封邮件挂掉。
归档到 weekly_reports 表，前端 Dashboard 可以回看历史。
"""
import json
import logging
from datetime import datetime, timedelta
from html import escape

from backend.collectors import openrouter as openrouter_collector
from backend.db import get_conn, record_status
from backend.engine import (
    alias_learner,
    hf_digest,
    openrouter_digest,
    leaderboard_digest,
    reddit_opinions,
    reddit_themes,
    release_digest,
    wechat_themes,
)
from backend.utils.email_sender import send_email

logger = logging.getLogger(__name__)


# --------------------------- utils ---------------------------

def _iso_week(dt: datetime | None = None) -> str:
    dt = dt or datetime.now()
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


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



def _render_html(data: dict) -> str:
    """Editorial-style weekly report (A+ design)."""
    BG = "#faf8f3"
    FG = "#1a1a1a"
    MUTED = "#4a4a4a"
    FAINT = "#8a8a8a"
    RULE = "#d4cfc3"
    ACCENT = "#991b1b"
    SERIF = "Georgia,'Source Serif Pro','Noto Serif SC',serif"
    SANS = "-apple-system,BlinkMacSystemFont,'Helvetica Neue','PingFang SC',sans-serif"

    ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX"]

    def _esc(s):
        return escape(str(s) if s is not None else "")

    def _fmt_dl(n):
        n = n or 0
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}K"
        return str(n)

    def _parse_link(e):
        try:
            detail = json.loads(e.get("detail_json") or "{}")
            return detail.get("url") or detail.get("html_url") or ""
        except Exception:
            return ""

    stats = data["stats"]
    week = data["week_number"]
    now_str = datetime.now().strftime("%B %d, %Y")

    masthead = f"""
    <div class="mr-masthead" style="padding:52px 56px 40px;text-align:center;background:{BG};border-bottom:3px double {FG};">
      <div style="font-family:{SERIF};font-size:11px;color:{FG};letter-spacing:0.5em;text-transform:uppercase;margin-bottom:10px;">MODELRADAR</div>
      <div style="font-family:{SERIF};font-size:40px;color:{FG};font-weight:700;letter-spacing:-0.015em;line-height:1.15;margin:0;">The Week in Models</div>
      <div style="font-family:{SERIF};font-style:italic;font-size:13px;color:{MUTED};margin-top:18px;">
        Issue <strong style="font-weight:600;">{_esc(week.split('-W')[-1])}</strong> &middot; {now_str}
      </div>
      <div style="font-family:{SANS};font-size:10px;color:{FAINT};margin-top:12px;letter-spacing:0.1em;text-transform:uppercase;">
        {stats['total_events']} alerts · {stats['new_releases']} releases · {stats['leaderboard_rows']} leaderboard rows
      </div>
    </div>
    """

    def sec(idx, title_en, title_zh, body):
        roman = ROMAN[idx - 1]
        return f"""
        <div class="mr-section" style="padding:56px 56px 44px;border-top:2px solid {FG};background:{BG};">
          <div style="font-family:{SERIF};font-size:28px;color:{FG};font-weight:700;letter-spacing:-0.015em;line-height:1.2;margin-bottom:32px;">
            <span style="color:{ACCENT};font-weight:400;font-size:22px;letter-spacing:0.05em;">§ {roman}</span>
            <span style="color:{FAINT};font-weight:400;margin:0 12px;">·</span>{title_zh}
            <span style="font-family:{SANS};font-size:11px;color:{FAINT};letter-spacing:0.25em;text-transform:uppercase;font-weight:400;margin-left:16px;vertical-align:middle;">{title_en}</span>
          </div>
          {body}
        </div>
        """

    def summary_para(summary):
        if not summary:
            return ""
        return f"""
        <p style="font-family:{SERIF};font-size:17px;color:{FG};line-height:1.9;margin:0 0 32px 0;font-weight:400;">
          {_esc(summary)}
        </p>
        """

    # --- I · Events (始终生成一句话概览；有 P0/P1 则在下方追加列表) ---
    def _first_clause(s):
        s = (s or "").strip()
        for sep in ["。", ". ", ".\n"]:
            if sep in s:
                return s.split(sep)[0]
        return s

    parts = []
    rel_items = data["releases"].get("items") or []
    if rel_items:
        names = "、".join(f"{r['org']}/{r['repo_name']}" for r in rel_items[:2])
        parts.append(f"本周 {len(rel_items)} 款新发布（{names}）")

    lb_first_sum = next((v.get("summary_md") for v in data["leaderboards"].values() if v.get("summary_md")), None)
    if lb_first_sum:
        parts.append(_first_clause(lb_first_sum))

    or_sum = data["openrouter"].get("summary_md")
    hf_sum = data["hf"].get("summary_md")
    if or_sum:
        parts.append(_first_clause(or_sum))
    elif hf_sum:
        parts.append(f"HF 趋势：{_first_clause(hf_sum)}")

    op_models = data["opinions"].get("models") or []
    theme_items = data["themes"].get("themes") or []
    if op_models:
        top_names = "、".join(m.get("model") or "" for m in op_models[:2])
        parts.append(f"讨论集中在 {top_names}")
    elif theme_items:
        parts.append(f"社区围绕 {theme_items[0].get('title') or ''}")

    digest_html = ""
    if parts:
        sentence = "；".join(parts) + "。"
        digest_html = f"""
        <p style="font-family:{SERIF};font-size:17px;color:{FG};line-height:1.9;margin:0 0 28px 0;">
          {_esc(sentence)}
        </p>
        """

    ev_rows = []
    for e in data["events"]:
        sev = e.get("severity") or "P2"
        src = e.get("source") or ""
        title = e.get("title") or ""
        link = _parse_link(e)
        link_html = f'&nbsp;<a href="{_esc(link)}" style="color:{ACCENT};text-decoration:none;">&raquo;</a>' if link else ""
        sev_html = f'<span style="font-family:{SANS};font-size:10px;color:{ACCENT};letter-spacing:0.15em;font-weight:700;">[{_esc(sev)}]</span>'
        ev_rows.append(f"""
        <div style="padding:14px 0;border-bottom:1px dotted {RULE};font-family:{SERIF};font-size:15px;color:{FG};line-height:1.7;">
          {sev_html} &nbsp;<em style="color:{FAINT};font-size:13px;">{_esc(src)}</em> &middot; {_esc(title)}{link_html}
        </div>
        """)

    if ev_rows:
        events_html = digest_html + "".join(ev_rows)
    elif digest_html:
        events_html = digest_html
    else:
        events_html = f'<div style="color:{FAINT};font-style:italic;font-family:{SERIF};">本周无数据。</div>'

    # --- II · Releases ---
    paras = []
    for r in rel_items:
        pub = (r.get("published_at") or "").split("T")[0]
        paper = r.get("paper_url") or ""
        paper_html = f' &middot; <a href="{_esc(paper)}" style="color:{ACCENT};">paper</a>' if paper else ""
        paras.append(f"""
        <p style="font-family:{SERIF};font-size:15px;color:{FG};line-height:1.8;margin:0 0 20px 0;">
          <strong>{_esc(r['org'])}/{_esc(r['repo_name'])}</strong>
          <span style="color:{FAINT};font-family:{SANS};font-size:12px;"> — {_esc(r.get('tag_name') or '')}, {_esc(pub)}</span>. {_esc(r.get('one_liner') or '')}
          &nbsp;<a href="{_esc(r.get('html_url') or '')}" style="color:{ACCENT};text-decoration:none;">release &raquo;</a>{paper_html}
        </p>
        """)
    releases_html = "".join(paras) if paras else f'<p style="color:{FAINT};font-style:italic;font-family:{SERIF};">本周无新发布。</p>'

    # --- III · Leaderboards ---
    lb_blocks = []
    for domain_key, d in data["leaderboards"].items():
        platforms_html = []
        for p in d["platforms"]:
            src = p["source"]
            src_label = {"lmarena": "LMArena", "aa": "Artificial Analysis", "superclue": "SuperCLUE"}.get(src, src)
            public_url = p.get("public_url") or ""
            items_p = p.get("top_n") or []
            rows_html = []
            for it in items_p[:20]:
                rank = it["rank"]
                change = it.get("change") or ""
                chg_html = f'<span style="font-family:{SANS};font-size:10px;color:{ACCENT};margin-left:6px;font-weight:600;">{_esc(change)}</span>' if change else ""
                score = it.get("score")
                score_html = f'<span style="font-family:{SANS};font-size:11px;color:{MUTED};">{score:.1f}</span>' if score is not None else ""
                rows_html.append(f"""
                <tr>
                  <td style="padding:6px 0;font-family:{SERIF};font-size:12px;color:{FAINT};width:24px;vertical-align:top;">{rank}.</td>
                  <td class="mr-name" style="padding:6px 0;font-family:{SERIF};font-size:14px;color:{FG};">{_esc(it['model_name'])}{chg_html}</td>
                  <td style="padding:6px 0;text-align:right;vertical-align:top;">{score_html}</td>
                </tr>
                """)
            label_html = (
                f'<a href="{_esc(public_url)}" style="color:{MUTED};text-decoration:none;border-bottom:1px solid {RULE};">{_esc(src_label)} &rarr;</a>'
                if public_url else _esc(src_label)
            )
            platforms_html.append(f"""
              <div style="font-family:{SERIF};font-style:italic;font-size:13px;color:{MUTED};margin-bottom:10px;letter-spacing:0.02em;">{label_html}</div>
              <table style="width:100%;border-collapse:collapse;">{''.join(rows_html)}</table>
            """)
        summary = d.get("summary_md", "")
        domain_summary = f"""
        <p style="font-family:{SERIF};font-size:16px;color:{FG};line-height:1.9;margin:0 0 24px 0;">
          {_esc(summary)}
        </p>
        """ if summary else ""
        n = max(len(platforms_html), 1)
        col_w = f"{100 // n}%"
        cells = []
        for i, p_html in enumerate(platforms_html):
            pad = "0 16px 0 0" if i == 0 else ("0 0 0 16px" if i == len(platforms_html) - 1 else "0 16px")
            cells.append(f'<td class="mr-lb-col" style="vertical-align:top;width:{col_w};padding:{pad};">{p_html}</td>')
        platforms_table = f'<table style="width:100%;border-collapse:collapse;table-layout:fixed;"><tr class="mr-lb-tr">{"".join(cells)}</tr></table>'
        lb_blocks.append(f"""
        <div style="margin-bottom:40px;">
          <div style="font-family:{SERIF};font-size:19px;color:{FG};font-weight:700;margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid {RULE};">
            {_esc(d['title'])}
          </div>
          {domain_summary}
          {platforms_table}
        </div>
        """)
    leaderboards_html = "".join(lb_blocks)

    # --- IV · HuggingFace ---
    hf = data["hf"]
    hf_top = hf.get("top") or []
    max_dls = max((it.get("downloads") or 0) for it in hf_top) if hf_top else 1
    hf_rows = []
    for it in hf_top:
        rank = it["rank"]
        change = it.get("change")
        chg_html = f'<span style="font-family:{SANS};font-size:10px;color:{ACCENT};margin-left:8px;font-weight:700;letter-spacing:0.08em;padding:1px 5px;border:1px solid {ACCENT};">NEW</span>' if change == "NEW" else ""
        likes = it.get("likes") or 0
        dls = it.get("downloads") or 0
        matched = it.get("matched_model") or ""
        matched_html = f'<span style="color:{FAINT};font-size:11px;font-style:italic;"> — {_esc(matched)}</span>' if matched else ""
        pct = max(3, int((dls / max_dls) * 100)) if max_dls else 0
        bar = f"""
        <div style="background:{RULE};height:4px;width:100%;">
          <div style="width:{pct}%;background:{FG};height:100%;"></div>
        </div>"""
        hf_rows.append(f"""
        <tr>
          <td style="padding:9px 0;font-family:{SERIF};font-size:12px;color:{FAINT};width:28px;vertical-align:top;">{rank}.</td>
          <td style="padding:9px 12px 9px 0;font-family:{SERIF};font-size:14px;color:{FG};vertical-align:middle;">
            <a class="mr-name" href="{_esc(it.get('hf_url') or '')}" style="color:{FG};text-decoration:none;">{_esc(it.get('model_id') or '')}</a>{chg_html}{matched_html}
          </td>
          <td class="mr-bar-cell" style="padding:9px 0;width:140px;vertical-align:middle;">{bar}</td>
          <td style="padding:9px 0 9px 12px;text-align:right;font-family:{SANS};font-size:11px;color:{MUTED};white-space:nowrap;vertical-align:middle;">
            {_fmt_dl(dls)}↓ &nbsp; {likes}♡
          </td>
        </tr>
        """)
    hf_summary = hf.get("summary_md") or ""
    hf_html = summary_para(hf_summary) + f'<table style="width:100%;border-collapse:collapse;">{"".join(hf_rows)}</table>'

    # --- V · OpenRouter ---
    or_data = data["openrouter"]
    or_top = or_data.get("top") or []
    max_tokens = max((it.get("total_tokens") or 0) for it in or_top) if or_top else 1
    or_rows = []
    for it in or_top:
        rank = it["rank"]
        chg = it.get("change_pct")
        is_new = it.get("is_new")
        if is_new:
            chg_html = f'<span style="font-family:{SANS};font-size:10px;color:{ACCENT};margin-left:8px;font-weight:700;letter-spacing:0.08em;padding:1px 5px;border:1px solid {ACCENT};">NEW</span>'
        elif chg is not None and chg >= 1.0:
            chg_html = f'<span style="font-family:{SANS};font-size:11px;color:{ACCENT};margin-left:8px;font-weight:700;">▲ {chg*100:.0f}%</span>'
        elif chg is not None and chg > 0:
            chg_html = f'<span style="font-family:{SANS};font-size:11px;color:{ACCENT};margin-left:8px;font-weight:600;">+{chg*100:.0f}%</span>'
        elif chg is not None and chg < 0:
            chg_html = f'<span style="font-family:{SANS};font-size:11px;color:{MUTED};margin-left:8px;">{chg*100:.0f}%</span>'
        else:
            chg_html = ""
        tokens = it.get("tokens_display") or "-"
        tokens_raw = it.get("total_tokens") or 0
        pct = max(3, int((tokens_raw / max_tokens) * 100)) if max_tokens else 0
        bar_color = FG if rank <= 3 else MUTED
        bar = f"""
        <div style="background:{RULE};height:6px;width:100%;">
          <div style="width:{pct}%;background:{bar_color};height:100%;"></div>
        </div>"""
        or_rows.append(f"""
        <tr>
          <td style="padding:11px 0;font-family:{SERIF};font-size:12px;color:{FAINT};width:28px;vertical-align:top;">{rank}.</td>
          <td style="padding:11px 12px 11px 0;font-family:{SERIF};font-size:14px;color:{FG};vertical-align:middle;">
            <a class="mr-name" href="{_esc(it.get('url') or '')}" style="color:{FG};text-decoration:none;font-weight:{'700' if rank <= 3 else '400'};">{_esc(it.get('name') or '')}</a>
            <span style="color:{FAINT};font-size:12px;font-style:italic;"> — {_esc(it.get('author') or '')}</span>{chg_html}
          </td>
          <td class="mr-bar-cell" style="padding:11px 0;width:180px;vertical-align:middle;">{bar}</td>
          <td style="padding:11px 0 11px 12px;text-align:right;font-family:{SANS};font-size:12px;color:{FG};font-weight:600;white-space:nowrap;vertical-align:middle;">
            {_esc(tokens)}
          </td>
        </tr>
        """)
    or_summary = or_data.get("summary_md") or ""
    top3_tokens = sum((it.get("total_tokens") or 0) for it in or_top[:3])
    total_tokens = sum((it.get("total_tokens") or 0) for it in or_top)
    top3_pct = int(top3_tokens / total_tokens * 100) if total_tokens else 0
    or_stat_card = f"""
    <div class="mr-stat-card" style="display:flex;gap:28px;padding:20px 24px;margin-bottom:26px;background:#ffffff;border:1px solid {RULE};">
      <div class="mr-stat-cell mr-stat-cell-first" style="flex:1;">
        <div style="font-family:{SANS};font-size:10px;color:{FAINT};letter-spacing:0.2em;text-transform:uppercase;margin-bottom:6px;">Top 3 Share</div>
        <div style="font-family:{SERIF};font-size:32px;color:{FG};font-weight:700;letter-spacing:-0.02em;">{top3_pct}<span style="font-size:18px;color:{MUTED};">%</span></div>
        <div style="font-family:{SERIF};font-style:italic;font-size:12px;color:{FAINT};margin-top:2px;">of top {len(or_top)} total</div>
      </div>
      <div class="mr-stat-cell" style="flex:1;border-left:1px solid {RULE};padding-left:24px;">
        <div style="font-family:{SANS};font-size:10px;color:{FAINT};letter-spacing:0.2em;text-transform:uppercase;margin-bottom:6px;">Rank 1</div>
        <div style="font-family:{SERIF};font-size:18px;color:{FG};font-weight:700;letter-spacing:-0.01em;">{_esc(or_top[0]['name']) if or_top else '-'}</div>
        <div style="font-family:{SANS};font-size:12px;color:{MUTED};margin-top:4px;font-weight:600;">{_esc(or_top[0]['tokens_display']) if or_top else ''} tokens</div>
      </div>
      <div class="mr-stat-cell" style="flex:1;border-left:1px solid {RULE};padding-left:24px;">
        <div style="font-family:{SANS};font-size:10px;color:{FAINT};letter-spacing:0.2em;text-transform:uppercase;margin-bottom:6px;">This Week</div>
        <div style="font-family:{SERIF};font-size:18px;color:{FG};font-weight:700;letter-spacing:-0.01em;">
          {sum(1 for it in or_top if it.get('is_new'))} <span style="font-size:13px;color:{MUTED};font-weight:400;">new</span> ·
          {sum(1 for it in or_top if it.get('change_pct') and it['change_pct'] >= 1.0)} <span style="font-size:13px;color:{MUTED};font-weight:400;">surged</span>
        </div>
        <div style="font-family:{SERIF};font-style:italic;font-size:12px;color:{FAINT};margin-top:4px;">surge = +100% WoW</div>
      </div>
    </div>
    """
    or_source_label = f"""
    <div style="font-family:{SERIF};font-style:italic;font-size:13px;color:{MUTED};margin-bottom:16px;letter-spacing:0.02em;">
      <a href="https://openrouter.ai/rankings" style="color:{MUTED};text-decoration:none;border-bottom:1px solid {RULE};">openrouter.ai/rankings &rarr;</a>
    </div>
    """
    openrouter_html = or_source_label + summary_para(or_summary) + or_stat_card + f'<table style="width:100%;border-collapse:collapse;">{"".join(or_rows)}</table>'

    # --- VI · Opinions ---
    models = data["opinions"].get("models") or []
    op_blocks = []
    for m in models:
        quotes = []
        for op in m.get("opinions") or []:
            quotes.append(f"""
            <blockquote style="margin:14px 0;padding:0 0 0 22px;border-left:3px solid {ACCENT};font-family:{SERIF};font-style:italic;font-size:15px;color:{FG};line-height:1.75;">
              &ldquo;{_esc(op.get('quote') or '')}&rdquo;
              <div style="margin-top:8px;font-family:{SANS};font-style:normal;font-size:11px;">
                <a href="{_esc(op.get('url') or '')}" style="color:{ACCENT};text-decoration:none;">source &raquo;</a>
              </div>
            </blockquote>
            """)
        op_blocks.append(f"""
        <div style="margin-bottom:28px;">
          <div style="font-family:{SERIF};font-size:17px;color:{FG};font-weight:700;">
            {_esc(m.get('model') or '')}
            <span style="font-family:{SANS};font-size:11px;color:{FAINT};font-weight:400;margin-left:10px;font-style:italic;">{m.get('post_count') or 0} posts</span>
          </div>
          {''.join(quotes)}
        </div>
        """)
    opinions_html = "".join(op_blocks) if op_blocks else f'<p style="font-family:{SERIF};font-style:italic;color:{FAINT};">{_esc(data["opinions"].get("fallback_md") or "")}</p>'

    # --- VII · Themes ---
    t_items = data["themes"].get("themes") or []
    t_blocks = []
    for t in t_items:
        posts = t.get("posts") or []
        links_html = " &middot; ".join(
            f'<a href="{_esc(p.get("url") or "")}" style="color:{FAINT};text-decoration:none;">r/{_esc(p.get("subreddit") or "")} {p.get("score", 0)}&uarr;</a>'
            for p in posts
        )
        posts_html = f'<div style="margin-top:10px;font-family:{SANS};font-size:11px;color:{FAINT};font-style:italic;">{links_html}</div>' if links_html else ""
        t_blocks.append(f"""
        <div style="margin-bottom:40px;">
          <div style="font-family:{SERIF};font-size:19px;color:{FG};font-weight:700;margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid {RULE};">
            {_esc(t.get('title') or '')}
          </div>
          <p style="font-family:{SERIF};font-size:16px;color:{FG};line-height:1.9;margin:0;">{_esc(t.get('summary') or '')}</p>
          {posts_html}
        </div>
        """)
    themes_html = "".join(t_blocks) if t_blocks else f'<p style="font-family:{SERIF};font-style:italic;color:{FAINT};">{_esc(data["themes"].get("fallback_md") or "")}</p>'

    # --- VIII · WeChat Themes ---
    w_items = (data.get("wechat") or {}).get("themes") or []
    w_blocks = []
    for t in w_items:
        articles = t.get("articles") or []
        art_rows = []
        for a in articles:
            angle = a.get("angle") or ""
            angle_html = f'<span style="color:{FG};">{_esc(angle)}</span>' if angle else ""
            art_rows.append(
                f'<div style="margin-top:8px;font-family:{SERIF};font-size:13px;color:{MUTED};line-height:1.7;">'
                f'<span style="font-family:{SANS};font-size:11px;color:{FAINT};letter-spacing:0.04em;text-transform:uppercase;">{_esc(a.get("source") or "")}</span> '
                f'&middot; <a href="{_esc(a.get("url") or "")}" style="color:{ACCENT};text-decoration:none;">{_esc((a.get("title") or "")[:80])}</a> '
                f'{"&mdash; " + angle_html if angle_html else ""}'
                f'</div>'
            )
        arts_html = "".join(art_rows)
        w_blocks.append(f"""
        <div style="margin-bottom:40px;">
          <div style="font-family:{SERIF};font-size:19px;color:{FG};font-weight:700;margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid {RULE};">
            {_esc(t.get('title') or '')}
          </div>
          <p style="font-family:{SERIF};font-size:16px;color:{FG};line-height:1.9;margin:0;">{_esc(t.get('summary') or '')}</p>
          {arts_html}
        </div>
        """)
    wechat_fallback = (data.get("wechat") or {}).get("fallback_md") or ""
    wechat_html = "".join(w_blocks) if w_blocks else f'<p style="font-family:{SERIF};font-style:italic;color:{FAINT};">{_esc(wechat_fallback)}</p>'

    footer = f"""
    <div class="mr-footer" style="padding:32px 56px 44px;border-top:3px double {FG};text-align:center;background:{BG};">
      <div style="font-family:{SERIF};font-size:11px;color:{FAINT};font-style:italic;letter-spacing:0.02em;">
        ModelRadar &middot; compiled {datetime.now().strftime('%B %d, %Y · %H:%M')}
      </div>
      <div style="font-family:{SANS};font-size:10px;color:{FAINT};letter-spacing:0.1em;text-transform:uppercase;margin-top:8px;">
        LMArena · Artificial Analysis · SuperCLUE · HuggingFace · OpenRouter · GitHub · Vendor Blogs · Reddit · WeChat
      </div>
    </div>
    """

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style type="text/css">
@media only screen and (max-width: 600px) {{
  body.mr-body {{ padding: 0 !important; }}
  .mr-container {{ max-width: 100% !important; box-shadow: none !important; }}
  .mr-masthead {{ padding: 32px 18px 24px !important; }}
  .mr-section {{ padding: 32px 18px 28px !important; }}
  .mr-footer {{ padding: 24px 18px 28px !important; }}
  .mr-lb-tr, .mr-lb-col {{ display: block !important; width: 100% !important; }}
  .mr-lb-col {{ padding: 0 0 28px 0 !important; }}
  .mr-stat-card {{ display: block !important; padding: 14px 16px !important; }}
  .mr-stat-cell {{ display: block !important; border-left: none !important; padding: 14px 0 0 0 !important; margin-top: 14px !important; border-top: 1px solid #d4cfc3 !important; }}
  .mr-stat-cell-first {{ border-top: none !important; margin-top: 0 !important; padding-top: 0 !important; }}
  .mr-bar-cell {{ display: none !important; }}
  .mr-name {{ word-break: break-all !important; overflow-wrap: anywhere !important; }}
}}
</style>
</head>
<body class="mr-body" style="margin:0;padding:32px 20px;background:#eeeae0;font-family:{SANS};">
<div class="mr-container" style="max-width:800px;margin:0 auto;background:{BG};box-shadow:0 1px 3px rgba(0,0,0,0.05);">
  {masthead}
  {sec(1, "ALERTS", "本周关键信号", events_html)}
  {sec(2, "NEW RELEASES", "新模型 / 开源发布", releases_html)}
  {sec(3, "LEADERBOARD SHIFTS", "榜单变化", leaderboards_html)}
  {sec(4, "HUGGINGFACE TRENDING", "HuggingFace 趋势", hf_html)}
  {sec(5, "OPENROUTER THROUGHPUT", "OpenRouter 真实调用量", openrouter_html)}
  {sec(6, "COMMUNITY VOICES", "社区声音", opinions_html)}
  {sec(7, "WHAT PEOPLE ARE DISCUSSING", "本周社区热议", themes_html)}
  {sec(8, "WECHAT PULSE", "中文社区观察", wechat_html)}
  {footer}
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
        "wechat":         {"theme_count":   len((data.get("wechat") or {}).get("themes") or []),
                           "article_count": (data.get("wechat") or {}).get("article_count", 0),
                           "used_llm":      (data.get("wechat") or {}).get("used_llm", False)},
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
    # 周报前强制抓一次 OR：collector 7d 间隔可能和周一不对齐，防止周报读到一周前的快照。
    # collect() 内部是 DELETE+INSERT，重复调用幂等；失败时 digest 仍能读 DB 里的旧数据兜底。
    _safe_call("openrouter_refresh", openrouter_collector.collect)
    openrouter        = _safe_call("openrouter",   openrouter_digest.generate) or {"top": [], "week_date": None, "any_previous": False, "summary_md": "", "used_llm": False}
    releases          = _safe_call("releases",     release_digest.generate, days=days) or {"items": [], "used_llm": False, "kept_count": 0, "noise_count": 0, "dedup_count": 0}
    opinions          = _safe_call("opinions",     reddit_opinions.generate, days=days) or {"models": [], "fallback_md": "(opinions 模块异常)"}
    # alias 自愈：先扫未匹配高分帖把新模型名自动加进 learned_aliases，
    # 再跑 opinions（虽然本轮已跑过，但下一次 Reddit 采集会受益）和 themes。
    # themes 过程里 LLM 识别的模型名也会走自动接受。
    alias_stats       = _safe_call("alias_learner", alias_learner.learn_from_reddit, days=days) or {}
    themes            = _safe_call("themes",        reddit_themes.generate, days=days) or {"themes": [], "post_count": 0, "used_llm": False, "fallback_md": "(themes 模块异常)"}
    wechat            = _safe_call("wechat_themes", wechat_themes.generate, days=days) or {"themes": [], "article_count": 0, "used_llm": False, "fallback_md": "(wechat_themes 模块异常)"}

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
        "wechat":        wechat,
        "alias_stats":   alias_stats,
    }


def generate_and_send(days: int = 7, dry_run: bool = False) -> dict:
    """完整流程：拼数据 → 渲染 HTML → 入库 → 发邮件。"""
    try:
        data = generate(days=days)
        html = _render_html(data)
        _persist(data, html)

        # 周报跑完把 opinions / themes 也写进 digest_cache，保证周一清晨 Dashboard 聚合板块立刻有内容。
        try:
            from backend.engine import mini_digest
            if data.get("opinions"):
                mini_digest._write_cache("opinions", days, data["opinions"])
            if data.get("themes"):
                mini_digest._write_cache("themes", days, data["themes"])
        except Exception as cache_err:
            logger.warning("[Weekly] digest_cache 双写失败（不影响周报发送）: %s", cache_err)

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
