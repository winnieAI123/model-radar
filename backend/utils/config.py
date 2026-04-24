"""环境变量读取层。所有模块通过 config 访问配置，不直接读 os.environ。"""
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass


def _get(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        raise RuntimeError(f"环境变量 {key} 未设置，请检查 .env 文件")
    return val or ""


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except ValueError:
        return default


def _get_bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, str(default)).lower() in ("1", "true", "yes", "on")


GITHUB_TOKEN = _get("GITHUB_TOKEN")

EMAIL_SENDER = _get("EMAIL_SENDER")
EMAIL_PASSWORD = _get("EMAIL_PASSWORD")  # 旧 SMTP 密码，Brevo 时代不再使用
EMAIL_RECEIVERS = [e.strip() for e in _get("EMAIL_RECEIVERS", "").split(",") if e.strip()]
BREVO_API_KEY = _get("BREVO_API_KEY")  # Brevo HTTP API key，必填（PaaS 屏蔽 SMTP 端口）

DEEPSEEK_API_KEY = _get("DEEPSEEK_API_KEY")
HF_TOKEN = _get("HF_TOKEN")

DB_PATH = _get("DB_PATH", str(_ROOT / "data" / "model_radar.db"))

INTERVAL_LEADERBOARD_MIN = _get_int("INTERVAL_LEADERBOARD_MIN", 1440)
INTERVAL_GITHUB_MIN = _get_int("INTERVAL_GITHUB_MIN", 60)
INTERVAL_DIFF_MIN = _get_int("INTERVAL_DIFF_MIN", 60)
INTERVAL_P0_ALERT_MIN = _get_int("INTERVAL_P0_ALERT_MIN", 30)
INTERVAL_REDDIT_MIN = _get_int("INTERVAL_REDDIT_MIN", 360)   # Reddit 6h 一次够用
INTERVAL_HF_MIN = _get_int("INTERVAL_HF_MIN", 240)           # HuggingFace 榜单 4h 一次
INTERVAL_BLOG_MIN = _get_int("INTERVAL_BLOG_MIN", 60)        # 厂商博客 1h 一次（求快）
INTERVAL_OPENROUTER_MIN = _get_int("INTERVAL_OPENROUTER_MIN", 10080)  # OpenRouter 周榜 7d 一次（OR 自己也是周粒度）
INTERVAL_WECHAT_MIN = _get_int("INTERVAL_WECHAT_MIN", 1440)         # 微信公众号 1 天 1 次
INTERVAL_MINI_DIGEST_MIN = _get_int("INTERVAL_MINI_DIGEST_MIN", 720)  # Dashboard 聚合缓存 12h 一次

# Reddit
REDDIT_PROXY = _get("REDDIT_PROXY", "")   # 本地调试可填 http://127.0.0.1:7890；Railway 留空
REDDIT_SUBS = _get("REDDIT_SUBS", "")     # 逗号分隔，留空走 DEFAULT_SUBREDDITS

COLD_START_ON_BOOT = _get_bool("COLD_START_ON_BOOT", True)

# Dashboard（Phase 2）
DASHBOARD_USER = _get("DASHBOARD_USER", "radar")
DASHBOARD_PASS = _get("DASHBOARD_PASS", "")  # 空 = 禁用 Basic Auth（仅本地用）
PORT = _get_int("PORT", 8000)
INTERVAL_HEAT_MIN = _get_int("INTERVAL_HEAT_MIN", 180)  # 热度评分每 3h 算一次

PROJECT_ROOT = _ROOT
CONFIG_DIR = _ROOT / "backend" / "config"
FRONTEND_DIR = _ROOT / "frontend"
