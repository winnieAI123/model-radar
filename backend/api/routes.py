"""Dashboard API 路由。所有接口返回 JSON，前端纯 fetch。"""
import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.auth import require_auth
from backend.db import get_conn

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


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
