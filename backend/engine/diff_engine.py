"""Diff Engine：对比新旧快照，检测变动事件。

支持的事件类型（MVP 版）:
- rank_change         榜单排名变动（new_rank < old_rank）
- rank_crowned        登顶榜单 Top 1（P0）
- new_model_on_board  首次进入某榜单
- new_release         GitHub 新 release
- new_repo            GitHub 组织新增仓库（P0）
- star_surge          24h star 增量 > 1000（P1）

每个事件带 dedupe_key，写入 change_events 表（UNIQUE index 去重）。
"""
import json
import logging
from datetime import datetime, timezone

from backend.db import get_conn, record_status

logger = logging.getLogger(__name__)

P0_RANK_SURGE_JUMP = 5      # 排名上升超过 5 位算 P0 震动
STAR_SURGE_THRESHOLD = 1000


def _latest_two_snapshots(conn, source: str, category: str) -> tuple[list | None, list | None]:
    """返回 (最新快照, 上一次快照)，每个是 [{model, rank, score}] 按 rank 升序。

    **关键防御**：把 60 秒内的多个 `scraped_at` 合并成同一个 snapshot bucket。
    旧版 collector 用 CURRENT_TIMESTAMP 默认值，跨秒写入会拆成多个 scraped_at；
    一旦后半截（比如只含 rank 233-347）被取作 prev，Top 10 全员会被误判为首次上榜。
    （2026-04-24 lmarena text 10 条 P0 误报即此路径触发。）
    """
    recent = conn.execute(
        """
        SELECT DISTINCT scraped_at FROM leaderboard_snapshots
        WHERE source=? AND category=?
        ORDER BY scraped_at DESC LIMIT 50
        """,
        (source, category),
    ).fetchall()
    if not recent:
        return None, None

    # 贪心合并：相邻 timestamp 间隔 <= 60s 视为同一批 scrape。
    buckets: list[list[str]] = []
    cur: list[str] = []
    last_t: datetime | None = None
    for r in recent:
        t_str = r["scraped_at"]
        try:
            t = datetime.fromisoformat(t_str)
        except Exception:
            continue
        if last_t is None or abs((last_t - t).total_seconds()) <= 60:
            cur.append(t_str)
        else:
            buckets.append(cur)
            cur = [t_str]
            if len(buckets) >= 2:
                break
        last_t = t
    if cur and len(buckets) < 2:
        buckets.append(cur)
    if not buckets:
        return None, None

    def _load(bucket_times: list[str]) -> list[dict]:
        placeholders = ",".join("?" for _ in bucket_times)
        return [dict(r) for r in conn.execute(
            f"SELECT model_name, rank, score FROM leaderboard_snapshots "
            f"WHERE source=? AND category=? AND scraped_at IN ({placeholders}) "
            f"ORDER BY rank ASC",
            (source, category, *bucket_times),
        ).fetchall()]

    latest = _load(buckets[0])
    prev = _load(buckets[1]) if len(buckets) >= 2 else None
    return latest, prev


def _insert_event(
    conn,
    *,
    event_type: str,
    severity: str,
    source: str,
    title: str,
    detail: dict,
    model_name: str | None,
    dedupe_key: str,
) -> bool:
    week = datetime.now(timezone.utc).strftime("%Y-W%U")
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO change_events
          (event_type, severity, source, title, detail_json, model_name, week_number, dedupe_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type, severity, source, title,
            json.dumps(detail, ensure_ascii=False, default=str),
            model_name, week, dedupe_key,
        ),
    )
    return cur.rowcount > 0


