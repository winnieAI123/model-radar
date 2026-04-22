"""Blog RSS Collector：抓厂商官方博客 RSS，入 blog_posts 表。

监听对象是"模型厂商自己说了什么"——不是社区讨论，不是榜单数据，而是厂商亲口宣布的
产品/模型/API 更新。Reddit 是迟到的噪声，博客是第一手信号，所以单独建一张表。

数据流：
  feedparser.parse(url) → entries → INSERT OR IGNORE（URL 主键天然去重）→
  matched_model = find_mentions(title + summary)[0]（能命中就填，不命中留空）

下游：
- diff_engine._diff_blog_posts() 给最近刚入库的每条 post 发一条 P1 事件
- weekly_report 单独有"厂商博客动态"一节渲染 blog_posts

选源原则：
- 只收官方（OpenAI News / Anthropic News / Google AI Blog / Meta AI）
- 不收 Medium / Substack / 第三方——这些容易掺水，放进来反而稀释信号
- 每次只抓 feed 顶部的前 N 条（RSS 本身通常就 20 条以内），不做深翻页
"""
import logging
import re
from html import unescape

import feedparser

from backend.db import get_conn, record_status
from backend.utils.model_alias import find_mentions

logger = logging.getLogger(__name__)

# 用户可以通过改这里扩展源。key 是 blog_posts.source 字段的值，人眼可读。
#
# 缺席说明（2026-04 核查）：
# - Anthropic News 不再提供 RSS（anthropic.com/news/rss.xml 404）。官方博客只能靠 HTML 抓，
#   留给后续增量迭代。现在覆盖 Anthropic 动态主要靠 GitHub releases + Reddit 讨论。
# - Meta AI Blog (ai.meta.com) 也砍了 RSS。用 research.facebook.com/feed 代替，覆盖 FAIR/Meta AI 研究产出。
FEEDS: list[dict] = [
    {"source": "openai",        "name": "OpenAI News",          "url": "https://openai.com/news/rss.xml"},
    {"source": "google_ai",     "name": "Google AI Blog",       "url": "https://blog.google/technology/ai/rss/"},
    {"source": "deepmind",      "name": "Google DeepMind",      "url": "https://deepmind.google/blog/rss.xml"},
    {"source": "meta_research", "name": "Meta Research (FAIR)", "url": "https://research.facebook.com/feed/"},
]

SUMMARY_TRUNC = 1000

# 去 HTML 标签：RSS summary 常带 <p><a> 等，送去匹配和周报展示都不需要
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    return unescape(_TAG_RE.sub(" ", s)).strip()


def _parse_published(entry) -> str | None:
    """把 feedparser 的日期字段统一转成 ISO "YYYY-MM-DD HH:MM:SS"。
    返回字符串格式必须能被 SQLite datetime() 比较，否则 diff_engine 的
    published_at 过滤（datetime('now', '-48 hours')）会失效。
    """
    import time as _t
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            try:
                return _t.strftime("%Y-%m-%d %H:%M:%S", t)
            except Exception:
                pass
    # 退路：feedparser 没解析出来（某些 feed 带不标准 RFC2822）
    # 用 email.utils.parsedate_to_datetime 兜一次
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(raw)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
    return None


def _match_model(text: str) -> str | None:
    hits = find_mentions(text, max_hits=1)
    return hits[0] if hits else None


def _persist(conn, source: str, entry) -> bool:
    url = entry.get("link") or entry.get("id")
    title = (entry.get("title") or "").strip()
    if not url or not title:
        return False
    summary = _strip_html(entry.get("summary") or entry.get("description") or "")[:SUMMARY_TRUNC]
    published = _parse_published(entry)
    matched = _match_model(f"{title}\n{summary}")

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO blog_posts
          (url, source, title, summary, published_at, matched_model)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (url, source, title, summary, published, matched),
    )
    return cur.rowcount > 0


def _fetch_feed(url: str):
    """feedparser 不抛异常，错误在 .bozo / .bozo_exception。我们包一层好记日志。"""
    fp = feedparser.parse(url, agent="ModelRadar/1.0")
    if fp.bozo and not fp.entries:
        raise RuntimeError(f"feedparser 解析失败: {fp.get('bozo_exception')}")
    return fp


def collect() -> dict:
    """遍历 FEEDS，返回 {source: new_count}。单个 feed 失败不影响其他。"""
    summary: dict[str, int | str] = {}
    any_success = False
    last_err: str | None = None

    try:
        with get_conn() as conn:
            for f in FEEDS:
                try:
                    fp = _fetch_feed(f["url"])
                except Exception as e:
                    logger.error("[Blog] %s 拉取失败: %s", f["source"], e)
                    summary[f["source"]] = f"error: {str(e)[:80]}"
                    last_err = str(e)[:200]
                    continue

                new_cnt = 0
                matched_cnt = 0
                for entry in fp.entries:
                    try:
                        inserted = _persist(conn, f["source"], entry)
                    except Exception as e:
                        logger.warning("[Blog] 写 %s 某条失败: %s", f["source"], e)
                        continue
                    if inserted:
                        new_cnt += 1
                    if _match_model(f"{entry.get('title','')}\n{_strip_html(entry.get('summary',''))[:400]}"):
                        matched_cnt += 1

                summary[f["source"]] = new_cnt
                any_success = True
                logger.info("[Blog] %s: fetched=%d new=%d matched=%d",
                            f["source"], len(fp.entries), new_cnt, matched_cnt)

        # 只要有一个源成功就算 collector 健康（部分站点经常临时 5xx）
        record_status("blog_rss", success=any_success, error=None if any_success else last_err)
        return summary
    except Exception as e:
        logger.exception("Blog RSS 采集整体失败: %s", e)
        record_status("blog_rss", success=False, error=str(e))
        raise


if __name__ == "__main__":
    import json as _j
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(_j.dumps(collect(), indent=2, ensure_ascii=False))
