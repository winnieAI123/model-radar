"""模型名归一化层。

跨源数据聚合的瓶颈：LMArena 写 `gpt-5-high`、AA 写 `GPT-5`、
SuperCLUE 写 `DeepSeek-V3.2-Thinking(思考)`、GitHub 写 `DeepSeek-V3`。
本模块把它们统一到 canonical 名字（`DeepSeek-V3` 等），让 heat_scorer 和
详情卡能把同一个模型的数据拼起来。

策略：
1. 先走 ALIAS 表（手工精确映射）
2. 否则走规则归一化（去日期、去思考/标准标注、小写化、分隔符归一）
3. 匹配不上 → 写入 _pending_mapping，每周人工补
"""
import json
import logging
import re
import sqlite3
from functools import lru_cache

from backend.db import get_conn

logger = logging.getLogger(__name__)


# --- 精确 alias 表（canonical → list of alias raw names） ---
# key = canonical（保持书面体面），value = 所有可能的 raw 名字（全小写匹配）
ALIAS_TABLE: dict[str, list[str]] = {
    # OpenAI
    "GPT-5":          ["gpt-5", "gpt-5-chat", "gpt-5-high", "gpt-5-pro", "openai/gpt-5",
                       "gpt5", "gpt 5", "chatgpt-5"],
    "GPT-5 mini":     ["gpt-5-mini", "gpt5-mini", "openai/gpt-5-mini", "gpt 5 mini"],
    "GPT-4o":         ["gpt-4o", "gpt-4o-2024-05-13", "gpt-4o-2024-08-06",
                       "gpt-4o-2024-11-20", "gpt-4o-latest", "chatgpt-4o-latest",
                       "openai/gpt-4o", "gpt4o", "gpt 4o"],
    "GPT-4o mini":    ["gpt-4o-mini", "gpt-4o-mini-2024-07-18", "openai/gpt-4o-mini", "gpt 4o mini"],
    "o1":             ["o1-pro", "o1-2024-12-17", "openai/o1", "o1 pro"],  # 纯"o1"太短，删掉避免误报
    "o3":             ["o3-mini", "openai/o3", "o3 mini", "o3-pro", "o3 pro"],
    # Anthropic
    "Claude Opus 4.7":      ["claude-opus-4.7", "claude-opus-4-7", "claude-opus-4.7-20260201",
                             "anthropic/claude-opus-4.7", "opus-4.7", "opus 4.7",
                             "claude opus 4.7", "claude-opus-4-7-thinking"],
    "Claude Opus 4.6":      ["claude-opus-4.6", "claude-opus-4-6", "claude-opus-4-6-20251009",
                             "anthropic/claude-opus-4.6", "opus-4.6", "opus 4.6",
                             "claude opus 4.6", "claude-opus-4-6-thinking"],
    "Claude Sonnet 4.6":    ["claude-sonnet-4.6", "claude-sonnet-4-6", "anthropic/claude-sonnet-4.6",
                             "sonnet-4.6", "sonnet 4.6", "claude sonnet 4.6"],
    "Claude Sonnet 4.5":    ["claude-sonnet-4.5", "claude-sonnet-4-5", "sonnet-4.5", "sonnet 4.5",
                             "claude sonnet 4.5"],
    "Claude Haiku 4.5":     ["claude-haiku-4.5", "claude-haiku-4-5-20251001", "haiku-4.5", "haiku 4.5",
                             "claude haiku 4.5"],
    "Claude 3.5 Sonnet":    ["claude-3-5-sonnet", "claude-3.5-sonnet", "claude-3-5-sonnet-20241022",
                             "3.5 sonnet", "sonnet 3.5"],
    # Google
    "Gemini 3 Pro":         ["gemini-3-pro", "gemini-3-pro-preview", "gemini-3-pro-image-preview",
                             "gemini 3 pro", "gemini3 pro", "gemini-3"],
    "Gemini 3.1 Flash":     ["gemini-3.1-flash", "gemini-3.1-flash-image-preview", "gemini 3.1 flash",
                             "gemini-3-1-flash"],
    "Gemini 2.5 Pro":       ["gemini-2.5-pro", "gemini-2.5-pro-exp", "gemini-2-5-pro", "gemini 2.5 pro"],
    "Gemini 2.5 Flash":     ["gemini-2.5-flash", "gemini-2-5-flash", "gemini 2.5 flash"],
    # DeepSeek
    "DeepSeek-V3":          ["deepseek-v3", "deepseek-v3-base", "deepseek/deepseek-v3",
                             "deepseek-ai/deepseek-v3", "deepseek v3", "ds-v3"],
    "DeepSeek-V3.1":        ["deepseek-v3.1", "deepseek-v3-1", "deepseek v3.1"],
    "DeepSeek-V3.2":        ["deepseek-v3.2", "deepseek-v3.2-thinking", "deepseek-v3-2", "deepseek v3.2"],
    "DeepSeek-R1":          ["deepseek-r1", "deepseek-r1-distill", "deepseek/deepseek-r1",
                             "deepseek r1", "ds-r1"],
    "DeepSeek-V4":          ["deepseek-v4", "deepseek v4", "ds-v4"],  # 预期会出，提前占位
    # Qwen
    "Qwen3":                ["qwen3", "qwen-3", "qwenlm/qwen3", "qwen3-235b", "qwen3-max", "qwen 3"],
    "Qwen3-Coder":          ["qwen3-coder", "qwen3 coder", "qwen-3-coder"],
    "Qwen3-VL":             ["qwen3-vl", "qwen3 vl", "qwen-3-vl"],
    "Qwen2.5-72B":          ["qwen2.5-72b-instruct", "qwen2.5-72b", "qwen-2.5-72b", "qwen2.5 72b"],
    "Qwen2.5":              ["qwen2.5", "qwen-2.5", "qwen 2.5"],
    # Moonshot
    "Kimi-K2":              ["kimi-k2", "moonshotai/kimi-k2", "kimi-k2-instruct",
                             "kimi k2"],
    "Kimi-K1.5":            ["kimi-k1.5", "kimi-1.5", "kimi k1.5"],
    "Kimi-CLI":             ["kimi-cli", "kimi cli"],
    # Zhipu GLM
    "GLM-4.7":              ["glm-4.7", "glm-4-7", "glm 4.7"],
    "GLM-5":                ["glm-5", "glm-5-air", "glm-5-plus", "glm 5"],
    "GLM-5.1":              ["glm-5.1", "glm 5.1"],
    # MiniMax
    "MiniMax-M1":           ["minimax-m1", "minimax-ai/minimax-m1", "minimax m1"],
    "MiniMax-M2":           ["minimax-m2", "minimax m2"],
    "MiniMax-Text-01":      ["minimax-text-01", "abab7-chat", "minimax text-01"],
    # Step
    "Step-2":               ["step-2", "step-2-16k", "stepfun-ai/step-2", "step 2"],
    "Step-Audio":           ["step-audio", "step-audio-editx", "stepfun-ai/step-audio", "step audio"],
    # Doubao
    "Doubao-Seed-1.8":      ["doubao-seed-1.8", "doubao-1-5", "doubao-1.5-pro", "doubao seed 1.8"],
    "Doubao-Seed-2.0":      ["doubao-seed-2.0", "doubao-seed-2-0-pro", "doubao seed 2.0"],
    # Meta
    "Llama-3.3-70B":        ["llama-3.3-70b", "llama-3-3-70b", "meta-llama/llama-3.3-70b",
                             "llama 3.3 70b"],
    "Llama-4":              ["llama-4", "meta-llama/llama-4", "llama 4", "llama4"],
    "Llama-3.1-405B":       ["llama-3.1-405b", "llama 3.1 405b"],
    # Mistral
    "Mistral Large":        ["mistral-large", "mistral-large-2407", "mistral-large-latest"],
    "Mistral Small":        ["mistral-small", "mistral-small-latest"],
    # xAI
    "Grok-4":               ["grok-4", "grok-4-latest", "grok 4"],
    "Grok-3":               ["grok-3", "grok-3-beta", "grok 3"],
    "Grok Imagine":         ["grok-imagine", "grok-imagine-video", "grok-imagine-video-720p",
                             "grok-imagine-video-1080p", "grok imagine"],
    # --- 视频 / 图像模型（AA + SuperCLUE + LMArena 常见）---
    # 注：各 resolution/preview/fast/audio 变体统一收敛到一个 canonical
    "Veo 3.1":              ["veo-3.1", "veo 3.1", "veo-3.1-fast", "veo-3.1-preview",
                             "veo-3.1-fast-preview", "veo-3.1-audio", "veo-3.1-audio-1080p",
                             "veo-3.1-fast-audio", "veo-3.1-fast-audio-1080p",
                             "veo 3.1 fast", "veo 3.1 preview", "veo 3.1 fast preview"],
    "Veo 3":                ["veo-3", "veo-3-audio", "veo-3-fast-audio"],
    "Dreamina Seedance 2.0":["dreamina-seedance-2.0", "dreamina-seedance-2.0-720p",
                             "dreamina-seedance-2.0-1080p", "dreamina seedance 2.0 720p",
                             "dreamina seedance 2.0 1080p", "dreamina seedance 2.0"],
    "Seedance v1.5 Pro":    ["seedance-v1.5-pro", "seedance v1.5 pro"],
    "HappyHorse-1.0":       ["happyhorse-1.0", "happyhorse"],
    "PixVerse V6":          ["pixverse-v6", "pixverse v6"],
    "PixVerse V5.6":        ["pixverse-v5.6", "pixverse v5.6"],
    "PixVerse V5":          ["pixverse-v5", "pixverse v5"],
    "Kling 3.0":            ["kling-3.0", "kling 3.0", "kling-3.0-1080p-pro", "kling-3.0-720p-standard",
                             "kling-3.0-omni", "kling-3.0-omni-1080p-pro", "kling-3.0-omni-720p-standard",
                             "kling 3.0 1080p (pro)", "kling 3.0 720p (standard)",
                             "kling 3.0 omni 1080p (pro)", "kling 3.0 omni 720p (standard)"],
    "Kling 2.5 Turbo":      ["kling-2.5-turbo", "kling-2.5-turbo-1080p", "kling 2.5 turbo 1080p"],
    "Vidu Q3":              ["vidu-q3", "vidu-q3-pro", "vidu q3", "vidu q3 pro"],
    "SkyReels V4":          ["skyreels-v4", "skyreels v4"],
    "Runway Gen-4.5":       ["runway-gen-4.5", "runway gen-4.5"],
    "Wan 2.6":              ["wan-2.6", "wan 2.6", "wan 2.6 (2025-12-16)", "wan-2.6-2025-12-16"],
    # 图像
    "GPT Image 1.5":        ["gpt-image-1.5", "gpt-image-1-5"],
    "GPT Image 1":          ["gpt-image-1"],
    "可灵 3.0":              ["可灵-3.0", "可灵 3.0", "kling-ai-3.0"],
    "即梦 3.1":              ["即梦-3.1", "即梦 3.1", "jimeng-3.1"],
    "Nano Banana Pro":      ["nano-banana-pro", "gemini-3-pro-image-preview (nano banana pro)",
                             "gemini 3 pro image preview"],
    "Sora 2":               ["sora-2", "sora 2"],
    "Sora 2 HD":            ["sora-2-hd", "sora 2 hd"],
}


