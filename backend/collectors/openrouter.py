"""OpenRouter Collector：抓 openrouter.ai/rankings 的周榜。

为什么单独建一张表：
- HF trending/downloads 反映"社区声量"（likes / 模型页下载）
- OpenRouter 反映"API 端真实调用量"——是真金白银的 token 消耗
- 两个信号互补：HF 热不代表有人真在生产环境里用它

数据路径：
- 页面：https://openrouter.ai/rankings
- 真数据在 HTML 里 Next.js 的 self.__next_f.push([1, "..."]) chunks 里
- 字段已经是 {model_permaslug, variant, total_completion_tokens, total_prompt_tokens,
              total_native_tokens_reasoning, count, change}
- 附近的 "rankingType":"week" 标签确认了就是周口径
- change 是小数：0.42 = +42%，-0.22 = -22%，null = 本周新进榜（UI 显示 "new"）

为什么选 HTML SSR 而不是 /api/frontend/models：
- /api/frontend/models 只是模型目录（687 条元信息），没有用量数字
- rankings 页面的 SSR HTML 已经把周数据嵌进 RSC payload 里
- 不需要 cookie/auth，标 User-Agent 就能访问，Railway 美国机直连即可

为什么选 SSR 而不是 RSC JSON 直连：
- 页面地址稳定（openrouter.ai/rankings）
- RSC 端点路径格式依赖 Next.js 版本，改版概率更大
"""
import json
import logging
import re
from collections import defaultdict

import requests

from backend.db import get_conn, record_status
from backend.utils.model_alias import find_mentions
from backend.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

RANKINGS_URL = "https://openrouter.ai/rankings"
MODELS_URL = "https://openrouter.ai/api/frontend/models"  # 有 permaslug + 官方 short_name
TOP_N = 30  # 落库 Top 30，周报只挑 Top 10-15 展示

# 每一行 model 数据的 JSON 结构。注意 non-greedy，允许中间字段顺序小变动。
_ROW_RE = re.compile(
    r'\{"date":"([^"]+)",'
    r'"model_permaslug":"([^"]+)",'
    r'"variant":"([^"]+)",'
    r'"total_completion_tokens":(\d+),'
    r'"total_prompt_tokens":(\d+),'
    r'"total_native_tokens_reasoning":(\d+),'
    r'"count":(\d+),'
    r'[^}]*?"change":([^,}]+)'
)

# Next.js 流式 RSC payload，每个 push 塞一段 escape 后的字符串。
_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*(".*?")\]\)', re.S)


