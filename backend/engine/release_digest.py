"""Release Digest：GitHub 本周 release 过滤 + LLM 批量写"参数变化 + 突破点"一句话。

过滤规则：
- is_prerelease=1 → 跳
- tag 或 release_name 含 `nightly`/`rc`/`alpha`/`beta`/`preview`（大小写不敏感）→ 跳

剩下的喂给 LLM，让它每条返回：
- one_liner: "参数变化 + 突破点"一句话（40-80 字）
- kind:     "model" / "tool" / "framework" / "eval" / "other"
- paper_url: 如果 body_preview 提到 arxiv/paper link 就抽出来，否则 ""

批量调用一次，减少 API 往返。失败时降级到模板（用 release_name 本身）。
"""
import json
import logging
import re
from datetime import datetime, timedelta

from backend.db import get_conn
from backend.utils import llm_client

logger = logging.getLogger(__name__)

_NOISE_WORDS = re.compile(r"(nightly|\brc\d*\b|\balpha\d*\b|\bbeta\d*\b|preview)", re.IGNORECASE)


HUMANIZER_PRINCIPLES = """写作原则：
- 陈述事实，不做营销渲染。禁用："展现了卓越能力"/"充分体现了"/"令人瞩目"/"里程碑式"/"革命性"/"引领行业潮流"。
- 短句优先。能一句说完不分两句。
- 具体 > 抽象：指名参数（专家数/上下文长度/活跃参数/指标分数），指名突破点（对比上一代或竞品）。

【关键】当 release body 信息稀薄（只是"版本迭代"/"bug fix"之类）时：
  不要只写"版本迭代，未披露参数变化"这种废话——读者不知道这个 repo 是干嘛的。
  你必须结合 repo 名称 + description + topics + 常识，**先用一句话说清楚"这个项目是做什么的"**，
  再提一下本次版本看起来改了什么（如果 body 里有任何信号）。
  例子：
    ❌ "版本迭代，未披露参数变化。"
    ✅ "DeepGEMM 是 DeepSeek 的 FP8 GEMM 矩阵乘法核心库，本次小版本发布未披露具体改动。"
    ✅ "Qwen 代码示例库 v0.1，整理了 CoWork 功能的中英文文档，面向集成 Qwen API 的开发者。"
  如果 repo description 也是空的、topics 也没有、你对这个项目完全不了解，
  才写"未披露参数变化"——但也至少把 repo 归到 model/tool/framework/eval/other 里哪一类。"""


def _is_noise_release(tag: str, release_name: str | None, is_prerelease: int | None) -> bool:
    if is_prerelease:
        return True
    blob = f"{tag or ''} {release_name or ''}"
    if _NOISE_WORDS.search(blob):
        return True
    return False


