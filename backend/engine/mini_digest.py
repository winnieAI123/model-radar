"""Mini Digest：Dashboard 用的聚合缓存生成器。

周报一周只跑一次 LLM 聚合，周中 6 天 Dashboard 读不到最新的'社区声音 / 本周热议'。
这个模块每 12h 跑一次，窗口仍然是近 7d，产出直接写 digest_cache 表，Dashboard 秒开。

两个任务（可独立跑）：
- run_opinions()  → kind='opinions' · 模型级 Reddit 观点聚合
- run_themes()    → kind='themes'   · 主题聚类

LLM 调用失败时 generator 自己会走 fallback，这里只负责把最终 payload 写缓存。
"""
import json
import logging
from datetime import datetime, timezone

from backend.db import get_conn, record_status
from backend.engine import reddit_opinions, reddit_themes

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 7


def _write_cache(kind: str, window_days: int, payload: dict) -> None:
    # 全项目统一 UTC（和 SQL CURRENT_TIMESTAMP / datetime('now') 一致），前端 relTime 会加 Z 解析。
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO digest_cache(kind, window_days, payload_json, generated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(kind, window_days) DO UPDATE SET
                payload_json = excluded.payload_json,
                generated_at = excluded.generated_at
            """,
            (kind, window_days, json.dumps(payload, ensure_ascii=False), now),
        )


def run_opinions(days: int = DEFAULT_WINDOW_DAYS) -> dict:
    logger.info("[MiniDigest] opinions start (days=%d)", days)
    try:
        payload = reddit_opinions.generate(days=days) or {"models": []}
        _write_cache("opinions", days, payload)
        record_status("mini_digest_opinions", success=True)
        logger.info("[MiniDigest] opinions done · model_count=%d",
                    len(payload.get("models") or []))
        return payload
    except Exception as e:
        logger.exception("[MiniDigest] opinions 失败: %s", e)
        record_status("mini_digest_opinions", success=False, error=str(e))
        raise


def run_themes(days: int = DEFAULT_WINDOW_DAYS) -> dict:
    logger.info("[MiniDigest] themes start (days=%d)", days)
    try:
        payload = reddit_themes.generate(days=days) or {"themes": []}
        _write_cache("themes", days, payload)
        record_status("mini_digest_themes", success=True)
        logger.info("[MiniDigest] themes done · theme_count=%d post_count=%d",
                    len(payload.get("themes") or []),
                    payload.get("post_count", 0))
        return payload
    except Exception as e:
        logger.exception("[MiniDigest] themes 失败: %s", e)
        record_status("mini_digest_themes", success=False, error=str(e))
        raise


def run_all(days: int = DEFAULT_WINDOW_DAYS) -> None:
    """调度器入口：两个都跑，互不阻塞。"""
    try:
        run_opinions(days=days)
    except Exception:
        pass
    try:
        run_themes(days=days)
    except Exception:
        pass


def read_cache(kind: str, window_days: int = DEFAULT_WINDOW_DAYS) -> dict | None:
    """给 Dashboard API 用。返回 {'payload': dict, 'generated_at': str} 或 None。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload_json, generated_at FROM digest_cache WHERE kind=? AND window_days=?",
            (kind, window_days),
        ).fetchone()
    if not row:
        return None
    try:
        return {
            "payload": json.loads(row["payload_json"]),
            "generated_at": row["generated_at"],
        }
    except Exception as e:
        logger.warning("[MiniDigest] 缓存 %s 解析失败: %s", kind, e)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_all()
