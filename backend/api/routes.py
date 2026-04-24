"""Dashboard API 路由。所有接口返回 JSON，前端纯 fetch。"""
import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.auth import require_auth
from backend.db import get_conn

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


# Dashboard 榜单：5 个 tab × N 个 source。每个 (source, category) 独立读最新两次快照算 Δ。
# 主 4 tab 覆盖 LLM / 文生图 / 文生视频 / 图生视频。"更多" tab 收 SuperCLUE 独有中文特色榜。
_SRC_LABEL = {"lmarena": "LMArena", "aa": "AA", "superclue": "SuperCLUE"}
_SRC_HOME = {
    "lmarena":   "https://lmarena.ai/leaderboard",
    "aa":        "https://artificialanalysis.ai/leaderboards/models",
    "superclue": "https://www.superclueai.com/",
}

_LB_TABS: dict[str, dict] = {
    "llm": {
        "label": "LLM",
        "sources": [
            {"source": "lmarena", "category": "text", "label": "LMArena"},
        ],
    },
    "t2i": {
        "label": "文生图",
        "sources": [
            {"source": "lmarena",   "category": "text_to_image", "label": "LMArena"},
            {"source": "aa",        "category": "text_to_image", "label": "AA"},
            {"source": "superclue", "category": "text_to_image", "label": "SuperCLUE"},
        ],
    },
    "t2v": {
        "label": "文生视频",
        "sources": [
            {"source": "lmarena",   "category": "text_to_video", "label": "LMArena"},
            {"source": "aa",        "category": "text_to_video", "label": "AA"},
            {"source": "superclue", "category": "text_to_video", "label": "SuperCLUE"},
        ],
    },
    "i2v": {
        "label": "图生视频",
        "sources": [
            {"source": "lmarena",   "category": "image_to_video", "label": "LMArena"},
            {"source": "aa",        "category": "image_to_video", "label": "AA"},
            {"source": "superclue", "category": "image_to_video", "label": "SuperCLUE"},
        ],
    },
    "extras": {
        "label": "更多",
        "sources": [
            {"source": "superclue", "category": "image_edit",    "label": "图像编辑"},
            {"source": "superclue", "category": "ref_to_video",  "label": "参考视频"},
            {"source": "superclue", "category": "text_to_speech","label": "文生语音"},
        ],
    },
}


def _parse_detail(row) -> dict:
    try:
        return json.loads(row["detail_json"] or "{}")
    except Exception:
        return {}


@router.get("/alerts")
def list_alerts(
    limit: int = Query(30, ge=1, le=200),
    severity: str | None = Query(None, description="P0/P1/P2，留空返回全部"),
    alerted: int | None = Query(None, ge=0, le=1),
):
    """最新变动事件。默认按时间倒序取 30 条。"""
    sql = "SELECT id, event_type, severity, source, title, detail_json, model_name, alerted, alert_status, alerted_at, created_at FROM change_events WHERE 1=1"
    params: list = []
    if severity:
        sql += " AND severity=?"; params.append(severity)
    if alerted is not None:
        sql += " AND alerted=?"; params.append(alerted)
    sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r["id"],
            "event_type": r["event_type"],
            "severity": r["severity"],
            "source": r["source"],
            "title": r["title"],
            "model_name": r["model_name"],
            "detail": _parse_detail(r),
            "alerted": bool(r["alerted"]),
            "alert_status": r["alert_status"] or ("pending" if not r["alerted"] else "suppressed"),
            "alerted_at": r["alerted_at"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@router.get("/heat")
def heat_top(limit: int = Query(20, ge=1, le=100)):
    """今天热度 Top N。如果今天还没算，回退到最近一天。"""
    with get_conn() as conn:
        latest_date = conn.execute(
            "SELECT MAX(date) AS d FROM heat_scores"
        ).fetchone()["d"]
        if not latest_date:
            return {"date": None, "items": []}
        rows = conn.execute(
            "SELECT model_name, score, dims_json FROM heat_scores "
            "WHERE date=? ORDER BY score DESC LIMIT ?",
            (latest_date, limit),
        ).fetchall()
    items = []
    for r in rows:
        try:
            dims = json.loads(r["dims_json"] or "{}")
        except Exception:
            dims = {}
        items.append({
            "model_name": r["model_name"],
            "score": r["score"],
            "dims": dims,
        })
    return {"date": latest_date, "items": items}


@router.get("/timeline")
def timeline(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    event_type: str | None = Query(None),
):
    """时间线：最近所有事件，不筛 severity。支持 offset 翻页。"""
    sql = "SELECT id, event_type, severity, source, title, model_name, created_at FROM change_events"
    params: list = []
    if event_type:
        sql += " WHERE event_type=?"; params.append(event_type)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM change_events" + (" WHERE event_type=?" if event_type else ""),
            [event_type] if event_type else [],
        ).fetchone()[0]
    return {"total": total, "items": [dict(r) for r in rows]}


