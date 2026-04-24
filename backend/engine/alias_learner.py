"""Alias Learner：从未匹配到 canonical 的高分 Reddit 帖自动抽候选模型名，并自动扩充 alias。

背景：
  alias 匹配永远跟不上新发布——Meta 突然放出 muse-spark，alias 表里没有，
  社区声音就看不到它。本模块每周扫一次 matched_model IS NULL 且热度够高的帖子，
  抽出"像模型名"的 token，直接写进 learned_aliases（model_alias 下次查询就能命中）。
  低置信度的候选（slash slug / 噪声）留在 pending_model_aliases 表里做 debug，不打扰周报。

抽取规则：
  - A. "Capitalized Word + 数字版本"：Gemini 3.1 / Opus-4.7 / Llama 4   → 自动接受
  - B. "Brand + 数字"（大写开头）：Qwen3 / GPT5 / K2                   → 自动接受
  - C. "vendor/slug" 形态：openai/gpt-5 / lovis93/crt-xxx             → 只记 pending，不自动接受
      （slug 是下游引用，不是独立名字，自动接受会污染 canonical 表）

排除：
  - 已经能被 find_mentions 识别的（已在手写 ALIAS_TABLE 或之前 learned_aliases 里）
  - 年份、月份、常见英文词、过短 (<4)

变体生成（写入 learned_aliases 时）：
  "Bonsai 1.7B" → 加 ["bonsai 1.7b", "bonsai-1.7b", "bonsai1.7b", "bonsai_1.7b"]
  让 normalize() 能覆盖各种分隔符形式。
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from backend.db import get_conn
from backend.utils.model_alias import find_mentions, register_learned_alias, ALIAS_TABLE

logger = logging.getLogger(__name__)


# --- 抽取模式 ---
# A. 大写开头英文词 + 连字符/空格 + 数字版本（Gemini 3.1, Opus-4.7, Llama 4, Bonsai 1.7B）
_PAT_WORD_VER = re.compile(
    r"\b([A-Z][A-Za-z]{2,}[\s\-]\d+(?:\.\d+)?[A-Za-z]{0,4})(?![A-Za-z0-9])"
)
# B. Brand 紧跟数字（Qwen3, GPT5, K2）—— 至少 2 字母 + 数字
_PAT_BRAND_NUM = re.compile(
    r"\b([A-Z][A-Za-z]{1,}\d+(?:\.\d+)?[A-Za-z]{0,4})(?![A-Za-z0-9])"
)
# C. vendor/slug —— 仅记 pending 用，不自动接受
_PAT_SLASH = re.compile(
    r"\b([a-z][a-z0-9\-]{2,20}/[a-z][a-z0-9\-.]{2,30})\b"
)


# --- 黑名单 ---
_STOPWORDS = {
    "github",  "python", "macos", "linux", "ubuntu",
    "chatgpt", "claude", "gemini", "gpt", "llama", "qwen", "kimi",  # 纯品牌名无版本不算
    "http", "https",
    "v1", "v2", "v3", "v4",
    "win11", "win10",
    "pm1", "pm2", "pm3", "am1", "am2",
}

_MONTHS = {"january", "february", "march", "april", "may", "june",
           "july", "august", "september", "october", "november", "december",
           "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec"}

_YEAR_RE = re.compile(r"^[A-Z][a-z]+[\s\-]20\d{2}$")

_SLASH_RHS_NOISE = {
    "news", "index", "home", "about", "blog", "docs", "api", "readme",
    "main", "master", "dev", "test", "demo", "example",
    "out", "in", "up", "down", "on", "off",
    "trying", "doing", "using", "going",
    "feature", "features", "issue", "issues", "pull", "commit",
    "subject", "object", "thing", "things",
    "left", "right", "top", "bottom",
    "yes", "no", "true", "false",
    "video", "image", "audio", "text",
    "file", "files", "folder", "path",
}


def _is_noise(raw: str) -> bool:
    """公共噪声判断：所有形态都要过。"""
    low = raw.lower()
    if len(low) < 4 or len(low) > 50:
        return True
    if low in _STOPWORDS:
        return True
    if _YEAR_RE.match(raw):
        return True
    if not re.search(r"[\d/\-]", raw):
        return True
    first_token = re.split(r"[\s\-]", low, maxsplit=1)[0]
    if first_token in _MONTHS:
        return True
    return False


# 小版本折叠：如果候选是已有 canonical 的"主版本.次版本"变体（如 Qwen3.6 → Qwen3 已存在），
# 不铸成新 canonical，避免同家族讨论被拆散。匹配规则：去掉 .小版本 后的字符串是已知 canonical。
_VERSION_SUFFIX_RE = re.compile(r"^([A-Za-z]+[0-9]+)\.[0-9]+[A-Za-z]*$")

def _is_subversion_of_known(raw: str) -> bool:
    """Qwen3.6 → 检查 Qwen3 是否已是 canonical；是则视为噪声（让主版本吸收讨论）。"""
    m = _VERSION_SUFFIX_RE.match(raw.strip())
    if not m:
        return False
    trunk = m.group(1)  # "Qwen3"
    # ALIAS_TABLE key 是 canonical 原样；也检查大小写变体
    for canonical in ALIAS_TABLE.keys():
        if canonical.lower() == trunk.lower():
            return True
    return False


def _is_slash_noise(raw: str) -> bool:
    """slash 形态的额外噪声判断。"""
    if "/" not in raw:
        return False
    left, _, right = raw.partition("/")
    right_head = re.split(r"[\-_.]", right, maxsplit=1)[0].lower()
    if right_head in _SLASH_RHS_NOISE:
        return True
    if len(left) <= 3 and left.isalpha():
        return True
    return False


def _variants(canonical: str) -> list[str]:
    """给一个 canonical（例如 "Bonsai 1.7B"）生成常见分隔符变体。"""
    low = canonical.strip().lower()
    # 用空格分割，生成 "a b c" / "a-b-c" / "abc" 等组合
    tokens = re.split(r"[\s\-_]+", low)
    tokens = [t for t in tokens if t]
    if not tokens:
        return []
    joined_space  = " ".join(tokens)
    joined_hyphen = "-".join(tokens)
    joined_under  = "_".join(tokens)
    joined_tight  = "".join(tokens)
    return list({joined_space, joined_hyphen, joined_under, joined_tight})


def _extract(text: str) -> tuple[list[str], list[str]]:
    """返回 (高置信度候选, slash 低置信度候选)。"""
    high: dict[str, str] = {}   # key=lower → 原样大小写
    low_conf: dict[str, str] = {}

    def _put(bucket: dict[str, str], raw: str) -> None:
        k = raw.lower()
        if k not in bucket:
            bucket[k] = raw

    for m in _PAT_WORD_VER.finditer(text):
        raw = m.group(1).strip()
        if _is_noise(raw) or _is_subversion_of_known(raw):
            continue
        _put(high, raw)
    for m in _PAT_BRAND_NUM.finditer(text):
        raw = m.group(1).strip()
        if _is_noise(raw) or _is_subversion_of_known(raw):
            continue
        _put(high, raw)
    for m in _PAT_SLASH.finditer(text):
        raw = m.group(1).strip()
        if _is_noise(raw) or _is_slash_noise(raw):
            continue
        _put(low_conf, raw)

    return list(high.values()), list(low_conf.values())


def _fetch_unmatched(conn, days: int, score_threshold: int) -> list[dict]:
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    rows = conn.execute(
        """
        SELECT post_id, title, selftext, score, url, created_utc
        FROM reddit_posts
        WHERE matched_model IS NULL
          AND score >= ?
          AND created_utc >= ?
        ORDER BY score DESC
        """,
        (score_threshold, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def _upsert_pending(conn, candidate: str, score: int, sample_url: str | None, source: str) -> None:
    conn.execute(
        """
        INSERT INTO pending_model_aliases
            (candidate, mention_count, total_score, sample_post_url, source)
        VALUES (?, 1, ?, ?, ?)
        ON CONFLICT(candidate) DO UPDATE SET
            mention_count   = mention_count + 1,
            total_score     = total_score + excluded.total_score,
            last_seen       = CURRENT_TIMESTAMP,
            sample_post_url = COALESCE(excluded.sample_post_url, sample_post_url)
        """,
        (candidate, score, sample_url, source),
    )


def _auto_accept(canonical: str, sample_url: str | None, source: str) -> bool:
    """写到 learned_aliases；已存在就跳过。返回是否真的新加入。"""
    return register_learned_alias(
        canonical=canonical,
        aliases=_variants(canonical),
        sample_url=sample_url,
        source=source,
    )


def learn_from_reddit(days: int = 7, score_threshold: int = 10) -> dict:
    """扫本周未匹配高分帖，高置信度候选自动进 learned_aliases，slash 形态只记 pending。"""
    accepted_new: list[str] = []
    pending_added = 0
    # 先在单个事务里把所有候选和 pending 都处理完，再在事务外做 learned_aliases 写入
    # （register_learned_alias 会自己开连接，不能和外层写事务共存）
    high_queue: list[tuple[str, str | None]] = []

    with get_conn() as conn:
        posts = _fetch_unmatched(conn, days=days, score_threshold=score_threshold)
        for p in posts:
            text = f"{p['title']}\n{p.get('selftext') or ''}"
            if find_mentions(text, max_hits=1):
                continue
            high, low_conf = _extract(text)
            for raw in high:
                high_queue.append((raw, p.get("url")))
            for raw in low_conf:
                _upsert_pending(conn, raw, p.get("score") or 0, p.get("url"), "token_scan_slash")
                pending_added += 1

    # 事务外：批量 auto-accept（每个调用内部自己开/关连接）
    seen_in_batch: set[str] = set()
    for raw, url in high_queue:
        key = raw.lower()
        if key in seen_in_batch:
            continue
        seen_in_batch.add(key)
        if _auto_accept(raw, sample_url=url, source="reddit_token"):
            accepted_new.append(raw)

    logger.info(
        "[AliasLearner] 扫 %d 条未匹配高分帖 → 自动接受 %d 个新 canonical（%s）；"
        "slash 形态 %d 次写入 pending 做 debug",
        len(posts), len(accepted_new),
        ", ".join(accepted_new[:8]) + ("…" if len(accepted_new) > 8 else ""),
        pending_added,
    )
    return {
        "posts_scanned": len(posts),
        "auto_accepted": accepted_new,
        "pending_added": pending_added,
    }


def record_llm_candidates(candidates: list[str], sample_url: str | None = None) -> None:
    """LLM themes 归纳完主题后，把它识别的"具体模型名"喂给我们 → 也走自动接受。"""
    if not candidates:
        return
    for raw in candidates:
        raw = (raw or "").strip()
        if _is_noise(raw) or _is_slash_noise(raw):
            continue
        if find_mentions(raw, max_hits=1):
            continue
        # LLM 给的候选必须能过 WORD_VER 或 BRAND_NUM 形态（避免纯英文词"Sparks"混进来）
        if not (_PAT_WORD_VER.fullmatch(raw) or _PAT_BRAND_NUM.fullmatch(raw)):
            continue
        _auto_accept(raw, sample_url=sample_url, source="llm_theme")


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(learn_from_reddit())
