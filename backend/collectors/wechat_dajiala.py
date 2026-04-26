"""WeChat Dajiala Collector：调大家啦（dajiala.com）API 备份采集指定公众号当日发文。

为什么单独搞这一路：
- 主链路 `wechat_rss.py` 走 WeWeRSS，登录态会随机过期且本端无感
- dajiala 用 API key 鉴权，无登录态问题，作为关键博主的兜底渠道
- 单价低（post_condition 0.08 元/次），2 个号每天 1 次月成本 ≈ 5 元

数据流：
  POST /fbmain/monitor/v3/post_condition?name=<昵称> → 当日发文 → INSERT OR IGNORE 到 blog_posts
  url 是表 PK，与 WeWeRSS 同一篇文章自动去重；source 命名一致 (`wechat_<name>`)
  下游 wechat_digest / closed_source_classifier / Dashboard 全部零改动

不做的事：
- 不调 history_by_ghid（贵 3 倍且本轮不需要历史/阅读量）
- 不抓全文（body_full 留空，全文仍依赖 WeWeRSS）
"""
import logging
import time
from datetime import datetime

import requests

from backend.db import get_conn, record_status
from backend.utils import config
from backend.utils.model_alias import find_mentions

logger = logging.getLogger(__name__)

API_URL = "https://www.dajiala.com/fbmain/monitor/v3/post_condition"
HTTP_TIMEOUT = 30
QPS_SLEEP = 0.3   # post_condition QPS 上限 5/s，0.3s 给足余量


def _fetch_today(name: str) -> list[dict]:
    """调 post_condition，返回当天该公众号发的文章列表。

    每条字典含: title / url / post_time(unix) / mp_nickname / mp_ghid 等
    code != 0 抛 RuntimeError，让外层 try/except 落库 system_status.last_error
    """
    resp = requests.post(
        API_URL,
        json={"name": name, "key": config.DAJIALA_KEY},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    code = data.get("code")
    if code != 0:
        raise RuntimeError(
            f"dajiala name={name} code={code} msg={data.get('msg') or data.get('message') or '?'}"
        )
    return data.get("data") or []


def _upsert(name: str, items: list[dict]) -> int:
    new_count = 0
    with get_conn() as conn:
        for it in items:
            url = (it.get("url") or "").strip()
            title = (it.get("title") or "").strip()
            if not url or not title:
                continue
            published = None
            ts = it.get("post_time")
            if ts:
                try:
                    published = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError, OSError):
                    published = None
            mentions = find_mentions(title)
            matched = mentions[0] if mentions else None
            cur = conn.execute(
                """INSERT OR IGNORE INTO blog_posts
                   (url, source, title, summary, body_full, published_at, matched_model)
                   VALUES (?, ?, ?, ?, NULL, ?, ?)""",
                (url, f"wechat_{name}", title, title[:1000], published, matched),
            )
            if cur.rowcount:
                new_count += 1
    return new_count


def collect() -> None:
    accounts = [a.strip() for a in (config.DAJIALA_ACCOUNTS or "").split(",") if a.strip()]
    if not config.DAJIALA_KEY or not accounts:
        logger.warning("[Dajiala] 未配置 DAJIALA_KEY 或 DAJIALA_ACCOUNTS，跳过")
        return

    total_new = 0
    total_returned = 0
    errors: list[str] = []

    for name in accounts:
        try:
            items = _fetch_today(name)
            new = _upsert(name, items)
            total_returned += len(items)
            total_new += new
            logger.info("[Dajiala] %s: %d 条返回，%d 条新入库", name, len(items), new)
        except Exception as e:
            logger.exception("[Dajiala] %s 失败：%s", name, e)
            errors.append(f"{name}: {e}")
        time.sleep(QPS_SLEEP)

    # 全部号都失败才标 success=False；部分号成功仍算 success（保留 last_error 由 record_status 处理）
    if errors and total_returned == 0:
        record_status("wechat_dajiala", success=False, error="; ".join(errors)[:500])
    else:
        record_status("wechat_dajiala", success=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    collect()