@retry_with_backoff(max_retries=2, base_delay=5.0)
def _fetch_html() -> str:
    resp = requests.get(
        RANKINGS_URL,
        headers={
            "User-Agent": "ModelRadar/1.0",
            "Accept": "text/html,application/xhtml+xml",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def _fetch_display_names() -> dict[str, str]:
    """拉 OR 官方模型目录，返回 permaslug → short_name 映射。
    short_name 就是网站上 rankings 页面展示的那个名字（Claude Sonnet 4.6 / MiMo-V2-Pro 等），
    所有 ~700 个模型都有，一次抓全量做本地 map。
    异常时返回空 dict，由调用方降级到 slug。
    """
    try:
        resp = requests.get(
            MODELS_URL,
            headers={"User-Agent": "ModelRadar/1.0", "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return {}
        mp: dict[str, str] = {}
        for m in items:
            slug = m.get("permaslug")
            name = m.get("short_name")
            if slug and name:
                mp[slug] = name
        return mp
    except Exception as e:
        logger.warning("[OpenRouter] 拉取 /api/frontend/models 失败，display_name 将降级为 slug: %s", e)
        return {}


def _extract_payload(html: str) -> str:
    """把所有 self.__next_f.push 的字符串片段拼成一个大 string。"""
    chunks = _CHUNK_RE.findall(html)
    if not chunks:
        raise RuntimeError("openrouter rankings: 没找到 __next_f 片段，页面结构可能变了")
    parts = []
    for c in chunks:
        try:
            parts.append(json.loads(c))
        except Exception:
            # 有些 chunk 不是完整 JSON 字符串（比如模块定义），忽略
            continue
    return "".join(parts)


def _parse_rows(payload: str) -> list[dict]:
    """解析所有 {model_permaslug + total_*} 行。多个 variant/date 都会出现，上层按周筛选。"""
    rows = []
    for m in _ROW_RE.finditer(payload):
        date, slug, variant, comp, prompt, reason, cnt, change_raw = m.groups()
        try:
            change = None if change_raw.strip() in ("null", "undefined") else float(change_raw)
        except ValueError:
            change = None
        rows.append({
            "date": date,
            "model_permaslug": slug,
            "variant": variant,
            "completion": int(comp),
            "prompt": int(prompt),
            "reasoning": int(reason),
            "count": int(cnt),
            "change": change,
        })
    return rows


def _aggregate_latest_week(rows: list[dict]) -> tuple[str, list[tuple[str, str, dict]]]:
    """找到最新日期，按 (permaslug, variant) 聚合。

    历史上这里把 free + standard 合并，但 openrouter.ai/rankings 页面是把它们当独立条目排序的，
    合并后我们的 #8 (MiniMax M2.5 = 633.8B standard + 89.6B free = 723.5B) 跟网站的 #8
    (Step 3.5 Flash, 722.1B 单一 variant) 直接错位。改成 (slug, variant) 单独排序对齐网站。
    """
    if not rows:
        return "", []
    # OR 的 date 是 "2026-04-21 00:00:00"，每周一采样一次
    latest_date = max(r["date"] for r in rows)
    latest = [r for r in rows if r["date"] == latest_date]

    agg: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "completion": 0, "prompt": 0, "reasoning": 0, "count": 0,
        "change": None,
    })
    for r in latest:
        key = (r["model_permaslug"], r["variant"])
        a = agg[key]
        a["completion"] += r["completion"]
        a["prompt"] += r["prompt"]
        a["reasoning"] += r["reasoning"]
        a["count"] += r["count"]
        if a["change"] is None and r["change"] is not None:
            a["change"] = r["change"]

    # 对齐 openrouter.ai/rankings 页面的 "Tokens" 列：completion + prompt + reasoning。
    ranked = sorted(
        [(slug, variant, v) for (slug, variant), v in agg.items()],
        key=lambda x: -(x[2]["completion"] + x[2]["prompt"] + x[2]["reasoning"]),
    )
    return latest_date, ranked


def _match_model(slug: str) -> str | None:
    """把 permaslug tail 扔给 find_mentions 做 canonical 对齐。
    例：anthropic/claude-4.6-sonnet-20260217 → Claude Sonnet 4.6
    """
    if not slug:
        return None
    tail = slug.split("/", 1)[-1]
    # 去掉尾部日期戳（常见 8 位数字），减少干扰
    tail = re.sub(r"-\d{8}$", "", tail)
    # 连字符/冒号/下划线 → 空格
    text = re.sub(r"[-_:]", " ", tail)
    hits = find_mentions(text, max_hits=1)
    return hits[0] if hits else None


def _persist(conn, week_date: str, ranked: list[tuple[str, str, dict]],
             display_names: dict[str, str]) -> int:
    """写 Top N 到 openrouter_rankings。同 week_date 的旧数据先清掉，避免 redeploy 累积重复快照。

    非 standard variant 在 permaslug 后缀 ":<variant>"，display_name 后缀 " (<variant>)"，
    与 OR 网站的 "Model Name (free)" 显示对齐，且保证 (slug, variant) 在表里不冲突。
    """
    conn.execute("DELETE FROM openrouter_rankings WHERE week_date=?", (week_date[:10],))
    inserted = 0
    for rank, (slug, variant, v) in enumerate(ranked[:TOP_N], start=1):
        author = slug.split("/", 1)[0] if "/" in slug else None
        total = v["completion"] + v["prompt"] + v["reasoning"]
        matched = _match_model(slug)
        display_base = display_names.get(slug)
        if variant and variant != "standard":
            stored_slug = f"{slug}:{variant}"
            suffix = f" ({variant})"
            # OR 自家 short_name 对"仅免费 variant"模型已经带 (free) 后缀，避免重复
            if display_base and not display_base.endswith(suffix):
                display = display_base + suffix
            else:
                display = display_base
        else:
            stored_slug = slug
            display = display_base
        cur = conn.execute(
            """
            INSERT INTO openrouter_rankings
              (week_date, rank, model_permaslug, author,
               total_tokens, completion_tokens, prompt_tokens, reasoning_tokens,
               request_count, change_pct, matched_model, display_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                week_date[:10],
                rank, stored_slug, author,
                total, v["completion"], v["prompt"], v["reasoning"],
                v["count"], v["change"], matched, display,
            ),
        )
        if cur.rowcount > 0:
            inserted += 1
    return inserted


def collect() -> dict:
    """抓一次 OR 周榜，写 Top 30。返回 {week_date, inserted, matched, display_hits}。"""
    try:
        html = _fetch_html()
        payload = _extract_payload(html)
        rows = _parse_rows(payload)
        week_date, ranked = _aggregate_latest_week(rows)
        if not ranked:
            raise RuntimeError(f"openrouter rankings: 解析到 0 条 weekly 数据（rows={len(rows)}）")

        display_names = _fetch_display_names()

        with get_conn() as conn:
            inserted = _persist(conn, week_date, ranked, display_names)
            matched = sum(1 for slug, _, _ in ranked[:TOP_N] if _match_model(slug) is not None)
            display_hits = sum(1 for slug, _, _ in ranked[:TOP_N] if slug in display_names)

        logger.info(
            "[OpenRouter] 周榜 %s: Top %d 写入 %d 条 · canonical=%d · display_name=%d",
            week_date[:10], min(len(ranked), TOP_N), inserted, matched, display_hits,
        )
        record_status("openrouter", success=True)
        return {"week_date": week_date[:10], "inserted": inserted,
                "matched": matched, "display_hits": display_hits}
    except Exception as e:
        logger.exception("OpenRouter 采集失败: %s", e)
        record_status("openrouter", success=False, error=str(e))
        raise


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(collect())
