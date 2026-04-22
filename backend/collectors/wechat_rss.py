"""WeChat RSS Collector：抓取 WeWe-RSS 订阅的公众号文章，入 blog_posts 表。

监听对象："大佬"或"专业媒体"的公众号文章（如赛博禅心、数字生命卡兹克等）。
数据流：
  requests.get(wewe_rss_url) → items → 提取 content_html → INSERT OR IGNORE
  
注意：依赖 Railway 端配置 FEED_MODE=fulltext 环境变量来输出全文 HTML。
"""
import logging
import re
from html import unescape
from datetime import datetime, timezone

import requests

from backend.db import get_conn, record_status
from backend.utils.model_alias import find_mentions

logger = logging.getLogger(__name__)

# 配置你的 Railway WeWe RSS JSON 接口地址
WEWE_RSS_URL = "https://wewe-rss-sqlite-production-b5ca.up.railway.app/feeds/all.json"

SUMMARY_TRUNC = 1000

# 去 HTML 标签：把 content_html 剥离成纯文本供 diff_engine 使用
_TAG_RE = re.compile(r"<[^>]+>")

def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    return unescape(_TAG_RE.sub(" ", s)).strip()

def _parse_published(date_str: str) -> str | None:
    """把 WeWe RSS 的 ISO 格式 (2026-04-22T02:13:25.000Z) 转成 SQLite datetime() 格式。"""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def _match_model(text: str) -> str | None:
    hits = find_mentions(text, max_hits=1)
    return hits[0] if hits else None

def _persist(conn, item) -> bool:
    url = item.get("url") or item.get("id")
    title = (item.get("title") or "").strip()
    if not url or not title:
        return False
        
    # WeWe RSS 在开启 FEED_MODE=fulltext 时，会在 content_html 中输出全文
    raw_html = item.get("content_html") or item.get("content_text") or item.get("summary") or ""
    summary = _strip_html(raw_html)[:SUMMARY_TRUNC]
    
    published = _parse_published(item.get("date_modified") or item.get("date_published") or "")
    
    # 尝试匹配模型名
    matched = _match_model(f"{title}\n{summary}")

    # 获取公众号名称作为 source
    author_data = item.get("author", {})
    author_name = author_data.get("name", "WeChat") if isinstance(author_data, dict) else str(author_data)
    # 加上前缀以区分这是微信源
    source = f"wechat_{author_name}"

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO blog_posts
          (url, source, title, summary, published_at, matched_model)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (url, source, title, summary, published, matched),
    )
    return cur.rowcount > 0

def collect() -> dict:
    """抓取 WeWe RSS 接口的数据"""
    summary_report: dict[str, int | str] = {}
    any_success = False
    last_err: str | None = None

    try:
        response = requests.get(WEWE_RSS_URL, timeout=120)
        response.raise_for_status()
        data = response.json()
        items = data.get("items", [])
        
        new_cnt = 0
        matched_cnt = 0
        
        with get_conn() as conn:
            for item in items:
                try:
                    inserted = _persist(conn, item)
                    if inserted:
                        new_cnt += 1
                        
                        raw_html = item.get("content_html") or ""
                        if _match_model(f"{item.get('title', '')}\n{_strip_html(raw_html)[:400]}"):
                            matched_cnt += 1
                except Exception as e:
                    logger.warning("[WeChat] 写某条微信文章失败: %s", e)
                    continue

        summary_report["wechat_rss"] = new_cnt
        any_success = True
        logger.info("[WeChat] fetched=%d new=%d matched=%d", len(items), new_cnt, matched_cnt)

        record_status("wechat_rss", success=any_success, error=None)
        return summary_report
        
    except Exception as e:
        logger.exception("WeChat RSS 采集整体失败: %s", e)
        record_status("wechat_rss", success=False, error=str(e))
        summary_report["wechat_rss"] = f"error: {str(e)[:80]}"
        raise

if __name__ == "__main__":
    import json as _j
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(_j.dumps(collect(), indent=2, ensure_ascii=False))