@router.get("/status")
def status():
    """系统健康：每个 collector 的最后运行时间和失败计数。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT collector, last_run_at, last_success_at, last_error, consecutive_fails "
            "FROM system_status ORDER BY collector"
        ).fetchall()
        counts = {}
        for t, sql in [
            ("leaderboard_rows", "SELECT COUNT(*) FROM leaderboard_snapshots"),
            ("github_repos",     "SELECT COUNT(DISTINCT org || '/' || repo_name) FROM github_snapshots"),
            ("github_releases",  "SELECT COUNT(*) FROM github_releases"),
            ("change_events",    "SELECT COUNT(*) FROM change_events"),
            ("pending_alerts",   "SELECT COUNT(*) FROM change_events WHERE alerted=0 AND severity='P0'"),
        ]:
            counts[t] = conn.execute(sql).fetchone()[0]
    return {
        "collectors": [dict(r) for r in rows],
        "counts": counts,
    }


@router.get("/events/{event_id}")
def event_detail(event_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM change_events WHERE id=?",
            (event_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "event not found")
    d = dict(row)
    d["detail"] = _parse_detail(row)
    return d


@router.get("/pending-mapping")
def pending_mapping(limit: int = Query(50, ge=1, le=500)):
    """未归一化的模型名，用于人工补 ALIAS_TABLE。"""
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT raw_name, source, seen_count, last_seen_at "
                "FROM _pending_mapping ORDER BY seen_count DESC, last_seen_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except Exception:
            return []
    return [dict(r) for r in rows]


@router.get("/weekly-reports")
def list_weekly_reports(limit: int = Query(20, ge=1, le=100)):
    """周报归档列表。前端用来做"历史周报"入口。"""
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT week_number, period_start, period_end, stats_json, sent_at, created_at "
                "FROM weekly_reports ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except Exception:
            return []
    out = []
    for r in rows:
        try:
            stats = json.loads(r["stats_json"] or "{}")
        except Exception:
            stats = {}
        out.append({
            "week_number":  r["week_number"],
            "period_start": r["period_start"],
            "period_end":   r["period_end"],
            "sent_at":      r["sent_at"],
            "created_at":   r["created_at"],
            "stats":        stats,
        })
    return out


@router.get("/weekly-reports/{week}")
def get_weekly_report(week: str):
    """返回某一周的完整 HTML（前端可 iframe 嵌入）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT week_number, html, sent_at, created_at FROM weekly_reports WHERE week_number=?",
            (week,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "weekly report not found")
    return {
        "week_number": row["week_number"],
        "sent_at":     row["sent_at"],
        "created_at":  row["created_at"],
        "html":        row["html"],
    }


# ──────────────────────────────────────────────────────────────────────────
# Dashboard v3 — 6 panels 聚合端点
# ──────────────────────────────────────────────────────────────────────────

def _last_success(conn, collector: str) -> str | None:
    row = conn.execute(
        "SELECT last_success_at FROM system_status WHERE collector=?",
        (collector,),
    ).fetchone()
    return row["last_success_at"] if row else None


