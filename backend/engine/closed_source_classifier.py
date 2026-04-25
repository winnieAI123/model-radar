"""闭源模型发布判定器 · LLM 主判定。

GitHub 监控只覆盖国内 7 个开源 org（deepseek-ai/QwenLM/MoonshotAI/...），但
ChatGPT 5.5 / Gemini 3 Pro / Claude Opus 4.7 / 混元 / 豆包 / 文心 这些闭源大新闻
只能从厂商官博 + 中文公众号扫。

判定流程（一层）：
1. 拉近 N 天内 blog_posts 全量（含 wechat_*）
2. 一次 batch 把 (id, source, title, summary 前 200 字) 喂给 DeepSeek，
   prompt 强约束："只把厂商正式发布新模型/新版本/重大能力扩展的标题判 true，
   测评/讨论/教程/集成案例/招聘 一律 false"
3. LLM 返回结构化 JSON，按 confidence 降序裁掉 < 0.6 的低分项
4. LLM 失败 / 配额超限 → 降级正则双规则（_FALLBACK_*）兜底，与
   weekly_report._safe_call 的"挂了就跳过该块"风格一致

成本：周报每周 1 次，单次 ≤200 条标题 batch，DeepSeek 单调用 < $0.001。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from backend.db import get_conn
from backend.utils import llm_client

logger = logging.getLogger(__name__)


# ----------------- LLM 输入裁剪 / Prompt -----------------

# 单 batch 上限：标题 + 200 字 summary ≈ 100 tokens/entry，250 条占 25K 输入。
# DeepSeek-chat 上下文 64K，留足输出空间。实测 7d 全量博客 + 微信 ≈ 50-150 条。
_MAX_INPUT_ITEMS = 250
_SUMMARY_TRUNC = 200

_SYSTEM_PROMPT = """你是 AI 模型情报分析员。任务：判断给定的博客/公众号文章是不是"厂商正式发布新模型/新版本"的公告。

判 true 的情况（很严格）：
- 厂商首次公开新模型（"Introducing GPT-5"、"Claude Opus 4.7 发布"、"豆包 1.5 正式上线"）
- 已有模型的重大版本升级（"Gemini 3 Pro is now available"、"GLM-5 开源"）
- 新形态扩展（视频/语音/视觉版本首发，如"Sora 2 launches"、"Qwen3-VL 上线"）

判 false 的情况：
- 测评/对比/讨论文（"GPT-5 vs Claude 实测"、"使用 Gemini 一周后的感受"）
- 教程/最佳实践（"如何用 Claude 写代码"、"GPT-5 prompt 工程指南"）
- 集成/案例/插件（"Cursor 集成 Claude Sonnet"、"飞书接入豆包"）
- 招聘/融资/财报/会议/行业评论
- 模型泛指但无具体发布（"AI 模型在某领域的应用"）
- API 价格调整、SDK 升级、文档更新
- 不确定时一律判 false（宁缺毋滥）

返回严格的 JSON 对象，schema 如下：
{
  "results": [
    {
      "id": <input id, integer>,
      "is_release": <true|false>,
      "model_name": "<规范化的模型名，如 GPT-5 / Claude Opus 4.7 / 豆包 1.5；不是发布则填空字符串>",
      "vendor": "<OpenAI / Anthropic / Google / 字节豆包 / 腾讯混元 / 等；不是发布填空>",
      "confidence": <0.0-1.0 的浮点数，越接近 1 越确信>
    },
    ...
  ]
}

