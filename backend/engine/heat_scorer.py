"""热度评分（Phase 3 · 三维）。

三维打分 → 0-100 归一：
  dim_rank    : 跨榜单综合排名分。越靠前越高，多源加权。
  dim_star    : GitHub 24h star 增量分。对数归一。
  dim_hf      : HuggingFace 综合热度。trending/downloads 榜单位次 + 24h 下载增量。

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

# 权重（三维合成，rank 仍主导；star / hf 各占 1/4）
W_RANK = 0.50
W_STAR = 0.25
W_HF = 0.25

# HF 内部子权重：rank 子信号（trending+downloads 榜位次）占 0.6，24h 下载增量占 0.4
W_HF_RANK = 0.6
W_HF_DOWNLOADS = 0.4


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


def _hf_scores() -> dict[str, float]:
    """HF 综合热度。matched_model 已是 canonical，直接用。

    两个子信号合成到 0-50：
      rank_sub       : trending + downloads 两榜 rank<=20，21-rank 求和后归一 0-30
      downloads_sub  : 最新 downloads - 24h 前 downloads，log 归一到 0-20
    """
    rank_raw: dict[str, float] = {}
    with get_conn() as conn:
        for list_type in ("trending", "downloads"):
            latest_time = conn.execute(
                "SELECT MAX(scraped_at) AS t FROM hf_snapshots WHERE list_type=?",
                (list_type,),
            ).fetchone()["t"]
            if not latest_time:
                continue
            rows = conn.execute(
                "SELECT matched_model, rank FROM hf_snapshots "
                "WHERE list_type=? AND scraped_at=? AND rank IS NOT NULL AND rank<=20 "
                "AND matched_model IS NOT NULL AND matched_model != ''",
                (list_type, latest_time),
            ).fetchall()
            for r in rows:
                rank_raw[r["matched_model"]] = (
                    rank_raw.get(r["matched_model"], 0.0) + max(0, 21 - r["rank"])
                )

        # 24h 下载增量：按 matched_model 聚合多个 model_id 的 delta（取最大）
        delta_rows = conn.execute(
            """
            WITH latest AS (
                SELECT matched_model, model_id, downloads,
                       ROW_NUMBER() OVER (PARTITION BY model_id ORDER BY scraped_at DESC) AS rn
                FROM hf_snapshots
                WHERE matched_model IS NOT NULL AND matched_model != ''
                  AND downloads IS NOT NULL
            ),
            old AS (
                SELECT model_id, downloads,
                       ROW_NUMBER() OVER (PARTITION BY model_id
                                          ORDER BY ABS(strftime('%s', scraped_at)
                                                       - strftime('%s', datetime('now', '-24 hours'))) ASC) AS rn
                FROM hf_snapshots
                WHERE scraped_at <= datetime('now', '-20 hours')
                  AND downloads IS NOT NULL
            )
            SELECT l.matched_model, l.model_id, (l.downloads - o.downloads) AS delta
            FROM latest l
            LEFT JOIN old o ON l.model_id = o.model_id
            WHERE l.rn = 1 AND (o.rn IS NULL OR o.rn = 1)
            """
        ).fetchall()

    dl_raw: dict[str, float] = {}
    for r in delta_rows:
        delta = r["delta"]
        if delta is None or delta <= 0:
            continue
        m = r["matched_model"]
        dl_raw[m] = max(dl_raw.get(m, 0.0), float(delta))

    rank_norm: dict[str, float] = {}
    if rank_raw:
        top = max(rank_raw.values()) or 1.0
        rank_norm = {m: 50.0 * W_HF_RANK * v / top for m, v in rank_raw.items()}

    dl_norm: dict[str, float] = {}
    if dl_raw:
        max_delta = max(dl_raw.values()) or 1.0
        denom = math.log(1 + max_delta) or 1.0
        dl_norm = {m: 50.0 * W_HF_DOWNLOADS * math.log(1 + v) / denom for m, v in dl_raw.items()}

    all_m = set(rank_norm) | set(dl_norm)
    return {m: round(rank_norm.get(m, 0.0) + dl_norm.get(m, 0.0), 2) for m in all_m}


def run() -> dict:
    """计算当日热度，写入 heat_scores。返回 {written, top_model}。"""
    ensure_tables()
    try:
        today = date.today().isoformat()
        rank = _rank_scores()
        star = _star_surge_scores()
        hf = _hf_scores()

        all_models = set(rank) | set(star) | set(hf)
        written = 0
        top = ("", 0.0)

        with get_conn() as conn:
            for m in all_models:
                dr = rank.get(m, 0.0)
                ds = star.get(m, 0.0)
                dh = hf.get(m, 0.0)
                score = round(W_RANK * dr + W_STAR * ds + W_HF * dh, 2)
                dims = {"rank": dr, "star24h": ds, "hf": dh}
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
