"""榜单采集适配层：调 scrapers → 写 SQLite leaderboard_snapshots。"""
import json
import logging
from datetime import datetime, timezone

from backend.db import get_conn, record_status
from backend.collectors import leaderboard_scrapers as scrapers

logger = logging.getLogger(__name__)


def _extract_score(row: dict) -> float | None:
    """从不同源的行里提取统一的 score。"""
    if "elo" in row and row["elo"] is not None:
        try:
            return float(row["elo"])
        except (TypeError, ValueError):
            pass
    if "score" in row and row["score"]:
        try:
            return float(str(row["score"]).split()[0])
        except (TypeError, ValueError):
            pass
    if "median" in row and row["median"] is not None:
        try:
            return float(row["median"])
        except (TypeError, ValueError):
            pass
    return None


def _persist(source: str, category: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    # 用**同一个** scraped_at 覆盖整批写入。
    # 过去默认的 CURRENT_TIMESTAMP 会对每行重新取系统秒 —— 当 ~300 行跨秒边界
    # 时，同一批 scrape 会被拆成两个 DISTINCT scraped_at；diff_engine 把后半截
    # 当成独立"上次快照"，导致 Top 10 全部被误判为首次上榜。
    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        for row in rows:
            model = row.get("model") or row.get("model_name") or ""
            if not model:
                continue
            rank = row.get("rank")
            try:
                rank_int = int(rank) if rank is not None else None
            except (TypeError, ValueError):
                rank_int = None
            score = _extract_score(row)
            conn.execute(
                """
                INSERT INTO leaderboard_snapshots(source, category, model_name, rank, score, extra_json, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (source, category, str(model), rank_int, score,
                 json.dumps(row, ensure_ascii=False, default=str), scraped_at),
            )
    return len(rows)


def collect() -> dict:
    """一次完整的榜单采集。成功时返回各源入库数量。"""
    summary = {}
    try:
        data = scrapers.scrape_all()
        for source, tracks in data.items():
            total = 0
            for category, rows in tracks.items():
                total += _persist(source, category, rows)
            summary[source] = total
        logger.info("榜单采集完成: %s", summary)
        record_status("leaderboard", success=True)
        return summary
    except Exception as e:
        logger.exception("榜单采集失败: %s", e)
        record_status("leaderboard", success=False, error=str(e))
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(collect())