def _diff_leaderboard(conn, source: str, category: str) -> int:
    latest, prev = _latest_two_snapshots(conn, source, category)
    if not latest or not prev:
        return 0

    prev_map = {r["model_name"]: r["rank"] for r in prev if r["rank"] is not None}
    new_events = 0

    for row in latest:
        model = row["model_name"]
        new_rank = row["rank"]
        if new_rank is None:
            continue
        old_rank = prev_map.get(model)

        if old_rank is None:
            # 首次上榜
            title = f"{model} 首次进入 {source} {category} 榜单（排名 {new_rank}）"
            key = f"new_on_board:{source}:{category}:{model}"
            # Top 10 首次进榜升 P0（值得邮件推送）。11+ 保持 P2 静默归档，不纳入 alert 流。
            # 2026-04-24 用户反馈：grok-imagine-image / reve-v1.5 / mai-image-2 进 text_to_image Top 10 没邮件。
            # bootstrap 首次扫描的 new_model_on_board 由 alert_manager 冷启动过滤拦截，不会批量误报。
            if _insert_event(conn,
                             event_type="new_model_on_board",
                             severity="P0" if new_rank <= 10 else "P2",
                             source=f"leaderboard:{source}",
                             title=title,
                             detail={"category": category, "new_rank": new_rank},
                             model_name=model,
                             dedupe_key=key):
                new_events += 1
            continue

        if new_rank == 1 and old_rank != 1:
            title = f"{model} 登顶 {source} {category} (上期 #{old_rank} → #1)"
            key = f"crowned:{source}:{category}:{model}:{_week_key()}"
            if _insert_event(conn,
                             event_type="rank_crowned",
                             severity="P0",
                             source=f"leaderboard:{source}",
                             title=title,
                             detail={"category": category, "old_rank": old_rank, "new_rank": 1},
                             model_name=model,
                             dedupe_key=key):
                new_events += 1
            continue

        jump = old_rank - new_rank  # 正数=上升
        if jump >= P0_RANK_SURGE_JUMP and new_rank <= 10:
            title = f"{model} 在 {source} {category} 跃升 {jump} 位 (#{old_rank} → #{new_rank})"
            key = f"surge:{source}:{category}:{model}:{_week_key()}"
            if _insert_event(conn,
                             event_type="rank_change",
                             severity="P1",
                             source=f"leaderboard:{source}",
                             title=title,
                             detail={"category": category, "old_rank": old_rank,
                                     "new_rank": new_rank, "jump": jump},
                             model_name=model,
                             dedupe_key=key):
                new_events += 1

    return new_events


def _diff_github_releases(conn) -> int:
    """检测过去 1 小时内新写入的 release。"""
    rows = conn.execute(
        """
        SELECT org, repo_name, tag_name, release_name, html_url, published_at, is_prerelease
        FROM github_releases
        WHERE scraped_at >= datetime('now', '-65 minutes')
        """
    ).fetchall()
    new_events = 0
    for r in rows:
        title = f"{r['org']}/{r['repo_name']} 发布 {r['tag_name']}"
        if r["release_name"]:
            title += f" — {r['release_name']}"
        key = f"release:{r['org']}:{r['repo_name']}:{r['tag_name']}"
        severity = "P2" if r["is_prerelease"] else "P1"
        if _insert_event(conn,
                         event_type="new_release",
                         severity=severity,
                         source="github",
                         title=title,
                         detail={
                             "org": r["org"], "repo": r["repo_name"],
                             "tag": r["tag_name"], "url": r["html_url"],
                             "published_at": r["published_at"],
                         },
                         model_name=r["repo_name"],
                         dedupe_key=key):
            new_events += 1
    return new_events


def _diff_github_new_repos(conn) -> int:
    """检测最近一次采集里第一次出现的 repo = 新建仓库。"""
    # 找出每个 (org, repo) 的首次快照时间
    rows = conn.execute(
        """
        SELECT org, repo_name, MIN(scraped_at) AS first_seen
        FROM github_snapshots
        GROUP BY org, repo_name
        HAVING first_seen >= datetime('now', '-65 minutes')
        """
    ).fetchall()
    new_events = 0
    for r in rows:
        title = f"{r['org']} 新增仓库 {r['repo_name']}"
        key = f"new_repo:{r['org']}:{r['repo_name']}"
        if _insert_event(conn,
                         event_type="new_repo",
                         severity="P0",
                         source="github",
                         title=title,
                         detail={"org": r["org"], "repo": r["repo_name"]},
                         model_name=r["repo_name"],
                         dedupe_key=key):
            new_events += 1
    return new_events