def _panel_alerts(conn) -> dict:
    pending = conn.execute(
        "SELECT COUNT(*) FROM change_events WHERE alerted=0 AND severity IN ('P0','P1')"
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT id, event_type, severity, source, title, model_name, detail_json, alerted, created_at "
        "FROM change_events WHERE alerted=0 AND severity IN ('P0','P1') "
        "ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "event_type": r["event_type"],
            "severity": r["severity"],
            "source": r["source"],
            "title": r["title"],
            "model_name": r["model_name"],
            "detail": _parse_detail(r),
            "created_at": r["created_at"],
        })
    return {"pending_count": pending, "recent": items}


def _panel_releases(conn) -> dict:
    """新模型 / 开源发布 — change_events 中 new_release/new_repo/new_blog_post，取最近 48h。"""
    rows = conn.execute(
        """
        SELECT id, event_type, severity, source, title, model_name, detail_json, created_at
        FROM change_events
        WHERE event_type IN ('new_release', 'new_repo', 'new_blog_post', 'star_surge')
          AND created_at >= datetime('now', '-48 hours')
        ORDER BY created_at DESC
        LIMIT 20
        """
    ).fetchall()
    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "event_type": r["event_type"],
            "severity": r["severity"],
            "source": r["source"],
            "title": r["title"],
            "model_name": r["model_name"],
            "detail": _parse_detail(r),
            "created_at": r["created_at"],
        })
    # updated_at 取 github/blog/wechat 中最晚的那次成功
    updated = max(
        (t for t in [
            _last_success(conn, "github"),
            _last_success(conn, "blog_rss"),
            _last_success(conn, "wechat_rss"),
        ] if t),
        default=None,
    )
    return {"updated_at": updated, "items": items}


def _gather_source_items(conn, source: str, category: str, limit: int = 10) -> dict:
    """单个 (source, category) 的最新 Top N + 与上次对比的 Δ。"""
    timeline = conn.execute(
        "SELECT DISTINCT scraped_at FROM leaderboard_snapshots "
        "WHERE source=? AND category=? ORDER BY scraped_at DESC LIMIT 2",
        (source, category),
    ).fetchall()
    if not timeline:
        return {"scraped_at": None, "prev_scraped_at": None, "items": []}
    latest_ts = timeline[0]["scraped_at"]
    prev_ts = timeline[1]["scraped_at"] if len(timeline) > 1 else None
    latest_rows = conn.execute(
        "SELECT rank, model_name, score, extra_json FROM leaderboard_snapshots "
        "WHERE source=? AND category=? AND scraped_at=? "
        "ORDER BY rank LIMIT ?",
        (source, category, latest_ts, limit),
    ).fetchall()
    prev_rank: dict = {}
    if prev_ts:
        for r in conn.execute(
            "SELECT rank, model_name FROM leaderboard_snapshots "
            "WHERE source=? AND category=? AND scraped_at=?",
            (source, category, prev_ts),
        ).fetchall():
            prev_rank[r["model_name"]] = r["rank"]
    items = []
    for r in latest_rows:
        name = r["model_name"]
        prev = prev_rank.get(name)
        if prev is None:
            delta = "new"
        elif prev == r["rank"]:
            delta = 0
        else:
            delta = prev - r["rank"]  # 正=上升，负=下降
        # lmarena LLM 类目 extra_json 里带 price_per_1m_tokens / context_length，按原样透传给前端
        # 另外 lmarena 把 "1504±9" 这种分数存在 extra.score 里（主 score 列是 None），做 fallback
        extra = {}
        if r["extra_json"]:
            try:
                extra = json.loads(r["extra_json"]) or {}
            except Exception:
                extra = {}
        score = r["score"] if r["score"] is not None else extra.get("score")
        items.append({
            "rank": r["rank"],
            "model_name": name,
            "score": score,
            "prev_rank": prev,
            "delta": delta,
            "price_per_1m_tokens": extra.get("price_per_1m_tokens"),
            "context_length": extra.get("context_length"),
        })
    return {"scraped_at": latest_ts, "prev_scraped_at": prev_ts, "items": items}