def _fetch_raw(period_start_iso: str) -> list[dict]:
    """取"本周发布"的 release（按 published_at），并从 github_snapshots 拉对应 repo 的 description / topics，
    帮 LLM 解释项目本身是干嘛的。

    ⚠️ 冷启动陷阱：早期版本用 `WHERE published_at >= ? OR scraped_at >= ?`，
    导致首次 bootstrap 扫描时把 2024/2025 年的历史 release 也拉进来（因为它们的 scraped_at 就是今天）。
    改为只看 published_at——周报意图就是"本周发布"，不是"本周第一次扫到"。
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.org, r.repo_name, r.tag_name, r.release_name,
                   r.published_at, r.body_preview, r.html_url, r.is_prerelease,
                   s.description AS repo_description,
                   s.topics      AS repo_topics,
                   s.stars       AS repo_stars
            FROM github_releases r
            LEFT JOIN (
                SELECT org, repo_name, description, topics, stars,
                       ROW_NUMBER() OVER (PARTITION BY org, repo_name ORDER BY scraped_at DESC) AS rn
                FROM github_snapshots
            ) s
              ON s.org = r.org AND s.repo_name = r.repo_name AND s.rn = 1
            WHERE r.published_at >= ?
            ORDER BY r.published_at DESC
            """,
            (period_start_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def _filter_releases(raw: list[dict], max_total: int = 25) -> tuple[list[dict], int, int]:
    """过滤 noise + 按 repo 去重（每 repo 只留 published_at 最新）+ 限总数。

    返回 (kept, noise_dropped, dedup_dropped)。
    """
    noise_drop = 0
    pre_dedup = []
    for r in raw:
        if _is_noise_release(r["tag_name"], r["release_name"], r["is_prerelease"]):
            noise_drop += 1
            continue
        pre_dedup.append(r)

    # 按 repo dedup：raw 已按 published_at DESC，第一条就是最新
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in pre_dedup:
        key = f"{r['org']}/{r['repo_name']}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    dedup_drop = len(pre_dedup) - len(deduped)

    # 总量封顶（按 published_at DESC 取前 max_total）
    deduped.sort(key=lambda x: _ts(x.get("published_at") or ""), reverse=True)
    kept = deduped[:max_total]
    return kept, noise_drop, dedup_drop


def _release_label(r: dict) -> str:
    return f"{r['org']}/{r['repo_name']} {r['tag_name']}"


def _build_llm_prompt(releases: list[dict]) -> list[dict]:
    items = []
    for r in releases:
        body = (r.get("body_preview") or "").strip()
        if len(body) > 1200:
            body = body[:1200] + "…"
        # topics 存成 JSON 字符串，解析出来给 LLM 更易读
        topics_raw = r.get("repo_topics") or ""
        try:
            topics = json.loads(topics_raw) if topics_raw.startswith("[") else []
        except Exception:
            topics = []
        items.append({
            "id":               r["id"],
            "repo":             f"{r['org']}/{r['repo_name']}",
            "repo_description": (r.get("repo_description") or "").strip(),
            "repo_topics":      topics,
            "repo_stars":       r.get("repo_stars") or 0,
            "tag":              r["tag_name"],
            "release_name":     r["release_name"] or "",
            "published_at":     r["published_at"],
            "body":             body,
            "html_url":         r["html_url"],
        })

    user_content = (
        f"{HUMANIZER_PRINCIPLES}\n\n"
        f"任务：下面是本周 GitHub 的 release 列表。请**逐条**生成一个 JSON 对象，汇总成数组返回。\n"
        f"每条对象字段：\n"
        f'  - "id": 整数，必须对应输入数据里的 id\n'
        f'  - "kind": "model" / "tool" / "framework" / "eval" / "other" 五选一；'
        f'    "model" 指发布/更新了可用模型权重或 API；"tool" 指 CLI / agent / SDK；'
        f'    "framework" 指训练或推理框架（含推理 kernel/算子库如 DeepGEMM/DeepEP）；'
        f'    "eval" 指评测 benchmark；其他归 "other"。\n'
        f'  - "one_liner": 40-100 字中文一句话。\n'
        f'    · "model" 类：写清楚参数变化（专家数/活跃参数/上下文长度/指标分数等）+ 对比上一代或竞品的突破点。\n'
        f'    · 其他类：先点明该 repo 是做什么的（结合 repo_description / repo_topics / repo 名），再说本次改了什么。\n'
        f'    · body 稀薄时尤其重要：读者不认识这个 repo，你必须先解释项目定位，不要只写"未披露参数变化"。\n'
        f'  - "paper_url": 从 body 里抽出 arxiv / tech-report 链接（以 http 开头），抽不到就 ""。\n\n'
        f"输入数据（JSON，含 repo_description / repo_topics 帮你理解每个项目定位）：\n"
        f"{json.dumps(items, ensure_ascii=False, indent=2)}\n\n"
        f"直接输出 JSON，顶层是一个数组，不要加 Markdown 代码块，不要加解释文本。"
    )
    return [
        {"role": "system", "content": "你是一名中文技术周报作者，擅长快速提炼开源发布的关键参数与突破点。"},
        {"role": "user",   "content": user_content},
    ]


def _parse_llm_array(raw: str | None) -> list[dict]:
    if not raw:
        return []
    s = raw.strip()
    if s.startswith("```"):
        # 去掉可能的 ``` ... ``` 包裹
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
    except Exception as e:
        logger.warning("[Release] LLM 返回无法解析为 JSON: %s", e)
        return []
    if isinstance(obj, dict) and "items" in obj:  # 某些模型会包一层
        obj = obj["items"]
    return obj if isinstance(obj, list) else []


def _template_item(r: dict) -> dict:
    return {
        "id":        r["id"],
        "kind":      "other",
        "one_liner": f"{_release_label(r)} 发布（未接入 LLM 摘要）。",
        "paper_url": "",
    }


def _merge(releases: list[dict], llm_items: list[dict]) -> list[dict]:
    """把 LLM 返回的摘要按 id 对齐回原 release 记录。LLM 没覆盖的走模板兜底。"""
    by_id = {}
    for it in llm_items:
        if not isinstance(it, dict):
            continue
        try:
            by_id[int(it["id"])] = it
        except Exception:
            continue

    out = []
    for r in releases:
        llm = by_id.get(r["id"]) or _template_item(r)
        kind = (llm.get("kind") or "other").strip().lower()
        if kind not in {"model", "tool", "framework", "eval", "other"}:
            kind = "other"
        out.append({
            **r,
            "kind":      kind,
            "one_liner": (llm.get("one_liner") or "").strip() or _template_item(r)["one_liner"],
            "paper_url": (llm.get("paper_url") or "").strip(),
        })
    return out


# 排序：model > eval > framework > tool > other，同类按 published_at 倒序
_KIND_ORDER = {"model": 0, "eval": 1, "framework": 2, "tool": 3, "other": 4}


def generate(days: int = 7) -> dict:
    period_start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    raw = _fetch_raw(period_start)
    releases, noise_drop, dedup_drop = _filter_releases(raw, max_total=25)
    logger.info("[Release] 原始 %d / noise 过滤 %d / 同 repo 去重 %d / 保留 %d",
                len(raw), noise_drop, dedup_drop, len(releases))

    if not releases:
        return {
            "items":        [],
            "used_llm":     False,
            "raw_count":    len(raw),
            "noise_count":  noise_drop,
            "dedup_count":  dedup_drop,
            "kept_count":   0,
        }

    raw_llm = llm_client.chat(
        _build_llm_prompt(releases),
        temperature=0.2,
        max_tokens=4000,
    )
    llm_items = _parse_llm_array(raw_llm)
    used_llm = bool(llm_items)
    if not used_llm:
        logger.warning("[Release] LLM 降级到模板")

    merged = _merge(releases, llm_items)
    merged.sort(key=lambda x: (_KIND_ORDER.get(x["kind"], 99), x["published_at"] or ""),
                reverse=False)
    # published_at 需要按倒序：二次排序稳定，先按 kind 再按时间
    merged.sort(key=lambda x: (_KIND_ORDER.get(x["kind"], 99), -_ts(x.get("published_at") or "")))

    return {
        "items":        merged,
        "used_llm":     used_llm,
        "raw_count":    len(raw),
        "noise_count":  noise_drop,
        "dedup_count":  dedup_drop,
        "kept_count":   len(merged),
    }


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    r = generate()
    print(f"\nraw={r['raw_count']} noise_filtered={r['noise_count']} kept={r['kept_count']} used_llm={r['used_llm']}\n")
    for it in r["items"]:
        print(f"[{it['kind']:10s}] {it['org']}/{it['repo_name']} {it['tag_name']}")
        print(f"    {it['one_liner']}")
        if it["paper_url"]:
            print(f"    📄 {it['paper_url']}")
        print()
