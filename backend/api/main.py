"""FastAPI 统一入口 · Phase 2。

架构决策：API + Worker 合并到单 service。
- uvicorn 起 FastAPI 主进程 (asyncio)
- APScheduler BackgroundScheduler (线程池) 在 lifespan 启动时注册 job
- 所有采集/分析 job 是同步代码，在独立线程跑，不阻塞 API 事件循环

这样 Railway 只需一个 service，节省 Hobby 资源；日后如果 job 变重，
可以把这个文件拆成 api/main.py + worker.py 分 service 部署。
"""
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api.routes import router as api_router
from backend.collectors import github_monitor, leaderboard as leaderboard_collector
from backend.collectors import reddit as reddit_collector
from backend.collectors import huggingface as hf_collector
from backend.collectors import blog_rss as blog_collector
from backend.collectors import openrouter as openrouter_collector
from backend.collectors import wechat_rss as wechat_collector
from backend.db import get_conn
from backend.engine import alert_manager, diff_engine, heat_scorer, weekly_report, mini_digest
from backend.utils import config

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
logger = logging.getLogger("modelradar")


def _safe(fn, name: str):
    def wrapped():
        t0 = time.monotonic()
        try:
            logger.info("▶ %s", name)
            fn()
            elapsed = time.monotonic() - t0
            logger.info("✔ %s elapsed=%.1fs", name, elapsed)
            if elapsed > 60:
                logger.warning("⏱ slow job=%s elapsed=%.1fs（>60s 阻塞了串行执行器）",
                               name, elapsed)
        except Exception as e:
            elapsed = time.monotonic() - t0
            logger.exception("✘ %s 异常 elapsed=%.1fs: %s", name, elapsed, e)
    wrapped.__name__ = f"safe_{name}"
    return wrapped


def _run_leaderboard(): leaderboard_collector.collect()


def _run_github():
    if not config.GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN 未设置，跳过 GitHub 采集")
        return
    github_monitor.collect()


def _run_diff():   diff_engine.run()
def _run_alerts(): alert_manager.send_p0_alerts()
def _run_heat():   heat_scorer.run()
def _run_reddit(): reddit_collector.collect()
def _run_hf():     hf_collector.collect()
def _run_blog():   blog_collector.collect()
def _run_openrouter(): openrouter_collector.collect()
def _run_wechat(): wechat_collector.collect()
def _run_mini_digest(): mini_digest.run_all()
def _run_weekly():
    # 发周报前必须刷一次"发布消息"类数据源（github release / 厂商博客 / 公众号），
    # 再跑一次 diff，让新抓到的 release/blog 转成 change_events 进入周报 § I/II。
    # 不前置 leaderboard/reddit/heat/mini_digest —— 前者 6min 间隔已够新，后三者 LLM 调用
    # 过慢会把周报生成拖到 5-10 分钟以上。实测单次刷新约 1-2 分钟。
    # （2026-04-24 问题：19:00 周报没写 DeepSeek V4 发布 → 原因：_run_weekly 不刷数据）
    for name, fn in [("github", _run_github), ("blog_rss", _run_blog),
                     ("wechat_rss", _run_wechat)]:
        try:
            fn()
        except Exception:
            logger.exception("[WeeklyPrefetch] %s 失败，继续发周报", name)
    try:
        _run_diff()
    except Exception:
        logger.exception("[WeeklyPrefetch] diff 失败，继续发周报")
    weekly_report.generate_and_send(days=7, dry_run=False)