# --- 归一化规则 ---
_DATE_SUFFIX_RE = re.compile(r"[-_]?(20\d{6}|20\d{2}-\d{2}-\d{2}|\d{6,8})$")
_PAREN_TAIL_RE  = re.compile(r"\s*[\(（][^)）]*[\)）]\s*$")  # 半角 + 全角
_TAIL_LABELS    = {"thinking", "instruct", "chat", "base", "preview", "experimental",
                   "standard", "思考", "标准"}


def _strip_tail_labels(name: str) -> str:
    """去掉末尾的 -thinking / -chat / -preview 等版本标签。"""
    parts = name.rsplit("-", 1)
    while len(parts) == 2 and parts[1].lower() in _TAIL_LABELS:
        name = parts[0]
        parts = name.rsplit("-", 1)
    return name


def _canonicalize(raw: str) -> str:
    """把任意 raw 名字压平成可比较形式（小写、去日期、去括号尾、分隔符归一）。"""
    s = raw.strip()
    # 去 GitHub 的 org/ 前缀
    if "/" in s:
        s = s.rsplit("/", 1)[1]
    # 去括号尾注
    s = _PAREN_TAIL_RE.sub("", s)
    # 分隔符统一为 -
    s = s.replace("_", "-").replace(".", ".").replace(" ", "-")
    # 去日期尾
    s = _DATE_SUFFIX_RE.sub("", s)
    # 小写
    s = s.lower()
    # 去尾部标签
    s = _strip_tail_labels(s)
    return s


