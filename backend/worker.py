"""ModelRadar 主调度进程（入口）。

本文件就是 Railway 的 startCommand 目标。启动后：
1. 初始化日志
2. 可选冷启动：立即跑一轮所有 collector
3. 用 APScheduler 的 BlockingScheduler 按 env 里的间隔循环跑
4. 收 SIGTERM / Ctrl-C 后优雅退出
"""
import logging
import signal
import sys
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from backend.utils import config
from backend.db import get_conn  # 触发建表

from backend.collectors import leaderboard as leaderboard_collector
from backend.collectors import github_monitor
from backend.collectors import reddit as reddit_collector
from backend.engine import diff_engine
from backend.engine import alert_manager
from backend.engine import weekly_report


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"


def _safe(fn, name: str):
    """包一层：job 异常不杀调度器。"""
    def wrapped():
        logger = logging.getLogger("worker")
        try:
            logger.info("▶ 启动任务 %s", name)
            fn()
            logger.info("✔ 完成任务 %s", name)
        except Exception as e:
            logger.exception("✘ 任务 %s 异常: %s", name, e)
    wrapped.__name__ = f"safe_{name}"
    return wrapped


def run_leaderboard():
    leaderboard_collector.collect()


def run_github():
    if not config.GITHUB_TOKEN:
        logging.getLogger("worker").warning(
            "GITHUB_TOKEN 未设置，跳过 GitHub 采集。请到 .env 里配置。"
        )
        return
    github_monitor.collect()


def run_diff():
    diff_engine.run()


def run_p0():
    alert_manager.send_p0_alerts()


def run_reddit():
    reddit_collector.collect()


def run_weekly_report():
    weekly_report.generate_and_send(days=7, dry_run=False)


def cold_start():
    logger = logging.getLogger("worker")
    logger.info("=" * 60)
    logger.info("冷启动：依次跑 leaderboard → github → reddit → diff → p0_alert")
    logger.info("=" * 60)
    _safe(run_leaderboard, "leaderboard")()
    _safe(run_github, "github")()
    _safe(run_reddit, "reddit")()
    _safe(run_diff, "diff")()
    _safe(run_p0, "p0_alert")()


def main():
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    logger = logging.getLogger("worker")

    # 触发建表
    with get_conn() as conn:
        conn.execute("SELECT 1")

    logger.info("ModelRadar worker 启动 @ %s", datetime.now().isoformat(timespec="seconds"))
    logger.info("收件人: %s", config.EMAIL_RECEIVERS or "未配置")
    logger.info("GITHUB_TOKEN: %s", "已配置" if config.GITHUB_TOKEN else "未配置")

    if config.COLD_START_ON_BOOT:
        cold_start()
    else:
        logger.info("COLD_START_ON_BOOT=false，跳过冷启动")

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(_safe(run_leaderboard, "leaderboard"),
                      IntervalTrigger(minutes=config.INTERVAL_LEADERBOARD_MIN),
                      id="leaderboard", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(run_github, "github"),
                      IntervalTrigger(minutes=config.INTERVAL_GITHUB_MIN),
                      id="github", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(run_diff, "diff"),
                      IntervalTrigger(minutes=config.INTERVAL_DIFF_MIN),
                      id="diff", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(run_p0, "p0_alert"),
                      IntervalTrigger(minutes=config.INTERVAL_P0_ALERT_MIN),
                      id="p0_alert", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(run_reddit, "reddit"),
                      IntervalTrigger(minutes=config.INTERVAL_REDDIT_MIN),
                      id="reddit", max_instances=1, coalesce=True)
    # 周五 19:00 Asia/Shanghai 发周报
    scheduler.add_job(_safe(run_weekly_report, "weekly_report"),
                      CronTrigger(day_of_week="fri", hour=19, minute=0,
                                  timezone="Asia/Shanghai"),
                      id="weekly_report", max_instances=1, coalesce=True)

    def _shutdown(signum, frame):
        logger.info("收到信号 %s，优雅退出...", signum)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info(
        "调度器启动，间隔: leaderboard=%dmin github=%dmin diff=%dmin p0=%dmin reddit=%dmin 周报=周五 19:00",
        config.INTERVAL_LEADERBOARD_MIN, config.INTERVAL_GITHUB_MIN,
        config.INTERVAL_DIFF_MIN, config.INTERVAL_P0_ALERT_MIN,
        config.INTERVAL_REDDIT_MIN,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