def _cold_start():
    logger.info("=" * 60)
    logger.info("冷启动：leaderboard → github → hf → blog → openrouter → wechat → reddit → diff → p0 → heat → mini_digest")
    logger.info("=" * 60)
    _safe(_run_leaderboard, "leaderboard")()
    _safe(_run_github, "github")()
    _safe(_run_hf, "huggingface")()
    _safe(_run_blog, "blog_rss")()
    _safe(_run_openrouter, "openrouter")()
    _safe(_run_wechat, "wechat_rss")()
    # reddit 必须加入冷启动：interval=360min，每次 redeploy 会把 APScheduler 计时器归零，
    # 没有冷启动触发的话，频繁部署期评论表会长期空置，社区声音总结退化到只啃标题。
    _safe(_run_reddit, "reddit")()
    _safe(_run_diff, "diff")()
    _safe(_run_alerts, "p0_alert")()
    _safe(_run_heat, "heat")()
    # mini_digest 接在 reddit 后面：确保 Dashboard 社区声音/热议用上最新评论，
    # 否则周中 Dashboard 会一直读 12h 前的缓存（尤其 redeploy 重置 interval 后）。
    _safe(_run_mini_digest, "mini_digest")()


# 单线程执行器：所有 job 串行跑，避免 SQLite 写锁冲突（WAL 只允许一个写者）。
# 之前多个 job 同 tick 撞车导致 mini_digest / github 偶发 "database is locked"。
#
# job_defaults:
#   misfire_grace_time=600 — APScheduler 默认是 1 秒。在串行执行器下，前一个 job
#       跑 25 秒（github）就足以把后面所有 tick 静默丢弃。改成 10 分钟容忍，
#       让 coalesce 能真正接住错过的 tick。
#       根因记录：2026-04-25 看到 7 个 job WARN "missed by 25s" → 全部 drop →
#       6h/12h job 整天没跑（diff_engine/blog/hf/heat/reddit/mini_digest）。
#   coalesce=True / max_instances=1 — 仍每个 job 单独显式设了，这里给个保险。
scheduler = BackgroundScheduler(
    timezone="Asia/Shanghai",
    executors={"default": ThreadPoolExecutor(max_workers=1)},
    job_defaults={
        "misfire_grace_time": 600,
        "coalesce": True,
        "max_instances": 1,
    },
)


def _on_job_event(event):
    """把 APScheduler 的 job 生命周期事件抬到应用层日志。

    EVENT_JOB_MISSED 默认只在 apscheduler.scheduler logger 打 WARN，容易淹没在
    Railway log 里。这里强制打 ERROR + 标注实际偏差秒数，让以后再丢 fire 一眼能看见。
    """
    if event.code == EVENT_JOB_MISSED:
        delay = (datetime.now(event.scheduled_run_time.tzinfo)
                 - event.scheduled_run_time).total_seconds()
        logger.error("⚠ MISSED job=%s scheduled=%s delay=%.1fs（被丢弃）",
                     event.job_id, event.scheduled_run_time, delay)
    elif event.code == EVENT_JOB_ERROR:
        logger.error("✘ ERROR job=%s exception=%s",
                     event.job_id, event.exception)