每条输入都要给一条结果，id 必须严格对应。confidence < 0.6 视为不确信，会被丢弃。"""


def _build_user_prompt(items: list[dict]) -> str:
    lines = ["请判断下列文章是否是「模型发布公告」。逐条返回。\n"]
    for it in items:
        sm = (it.get("summary") or "").strip().replace("\n", " ")
        if len(sm) > _SUMMARY_TRUNC:
            sm = sm[:_SUMMARY_TRUNC] + "…"
        lines.append(f"id={it['id']} | source={it['source']} | title={it['title']}")
        if sm:
            lines.append(f"  summary: {sm}")
        lines.append("")
    return "\n".join(lines)


# ----------------- 兜底正则（LLM 挂时降级用） -----------------

_FALLBACK_MODEL_RE = re.compile(
    r"(?:gpt[-\s]?[345](?:\.\d+)?|chatgpt[-\s]?\d?(?:\.\d+)?|"
    r"claude(?:\s+(?:opus|sonnet|haiku))?[-\s]?\d(?:\.\d+)?|"
    r"gemini[-\s]?\d(?:\.\d+)?|grok[-\s]?\d(?:\.\d+)?|llama[-\s]?\d(?:\.\d+)?|"
    r"sora[-\s]?\d|veo[-\s]?\d|imagen[-\s]?\d|nano[-\s]?banana|"
    r"hunyuan[-\s]?\w?\d?|混元|"
    r"ernie[-\s]?\d(?:\.\d+)?|文心一言|文心\s*\d|"
    r"doubao[-\s]?\d|豆包|"
    r"qwen[-\s]?\d(?:\.\d+)?(?:-(?:vl|coder|max|plus|turbo|omni))?|通义|"
    r"glm[-\s]?\d(?:\.\d+)?|chatglm[-\s]?\d|"
    r"step[-\s]?\d|阶跃星辰|"
    r"minimax[-\s]?\w?\d?|mimo[-\s]?\w?\d?|"
    r"kimi[-\s]?(?:k\d|\d(?:\.\d+)?)|"
    r"deepseek[-\s]?(?:v|r)\d(?:\.\d+)?|"
    r"yi[-\s]?\d(?:\.\d+)?|baichuan[-\s]?\d|"
    r"seedance|seedream|kling[-\s]?\d|可灵|pixverse[-\s]?v?\d)",
    re.IGNORECASE,
)
_FALLBACK_VERB_RE = re.compile(
    r"(?:\bintroduc(?:ing|e|es|ed)\b|\bannounc(?:ing|e|es|ed)\b|"
    r"\blaunch(?:ing|es|ed)?\b|\bunveil(?:s|ing|ed)?\b|\bdebut(?:s|ing|ed)?\b|"
    r"\bnow\s+(?:available|live|here|open)\b|\bavailable\s+(?:now|today)\b|"
    r"\brelease\s+of\b|\breleas(?:ing|ed)\b|"
    r"发布|推出|上线|登场|首发|正式发布|亮相|开放(?:使用|试用|内测)?|今日上线|开源)",
    re.IGNORECASE,
)


def _fallback_classify(items: list[dict]) -> list[dict]:
    """LLM 不可用时的兜底：模型名 + 发布动词 双规则同时命中才算。"""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for it in items:
        title = (it.get("title") or "").strip()
        m_model = _FALLBACK_MODEL_RE.search(title)
        if not m_model:
            continue
        if not _FALLBACK_VERB_RE.search(title):
            continue
        model = m_model.group(0).strip()
        key = (it["source"] or "", model.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "source": it["source"],
            "title": title,
            "url": it["url"],
            "model": model,
            "vendor": "",
            "confidence": 0.6,    # fallback 默认中等置信度
            "published_at": it.get("published_at"),
            "via": "regex_fallback",
        })
    return out


# ----------------- 主入口 -----------------

def _fetch_blog_posts(period_start_iso: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT rowid AS id, source, title, summary, url, published_at
            FROM blog_posts
            WHERE published_at >= ?
              AND title IS NOT NULL AND title != ''
            ORDER BY published_at DESC
            LIMIT ?
            """,
            (period_start_iso, _MAX_INPUT_ITEMS),
        ).fetchall()
    return [dict(r) for r in rows]


def _parse_llm_results(raw: dict | None) -> dict[int, dict]:
    """LLM 返回的 dict → {input_id: {is_release, model_name, vendor, confidence}}。

    LLM 偶尔会漏 id 或返回字符串数字 — 全部 best-effort 解析，无效项跳过。
    """
    if not raw or not isinstance(raw, dict):
        return {}
    arr = raw.get("results")
    if not isinstance(arr, list):
        return {}
    by_id: dict[int, dict] = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        try:
            iid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        by_id[iid] = {
            "is_release": bool(item.get("is_release")),
            "model_name": (item.get("model_name") or "").strip(),
            "vendor":     (item.get("vendor") or "").strip(),
            "confidence": float(item.get("confidence") or 0.0),
        }
    return by_id


def generate(days: int = 7, min_confidence: float = 0.6) -> list[dict]:
    """主入口：返回近 days 天的"闭源模型发布"列表。

    每条形状：{source, title, url, model, vendor, confidence, published_at, via}
    via 字段用于诊断：'llm' 表示 DeepSeek 判 true，'regex_fallback' 表示 LLM 挂了走正则。

    LLM 失败 → 走正则兜底；正则也无命中则返回空列表。失败不抛异常。
    """
    period_start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    items = _fetch_blog_posts(period_start)
    if not items:
        return []

    logger.info("[ClosedSrc] 待判定 %d 条 blog_posts (近 %dd)", len(items), days)

    # LLM 主判定
    user_prompt = _build_user_prompt(items)
    try:
        raw = llm_client.chat_json(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=6000,
        )
    except Exception as e:
        logger.exception("[ClosedSrc] LLM 调用异常: %s", e)
        raw = None

    by_id = _parse_llm_results(raw)
    if not by_id:
        logger.warning("[ClosedSrc] LLM 不可用，降级到正则兜底")
        return _fallback_classify(items)

    # 合并 + 过滤：只留 is_release=true 且 confidence ≥ min_confidence 的
    out: list[dict] = []
    for it in items:
        verdict = by_id.get(it["id"])
        if not verdict or not verdict["is_release"]:
            continue
        if verdict["confidence"] < min_confidence:
            continue
        out.append({
            "source":       it["source"],
            "title":        (it["title"] or "").strip(),
            "url":          it["url"],
            "model":        verdict["model_name"] or "",
            "vendor":       verdict["vendor"]     or "",
            "confidence":   verdict["confidence"],
            "published_at": it.get("published_at"),
            "via":          "llm",
        })

    # (vendor, model_name 小写) 去重 — LLM 偶尔会把同一模型在不同博文里都判 true
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for o in out:
        key = (o["vendor"].lower(), o["model"].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(o)

    deduped.sort(key=lambda x: (-x["confidence"], x.get("published_at") or ""), reverse=False)
    deduped.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    logger.info("[ClosedSrc] LLM 判出 %d 条发布（候选 %d/%d）",
                len(deduped), len(out), len(items))
    return deduped


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    items = generate(days=7)
    print(f"\n=== {len(items)} closed-source releases (近 7d) ===\n")
    for it in items:
        print(f"  [{it['vendor']:14s}] {it['model']:20s} (conf={it['confidence']:.2f}, via={it['via']})")
        print(f"    {it['title']}")
        print(f"    {it['url']}\n")