def _load_learned() -> dict[str, list[str]]:
    """读 learned_aliases 表，返回 {canonical: [aliases...]}。
    DB 还没初始化（第一次启动）或表不存在时返回空字典。
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT canonical, aliases_json FROM learned_aliases"
            ).fetchall()
        out: dict[str, list[str]] = {}
        for r in rows:
            try:
                aliases = json.loads(r["aliases_json"]) or []
            except Exception:
                aliases = []
            if isinstance(aliases, list):
                out[r["canonical"]] = [a for a in aliases if isinstance(a, str)]
        return out
    except sqlite3.OperationalError:
        return {}
    except Exception as e:
        logger.warning("[model_alias] load learned_aliases failed: %s", e)
        return {}


def _merged_table() -> dict[str, list[str]]:
    """手写 ALIAS_TABLE + 自动学到的 learned_aliases，key 冲突时手写优先。"""
    merged = {k: list(v) for k, v in ALIAS_TABLE.items()}
    for canonical, aliases in _load_learned().items():
        if canonical in merged:
            # 手写已有该 canonical：把学到的 aliases 合并进去
            existing = {a.lower() for a in merged[canonical]}
            for a in aliases:
                if a.lower() not in existing:
                    merged[canonical].append(a)
        else:
            merged[canonical] = list(aliases)
    return merged


def _build_reverse_index() -> dict[str, str]:
    """alias (canonicalized) → canonical_name。进程内缓存。"""
    idx: dict[str, str] = {}
    for canonical, aliases in _merged_table().items():
        # canonical 自己也是一个 alias
        idx[_canonicalize(canonical)] = canonical
        for a in aliases:
            idx[_canonicalize(a)] = canonical
    return idx


_INDEX = _build_reverse_index()


@lru_cache(maxsize=2048)
def normalize(raw_name: str) -> str | None:
    """返回 canonical 名字。匹配不到返回 None（由调用方决定是否写 pending_mapping）。"""
    if not raw_name:
        return None
    key = _canonicalize(raw_name)
    if key in _INDEX:
        return _INDEX[key]
    # 二次尝试：对 raw 做更激进的清理（去版本号尾，如 -v2 -v3）
    key2 = re.sub(r"-v\d+(\.\d+)?$", "", key)
    if key2 != key and key2 in _INDEX:
        return _INDEX[key2]
    return None


# -------- 全文扫描（用于 Reddit 这种自由文本）--------
# 构造 patterns：alias 里的 空格/点/连字符/下划线 视为等价分隔符，
# 匹配时必须有词边界（避免 "llama" 匹中 "llamada"）。
# 过滤掉太短或太泛的 alias（最短 5 字符，含数字或连字符）避免误报。
_MIN_ALIAS_LEN = 5

def _pattern_for(alias: str) -> re.Pattern | None:
    a = alias.strip()
    if len(a) < _MIN_ALIAS_LEN:
        return None
    # 含 CJK 字符：直接 literal 匹配，不套词边界（CJK 无空格）
    if any("\u4e00" <= ch <= "\u9fff" for ch in a):
        return re.compile(re.escape(a), re.IGNORECASE)
    # ASCII：分隔符归并 + 词边界
    # "Claude Opus 4.7" / "claude-opus-4.7" / "claude_opus_4.7" 统一匹配
    tokens = re.split(r"[\s\-_]+", a)
    body = r"[\s\-_]*".join(re.escape(t) for t in tokens if t)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def _build_mention_patterns() -> list[tuple[str, re.Pattern, int]]:
    """返回 [(canonical, pattern, specificity)]，specificity 越大越优先（用于重叠裁决）。"""
    out: list[tuple[str, re.Pattern, int]] = []
    seen_raw: set[str] = set()
    for canonical, aliases in _merged_table().items():
        candidates = [canonical] + list(aliases)
        for a in candidates:
            a = a.strip()
            if not a or a.lower() in seen_raw:
                continue
            seen_raw.add(a.lower())
            p = _pattern_for(a)
            if p is None:
                continue
            # 特异性 = alias 长度；越长越具体（"claude opus 4.7" > "claude"）
            out.append((canonical, p, len(a)))
    # 排序：长的先匹配
    out.sort(key=lambda x: -x[2])
    return out


_MENTION_PATTERNS: list[tuple[str, re.Pattern, int]] = _build_mention_patterns()


def find_mentions(text: str, max_hits: int = 5) -> list[str]:
    """在自由文本里扫出所有被提到的 canonical 模型名（不重复，按特异性排序）。

    - 对 alias 表里每个 alias 做词边界 + 分隔符宽松的正则匹配
    - 短于 _MIN_ALIAS_LEN 的 alias 被忽略（避免"o1"/"glm"这种误报）
    - 返回最多 max_hits 个不同 canonical
    """
    if not text:
        return []
    hit_canonicals: list[str] = []
    seen: set[str] = set()
    for canonical, pattern, _spec in _MENTION_PATTERNS:
        if canonical in seen:
            continue
        if pattern.search(text):
            seen.add(canonical)
            hit_canonicals.append(canonical)
            if len(hit_canonicals) >= max_hits:
                break
    return hit_canonicals


def primary_mention(text: str) -> str | None:
    """自由文本里第一个（最具体的）命中的 canonical 名字。"""
    hits = find_mentions(text, max_hits=1)
    return hits[0] if hits else None


def _reload_caches() -> None:
    """alias_learner 写入 learned_aliases 后调这个，热更新进程内缓存。"""
    global _INDEX, _MENTION_PATTERNS
    _INDEX = _build_reverse_index()
    _MENTION_PATTERNS = _build_mention_patterns()
    normalize.cache_clear()


def register_learned_alias(canonical: str, aliases: list[str],
                           sample_url: str | None = None,
                           source: str = "reddit_token") -> bool:
    """把自动学到的 alias 永久化到 learned_aliases 表，并热更新 in-memory 索引。

    返回 True 表示新插入；False 表示该 canonical 已存在（手写表或 learned 表），跳过。
    """
    canonical = (canonical or "").strip()
    if not canonical:
        return False
    # 已经被手写表或已学习表覆盖 → 跳过
    if _canonicalize(canonical) in _INDEX:
        return False
    aliases = [a.strip() for a in (aliases or []) if a and a.strip()]
    # 去重、去掉与 canonical 重复的
    seen: set[str] = {canonical.lower()}
    uniq = []
    for a in aliases:
        if a.lower() in seen:
            continue
        seen.add(a.lower())
        uniq.append(a)
    try:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO learned_aliases (canonical, aliases_json, sample_url, source)
                VALUES (?, ?, ?, ?)
                """,
                (canonical, json.dumps(uniq, ensure_ascii=False), sample_url, source),
            )
    except Exception as e:
        logger.warning("[model_alias] register_learned_alias(%s) 失败: %s", canonical, e)
        return False
    _reload_caches()
    return True