def _register_jobs():
    # 错峰首次运行：APScheduler IntervalTrigger 不传 start_date 时，所有 job 的
    # 起算点都是注册当下（同一秒），导致每过 6/12h 一批 job 同时到期。串行执行器下
    # 后到的 job tick 会被压住直到前面跑完，超过 misfire_grace_time 就被丢弃。
    # 这里给每个 job 加 30s 递增偏移，让首次运行时间错开 → 后续每个 interval
    # 周期天然保持错峰。
    now = datetime.now(scheduler.timezone)
    def first_run(offset_s: int) -> datetime:
        return now + timedelta(seconds=offset_s)

    specs = [
        ("leaderboard", _run_leaderboard, config.INTERVAL_LEADERBOARD_MIN, 30),
        ("github",      _run_github,      config.INTERVAL_GITHUB_MIN,      60),
        ("diff",        _run_diff,        config.INTERVAL_DIFF_MIN,        90),
        ("p0_alert",    _run_alerts,      config.INTERVAL_P0_ALERT_MIN,    120),
        ("heat",        _run_heat,        config.INTERVAL_HEAT_MIN,        150),
        ("reddit",      _run_reddit,      config.INTERVAL_REDDIT_MIN,      180),
        ("huggingface", _run_hf,          config.INTERVAL_HF_MIN,          210),
        ("blog_rss",    _run_blog,        config.INTERVAL_BLOG_MIN,        240),
        ("openrouter",  _run_openrouter,  config.INTERVAL_OPENROUTER_MIN,  270),
        ("wechat_rss",  _run_wechat,      config.INTERVAL_WECHAT_MIN,      300),
        ("mini_digest", _run_mini_digest, config.INTERVAL_MINI_DIGEST_MIN, 330),
    ]
    for job_id, fn, interval_min, offset_s in specs:
        scheduler.add_job(
            _safe(fn, job_id),
            IntervalTrigger(minutes=interval_min),
            id=job_id,
            next_run_time=first_run(offset_s),
        )
    # 周五 19:00 Asia/Shanghai 发周报
    scheduler.add_job(_safe(_run_weekly, "weekly_report"),
                      CronTrigger(day_of_week="fri", hour=19, minute=0,
                                  timezone="Asia/Shanghai"),
                      id="weekly_report")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    with get_conn() as conn:
        conn.execute("SELECT 1")  # 触发建表

    if not config.DASHBOARD_PASS:
        logger.warning("⚠ DASHBOARD_PASS 为空，Basic Auth 已禁用（仅本地开发可用）")

    if config.COLD_START_ON_BOOT:
        # 冷启动在后台线程跑，不阻塞 uvicorn 启动
        t = threading.Thread(target=_cold_start, name="cold_start", daemon=True)
        t.start()
    else:
        logger.info("COLD_START_ON_BOOT=false，跳过冷启动")

    _register_jobs()
    scheduler.add_listener(_on_job_event, EVENT_JOB_MISSED | EVENT_JOB_ERROR)
    scheduler.start()
    logger.info(
        "调度器启动 · leaderboard=%dmin github=%dmin diff=%dmin p0=%dmin heat=%dmin "
        "reddit=%dmin hf=%dmin blog=%dmin openrouter=%dmin wechat=%dmin mini_digest=%dmin 周报=周五19:00",
        config.INTERVAL_LEADERBOARD_MIN, config.INTERVAL_GITHUB_MIN,
        config.INTERVAL_DIFF_MIN, config.INTERVAL_P0_ALERT_MIN,
        config.INTERVAL_HEAT_MIN, config.INTERVAL_REDDIT_MIN,
        config.INTERVAL_HF_MIN, config.INTERVAL_BLOG_MIN,
        config.INTERVAL_OPENROUTER_MIN, config.INTERVAL_WECHAT_MIN,
        config.INTERVAL_MINI_DIGEST_MIN,
    )
    try:
        yield
    finally:
        logger.info("关闭调度器...")
        scheduler.shutdown(wait=False)


app = FastAPI(title="ModelRadar", version="0.2.0", lifespan=lifespan)
app.include_router(api_router)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# 静态资源 + 首页
# 首页挂 require_auth：Chrome/Safari 对 fetch() 的 401 不再弹 Basic Auth 对话框，
# 只有主文档本身 401 才弹。如果 / 不鉴权，用户会看到"加载中..."永远转，因为 /api/*
# fetch 都 silently 401。/static/* 仍放行（CSS/JS 无秘密），浏览器拿到 / 的 creds 后
# 后续 /api/* 自动带 Authorization。
if config.FRONTEND_DIR.exists():
    from backend.api.auth import require_auth as _require_auth
    from fastapi import Depends

    app.mount("/static", StaticFiles(directory=str(config.FRONTEND_DIR)), name="static")

    @app.get("/")
    def index(_: str = Depends(_require_auth)):
        return FileResponse(config.FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.api.main:app",
        host="0.0.0.0",
        port=config.PORT,
        log_level="info",
    )
