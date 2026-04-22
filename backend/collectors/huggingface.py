"""HuggingFace Collector：抓 trending + downloads 两个榜单，写 hf_snapshots。

HF 公开 API（无需 token 即可访问，token 只用来提高 rate limit）：
  GET https://huggingface.co/api/models?sort=trending&direction=-1&limit=50
  GET https://huggingface.co/api/models?sort=downloads&direction=-1&limit=50

返回字段参考：
  id, author, downloads, likes, pipeline_tag, tags, createdAt, lastModified, trendingScore

命名注意：
- API 里 `id` 是 "vendor/modelname" 形态（e.g. "meta-llama/Llama-4-Maverick"）
- model_alias.find_mentions() 通常是识别 "Llama 4 Maverick" 这种空格名
- 我们把 id 的后半段用连字符拆开扔给 find_mentions，尽量匹配到 canonical
- 没命中 matched_model 留空，不影响基础快照；后续 alias 扩充后再跑一次能回填

冷启动说明：
- "7 天下载量环比 > 200%" 这类 delta 规则需要至少 14 天数据，heat_scorer 目前先不启用
- 本 collector 只负责"拿数据"，diff/heat 的规则后续按需加
"""
import json
import logging
from datetime import datetime, timezone

import requests

from backend.db import get_conn, record_status
from backend.utils import config
from backend.utils.model_alias import find_mentions
from backend.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

API_URL = "https://huggingface.co/api/models"

# 两个榜各取 50 条：trending 反映"正在被讨论"，downloads 反映"真有人在用"
# HF API 的 sort 字段要传数据字段名——trending 的字段叫 trendingScore，不是 trending
LIST_CONFIGS = [
    {"list_type": "trending",  "sort": "trendingScore", "limit": 50},
    {"list_type": "downloads", "sort": "downloads",     "limit": 50},
]


def _headers() -> dict:
    h = {
        "User-Agent": "ModelRadar/1.0",
        "Accept": "application/json",
    }
    if config.HF_TOKEN:
        h["Authorization"] = f"Bearer {config.HF_TOKEN}"
    return h


@retry_with_backoff(max_retries=2, base_delay=4.0)
def _fetch(list_type: str, sort: str, limit: int) -> list[dict]:
    resp = requests.get(
        API_URL,
        headers=_headers(),
        params={
            "sort": sort,
            "direction": -1,
            "limit": limit,
            # full=true 拿 pipeline_tag、tags、lastModified 等字段
            "full": "true",
        },
        timeout=25,
    )
    if resp.status_code == 429:
        raise RuntimeError(f"HF API 429 (sort={sort})")
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError(f"HF API 响应异常: {type(data).__name__}")
    return data


def _id_to_match_text(model_id: str) -> str:
    """把 "meta-llama/Llama-4-Maverick-17B" 拆成 find_mentions 能吃的自然文本。
    find_mentions 内部是 word-boundary 匹配，"Llama 4 Maverick" 这种空格形式最好认。
    """
    if not model_id:
        return ""
    tail = model_id.split("/", 1)[-1]
    # 连字符/下划线 → 空格；保留数字原样
    return tail.replace("-", " ").replace("_", " ")


def _match_model(model_id: str) -> str | None:
    text = _id_to_match_text(model_id)
    if not text:
        return None
    hits = find_mentions(text, max_hits=1)
    return hits[0] if hits else None


def _parse_ts(v) -> str | None:
    """HF 返回的时间是 ISO8601，直接存字符串即可。防一下 None/非法值。"""
    if not v:
        return None
    if isinstance(v, str):
        return v[:40]
    return None


def _persist(conn, item: dict, list_type: str, rank: int) -> bool:
    model_id = item.get("id") or item.get("modelId")
    if not model_id:
        return False
    author = model_id.split("/", 1)[0] if "/" in model_id else None
    matched = _match_model(model_id)

    cur = conn.execute(
        """
        INSERT INTO hf_snapshots
          (model_id, author, list_type, rank, downloads, likes,
           pipeline_tag, tags_json, created_at, last_modified, matched_model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            model_id,
            author,
            list_type,
            rank,
            item.get("downloads"),
            item.get("likes"),
            item.get("pipeline_tag"),
            json.dumps(item.get("tags") or [], ensure_ascii=False),
            _parse_ts(item.get("createdAt")),
            _parse_ts(item.get("lastModified")),
            matched,
        ),
    )
    return cur.rowcount > 0


def collect() -> dict:
    """抓 trending + downloads 两张榜，写 hf_snapshots。返回 {list_type: count}。"""
    summary: dict[str, int | str] = {}
    try:
        with get_conn() as conn:
            for cfg in LIST_CONFIGS:
                lt = cfg["list_type"]
                try:
                    items = _fetch(lt, cfg["sort"], cfg["limit"])
                except Exception as e:
                    logger.error("[HF] 抓 %s 失败: %s", lt, e)
                    summary[lt] = f"error: {str(e)[:80]}"
                    continue

                inserted = 0
                matched = 0
                for rank, it in enumerate(items, start=1):
                    if _persist(conn, it, lt, rank):
                        inserted += 1
                    if _match_model(it.get("id") or ""):
                        matched += 1

                summary[lt] = inserted
                logger.info("[HF] %s 榜：写入 %d / %d 条，匹配 canonical %d 条",
                            lt, inserted, len(items), matched)

        record_status("huggingface", success=True)
        return summary
    except Exception as e:
        logger.exception("HuggingFace 采集失败: %s", e)
        record_status("huggingface", success=False, error=str(e))
        raise


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(collect())
