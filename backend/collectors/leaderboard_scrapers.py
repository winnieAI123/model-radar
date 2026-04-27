"""三源榜单采集器（移植自 professional-research/scripts/collect_leaderboard.py）。

与原版差异：
- 去掉 CSV 写入，纯返回 dict 数据
- 去掉 utils.read_config 依赖，直接读项目内 config/leaderboard.json
- 所有 scraper 函数签名改为 () -> dict[category, list[row]]，不再需要 date_str / data_dir
"""
import json
import logging
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from backend.utils import config
from backend.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
}


_config_cache = None


def _load_config() -> dict:
    global _config_cache
    if _config_cache is None:
        path = config.CONFIG_DIR / "leaderboard.json"
        with open(path, "r", encoding="utf-8") as f:
            _config_cache = json.load(f)
    return _config_cache


# ============================================================
# LMArena (arena.ai) — HTML table parsing
# ============================================================
@retry_with_backoff(max_retries=2, base_delay=5.0)
def _fetch_lmarena_category(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def scrape_lmarena() -> dict[str, list[dict]]:
    cfg = _load_config()["sources"]["lmarena"]
    base_url = cfg["base_url"]
    categories = cfg["categories"]

    all_data = {}
    for category, cat_info in categories.items():
        try:
            url = f"{base_url}/{cat_info.get('url_path', category)}"
            logger.info("[LM] 请求 %s", url)
            html = _fetch_lmarena_category(url)

            soup = BeautifulSoup(html, "html.parser")
            table = soup.find("table")
            if not table:
                raise RuntimeError(f"[{category}] 未找到排行榜表格")

            rows = table.find("tbody").find_all("tr")
            # labs 榜列布局完全不同（Lab Rank / Lab / Model Score / Model Rank / Rank Spread），
            # 不能复用 per-model parser；走专用解析。
            if category.endswith("-by-labs"):
                results = _parse_lmarena_labs_rows(rows)
            else:
                results = _parse_lmarena_model_rows(rows, cat_info)

            safe_name = category.replace("-", "_")
            all_data[safe_name] = results
            logger.info("[LM] %s: %d 条", category, len(results))
        except Exception as e:
            logger.error("[LM] %s 失败: %s", category, e)
            all_data[category.replace("-", "_")] = []

    return all_data


def _parse_lmarena_model_rows(rows, cat_info) -> list[dict]:
    results = []
    for row in rows:
        tds = row.find_all("td")
        if len(tds) < cat_info["cols"]:
            continue

        rank_spans = tds[1].find_all("span")
        if len(rank_spans) >= 2:
            rank_lower = _safe_int(rank_spans[0].get_text(strip=True))
            rank_upper = _safe_int(rank_spans[1].get_text(strip=True))
        else:
            raw = tds[1].get_text(strip=True)
            rank_lower = raw
            rank_upper = raw

        score = tds[3].get_text(strip=True).replace("Preliminary", "").strip()
        entry = {
            "rank": _safe_int(tds[0].get_text(strip=True)),
            "rank_lower": rank_lower,
            "rank_upper": rank_upper,
            "model": _parse_model_name(tds[2]),
            "score": score,
            "votes": _safe_int(tds[4].get_text(strip=True).replace(",", "")),
        }
        if cat_info["cols"] >= 7:
            entry["price_per_1m_tokens"] = tds[5].get_text(strip=True)
            entry["context_length"] = tds[6].get_text(strip=True)
        results.append(entry)
    return results


def _parse_lmarena_labs_rows(rows) -> list[dict]:
    """LMArena ?rankBy=labs 榜单专用 parser。

    列布局：
      td[0] Lab Rank        — "1"
      td[1] Lab cell        — 多个 <span>：[lab_name, top_model_with_proprietary_tag]
                              （用户给的 XPath：td[2]/div/div[2]/div/span title=Anthropic）
      td[2] Model Score     — "1503 ±8"（顶级模型 Elo + 置信区间，可能含 "Preliminary"）
      td[3] Model Rank      — 顶级模型在 per-model 榜的排名
      td[4] Rank Spread     — "1 6" = 该 lab 模型在 per-model 榜上的排名跨度
    """
    results = []
    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 5:
            continue
        spans = tds[1].find_all("span")
        if not spans:
            continue
        lab_name = spans[0].get_text(strip=True)
        if not lab_name:
            continue
        top_model = spans[1].get_text(strip=True).replace("· Proprietary", "").strip() if len(spans) > 1 else ""

        score_raw = tds[2].get_text(" ", strip=True).replace("Preliminary", "").strip()
        # "1503 ±8" → 紧凑成 "1503±8"
        score = re.sub(r"\s+", "", score_raw)

        spread_text = tds[4].get_text(" ", strip=True)
        spread_parts = spread_text.split()

        results.append({
            "rank": _safe_int(tds[0].get_text(strip=True)),
            "model": lab_name,                          # 公司名落在 model 字段，便于复用 _persist
            "score": score,                              # 顶级模型 Elo
            "top_model": top_model,
            "top_model_rank": _safe_int(tds[3].get_text(strip=True)),
            "rank_spread": spread_text,
            "rank_spread_min": _safe_int(spread_parts[0]) if len(spread_parts) >= 1 else None,
            "rank_spread_max": _safe_int(spread_parts[-1]) if len(spread_parts) >= 1 else None,
        })
    return results


# ============================================================
# ArtificialAnalysis.ai — Next.js RSC flight
# ============================================================
@retry_with_backoff(max_retries=2, base_delay=5.0)
def _fetch_aa_rsc(url: str, page_path: str) -> str:
    headers = {**HEADERS, "RSC": "1", "Next-Url": page_path}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def scrape_artificial_analysis() -> dict[str, list[dict]]:
    cfg = _load_config()["sources"]["artificial_analysis"]
    base_url = cfg["base_url"]
    rsc_pages = cfg["rsc_pages"]

    all_data = {}
    for track_key, page_path in rsc_pages.items():
        try:
            url = f"{base_url}{page_path}"
            logger.info("[AA] 请求 %s", url)
            text = _fetch_aa_rsc(url, page_path)
            models = _parse_rsc_flight(text)
            all_data[track_key] = models
            logger.info("[AA] %s: %d 条", track_key, len(models))
        except Exception as e:
            logger.error("[AA] %s 失败: %s", track_key, e)
            all_data[track_key] = []

    return all_data


# ============================================================
# SuperCLUE — Vue JS bundle inline data
# ============================================================
def scrape_superclue() -> dict[str, list[dict]]:
    cfg = _load_config()["sources"]["superclue"]
    base_url = cfg["base_url"]
    cat_order = cfg["category_order"]

    try:
        resp = requests.get(base_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        m = re.search(r"/assets/(vue-vendor-[A-Za-z0-9_-]+\.js)", resp.text)
        if not m:
            raise RuntimeError("无法在 SuperCLUE 首页找到 vue-vendor JS 路径")
        vendor_url = f"{base_url}/assets/{m.group(1)}"

        logger.info("[SC] 下载 %s", vendor_url)
        with requests.get(vendor_url, headers=HEADERS, timeout=60, stream=True) as r:
            r.raise_for_status()
            js = b"".join(r.iter_content(chunk_size=65536)).decode("utf-8")
        logger.info("[SC] JS bundle %d bytes", len(js))

        entries = _extract_sc_inline_entries(js)
        groups = _split_by_rank1(entries)
        logger.info("[SC] %d 条记录, %d 个赛道", len(entries), len(groups))

        all_data = {}
        for i, group in enumerate(groups):
            name = cat_order[i] if i < len(cat_order) else f"unknown_{i}"
            _sc_sanity_check(name, group)
            all_data[name] = group
        return all_data
    except Exception as e:
        logger.error("[SC] 失败: %s", e)
        return {}


# ============================================================
# Internal helpers
# ============================================================
def _parse_model_name(td) -> str:
    link = td.find("a")
    if link:
        for span in link.find_all("span"):
            text = span.get_text(strip=True)
            if text and not text.startswith(("·", "Proprietary", "Open", "API")):
                return text

    parts = [p.strip() for p in td.get_text(separator="|", strip=True).split("|") if p.strip()]
    skip = {"Proprietary", "Open", "API", "·"}
    filtered = [p for p in parts if p not in skip and not p.startswith("·")]
    return filtered[1] if len(filtered) >= 2 else (filtered[0] if filtered else td.get_text(strip=True))


def _safe_int(s):
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return s


def _parse_rsc_flight(text: str) -> list[dict]:
    for line in text.split("\n"):
        if '"rank"' not in line or '"elo"' not in line:
            continue
        if '"formatted"' not in line or '"values"' not in line:
            continue
        m = re.match(r"[\da-f]+:(.*)", line, re.DOTALL)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue

        entries = []
        _find_rsc_entries(data, entries)
        if not entries:
            continue

        seen = set()
        unique = []
        for e in entries:
            mid = e.get("values", {}).get("id", "")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            unique.append(e)
        unique.sort(key=lambda e: e.get("formatted", {}).get("rank", 9999))
        return [_normalize_rsc_entry(e) for e in unique]
    return []


def _find_rsc_entries(obj, results: list):
    if isinstance(obj, dict):
        if "formatted" in obj and "values" in obj:
            results.append(obj)
        else:
            for v in obj.values():
                _find_rsc_entries(v, results)
    elif isinstance(obj, list):
        for item in obj:
            _find_rsc_entries(item, results)


def _normalize_rsc_entry(entry: dict) -> dict:
    fmt = entry.get("formatted", {})
    vals = entry.get("values", {})
    creator = vals.get("creator", {})
    return {
        "rank": fmt.get("rank", vals.get("rank", 0) + 1),
        "model": vals.get("name", ""),
        "creator": creator.get("name", ""),
        "elo": round(vals.get("elo", 0), 2),
        "ci": vals.get("ci", ""),
        "samples": vals.get("appearances", 0),
        "released": vals.get("released", ""),
        "price_per_1k_images": vals.get("pricePer1kImages", None),
        "win_rate": round(vals.get("winRate", 0) * 100, 1),
        "is_open_weights": vals.get("openWeightsUrl") is not None,
        "is_current": vals.get("isCurrent", False),
    }


def _extract_sc_inline_entries(js: str) -> list[dict]:
    entries = []
    pattern = r'\{rank:\d+,model:"[^"]+",org:"[^"]+",median:[\d.]+'
    for m in re.finditer(pattern, js):
        start = m.start()
        depth, end = 0, start
        for i in range(start, min(start + 500, len(js))):
            if js[i] == "{":
                depth += 1
            elif js[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        entry_str = js[start:end]
        json_str = re.sub(r"(\w+)\s*:", r'"\1":', entry_str)
        json_str = json_str.replace('""', '"')
        try:
            entries.append(json.loads(json_str))
        except json.JSONDecodeError:
            pass
    return entries


_SC_CATEGORY_KEYWORDS = {
    "text_to_image":  ["flux", "imagen", "gpt-image", "nano-banana", "nano banana", "seedream", "dall-e", "qwen-image", "midjourney", "firefly"],
    "text_to_video":  ["seedance", "sora", "runway gen", "wan", "cogvideo", "vidu q3"],
    "image_to_video": ["kling", "可灵", "veo", "pixverse", "dreamina", "gen-3", "gen-4", "hailuo", "luma"],
    "text_to_speech": ["tts", "speech", "voice", "语音", "azure neural", "doubao-seed-tts"],
    "ref_to_video":   ["vidu q2", "vivago", "pika", "kling 1", "可灵 1"],
    "web_coding":     ["glm", "qwen3", "kimi", "claude", "gpt-5", "deepseek", "gemini-3"],
}


def _sc_sanity_check(name: str, group: list[dict]) -> None:
    """粗粒度检查：用 Top-5 模型名关键词反推真实类别，若与 config 分配严重不符就 WARNING。
    （2026-04-24 SuperCLUE 改 JS bundle 顺序一次，把 text_to_video 的内容放进了 text_to_image 槽；靠这个护栏早期发现。）"""
    if name not in _SC_CATEGORY_KEYWORDS:
        return
    top5 = [str(e.get("model", "")).lower() for e in group[:5]]
    if not top5:
        return
    scores: dict[str, int] = {}
    for cat, kws in _SC_CATEGORY_KEYWORDS.items():
        scores[cat] = sum(1 for m in top5 for kw in kws if kw in m)
    best_cat = max(scores, key=scores.get) if scores else name
    if scores.get(best_cat, 0) >= 2 and best_cat != name and scores[best_cat] > scores.get(name, 0):
        logger.warning(
            "[SC] 类别疑似错配：config 标为 %r 但 Top5 关键词更像 %r | Top5=%s | 请检查 backend/config/leaderboard.json 的 superclue.category_order",
            name, best_cat, top5,
        )


def _split_by_rank1(entries: list[dict]) -> list[list[dict]]:
    categories = []
    current = []
    for entry in entries:
        if entry.get("rank") == 1 and current:
            categories.append(current)
            current = []
        current.append(entry)
    if current:
        categories.append(current)
    return categories


def scrape_all() -> dict[str, dict[str, list[dict]]]:
    """统一入口：一次抓完三源。返回 {'lmarena': {...}, 'aa': {...}, 'superclue': {...}}"""
    return {
        "lmarena": scrape_lmarena(),
        "aa": scrape_artificial_analysis(),
        "superclue": scrape_superclue(),
    }