def record_pending(raw_name: str, source: str) -> None:
    """归一化失败的名字写入 _pending_mapping，每周人工审。"""
    try:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO _pending_mapping(raw_name, source, seen_count, last_seen_at)
                VALUES (?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(raw_name, source) DO UPDATE SET
                    seen_count = seen_count + 1,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (raw_name, source),
            )
    except sqlite3.OperationalError:
        # 表还没建 → 忽略（heat_scorer 建表后会自动补齐）
        pass


def normalize_or_record(raw_name: str, source: str) -> str:
    """归一化，失败就记 pending。降级时返回 _canonicalize(raw_name)，
    保证 `HappyHorse-1.0` 和 `happyhorse-1.0` 这种同义变体聚合到同一个键（小写形式）。
    用户手工把 raw 加到 ALIAS_TABLE 后，下次跑会自动切到 pretty 名。
    """
    c = normalize(raw_name)
    if c:
        return c
    record_pending(raw_name, source)
    return _canonicalize(raw_name)


PENDING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS _pending_mapping (
    raw_name      TEXT NOT NULL,
    source        TEXT NOT NULL,
    seen_count    INTEGER DEFAULT 1,
    last_seen_at  DATETIME,
    PRIMARY KEY (raw_name, source)
);
"""


def ensure_pending_table() -> None:
    with get_conn() as conn:
        conn.executescript(PENDING_TABLE_SQL)


if __name__ == "__main__":
    ensure_pending_table()
    cases = [
        "gpt-4o-2024-05-13",
        "openai/gpt-4o",
        "GPT-4o",
        "DeepSeek-V3.2-Thinking",
        "deepseek-ai/DeepSeek-V3",
        "claude-opus-4-6-20251009",
        "Kimi-K2",
        "moonshotai/Kimi-K2",
        "Qwen3",
        "QwenLM/Qwen3",
        "unknownmodel-xyz",
    ]
    for c in cases:
        print(f"  {c:40s} -> {normalize(c)}")
