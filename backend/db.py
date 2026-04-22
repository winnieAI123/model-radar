"""SQLite 数据层。负责连接、建表、WAL 模式配置。
所有模块通过 get_conn() 获取连接，不要自己 sqlite3.connect()。
"""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from backend.utils import config

_lock = threading.Lock()
_initialized = False


SCHEMA_SQL = """
-- 榜单快照 (每次采集存一批)
CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    category    TEXT NOT NULL,
    model_name  TEXT NOT NULL,
    rank        INTEGER,
    score       REAL,
    extra_json  TEXT,
    scraped_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lb_model ON leaderboard_snapshots(model_name, source, category);
CREATE INDEX IF NOT EXISTS idx_lb_time  ON leaderboard_snapshots(scraped_at);
CREATE INDEX IF NOT EXISTS idx_lb_sc    ON leaderboard_snapshots(source, category, scraped_at);

-- GitHub 仓库快照
CREATE TABLE IF NOT EXISTS github_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org         TEXT NOT NULL,
    repo_name   TEXT NOT NULL,
    stars       INTEGER,
    forks       INTEGER,
    open_issues INTEGER,
    pushed_at   DATETIME,
    description TEXT,
    topics      TEXT,
    scraped_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_gh_repo ON github_snapshots(org, repo_name, scraped_at);

-- GitHub Release
CREATE TABLE IF NOT EXISTS github_releases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org           TEXT NOT NULL,
    repo_name     TEXT NOT NULL,
    tag_name      TEXT NOT NULL,
    release_name  TEXT,
    published_at  DATETIME,
    body_preview  TEXT,
    html_url      TEXT,
    is_prerelease INTEGER DEFAULT 0,
    scraped_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(org, repo_name, tag_name)
);

-- 变动事件
-- alert_status: pending(未处理) / sent(已真的发邮件) / suppressed(冷启动抑制 or 非 P0 无需邮件)
CREATE TABLE IF NOT EXISTS change_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT NOT NULL,
    severity     TEXT NOT NULL,
    source       TEXT NOT NULL,
    title        TEXT NOT NULL,
    detail_json  TEXT,
    model_name   TEXT,
    alerted      INTEGER DEFAULT 0,
    alerted_at   DATETIME,
    alert_status TEXT DEFAULT 'pending',
    week_number  TEXT,
    dedupe_key   TEXT,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_events_sev   ON change_events(severity, alerted, created_at);
CREATE INDEX IF NOT EXISTS idx_events_week  ON change_events(week_number);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedupe ON change_events(dedupe_key) WHERE dedupe_key IS NOT NULL;

-- 采集器健康状态
CREATE TABLE IF NOT EXISTS system_status (
    collector        TEXT PRIMARY KEY,
    last_run_at      DATETIME,
    last_success_at  DATETIME,
    last_error       TEXT,
    consecutive_fails INTEGER DEFAULT 0
);

-- 周报归档（Phase 3，周一 9:00 cron 生成）
CREATE TABLE IF NOT EXISTS weekly_reports (
    week_number   TEXT PRIMARY KEY,       -- ISO 周，如 2026-W17
    period_start  DATETIME,
    period_end    DATETIME,
    html          TEXT NOT NULL,
    stats_json    TEXT,                    -- 贴数 / LLM 是否用了 / 事件数等统计
    sent_at       DATETIME,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Reddit 帖子（社区声音，Phase 3 Community Digest 的输入）
CREATE TABLE IF NOT EXISTS reddit_posts (
    post_id       TEXT PRIMARY KEY,       -- Reddit 自己的 t3_xxx 的 xxx
    subreddit     TEXT NOT NULL,
    title         TEXT NOT NULL,
    author        TEXT,
    selftext      TEXT,                    -- 正文预览（截断 800 字）
    url           TEXT,                    -- permalink
    score         INTEGER DEFAULT 0,
    num_comments  INTEGER DEFAULT 0,
    created_utc   INTEGER,                 -- Reddit 给的秒级 UTC 时间戳
    matched_model TEXT,                    -- 归一化后的 canonical 模型名，未命中为 NULL
    matched_in    TEXT,                    -- 'title' / 'selftext' 指示命中位置
    scraped_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_reddit_model ON reddit_posts(matched_model, created_utc);
CREATE INDEX IF NOT EXISTS idx_reddit_sub   ON reddit_posts(subreddit, created_utc);
CREATE INDEX IF NOT EXISTS idx_reddit_time  ON reddit_posts(created_utc);

-- 候选模型别名（低置信度池）：slash 形态 / 噪声候选留在这里做 debug，不展示。
-- 高置信度的会进 learned_aliases，直接被 model_alias 加载。
CREATE TABLE IF NOT EXISTS pending_model_aliases (
    candidate       TEXT PRIMARY KEY,
    mention_count   INTEGER DEFAULT 0,
    total_score     INTEGER DEFAULT 0,
    first_seen      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sample_post_url TEXT,
    source          TEXT,
    status          TEXT DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_model_aliases(status, mention_count);

-- 自动学到的别名：alias_learner 从 Reddit 扫出来并通过置信度过滤的候选会自动进这里。
-- model_alias 在 import 时加载这张表，与手写 ALIAS_TABLE 合并，增量扩充 canonical 覆盖。
-- aliases_json: 自动生成的变体列表（小写），例如 ["gemma 4", "gemma-4", "gemma4"]。
CREATE TABLE IF NOT EXISTS learned_aliases (
    canonical    TEXT PRIMARY KEY,
    aliases_json TEXT NOT NULL,
    sample_url   TEXT,
    source       TEXT,
    added_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- HuggingFace 模型快照（趋势/下载量维度）。
-- 每次采集写一批快照（model_id, list_type, scraped_at 组合唯一）。
-- list_type='trending' / 'downloads' 区分排行榜类型，rank 是该榜的位次。
-- downloads 字段是 HF 返回的"总下载量"，历史对比要 JOIN 两次快照算 delta。
CREATE TABLE IF NOT EXISTS hf_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id       TEXT NOT NULL,           -- e.g. "meta-llama/Llama-4"
    author         TEXT,                    -- vendor/org 部分
    list_type      TEXT NOT NULL,           -- 'trending' / 'downloads'
    rank           INTEGER,                 -- 该 list 里的位次，1 为榜首
    downloads      INTEGER,
    likes          INTEGER,
    pipeline_tag   TEXT,                    -- e.g. "text-generation" / "image-to-video"
    tags_json      TEXT,                    -- 原始 tags 数组 JSON
    created_at     DATETIME,                -- HF 上的创建时间
    last_modified  DATETIME,
    matched_model  TEXT,                    -- 归一化后的 canonical（命中才填）
    scraped_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_hf_model     ON hf_snapshots(model_id, scraped_at);
CREATE INDEX IF NOT EXISTS idx_hf_list      ON hf_snapshots(list_type, scraped_at, rank);
CREATE INDEX IF NOT EXISTS idx_hf_matched   ON hf_snapshots(matched_model, scraped_at);

-- 厂商官方博客 RSS：OpenAI / Anthropic / Google / Meta 等。
-- url 做主键就能天然去重（同一条 post 反复抓也只会 INSERT OR IGNORE）。
-- matched_model: 尝试把 title+summary 匹配到 canonical（用于 diff_engine 生成 P1 事件）。
CREATE TABLE IF NOT EXISTS blog_posts (
    url           TEXT PRIMARY KEY,
    source        TEXT NOT NULL,              -- e.g. "openai" / "anthropic" / "google_ai" / "meta_ai"
    title         TEXT NOT NULL,
    summary       TEXT,                       -- RSS 给的 summary 截断 1000 字
    published_at  DATETIME,
    matched_model TEXT,
    scraped_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_blog_source  ON blog_posts(source, published_at);
CREATE INDEX IF NOT EXISTS idx_blog_matched ON blog_posts(matched_model, published_at);

-- OpenRouter 周榜：每周谁在被真调用（token 量口径，非下载/likes 这种社区声量）
-- 数据源：https://openrouter.ai/rankings 页面里 Next.js RSC payload，rankingType="week"
-- week_date 就是 OR 页面那个周点的 ISO 日期；同一 scrape 会写 Top 30 条。
-- change_pct: 周环比变化的小数（0.42 = +42%）；null 表示本周新进（UI 显示 NEW）。
CREATE TABLE IF NOT EXISTS openrouter_rankings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    week_date         DATE     NOT NULL,        -- 周榜日期（OR 自己标的）
    rank              INTEGER  NOT NULL,        -- 本周 Top 内的序号（1..30）
    model_permaslug   TEXT     NOT NULL,        -- e.g. "anthropic/claude-4.6-sonnet-20260217"
    author            TEXT,                     -- permaslug 前缀
    total_tokens      INTEGER  NOT NULL,        -- completion + prompt，便于排序
    completion_tokens INTEGER,
    prompt_tokens     INTEGER,
    reasoning_tokens  INTEGER,
    request_count     INTEGER,
    change_pct        REAL,                     -- NULL = new（本周新进榜）
    matched_model     TEXT,                     -- canonical 对齐（find_mentions）
    scraped_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_or_week    ON openrouter_rankings(week_date, rank);
CREATE INDEX IF NOT EXISTS idx_or_scraped ON openrouter_rankings(scraped_at);
CREATE INDEX IF NOT EXISTS idx_or_matched ON openrouter_rankings(matched_model, scraped_at);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量 schema 迁移。只加列、不动数据结构。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(change_events)").fetchall()}
    if "alert_status" not in cols:
        conn.execute("ALTER TABLE change_events ADD COLUMN alert_status TEXT DEFAULT 'pending'")
    # 回填：alerted=1 的老数据统一标 suppressed（真发过的也这么标，视觉不误导）。
    # 这条 UPDATE 是幂等的，每次启动执行一次成本可忽略。
    conn.execute(
        "UPDATE change_events SET alert_status='suppressed' "
        "WHERE alerted=1 AND (alert_status IS NULL OR alert_status='pending')"
    )


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    _migrate(conn)
    conn.commit()


def _ensure_dir() -> None:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn():
    """获取一个 sqlite3 连接。首次调用会初始化数据库。
    用法:
        with get_conn() as conn:
            conn.execute("SELECT ...")
    """
    global _initialized
    _ensure_dir()
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        if not _initialized:
            with _lock:
                if not _initialized:
                    _init_db(conn)
                    _initialized = True
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_status(collector: str, success: bool, error: str | None = None) -> None:
    """采集器每次跑完调一次，更新 system_status。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT consecutive_fails FROM system_status WHERE collector=?",
            (collector,),
        ).fetchone()
        fails = row["consecutive_fails"] if row else 0
        if success:
            conn.execute(
                """
                INSERT INTO system_status(collector, last_run_at, last_success_at, last_error, consecutive_fails)
                VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL, 0)
                ON CONFLICT(collector) DO UPDATE SET
                    last_run_at=CURRENT_TIMESTAMP,
                    last_success_at=CURRENT_TIMESTAMP,
                    last_error=NULL,
                    consecutive_fails=0
                """,
                (collector,),
            )
        else:
            conn.execute(
                """
                INSERT INTO system_status(collector, last_run_at, last_error, consecutive_fails)
                VALUES (?, CURRENT_TIMESTAMP, ?, 1)
                ON CONFLICT(collector) DO UPDATE SET
                    last_run_at=CURRENT_TIMESTAMP,
                    last_error=excluded.last_error,
                    consecutive_fails=consecutive_fails+1
                """,
                (collector, (error or "")[:500]),
            )


if __name__ == "__main__":
    with get_conn() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        print("Tables:", [t["name"] for t in tables])
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        print("journal_mode:", mode)
