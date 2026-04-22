"""Reddit Collector：抓目标 subreddit 的最新/热门帖子，自动匹配 canonical 模型名。

适配自 user-research-analyst/scripts/collectors/reddit_collector.py，改动：
- 去掉硬编码代理（Railway 美国机直连；本地开发想走代理就设 REDDIT_PROXY 环境变量）
- 支持 subreddit 限定搜索（restrict_sr=on）
- 落 SQLite 而不是 CSV，走项目已有的 reddit_posts 表
- 入库前用 backend.utils.model_alias.normalize() 把 title/selftext 匹配到 canonical 名

本模块只管"采"，Phase 3 的 community_digest.py 再读数据库做聚合总结。
"""
import logging
import random
import re
import time

import requests

from backend.db import get_conn, record_status
from backend.utils import config
from backend.utils.model_alias import find_mentions, normalize
from backend.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

BASE_URL = "https://www.reddit.com"

# 默认跟踪的 sub。用户可以通过环境变量 REDDIT_SUBS 覆盖（逗号分隔）。
DEFAULT_SUBREDDITS = [
    "LocalLLaMA",
    "StableDiffusion",
    "singularity",
    "ChatGPT",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    ),
}

# 正文截断阈值。Reddit 长帖动辄几千字，对匹配和存储都没必要。
SELFTEXT_TRUNC = 800

# 模型关键词抽取：从 title 中抠出英文/数字/点/斜杠/短横组成的 token。
# 像 "GPT-5 is crazy" → 拿到 "GPT-5"；"openai/gpt-4o beats..." → "openai/gpt-4o"。
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9.\-/]{1,40}")


def _session(proxy: str | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


@retry_with_backoff(max_retries=2, base_delay=5.0)
def _fetch(session: requests.Session, url: str) -> dict:
    resp = session.get(url, timeout=20)
    if resp.status_code == 429:
        # 专门的限流分支：sleep 60s 然后让 retry 装饰器再试一次
        logger.warning("[Reddit] 429 限流: %s，sleep 60s", url)
        time.sleep(60)
        raise RuntimeError(f"reddit 429 on {url}")
    resp.raise_for_status()
    return resp.json()


def _match_model(text: str) -> str | None:
    """在文本中找第一个 canonical 模型名。
    先走 find_mentions（全文词边界扫描，命中率高），退化到原 token 逐个 normalize。
    """
    if not text:
        return None
    hits = find_mentions(text, max_hits=1)
    if hits:
        return hits[0]
    # 兜底：逐 token normalize（抓漏网之鱼）
    seen: set[str] = set()
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok.lower() in seen:
            continue
        seen.add(tok.lower())
        c = normalize(tok)
        if c:
            return c
    return None


def _persist(conn, post: dict, matched_model: str | None, matched_in: str | None) -> bool:
    """写一条 post。已存在（INSERT OR IGNORE）返回 False。"""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO reddit_posts
          (post_id, subreddit, title, author, selftext, url,
           score, num_comments, created_utc, matched_model, matched_in)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            post["id"],
            post["subreddit"],
            post["title"],
            post.get("author"),
            (post.get("selftext") or "")[:SELFTEXT_TRUNC],
            post.get("url"),
            post.get("score", 0),
            post.get("num_comments", 0),
            int(post["created_utc"]) if post.get("created_utc") else None,
            matched_model,
            matched_in,
        ),
    )
    return cur.rowcount > 0


def _parse_listing(data: dict, subreddit: str) -> list[dict]:
    """把 Reddit listing JSON 扁平化成我们需要的字段。"""
    out = []
    for item in data.get("data", {}).get("children", []):
        if item.get("kind") != "t3":
            continue
        d = item.get("data") or {}
        out.append({
            "id":           d.get("id"),
            "subreddit":    d.get("subreddit") or subreddit,
            "title":        d.get("title") or "",
            "author":       d.get("author"),
            "selftext":     d.get("selftext") or "",
            "url":          f"{BASE_URL}{d.get('permalink', '')}",
            "score":        d.get("score", 0),
            "num_comments": d.get("num_comments", 0),
            "created_utc":  d.get("created_utc"),
        })
    return out


def _sub_top(session: requests.Session, subreddit: str, limit: int = 50,
             time_filter: str = "week") -> list[dict]:
    """抓 /r/{sub}/top.json?t=week。"""
    url = f"{BASE_URL}/r/{subreddit}/top.json?limit={limit}&t={time_filter}"
    data = _fetch(session, url)
    return _parse_listing(data, subreddit)


def _sub_search(session: requests.Session, subreddit: str, query: str,
                limit: int = 25) -> list[dict]:
    """sub 内关键词搜索。restrict_sr=on 是跟原脚本最大的区别。"""
    url = (
        f"{BASE_URL}/r/{subreddit}/search.json"
        f"?q={requests.utils.quote(query)}"
        f"&restrict_sr=on&sort=new&t=week&limit={limit}"
    )
    data = _fetch(session, url)
    return _parse_listing(data, subreddit)


