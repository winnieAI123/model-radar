"""Twitter Collector：调 twitterapi.io 抓 @drfeifei 等闭源大佬最新推文。

为什么单独做这一路：
- 李飞飞 World Labs 世界模型这类完全闭源，GitHub / 公众号都漏
- 个人推文是最早信源（领先厂商博客 + 媒体报道几小时）
- twitterapi.io 第三方代理 API（X-API-Key 鉴权），比 X 官方 v2 便宜且无验证门槛

数据流：
  GET /twitter/tweet/advanced_search?query=from:<handle>&queryType=Latest
    → INSERT OR IGNORE 到 blog_posts (source='twitter_<handle>')
  url 是表 PK，自动去重；source 命名沿用 wechat_<name>/blog_<vendor> 约定
  下游 diff_engine / closed_source_classifier / Dashboard §I 全部零改动
"""
import logging
import time
from datetime import datetime, timezone

import requests

from backend.db import get_conn, record_status
from backend.utils import config
from backend.utils.model_alias import find_mentions

logger = logging.getLogger(__name__)

API_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
HTTP_TIMEOUT = 30
QUERY_TYPE = "Latest"   # 时间倒序，避免 Top 模式按互动量排序遗漏冷门新推
QPS_SLEEP = 0.5         # twitterapi.io 限速 5 req/s，0.5s 给余量


def _fetch_latest(handle: str) -> list[dict]:
    """拉 from:<handle> 的最新 1 页推文（≈20 条）。失败抛 RuntimeError。"""
    proxies = None
    if config.TWITTER_PROXY:
        proxies = {"http": config.TWITTER_PROXY, "https": config.TWITTER_PROXY}
    resp = requests.get(
        API_URL,
        headers={"X-API-Key": config.TWITTER_API_KEY},
        params={"query": f"from:{handle}", "queryType": QUERY_TYPE, "cursor": ""},
        proxies=proxies,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    tweets = data.get("tweets")
    if not isinstance(tweets, list):
        raise RuntimeError(f"twitterapi.io handle={handle} 返回结构异常：{str(data)[:300]}")
    return tweets


def _parse_created_at(s: str) -> str | None:
    """'Tue Apr 14 17:52:38 +0000 2026' → 'YYYY-MM-DD HH:MM:SS' (UTC naive)。

    解析失败返回 None；diff_engine 用 published_at IS NOT NULL 过滤，
    NULL 推文不会触发事件，但仍躺在 blog_posts 里供周报扫描。
    """
    try:
        dt = datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _upsert(handle: str, tweets: list[dict]) -> int:
    new_count = 0
    with get_conn() as conn:
        for t in tweets:
            tid = t.get("id")
            text = (t.get("text") or "").strip()
            if not tid or not text:
                continue
            url = (t.get("url") or f"https://x.com/{handle}/status/{tid}").strip()
            published = _parse_created_at(t.get("createdAt") or "")
            mentions = find_mentions(text)
            matched = mentions[0] if mentions else None
            title = text.replace("\n", " ")[:80]
            cur = conn.execute(
                """INSERT OR IGNORE INTO blog_posts
                   (url, source, title, summary, body_full, published_at, matched_model)
                   VALUES (?, ?, ?, ?, NULL, ?, ?)""",
                (url, f"twitter_{handle}", title, text[:1000], published, matched),
            )
            if cur.rowcount:
                new_count += 1
    return new_count


def collect() -> None:
    if not config.TWITTER_API_KEY:
        logger.warning("[Twitter] 未配置 TWITTER_API_KEY，跳过")
        return
    handles = [h.strip() for h in (config.TWITTER_HANDLES or "").split(",") if h.strip()]
    if not handles:
        logger.warning("[Twitter] TWITTER_HANDLES 为空，跳过")
        return

    total_new = 0
    total_returned = 0
    errors: list[str] = []

    for handle in handles:
        try:
            tweets = _fetch_latest(handle)
            new = _upsert(handle, tweets)
            total_returned += len(tweets)
            total_new += new
            logger.info("[Twitter] %s: %d 条返回，%d 条新入库", handle, len(tweets), new)
        except Exception as e:
            logger.exception("[Twitter] %s 失败：%s", handle, e)
            errors.append(f"{handle}: {e}")
        time.sleep(QPS_SLEEP)

    if errors and total_returned == 0:
        record_status("twitter_feifei", success=False, error="; ".join(errors)[:500])
    else:
        record_status("twitter_feifei", success=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    collect()