def _panel_leaderboards(conn) -> dict:
    """5 个 tab，每个 tab 下 N 个 source 各取 Top 10 + Δ。"""
    categories: dict = {}
    for tab_key, tab_cfg in _LB_TABS.items():
        sources_out = []
        for s in tab_cfg["sources"]:
            payload = _gather_source_items(conn, s["source"], s["category"], limit=10)
            sources_out.append({
                "source":   s["source"],
                "category": s["category"],
                "label":    s["label"],
                "url":      _SRC_HOME.get(s["source"]),
                "scraped_at":      payload["scraped_at"],
                "prev_scraped_at": payload["prev_scraped_at"],
                "items":    payload["items"],
            })
        categories[tab_key] = {
            "label":   tab_cfg["label"],
            "sources": sources_out,
        }
    return {
        "updated_at": _last_success(conn, "leaderboard"),
        "categories": categories,
    }


def _panel_hf(conn) -> dict:
    """HuggingFace Trending Top 8 + Downloads Top 8。"""
    out = {}
    for list_type in ("trending", "downloads"):
        latest_ts_row = conn.execute(
            "SELECT MAX(scraped_at) FROM hf_snapshots WHERE list_type=?",
            (list_type,),
        ).fetchone()
        latest_ts = latest_ts_row[0] if latest_ts_row else None
        if not latest_ts:
            out[list_type] = []
            continue
        rows = conn.execute(
            "SELECT rank, model_id, author, downloads, likes, pipeline_tag, matched_model, last_modified "
            "FROM hf_snapshots WHERE list_type=? AND scraped_at=? ORDER BY rank LIMIT 8",
            (list_type, latest_ts),
        ).fetchall()
        out[list_type] = [dict(r) for r in rows]
    return {
        "updated_at": _last_success(conn, "huggingface"),
        "trending": out.get("trending", []),
        "downloads": out.get("downloads", []),
    }


def _panel_openrouter(conn) -> dict:
    """OpenRouter 最新一周 Top 10（表无 UNIQUE 约束，冷启动每次都会 INSERT 一批同数据，必须筛出最近一次 scrape）。"""
    latest = conn.execute(
        "SELECT MAX(week_date) FROM openrouter_rankings"
    ).fetchone()[0]
    if not latest:
        return {"updated_at": _last_success(conn, "openrouter"), "week_date": None, "items": []}
    last_scrape = conn.execute(
        "SELECT MAX(scraped_at) FROM openrouter_rankings WHERE week_date=?",
        (latest,),
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT rank, model_permaslug, author, total_tokens, request_count, change_pct, "
        "       matched_model, display_name "
        "FROM openrouter_rankings WHERE week_date=? AND scraped_at=? ORDER BY rank LIMIT 10",
        (latest, last_scrape),
    ).fetchall()
    return {
        "updated_at": _last_success(conn, "openrouter"),
        "week_date": latest,
        "items": [dict(r) for r in rows],
    }


def _panel_digest(conn, kind: str, window_days: int = 7) -> dict:
    row = conn.execute(
        "SELECT payload_json, generated_at FROM digest_cache WHERE kind=? AND window_days=?",
        (kind, window_days),
    ).fetchone()
    if not row:
        return {"generated_at": None, "window_days": window_days, "payload": None}
    try:
        payload = json.loads(row["payload_json"])
    except Exception:
        payload = None
    return {"generated_at": row["generated_at"], "window_days": window_days, "payload": payload}


@router.get("/dashboard")
def dashboard():
    """一次返回 6 个板块 + 告警条的完整 payload，前端单请求秒开。"""
    with get_conn() as conn:
        return {
            "alerts":       _panel_alerts(conn),
            "releases":     _panel_releases(conn),
            "leaderboards": _panel_leaderboards(conn),
            "hf":           _panel_hf(conn),
            "openrouter":   _panel_openrouter(conn),
            "opinions":     _panel_digest(conn, "opinions"),
            "themes":       _panel_digest(conn, "themes"),
        }


@router.post("/alerts/{event_id}/ack")
def ack_alert(event_id: int):
    """标记一条告警为已读。Dashboard 顶栏红点点掉。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM change_events WHERE id=?", (event_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "event not found")
        conn.execute(
            "UPDATE change_events SET alerted=1, alerted_at=datetime('now'), alert_status='acked' WHERE id=?",
            (event_id,),
        )
    return {"ok": True, "id": event_id}
