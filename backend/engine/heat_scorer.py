"""热度评分（简化版 · Phase 2）。

两维打分 → 0-100 归一：
  dim_rank    : 跨榜单综合排名分。越靠前越高，多源加权。
  dim_star    : GitHub 24h star 增量分。对数归一。

Phase 4 再扩到 5 维（加 HF 下载、Reddit 热度、Blog 覆盖）。

每日写入一条 heat_scores 行（model_name, date, score, dims_json）。
"""
import json
import logging
import math
from datetime import date, datetime, timezone

from backend.db import get_conn, record_status
from backend.utils.model_alias import normalize_or_record, ensure_pending_table

logger = logging.getLogger(__name__)


HEAT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS heat_scores (
    model_name  TEXT NOT NULL,
    date        TEXT NOT NULL,
    score       REAL NOT NULL,
    dims_json   TEXT,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (model_name, date)
);
CREATE INDEX IF NOT EXISTS idx_heat_date ON heat_scores(date, score DESC);
"""

# 权重（Phase 4 扩到 5 维时重调）
W_RANK = 0.65
W_STAR = 0.35


def ensure_tables() -> None:
    ensure_pending_table()
    with get_conn() as conn:
        conn.executescript(HEAT_TABLE_SQL)


def _rank_scores() -> dict[str, float]:
    """扫最新一轮榜单快照。对每个 (source, category) 里 rank<=20 的模型，
    给一个 rank_score = max(0, 21 - rank)，然后跨榜单求和 → 归一到 0-50。"""
    raw: dict[str, float] = {}
    with get_conn() as conn:
        pairs = conn.execute(
            "SELECT DISTINCT source, category FROM leaderboard_snapshots"
        ).fetchall()
        for p in pairs:
            latest_time = conn.execute(
                "SELECT MAX(scraped_at) AS t FROM leaderboard_snapshots "
                "WHERE source=? AND category=?",
                (p["source"], p["category"]),
            ).fetchone()["t"]
            if not latest_time:
                continue
            rows = conn.execute(
                "SELECT model_name, rank FROM leaderboard_snapshots "
                "WHERE source=? AND category=? AND scraped_at=? AND rank IS NOT NULL "
                "AND rank <= 20",
                (p["source"], p["category"], latest_time),
            ).fetchall()
            for r in rows:
                canon = normalize_or_record(r["model_name"], f"leaderboard:{p['source']}")
                raw[canon] = raw.get(canon, 0.0) + max(0, 21 - r["rank"])

    if not raw:
        return {}
    top = max(raw.values()) or 1.0
    return {m: round(50 * v / top, 2) for m, v in raw.items()}


def _star_surge_scores() -> dict[str, float]:
    """对每个 repo 计算最近 24h star 增量，log 归一后映射到 0-50。
    模型名按 repo_name 走归一化。"""
    raw: dict[str, float] = {}
    with get_conn() as conn:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT org, repo_name, stars,
                       ROW_NUMBER() OVER (PARTITION BY org, repo_name ORDER BY scraped_at DESC) AS rn
                FROM github_snapshots
            ),
            old AS (
                SELECT org, repo_name, stars,
                       ROW_NUMBER() OVER (PARTITION BY org, repo_name
                                          ORDER BY ABS(strftime('%s', scraped_at)
                                                       - strftime('%s', datetime('now', '-24 hours'))) ASC) AS rn
                FROM github_snapshots
                WHERE scraped_at <= datetime('now', '-20 hours')
            )
            SELECT l.org, l.repo_name, (l.stars - o.stars) AS delta
            FROM latest l
            LEFT JOIN old o ON l.org = o.org AND l.repo_name = o.repo_name
            WHERE l.rn = 1 AND (o.rn IS NULL OR o.rn = 1)
            """
        ).fetchall()
        for r in rows:
            delta = r["delta"]
            if delta is None or delta <= 0:
                continue
            canon = normalize_or_record(f"{r['org']}/{r['repo_name']}", "github")
            raw[canon] = max(raw.get(canon, 0.0), float(delta))

    if not raw:
        return {}
    # log 归一：log(1+delta) / log(1+max) * 50
    max_delta = max(raw.values()) or 1.0
    denom = math.log(1 + max_delta) or 1.0
    return {m: round(50 * math.log(1 + v) / denom, 2) for m, v in raw.items()}


def run() -> dict:
    """计算当日热度，写入 heat_scores。返回 {written, top_model}。"""
    ensure_tables()
    try:
        today = date.today().isoformat()
        rank = _rank_scores()
        star = _star_surge_scores()

        all_models = set(rank) | set(star)
        written = 0
        top = ("", 0.0)

        with get_conn() as conn:
            for m in all_models:
                dr = rank.get(m, 0.0)
                ds = star.get(m, 0.0)
                score = round(W_RANK * dr + W_STAR * ds, 2)
                dims = {"rank": dr, "star24h": ds}
                conn.execute(
                    """
                    INSERT INTO heat_scores(model_name, date, score, dims_json)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(model_name, date) DO UPDATE SET
                        score = excluded.score,
                        dims_json = excluded.dims_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (m, today, score, json.dumps(dims)),
                )
                written += 1
                if score > top[1]:
                    top = (m, score)

        logger.info("Heat scorer: %d models, top=%s(%.1f)", written, top[0], top[1])
        record_status("heat_scorer", success=True)
        return {"written": written, "top_model": top[0], "top_score": top[1]}
    except Exception as e:
        logger.exception("Heat scorer 失败: %s", e)
        record_status("heat_scorer", success=False, error=str(e))
        raise


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(run())
