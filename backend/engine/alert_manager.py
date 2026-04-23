"""告警管理：查 change_events 表里未告警的 P0 事件，拼邮件发送，成功后打 alerted 标记。

关键：严格去重。每个事件最多发一次邮件。即使 cron 每 30min 跑一次，也不会重复骚扰。
"""
import json
import logging
from datetime import datetime

from backend.db import get_conn, record_status
from backend.utils.email_sender import send_email

logger = logging.getLogger(__name__)

MAX_P0_PER_RUN = 10  # 单次批量发送上限，避免异常情况下一下发几十封


def _render_html(events: list[dict]) -> tuple[str, str]:
    """返回 (subject, html_body)。events 都是 sqlite3.Row 或 dict。"""
    n = len(events)
    if n == 1:
        subject = f"🔴 ModelRadar · {events[0]['title'][:60]}"
    else:
        subject = f"🔴 ModelRadar · {n} 条 P0 信号"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    items_html = []
    for e in events:
        detail = {}
        try:
            detail = json.loads(e["detail_json"] or "{}")
        except Exception:
            pass
        link = detail.get("url") or detail.get("html_url") or ""
        link_html = (
            f'<div style="margin-top:6px;"><a href="{link}" '
            f'style="color:#1a7fd4;text-decoration:none;">🔗 {link}</a></div>'
            if link else ""
        )
        items_html.append(f"""
        <tr><td style="padding:14px 18px;border-bottom:1px solid #e8e8ed;">
          <div style="font-size:14px;color:#666;">
            [{e['event_type']}] · {e['source']}
          </div>
          <div style="font-size:16px;font-weight:600;color:#111;margin-top:4px;">
            {e['title']}
          </div>
          {link_html}
        </td></tr>
        """)

    body = f"""\
<!doctype html>
<html><body style="margin:0;padding:24px;background:#f5f5f7;font-family:-apple-system,Segoe UI,sans-serif;">
<div style="max-width:640px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;">
  <div style="padding:20px 24px;background:#b91c1c;color:white;">
    <div style="font-size:18px;font-weight:600;">ModelRadar 紧急通知</div>
    <div style="font-size:13px;opacity:0.85;margin-top:4px;">{n} 条 P0 事件 · {now}</div>
  </div>
  <table style="width:100%;border-collapse:collapse;">
    {''.join(items_html)}
  </table>
  <div style="padding:18px 24px;background:#fafafa;color:#999;font-size:11px;text-align:center;">
    ModelRadar 自动推送 · 仅供内部团队参考
  </div>
</div>
</body></html>"""
    return subject, body


def send_p0_alerts() -> dict:
    """发送未发过的 P0 告警。返回 {fetched, sent, marked}。"""
    result = {"fetched": 0, "sent": 0, "marked": 0}
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, event_type, severity, source, title, detail_json, model_name, created_at
                FROM change_events
                WHERE severity='P0' AND alerted=0
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (MAX_P0_PER_RUN,),
            ).fetchall()
            events = [dict(r) for r in rows]

        result["fetched"] = len(events)
        if not events:
            record_status("alert_p0", success=True)
            return result

        # Bootstrap 过滤：首次扫描某 org 时，全部存量仓库会被判成 new_repo P0（冷启动误报）。
        # 借鉴 weekly_report.py:79-101 —— 与该 org 的 first_scan 时间相差 <5min 的 new_repo/new_release 直接标 bootstrap_skipped。
        with get_conn() as conn:
            scan_rows = conn.execute(
                "SELECT org, MIN(scraped_at) AS first_at FROM github_snapshots GROUP BY org"
            ).fetchall()
        first_scan = {r["org"]: r["first_at"] for r in scan_rows}

        real_events: list[dict] = []
        bootstrap_ids: list[int] = []
        for e in events:
            if e["event_type"] in ("new_repo", "new_release"):
                try:
                    org = (json.loads(e.get("detail_json") or "{}") or {}).get("org")
                except Exception:
                    org = None
                first_at = first_scan.get(org)
                if first_at:
                    try:
                        t_event = datetime.fromisoformat(e["created_at"])
                        t_first = datetime.fromisoformat(first_at)
                        if abs((t_event - t_first).total_seconds()) < 300:
                            bootstrap_ids.append(e["id"])
                            continue
                    except Exception:
                        pass
            real_events.append(e)

        if bootstrap_ids:
            with get_conn() as conn:
                placeholders = ",".join("?" for _ in bootstrap_ids)
                conn.execute(
                    f"UPDATE change_events SET alerted=1, alerted_at=CURRENT_TIMESTAMP, "
                    f"alert_status='bootstrap_skipped' WHERE id IN ({placeholders})",
                    bootstrap_ids,
                )
            logger.info("[AlertManager] 跳过 %d 条 bootstrap 误报（已标已处理）", len(bootstrap_ids))

        if not real_events:
            record_status("alert_p0", success=True)
            return result

        events = real_events
        subject, body = _render_html(events)
        ok = send_email(subject=subject, html_body=body)
        if not ok:
            record_status("alert_p0", success=False, error="email send failed")
            return result

        result["sent"] = len(events)
        with get_conn() as conn:
            ids = [e["id"] for e in events]
            placeholders = ",".join("?" for _ in ids)
            cur = conn.execute(
                f"UPDATE change_events SET alerted=1, alerted_at=CURRENT_TIMESTAMP, "
                f"alert_status='sent' WHERE id IN ({placeholders})",
                ids,
            )
            result["marked"] = cur.rowcount

        logger.info("P0 alert 发送 %d 条, 标记 %d 条", result["sent"], result["marked"])
        record_status("alert_p0", success=True)
        return result
    except Exception as e:
        logger.exception("P0 alert 失败: %s", e)
        record_status("alert_p0", success=False, error=str(e))
        raise


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(send_p0_alerts())
