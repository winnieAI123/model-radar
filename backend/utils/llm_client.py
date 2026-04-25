"""DeepSeek LLM 客户端。

DeepSeek 兼容 OpenAI Chat Completion 协议，所以这里直接用 requests 打
https://api.deepseek.com/chat/completions，避免引入 openai SDK。

两个公开方法：
- chat(messages, ...) -> str | None       # 取 choices[0].message.content
- chat_json(messages, ...) -> dict | None # 要求返回 JSON 对象

失败一律返回 None，让调用方降级到纯模板。周报/digest 如果跑不成 LLM，
还能发模板邮件；挂在 LLM 上等于全链路瘫痪，不值得。
"""
import json
import logging
import re
import time

import requests

from backend.utils import config

logger = logging.getLogger(__name__)

API_URL = "https://api.deepseek.com/chat/completions"
# 2026-04-25 切换：deepseek-chat / deepseek-reasoner 将在 2026-07-24 弃用
# （官方公告：https://api-docs.deepseek.com）。统一升级到 V4 系列：
#   deepseek-v4-flash  — 大上下文 + 低延迟，覆盖周报全部 batch 分类/摘要任务
#   deepseek-v4-pro    — 推理更强但更贵，目前未用；如某块 LLM 输出质量降级再切换
# 上下文窗口达到百万级（适合 closed_source_classifier 一次塞 ≤200 条标题 + 摘要）。
DEFAULT_MODEL = "deepseek-v4-flash"
JSON_MODEL = "deepseek-v4-flash"

# 超时策略：连接 10s，读 90s（LLM 输出长文要等）
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 90

# 全局去 emoji：DeepSeek 偶尔在中文文本里插 🔥 / 📊 / ✅ 等 emoji 强调，
# 用户希望周报保持排版克制（2026-04-24 反馈），统一在 LLM 输出口剥掉。
# 只匹配真 emoji/象形符号/dingbats/flag，不碰 → ← 这类箭头（我们自己的模板在用）。
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"    # emoticons
    "\U0001F300-\U0001F5FF"    # symbols & pictographs (含 🔥 📊 🌟)
    "\U0001F680-\U0001F6FF"    # transport & map symbols
    "\U0001F700-\U0001F77F"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"    # 🧠 🤖 emoji
    "\U0001FA00-\U0001FAFF"
    "\U00002700-\U000027BF"    # dingbats ✂ ✅ ❌
    "\U0001F1E0-\U0001F1FF"    # flags
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(s: str | None) -> str | None:
    if not s:
        return s
    return _EMOJI_RE.sub("", s)


class LLMError(RuntimeError):
    pass


def _post(payload: dict, max_retries: int = 2) -> dict | None:
    """打 API，返回 parsed json 或 None。不抛，让调用方决定降级。"""
    if not config.DEEPSEEK_API_KEY:
        logger.warning("DEEPSEEK_API_KEY 未配置，跳过 LLM 调用")
        return None

    headers = {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type":  "application/json",
    }

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                API_URL, headers=headers, json=payload,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            if resp.status_code == 429:
                wait = 2 ** attempt * 5
                logger.warning("[LLM] 429 限流，sleep %ds", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                # 服务端错误：短退避后再试
                last_err = LLMError(f"{resp.status_code} {resp.text[:200]}")
                time.sleep(3 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            logger.warning("[LLM] 网络错误 (%d/%d): %s", attempt + 1, max_retries + 1, e)
            time.sleep(3 * (attempt + 1))
        except Exception as e:
            # 其它错误（如 4xx）直接返回，没必要重试
            logger.error("[LLM] 调用失败: %s", e)
            return None

    logger.error("[LLM] 重试用尽，最后错误: %s", last_err)
    return None


def chat(
    messages: list[dict],
    model: str = DEFAULT_MODEL,
    temperature: float = 0.3,
    max_tokens: int = 2048,
) -> str | None:
    """普通文本对话。成功返回 content 字符串，失败返回 None。"""
    data = _post({
        "model":       model,
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  max_tokens,
        "stream":      False,
    })
    if not data:
        return None
    try:
        return _strip_emoji(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError) as e:
        logger.error("[LLM] 响应格式异常: %s, raw=%s", e, str(data)[:300])
        return None


def chat_json(
    messages: list[dict],
    model: str = JSON_MODEL,
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> dict | None:
    """要求 LLM 返回 JSON 对象。成功返回 dict，失败返回 None。"""
    data = _post({
        "model":           model,
        "messages":        messages,
        "temperature":     temperature,
        "max_tokens":      max_tokens,
        "response_format": {"type": "json_object"},
        "stream":          False,
    })
    if not data:
        return None
    try:
        content = _strip_emoji(data["choices"][0]["message"]["content"])
        return json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.error("[LLM] JSON 解析失败: %s, raw=%s", e, str(data)[:300])
        return None


def ping() -> bool:
    """快速测试 API key 是否可用。"""
    r = chat(
        [{"role": "user", "content": "ping. reply 'pong' only."}],
        max_tokens=10, temperature=0.0,
    )
    return bool(r and "pong" in r.lower())


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("ping:", ping())