def _diff_github_star_surge(conn) -> int:
    """对比 24h 前和最新 star 数，差值 > 阈值触发 P1。"""
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
        SELECT l.org, l.repo_name, l.stars AS cur_stars, o.stars AS old_stars,
               (l.stars - o.stars) AS delta
        FROM latest l
        JOIN old o ON l.org = o.org AND l.repo_name = o.repo_name
        WHERE l.rn = 1 AND o.rn = 1 AND (l.stars - o.stars) >= ?
        """,
        (STAR_SURGE_THRESHOLD,),
    ).fetchall()
    new_events = 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for r in rows:
        title = f"{r['org']}/{r['repo_name']} 24h ⭐ +{r['delta']} (当前 {r['cur_stars']})"
        key = f"star_surge:{r['org']}:{r['repo_name']}:{today}"
        if _insert_event(conn,
                         event_type="star_surge",
                         severity="P1",
                         source="github",
                         title=title,
                         detail={"org": r["org"], "repo": r["repo_name"],
                                 "delta_24h": r["delta"], "current": r["cur_stars"]},
                         model_name=r["repo_name"],
                         dedupe_key=key):
            new_events += 1
    return new_events


def _diff_blog_posts(conn) -> int:
    """把"最近刚入库 且 发表时间在近 48h"的博客文章转成 P1 事件。

    两个条件都要有：
    - scraped_at 近期：保证是"这次运行新发现的"，避免反复触发
    - published_at 近 48h：冷启动时 RSS 会回吐几百条历史文章，
      只有发表时间近的才是真"新闻"，否则周一的周报里会出现 2023 的 OpenAI 旧文
    """
    rows = conn.execute(
        """
        SELECT source, url, title, summary, published_at, matched_model
        FROM blog_posts
        WHERE scraped_at >= datetime('now', '-65 minutes')
          AND published_at IS NOT NULL
          AND published_at >= datetime('now', '-48 hours')
        """
    ).fetchall()
    new_events = 0
    for r in rows:
        title = f"[{r['source']}] {r['title']}"
        key = f"blog:{r['url']}"
        if _insert_event(conn,
                         event_type="new_blog_post",
                         severity="P1",
                         source=f"blog:{r['source']}",
                         title=title,
                         detail={
                             "source": r["source"], "url": r["url"],
                             "published_at": r["published_at"],
                             "summary": (r["summary"] or "")[:400],
                         },
                         model_name=r["matched_model"],
                         dedupe_key=key):
            new_events += 1
    return new_events


def _week_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-W%U")


def run() -> dict:
    """运行一次全量 diff。返回各类型新事件计数。"""
    summary = {"leaderboard": 0, "new_release": 0, "new_repo": 0,
               "star_surge": 0, "new_blog_post": 0}
    try:
        with get_conn() as conn:
            # 榜单 diff：对每个 (source, category) 跑
            pairs = conn.execute(
                "SELECT DISTINCT source, category FROM leaderboard_snapshots"
            ).fetchall()
            for p in pairs:
                summary["leaderboard"] += _diff_leaderboard(conn, p["source"], p["category"])

            summary["new_release"] += _diff_github_releases(conn)
            summary["new_repo"] += _diff_github_new_repos(conn)
            summary["star_surge"] += _diff_github_star_surge(conn)
            summary["new_blog_post"] += _diff_blog_posts(conn)

        logger.info("Diff engine: %s", summary)
        record_status("diff_engine", success=True)
        return summary
    except Exception as e:
        logger.exception("Diff engine 失败: %s", e)
        record_status("diff_engine", success=False, error=str(e))
        raise


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(run())