def _top_heat_models(limit: int = 5) -> list[str]:
    """从最近一次热度榜取 Top N canonical 模型名，用作定向搜索的 query。
    没有 heat_scores 数据（冷启动期）时返回空列表，search 阶段自然跳过。
    """
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(date) AS d FROM heat_scores").fetchone()
        if not row or not row["d"]:
            return []
        rows = conn.execute(
            "SELECT model_name FROM heat_scores WHERE date=? "
            "ORDER BY score DESC LIMIT ?",
            (row["d"], limit),
        ).fetchall()
    return [r["model_name"] for r in rows]


def collect(subreddits: list[str] | None = None,
            per_sub_limit: int = 50,
            time_filter: str = "week",
            proxy: str | None = None,
            search_top_n: int = 5,
            search_per_query_limit: int = 15) -> dict:
    """一次完整的 Reddit 采集，分两段：

    1. Pulse（top.json）：每个 sub 抓 top/week 前 N 条，捕捉"社区在聊什么"——
       大部分是 meme/泛讨论，命中率低（~3%）但能喂给 Community Digest 的 LLM。
    2. Search（search.json?restrict_sr=on）：热度 Top N × 每个 sub 做 query，
       命中率接近 100%（query 本身就是 canonical 名），用于模型维度的定向追踪。

    两段写同一张 reddit_posts 表（post_id 主键去重），matched_in 字段区分来源：
    title / selftext / search:<query>。冷启动期没有 heat_scores 时，search 段会自动跳过。

    返回 {"pulse": {sub: stats}, "search": {query: stats}}。
    """
    subs = subreddits or [s.strip() for s in (config.REDDIT_SUBS or ",".join(DEFAULT_SUBREDDITS)).split(",") if s.strip()]
    proxy = proxy if proxy is not None else (config.REDDIT_PROXY or None)

    session = _session(proxy)
    pulse_summary: dict[str, dict] = {}
    search_summary: dict[str, dict] = {}

    try:
        with get_conn() as conn:
            # ---- Phase 1: Pulse ----
            for i, sub in enumerate(subs):
                try:
                    posts = _sub_top(session, sub, limit=per_sub_limit, time_filter=time_filter)
                except Exception as e:
                    logger.error("[Reddit] /r/%s top 拉取失败: %s", sub, e)
                    pulse_summary[sub] = {"error": str(e)[:100]}
                    continue

                new_cnt = 0
                matched_cnt = 0
                for p in posts:
                    if not p.get("id"):
                        continue
                    matched_model = _match_model(p["title"])
                    matched_in = "title" if matched_model else None
                    if not matched_model:
                        matched_model = _match_model((p.get("selftext") or "")[:400])
                        matched_in = "selftext" if matched_model else None
                    if _persist(conn, p, matched_model, matched_in):
                        new_cnt += 1
                    if matched_model:
                        matched_cnt += 1

                pulse_summary[sub] = {
                    "fetched": len(posts),
                    "new":     new_cnt,
                    "matched": matched_cnt,
                }
                logger.info("[Reddit][pulse] /r/%s: fetched=%d new=%d matched=%d",
                            sub, len(posts), new_cnt, matched_cnt)

                if i < len(subs) - 1:
                    time.sleep(random.uniform(1.5, 3.5))

            # ---- Phase 2: Search ----
            top_models = _top_heat_models(limit=search_top_n)
            if not top_models:
                logger.info("[Reddit][search] 没有 heat_scores 数据，跳过 search 段")
            else:
                logger.info("[Reddit][search] 对 Top %d 热度模型做 sub 内搜索: %s",
                            len(top_models), top_models)
                for q_i, query in enumerate(top_models):
                    for s_i, sub in enumerate(subs):
                        key = f"{query}@{sub}"
                        try:
                            posts = _sub_search(session, sub, query, limit=search_per_query_limit)
                        except Exception as e:
                            logger.warning("[Reddit][search] /r/%s q=%s 失败: %s", sub, query, e)
                            search_summary[key] = {"error": str(e)[:100]}
                            time.sleep(random.uniform(1.5, 3.0))
                            continue

                        new_cnt = 0
                        for p in posts:
                            if not p.get("id"):
                                continue
                            # search 段的 matched_model 就是我们搜的 query 本身（canonical）
                            if _persist(conn, p, query, f"search:{query}"):
                                new_cnt += 1

                        search_summary[key] = {
                            "fetched": len(posts),
                            "new":     new_cnt,
                            "matched": len(posts),
                        }
                        logger.info("[Reddit][search] /r/%s q=%s: fetched=%d new=%d",
                                    sub, query, len(posts), new_cnt)

                        # search 段请求更多，节奏更保守
                        is_last = q_i == len(top_models) - 1 and s_i == len(subs) - 1
                        if not is_last:
                            time.sleep(random.uniform(2.0, 4.0))

        record_status("reddit", success=True)
        return {"pulse": pulse_summary, "search": search_summary}
    except Exception as e:
        logger.exception("Reddit 采集整体失败: %s", e)
        record_status("reddit", success=False, error=str(e))
        raise


if __name__ == "__main__":
    import json as _json
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(_json.dumps(collect(per_sub_limit=10), indent=2, ensure_ascii=False))
