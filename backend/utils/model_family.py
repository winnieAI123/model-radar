"""社区声音显示层 · 家族聚合。

只在 Dashboard 的"社区声音" panel 和周报 §I Reddit 行用：把 Qwen3 / Qwen3-Coder /
Qwen3-VL 等同家族 canonical 合成一张卡，卡里的每条观点引用自然会标具体版本号。

严格约束：不动 canonical 粒度。榜单、热度、diff_engine 全部继续按 canonical 工作。
只有 digest_cache 的写入（mini_digest + weekly_report 双写）和周报 §I 概览行
先过一次 rollup，让概览维度看到 family。
"""
from __future__ import annotations

from backend.utils.model_alias import normalize as _normalize_alias

# canonical → family 标签。没在表里的 canonical 保持原样（单成员 family）。
MODEL_FAMILY: dict[str, str] = {
    # OpenAI LLM
    "GPT-5": "GPT", "GPT-5 mini": "GPT",
    "GPT-4o": "GPT", "GPT-4o mini": "GPT",
    "o1": "GPT", "o3": "GPT",
    # Anthropic
    "Claude Opus 4.7": "Claude", "Claude Opus 4.6": "Claude",
    "Claude Sonnet 4.6": "Claude", "Claude Sonnet 4.5": "Claude",
    "Claude Haiku 4.5": "Claude", "Claude 3.5 Sonnet": "Claude",
    # Google LLM
    "Gemini 3 Pro": "Gemini", "Gemini 3.1 Flash": "Gemini",
    "Gemini 2.5 Pro": "Gemini", "Gemini 2.5 Flash": "Gemini",
    # DeepSeek
    "DeepSeek-V3": "DeepSeek", "DeepSeek-V3.1": "DeepSeek",
    "DeepSeek-V3.2": "DeepSeek", "DeepSeek-R1": "DeepSeek",
    "DeepSeek-V4": "DeepSeek",
    # Qwen
    "Qwen3": "Qwen", "Qwen3-Coder": "Qwen", "Qwen3-VL": "Qwen",
    "Qwen2.5-72B": "Qwen", "Qwen2.5": "Qwen",
    # Moonshot
    "Kimi-K2": "Kimi", "Kimi-K1.5": "Kimi", "Kimi-CLI": "Kimi",
    # Zhipu
    "GLM-4.7": "GLM", "GLM-5": "GLM", "GLM-5.1": "GLM",
    # MiniMax
    "MiniMax-M1": "MiniMax", "MiniMax-M2": "MiniMax", "MiniMax-Text-01": "MiniMax",
    # Doubao
    "Doubao-Seed-1.8": "Doubao", "Doubao-Seed-2.0": "Doubao",
    # Meta
    "Llama-3.3-70B": "Llama", "Llama-4": "Llama", "Llama-3.1-405B": "Llama",
    # Mistral
    "Mistral Large": "Mistral", "Mistral Small": "Mistral",
    # xAI（Grok Imagine 独立，是视频线）
    "Grok-4": "Grok", "Grok-3": "Grok",
    # Step（Step-Audio 独立，是音频线）
    "Step-2": "Step",
    # 视频线
    "Veo 3.1": "Veo", "Veo 3": "Veo",
    "Dreamina Seedance 2.0": "Seedance", "Seedance v1.5 Pro": "Seedance",
    "Kling 3.0": "Kling", "Kling 2.5 Turbo": "Kling",
    "PixVerse V6": "PixVerse", "PixVerse V5.6": "PixVerse", "PixVerse V5": "PixVerse",
    # 图像线
    "Sora 2": "Sora", "Sora 2 HD": "Sora",
    "GPT Image 1": "GPT Image", "GPT Image 1.5": "GPT Image",
}


def get_family(canonical: str) -> str:
    """canonical → family 标签。没映射的返回原样（作为单成员 family）。

    自愈：如果传入的 raw 是别名（如 reddit_posts.matched_model 里残留的 "qwen3.6"
    或 "qwen-code" 这种 alias_table 后加进去的旧名），先过一遍 normalize() 拿到
    真 canonical，再查 family。这样旧数据不需要 backfill 也能正确归并。
    """
    if not canonical:
        return canonical
    # 第一次直接查 — 多数情况已经是 canonical（"Qwen3", "Claude Opus 4.7"）
    if canonical in MODEL_FAMILY:
        return MODEL_FAMILY[canonical]
    # 兜底：可能是 alias，归一化再查
    norm = _normalize_alias(canonical)
    if norm and norm in MODEL_FAMILY:
        return MODEL_FAMILY[norm]
    # 仍找不到：保持原样，作为单成员 family
    return norm or canonical


def rollup_opinions(payload: dict, opinions_per_card: int = 4) -> dict:
    """把 reddit_opinions.generate() 的 per-canonical payload 合成 per-family。

    输入 payload 形状：{"models": [{"model","post_count","opinions","used_llm"}], "fallback_md"}
    输出同形状，但 model 字段是 family，opinions 合并去重后截取 Top N。
    观点顺序：保留原 per-canonical 顺序拼接；跨 canonical 通过 url 去重。
    """
    if not payload or not isinstance(payload, dict):
        return payload

    models = payload.get("models") or []
    if not models:
        return payload

    # 按 family 聚合
    buckets: dict[str, dict] = {}
    order: list[str] = []  # 保留首次出现顺序（top_models_by_posts 已按热度排好）
    for entry in models:
        if not isinstance(entry, dict):
            continue
        canon = entry.get("model") or ""
        family = get_family(canon)
        if family not in buckets:
            buckets[family] = {
                "model": family,
                "canonicals": [],
                "post_count": 0,
                "opinions": [],
                "_seen_urls": set(),
                "used_llm": False,
            }
            order.append(family)
        b = buckets[family]
        if canon and canon not in b["canonicals"]:
            b["canonicals"].append(canon)
        b["post_count"] += int(entry.get("post_count") or 0)
        b["used_llm"] = b["used_llm"] or bool(entry.get("used_llm"))
        for op in (entry.get("opinions") or []):
            if not isinstance(op, dict):
                continue
            u = (op.get("url") or "").strip()
            if u and u in b["_seen_urls"]:
                continue
            if u:
                b["_seen_urls"].add(u)
            b["opinions"].append(op)

    # 按聚合后 post_count 重新排序（多成员家族讨论量自然更高，让它排前）
    rolled = []
    for fam in order:
        b = buckets[fam]
        b["opinions"] = b["opinions"][:opinions_per_card]
        b.pop("_seen_urls", None)
        rolled.append(b)
    rolled.sort(key=lambda x: -x["post_count"])

    out = dict(payload)
    out["models"] = rolled
    return out
