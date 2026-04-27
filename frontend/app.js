// ModelRadar Dashboard v3 · 单请求 /api/dashboard → 6 板块渲染。

const $ = (sel) => document.querySelector(sel);

const state = {
  lbTab: "llm",                       // llm | t2i | t2v | i2v
  lbSource: 0,                        // 当前 tab 下的 source 索引
  lbData: null,                       // 缓存最近一次 dashboard.leaderboards
  lbFilter: { openness: "all" },      // 模型榜的开/闭源过滤
  companiesTab: "llm",                // 公司榜 tab：llm | code | t2i | t2v | i2v
  companiesData: null,                // 缓存最近一次 dashboard.companies
  hfFilter: { openness: "all" },      // HF 看板 开/闭源过滤（同时作用于 Trending + Downloads）
  hfData: null,                       // 缓存最近一次 dashboard.hf
  orFilter: { openness: "all" },      // OpenRouter 周榜 开/闭源过滤
  orData: null,                       // 缓存最近一次 dashboard.openrouter
};

async function jfetch(url, opts = {}) {
  const r = await fetch(url, { credentials: "include", ...opts });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

function parseUtc(iso) {
  if (!iso) return null;
  return new Date(iso.replace(" ", "T") + "Z");
}

function fmtDate(iso) {
  const d = parseUtc(iso); if (!d) return "—";
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${mm}-${dd} ${hh}:${mi}`;
}

function relTime(iso) {
  const d = parseUtc(iso); if (!d) return "—";
  const diff = Date.now() - d.getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}秒前`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}分钟前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}小时前`;
  const dd = Math.floor(h / 24);
  return `${dd}天前`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function setUpdated(sel, iso) {
  const el = $(sel);
  if (!el) return;
  if (!iso) { el.textContent = "—"; return; }
  el.textContent = "更新于 " + relTime(iso);
  el.title = iso;
}

// ── 开源/闭源判定（HF / OR / 模型榜共用）──

const OPEN_ORGS = new Set([
  "deepseek-ai", "deepseek", "moonshotai", "moonshot", "qwenlm", "qwen", "thudm", "z-ai", "meta-llama", "meta",
  "mistralai", "stepfun-ai", "stepfun", "minimax-ai", "minimax", "alibaba-nlp", "alibaba", "01-ai",
  "baidu", "tencent", "xtuner", "internlm",
  // 通用开源贡献者 / 量化器：他们 host 的几乎都是开源模型的 fork / quant
  "nvidia", "xiaomi", "inclusionai", "ant-research",
  "unsloth", "bartowski", "mradermacher", "thebloke", "nousresearch", "lmsys", "lmsys-org",
]);

// 模型名级开/闭源判定。按数组顺序匹配，先命中先赢。
// FLUX / Mistral 这类一家公司同时有开闭源的，用子款式 (dev/schnell vs pro/max) 细分。
const OPENNESS_PATTERNS = [
  // FLUX 细分：dev/schnell/klein 开源；pro/max/ultra/kontext-pro 闭源
  // [\s.\-\d]* 允许 flux 和子款式之间混合数字 / 点 / 空格 / 连字符（如 FLUX.1-dev / FLUX 1.1 Pro）
  { re: /\bflux[\s.\-\d]*(?:dev|schnell|klein)\b/i, v: "open" },
  { re: /\bflux[\s.\-\d]*(?:pro|max|ultra|kontext[\s.\-]*pro)\b/i, v: "closed" },
  // Qwen3-Max 闭源（其它 Qwen 开源，放下面）
  { re: /\bqwen3?[\s.\-]*max\b/i, v: "closed" },
  // Mistral Large 闭源（其它 Mistral/Mixtral 开源）
  { re: /\bmistral[\s.\-]*large\b/i, v: "closed" },

  // gpt-oss 是 OpenAI 的开源权重模型，必须先于下面的 gpt 闭源 pattern 命中
  { re: /\bgpt[\s.\-]?oss\b/i, v: "open" },

  // 开源模型
  { re: /\b(?:deepseek|kimi[\s.\-]*k?\d+|glm[\s.\-]*\d|qwen[\s\-.]?(?:\d|image|coder|audio|math|vl|qwq)|qwq|yi[\s.\-]*\d|llama[\s.\-]*\d?|mistral|mixtral|hunyuan|wan[\s.\-]*[0-9v]|cogvideox|open[\s.\-]?sora|ltx[\s.\-]?video|mochi|skyreels|bagel|hidream|stable[\s.\-]?diffusion|sdxl|sd[\s.\-]?\d|step[\s.\-]*\d|minimax[\s.\-]?m\d|internlm|baichuan|phi[\s.\-]*\d|nemotron|mimo[\s.\-]?v?\d|ling[\s.\-]?\d)/i, v: "open" },

  // 闭源模型
  { re: /\b(?:gpt[\s.\-]?[345]|gpt[\s.\-]?4[\w\-]*|chatgpt|o[1-4][\s.\-]?(?:mini|preview|pro)?|claude|gemini|nano[\s.\-]?banana|grok|nova[\s.\-]?(?:pro|lite|micro)?|titan|command[\s.\-]?[ra]?|doubao|ernie|文心|sora|veo[\s.\-]?\d?|kling|runway|pika|hailuo|pixverse|vidu|luma|ray[\s.\-]?\d|seedance|seedream|dreamina|marey|haiper|midjourney|ideogram|dall[\s.\-]?e|imagen|firefly|recraft|happy[\s.\-]?horse)|可灵|海螺|即梦/i, v: "closed" },
];

function opennessByName(text) {
  if (!text) return null;
  for (const { re, v } of OPENNESS_PATTERNS) {
    if (re.test(text)) return v;
  }
  return null;
}

// HF 全 id ("deepseek-ai/DeepSeek-V4-Pro") + OR slug 都用这个统一判定。
// 优先 name regex（命中"gpt"/"claude"/"deepseek"等明确品牌）；
// 没命中再用 author 做开源白名单兜底（不用 CLOSED_ORGS 兜底——HF 上 openai/whisper、google/gemma 都是开权重）。
function modelOpenness(text, author) {
  const v = opennessByName(text);
  if (v) return v;
  if (author && OPEN_ORGS.has(String(author).toLowerCase())) return "open";
  return null;
}

function opennessChip(v) {
  if (v === "open")   return ` <span class="tag-chip tag-open-open" style="margin-left:6px">开</span>`;
  if (v === "closed") return ` <span class="tag-chip tag-open-closed" style="margin-left:6px">闭</span>`;
  return "";
}

// 通用 openness filter 渲染 + 行筛选辅助
function opennessFilterRow(currentValue, dataAttr) {
  return `
    <div class="alert-filter-row lb-filter-row">
      <span class="filter-label">开源</span>
      <button class="filter-chip ${currentValue === 'all' ? 'active' : ''}" data-${dataAttr}="all">全部</button>
      <button class="filter-chip ${currentValue === 'open' ? 'active' : ''}" data-${dataAttr}="open">开源</button>
      <button class="filter-chip ${currentValue === 'closed' ? 'active' : ''}" data-${dataAttr}="closed">闭源</button>
    </div>`;
}

// ── Panel 1 · 发布 ──
function renderReleasesPanel(d) {
  setUpdated("#u-releases", d.updated_at);
  if (!d.items?.length) {
    $("#p-releases").classList.remove("loading");
    $("#p-releases").innerHTML = `<div class="empty">近 48 小时没有新发布</div>`;
    return;
  }
  const typeLabel = {
    new_release: ["release", "Release"],
    new_repo: ["repo", "新仓库"],
    new_blog_post: ["blog", "博客"],
    star_surge: ["repo", "Star 暴涨"],
  };
  $("#p-releases").classList.remove("loading");
  $("#p-releases").innerHTML = d.items.map((e) => {
    const src = esc(e.source || "");
    const isWechat = src.startsWith("wechat_");
    const [cls, label] = isWechat ? ["wechat", "公众号"] : (typeLabel[e.event_type] || ["", e.event_type]);
    const link = e.detail?.url || e.detail?.html_url;
    const titleHtml = link
      ? `<a href="${esc(link)}" target="_blank" rel="noopener">${esc(e.title)}</a>`
      : esc(e.title);
    const author = isWechat ? src.replace(/^wechat_/, "") : src;
    return `
      <div class="release-item">
        <div class="rel-head">
          <span class="rel-chip ${cls}">${esc(label)}</span>
          ${e.model_name ? `<span class="rel-chip">${esc(e.model_name)}</span>` : ""}
        </div>
        <div class="rel-title">${titleHtml}</div>
        <div class="rel-meta">
          <span>${esc(author)}</span>
          <span>·</span>
          <span>${relTime(e.created_at)}</span>
        </div>
      </div>`;
  }).join("");
}

// ── Panel 2 · 榜单（4 tabs）──
function deltaSpan(delta) {
  if (delta === "new") return `<span class="delta delta-new">新</span>`;
  if (delta == null || delta === 0) return `<span class="delta delta-flat">—</span>`;
  if (delta > 0) return `<span class="delta delta-up">↑${delta}</span>`;
  return `<span class="delta delta-down">↓${-delta}</span>`;
}

function renderLbPanel(d) {
  setUpdated("#u-lb", d.updated_at);
  state.lbData = d;
  const cat = d.categories?.[state.lbTab];
  const holder = $("#p-lb");
  holder.classList.remove("loading");
  if (!cat || !cat.sources?.length) {
    holder.innerHTML = `<div class="empty">暂无该类目数据</div>`;
    return;
  }
  if (state.lbSource >= cat.sources.length) state.lbSource = 0;
  const src = cat.sources[state.lbSource];

  const chips = cat.sources.map((s, i) => {
    const active = i === state.lbSource ? "active" : "";
    const hasData = (s.items || []).length > 0;
    const jump = s.url
      ? `<a class="lb-jump" href="${esc(s.url)}" target="_blank" rel="noopener" title="去 ${esc(s.label)} 官网">↗</a>`
      : "";
    return `<span class="lb-source-chip ${active}${hasData ? "" : " empty"}" data-src-idx="${i}">
      <button type="button" class="chip-label" data-src-idx="${i}">${esc(s.label)}${hasData ? "" : " · 无"}</button>
      ${jump}
    </span>`;
  }).join("");
  const chipsHtml = `<div class="lb-source-row">${chips}</div>`;

  // 开/闭源 filter 行：复用 alert-filter-row 样式
  const f = state.lbFilter;
  const filterHtml = `
    <div class="alert-filter-row lb-filter-row">
      <span class="filter-label">开源</span>
      <button class="filter-chip ${f.openness === 'all' ? 'active' : ''}" data-lbf="openness" data-v="all">全部</button>
      <button class="filter-chip ${f.openness === 'open' ? 'active' : ''}" data-lbf="openness" data-v="open">开源</button>
      <button class="filter-chip ${f.openness === 'closed' ? 'active' : ''}" data-lbf="openness" data-v="closed">闭源</button>
    </div>`;

  if (!src.items?.length) {
    holder.innerHTML = chipsHtml + filterHtml + `<div class="empty">该平台暂无数据</div>`;
  } else {
    // 仅 lmarena + LLM 榜单在第三列展示价格（OR 官方 extra_json.price_per_1m_tokens，形如 "$5/$25"）
    const showPrice = src.source === "lmarena" && state.lbTab === "llm";
    const col3Label = showPrice ? "Price $/M" : "评分";
    const headerHtml = `
      <div class="lb-row lb-header">
        <span class="rank">#</span>
        <span class="name">模型</span>
        <span class="score">${col3Label}</span>
        <span class="delta">Δ</span>
      </div>`;
    // 开/闭源过滤：'all' 不过滤；'open'/'closed' 时 unknown 模型一并隐藏（避免误归类）
    const filtered = src.items.filter((r) => {
      if (state.lbFilter.openness === "all") return true;
      return opennessByName(r.model_name) === state.lbFilter.openness;
    });
    const rowsHtml = filtered.length === 0
      ? `<div class="empty" style="padding:18px">当前过滤下无匹配项</div>`
      : filtered.map((r) => {
          // lmarena 的 score 形如 "1504±9"（字符串），aa/superclue 是数字；都透传为字串展示
          const scoreRaw = r.score;
          const scoreText = scoreRaw == null ? "—"
            : (typeof scoreRaw === "number" ? scoreRaw.toFixed(0) : String(scoreRaw));
          const col3 = showPrice
            ? (r.price_per_1m_tokens ? esc(r.price_per_1m_tokens) : "—")
            : esc(scoreText);
          const op = opennessByName(r.model_name);
          const opChip = op ? `<span class="tag-chip tag-open-${op}" style="margin-left:6px">${op === 'open' ? '开' : '闭'}</span>` : "";
          return `
          <div class="lb-row">
            <span class="rank">#${r.rank}</span>
            <span class="name" title="${esc(r.model_name)}">${esc(r.model_name)}${opChip}</span>
            <span class="score">${col3}</span>
            ${deltaSpan(r.delta)}
          </div>`;
        }).join("");
    holder.innerHTML = chipsHtml + filterHtml + headerHtml + rowsHtml;
  }

  holder.querySelectorAll(".chip-label[data-src-idx]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.lbSource = parseInt(btn.dataset.srcIdx);
      renderLbPanel(state.lbData);
    });
  });
  holder.querySelectorAll(".filter-chip[data-lbf]").forEach((b) => {
    b.addEventListener("click", () => {
      state.lbFilter[b.dataset.lbf] = b.dataset.v;
      renderLbPanel(state.lbData);
    });
  });
}

// ── Panel 2·b · 公司榜（LMArena By Lab，5 tabs）──
function renderCompaniesPanel(d) {
  setUpdated("#u-companies", d.updated_at);
  state.companiesData = d;
  const holder = $("#p-companies");
  holder.classList.remove("loading");
  const tabs = d.tabs || [];
  if (!tabs.length) {
    holder.innerHTML = `<div class="empty">暂无公司榜数据</div>`;
    return;
  }
  // 找当前 tab；找不到（首次或 tab 被下线）回落第一个有数据的
  let activeIdx = tabs.findIndex((t) => t.key === state.companiesTab);
  if (activeIdx < 0) {
    activeIdx = tabs.findIndex((t) => (t.items || []).length > 0);
    if (activeIdx < 0) activeIdx = 0;
    state.companiesTab = tabs[activeIdx].key;
  }
  const active = tabs[activeIdx];

  const chips = tabs.map((t) => {
    const isActive = t.key === state.companiesTab ? "active" : "";
    const hasData = (t.items || []).length > 0;
    const jump = t.url
      ? `<a class="lb-jump" href="${esc(t.url)}" target="_blank" rel="noopener" title="去 ${esc(t.label)}">↗</a>`
      : "";
    return `<span class="lb-source-chip ${isActive}${hasData ? "" : " empty"}">
      <button type="button" class="chip-label" data-comp-tab="${esc(t.key)}">${esc(t.label)}${hasData ? "" : " · 无"}</button>
      ${jump}
    </span>`;
  }).join("");
  const chipsHtml = `<div class="lb-source-row">${chips}</div>`;

  const headerRow = `
    <div class="lb-row lb-header">
      <span class="rank">#</span>
      <span class="name">公司 / Lab</span>
      <span class="score">评分</span>
      <span class="delta">Δ</span>
    </div>`;
  const items = active.items || [];
  const rowsHtml = items.length === 0
    ? `<div class="empty" style="padding:18px">该类目暂无数据</div>`
    : items.map((r) => {
        const scoreRaw = r.score;
        const scoreText = scoreRaw == null ? "—"
          : (typeof scoreRaw === "number" ? scoreRaw.toFixed(0) : String(scoreRaw));
        return `
          <div class="lb-row">
            <span class="rank">#${r.rank}</span>
            <span class="name" title="${esc(r.model_name)}">${esc(r.model_name)}</span>
            <span class="score">${esc(scoreText)}</span>
            ${deltaSpan(r.delta)}
          </div>`;
      }).join("");
  holder.innerHTML = chipsHtml + (items.length ? headerRow : "") + rowsHtml;

  holder.querySelectorAll(".chip-label[data-comp-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.companiesTab = btn.dataset.compTab;
      renderCompaniesPanel(state.companiesData);
    });
  });
}

// ── Panel 3 · HuggingFace ──
function renderHfPanel(d) {
  setUpdated("#u-hf", d.updated_at);
  state.hfData = d;
  const f = state.hfFilter.openness;
  const filterHtml = opennessFilterRow(f, "hff");

  const sect = (label, items, sectionUrl) => {
    const jump = sectionUrl
      ? `<a class="lb-jump" href="${esc(sectionUrl)}" target="_blank" rel="noopener" title="去 HuggingFace ${esc(label)}">↗</a>`
      : "";
    const head = `<div class="hf-section-title">${label} ${jump}</div>`;
    if (!items?.length) return head + `<div class="empty" style="padding:16px">—</div>`;
    // openness 判定：HF 全 id 是 "author/Model-Name" 格式，author 直接做白名单兜底
    const filtered = items.filter((r) => {
      if (f === "all") return true;
      const author = (r.author || (r.model_id || "").split("/")[0] || "").toLowerCase();
      return modelOpenness(r.model_id || "", author) === f;
    });
    if (filtered.length === 0) {
      return head + `<div class="empty" style="padding:14px">当前过滤下无匹配项</div>`;
    }
    const rows = filtered.map((r) => {
      const href = r.model_id ? `https://huggingface.co/${r.model_id}` : null;
      const author = (r.author || (r.model_id || "").split("/")[0] || "").toLowerCase();
      const op = modelOpenness(r.model_id || "", author);
      const nameHtml = href
        ? `<a class="name" href="${esc(href)}" target="_blank" rel="noopener" title="${esc(r.model_id)}">${esc(r.model_id)}${opennessChip(op)}</a>`
        : `<span class="name" title="${esc(r.model_id)}">${esc(r.model_id)}${opennessChip(op)}</span>`;
      return `
      <div class="list-row">
        <span class="rank">#${r.rank}</span>
        ${nameHtml}
        <span class="score">${r.downloads ? (r.downloads/1000).toFixed(0) + "K" : (r.likes ? r.likes + "♥" : "—")}</span>
        <span></span>
      </div>`;
    }).join("");
    return head + rows;
  };
  const holder = $("#p-hf");
  holder.classList.remove("loading");
  holder.innerHTML = filterHtml +
    sect("🔥 Trending", d.trending, "https://huggingface.co/models?sort=trending") +
    sect("⬇ Downloads", d.downloads, "https://huggingface.co/models?sort=downloads");

  holder.querySelectorAll(".filter-chip[data-hff]").forEach((b) => {
    b.addEventListener("click", () => {
      state.hfFilter.openness = b.dataset.hff;
      renderHfPanel(state.hfData);
    });
  });
}

// ── Panel 4 · OpenRouter ──
function renderOrPanel(d) {
  setUpdated("#u-or", d.updated_at);
  state.orData = d;
  const holder = $("#p-or");
  holder.classList.remove("loading");
  if (!d.items?.length) {
    holder.innerHTML = `<div class="empty">暂无数据</div>`;
    return;
  }
  const headerLink = `<a href="https://openrouter.ai/rankings" target="_blank" rel="noopener" class="jump-inline">↗ 去 OpenRouter</a>`;
  const header = `<div class="or-header">${d.week_date ? "周 · " + esc(d.week_date) : ""}${headerLink}</div>`;
  const f = state.orFilter.openness;
  const filterHtml = opennessFilterRow(f, "orf");

  const filtered = d.items.filter((r) => {
    if (f === "all") return true;
    const text = (r.display_name || "") + " " + (r.model_permaslug || "");
    return modelOpenness(text, r.author) === f;
  });

  const rowsHtml = filtered.length === 0
    ? `<div class="empty" style="padding:18px">当前过滤下无匹配项</div>`
    : filtered.map((r) => {
        const name = r.display_name || r.model_permaslug;
        const tok = r.total_tokens ? `${(r.total_tokens/1e9).toFixed(1)}B tok` : "—";
        const chgPct = r.change_pct != null ? (r.change_pct * 100) : null;
        const chg = chgPct != null
          ? (chgPct >= 0
              ? `<span class="delta delta-up">+${chgPct.toFixed(0)}%</span>`
              : `<span class="delta delta-down">${chgPct.toFixed(0)}%</span>`)
          : `<span class="delta delta-flat">—</span>`;
        const href = (r.author && r.model_permaslug)
          ? `https://openrouter.ai/${r.author}/${r.model_permaslug}`
          : null;
        const op = modelOpenness((name || "") + " " + (r.model_permaslug || ""), r.author);
        const nameHtml = href
          ? `<a class="name" href="${esc(href)}" target="_blank" rel="noopener" title="${esc(name)}">${esc(name)}${opennessChip(op)}</a>`
          : `<span class="name" title="${esc(name)}">${esc(name)}${opennessChip(op)}</span>`;
        return `
          <div class="list-row">
            <span class="rank">#${r.rank}</span>
            ${nameHtml}
            <span class="score">${tok}</span>
            ${chg}
          </div>`;
      }).join("");
  holder.innerHTML = header + filterHtml + rowsHtml;

  holder.querySelectorAll(".filter-chip[data-orf]").forEach((b) => {
    b.addEventListener("click", () => {
      state.orFilter.openness = b.dataset.orf;
      renderOrPanel(state.orData);
    });
  });
}

// ── Panel 5 · 社区声音 (opinions) ──
// reddit_opinions.generate() payload：{models: [{model, post_count, opinions:[{quote,url}], used_llm}]}
function renderOpinionsPanel(d) {
  setUpdated("#u-opinions", d.generated_at);
  const models = d.payload?.models;
  if (!models?.length) {
    $("#p-opinions").classList.remove("loading");
    $("#p-opinions").innerHTML = `<div class="empty">聚合缓存为空<br><span style="font-size:11px">每 12h 自动生成</span></div>`;
    return;
  }
  $("#p-opinions").classList.remove("loading");
  $("#p-opinions").innerHTML = models.slice(0, 5).map((m) => {
    const name = m.model || m.name || m.model_name || "—";
    const postCount = m.post_count ?? 0;
    const opinions = Array.isArray(m.opinions) ? m.opinions : (Array.isArray(m.quotes) ? m.quotes : []);
    const meta = postCount
      ? `<span class="meta">· ${postCount} 帖 · ${opinions.length} 条观点</span>`
      : `<span class="meta">· ${opinions.length} 条观点</span>`;
    const quotesHtml = opinions.slice(0, 3).map((o) => {
      const quoteText = o.quote || o.text || String(o);
      if (!quoteText) return "";
      const quoteUrl = o.url || o.permalink || "";
      const srcLabel = o.source === "comment" ? "评论" : (o.source === "post" ? "原帖" : "");
      const badge = srcLabel
        ? `<span class="src-badge src-${esc(o.source)}">${srcLabel}</span>`
        : "";
      const body = `${badge}「${esc(String(quoteText).slice(0, 160))}」`;
      return quoteUrl
        ? `<a class="quote" href="${esc(quoteUrl)}" target="_blank" rel="noopener">${body}</a>`
        : `<div class="quote">${body}</div>`;
    }).join("");
    return `
      <div class="opinion-item">
        <div class="model">${esc(name)} ${meta}</div>
        ${quotesHtml}
      </div>`;
  }).join("");
}

// ── Panel 6 · 本周热议 (themes) ──
function renderThemesPanel(d) {
  setUpdated("#u-themes", d.generated_at);
  const themes = d.payload?.themes;
  if (!themes?.length) {
    $("#p-themes").classList.remove("loading");
    $("#p-themes").innerHTML = `<div class="empty">聚合缓存为空<br><span style="font-size:11px">每 12h 自动生成</span></div>`;
    return;
  }
  $("#p-themes").classList.remove("loading");
  $("#p-themes").innerHTML = `<div class="themes-grid">${themes.map((t) => {
    const title = t.title || t.theme || "主题";
    const desc = t.summary || t.description || t.desc || "";
    const posts = Array.isArray(t.posts) ? t.posts : (Array.isArray(t.examples) ? t.examples : []);
    const postList = posts.slice(0, 3).map((p) => {
      const href = p.url || p.permalink || "#";
      const ptitle = p.title || p.text || p.snippet || href;
      return `<a class="post-link" href="${esc(href)}" target="_blank" rel="noopener">• ${esc(ptitle)}</a>`;
    }).join("");
    return `
      <div class="theme-card">
        <h3>${esc(title)}</h3>
        ${desc ? `<div class="desc">${esc(desc)}</div>` : ""}
        ${postList ? `<div class="posts">${postList}</div>` : ""}
      </div>`;
  }).join("")}</div>`;
}

// ── 顶层 loader ──
async function loadDashboard() {
  const d = await jfetch("/api/dashboard");
  renderReleasesPanel(d.releases);
  renderLbPanel(d.leaderboards);
  renderCompaniesPanel(d.companies);
  renderHfPanel(d.hf);
  renderOrPanel(d.openrouter);
  renderOpinionsPanel(d.opinions);
  renderThemesPanel(d.themes);
}

// ── 底栏统计 + 最后刷新时间 + 调度健康 ──
async function loadStatus() {
  const s = await jfetch("/api/status");
  const c = s.counts || {};
  const statsHtml = [
    ["榜单快照", c.leaderboard_rows],
    ["仓库", c.github_repos],
    ["Release", c.github_releases],
    ["变动事件", c.change_events],
  ].filter(([, v]) => v != null)
   .map(([k, v]) => `<span>${esc(k)} ${v.toLocaleString()}</span>`)
   .join(`<span class="sep">·</span>`);
  $("#footer-stats").innerHTML = statsHtml;
  $("#last-update").textContent = "刷新 " + new Date().toTimeString().slice(0, 5);

  renderScheduleHealth(s.collectors || []);
}

// "240" (分钟) → "4h" / "30m" / "7d" 人类可读
function fmtInterval(min) {
  if (min == null) return "—";
  if (min < 60) return `${min}m`;
  if (min < 1440) return `${(min / 60).toFixed(min % 60 ? 1 : 0)}h`;
  return `${(min / 1440).toFixed(min % 1440 ? 1 : 0)}d`;
}

function renderScheduleHealth(collectors) {
  const holder = $("#schedule-health");
  if (!holder) return;
  if (!collectors.length) {
    holder.innerHTML = `<div class="empty" style="padding:16px">暂无调度数据</div>`;
    $("#schedule-overdue-count").textContent = "0";
    return;
  }
  // 排序：超期的在前，按超期时间降序；其余按 collector 名字母序
  const sorted = collectors.slice().sort((a, b) => {
    if (a.is_overdue !== b.is_overdue) return a.is_overdue ? -1 : 1;
    if (a.is_overdue && b.is_overdue) return (b.overdue_by_min || 0) - (a.overdue_by_min || 0);
    return a.collector.localeCompare(b.collector);
  });
  const overdueCount = collectors.filter((r) => r.is_overdue).length;
  $("#schedule-overdue-count").textContent = String(overdueCount);
  const badge = $("#schedule-overdue-count");
  if (badge) badge.style.color = overdueCount ? "var(--accent)" : "var(--faint)";

  const rows = sorted.map((r) => {
    const last = r.last_run_at ? relTime(r.last_run_at) : "从未运行";
    const interval = fmtInterval(r.expected_interval_min);
    const fails = r.consecutive_fails || 0;
    let statusHtml;
    if (!r.last_run_at) {
      statusHtml = `<span class="sh-status sh-never">⚪ 未跑过</span>`;
    } else if (r.is_overdue) {
      statusHtml = `<span class="sh-status sh-overdue">🔴 超期 ${fmtInterval(Math.round(r.overdue_by_min))}</span>`;
    } else if (fails > 0) {
      statusHtml = `<span class="sh-status sh-failing">🟠 连续失败 ${fails}</span>`;
    } else {
      statusHtml = `<span class="sh-status sh-ok">🟢 正常</span>`;
    }
    const errTip = r.last_error ? ` title="${esc(r.last_error).slice(0, 200)}"` : "";
    return `
    <div class="sh-row"${errTip}>
      <span class="sh-name">${esc(r.collector)}</span>
      <span class="sh-last">${last}</span>
      <span class="sh-interval">每 ${interval}</span>
      ${statusHtml}
    </div>`;
  }).join("");
  const header = `
    <div class="sh-row sh-header">
      <span class="sh-name">Collector</span>
      <span class="sh-last">最后运行</span>
      <span class="sh-interval">期望节奏</span>
      <span>状态</span>
    </div>`;
  const hint = overdueCount
    ? `<div class="sh-hint">⚠ ${overdueCount} 个板块超过期望节奏。SSH 查 log：<code>railway logs --filter '${esc(sorted[0].collector)}'</code></div>`
    : `<div class="sh-hint">✓ 全部 collector 在节奏内运行</div>`;
  holder.innerHTML = hint + header + rows;
}

// ── 历史周报 ──
async function loadWeeklyReports() {
  const rows = await jfetch("/api/weekly-reports?limit=12");
  const holder = $("#weekly-reports");
  if (!rows.length) {
    holder.innerHTML = `<div style="padding:24px;color:var(--muted);text-align:center;">还没有周报（下周五 19:00 自动生成）</div>`;
    return;
  }
  holder.innerHTML = rows.map((r) => {
    const sent = r.sent_at ? `📧 已发 ${fmtDate(r.sent_at)}` : `⚪ 未发送`;
    const st = r.stats || {};
    const postCount = st.digest?.post_count ?? 0;
    const eventCount = st.events_count ?? 0;
    return `
      <div class="weekly-row">
        <span class="week">${esc(r.week_number)}</span>
        <span class="meta">${eventCount} 事件 · ${postCount} 帖 · ${sent}</span>
        <a class="btn view-btn" data-week="${esc(r.week_number)}" href="#">查看</a>
      </div>`;
  }).join("");
  holder.querySelectorAll(".view-btn").forEach((b) => {
    b.addEventListener("click", async (ev) => {
      ev.preventDefault();
      const w = b.dataset.week;
      const data = await jfetch(`/api/weekly-reports/${encodeURIComponent(w)}`);
      openReportModal(w, data.html);
    });
  });
}

function openReportModal(week, html) {
  let modal = $("#report-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "report-modal";
    modal.className = "report-modal";
    modal.innerHTML = `
      <div class="report-modal-inner">
        <div class="report-modal-head">
          <span id="report-modal-title"></span>
          <button class="btn" id="report-modal-close">✕</button>
        </div>
        <iframe id="report-frame" sandbox="allow-same-origin"></iframe>
      </div>`;
    document.body.appendChild(modal);
    $("#report-modal-close").addEventListener("click", () => modal.classList.remove("open"));
    modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.remove("open"); });
  }
  $("#report-modal-title").textContent = `周报 · ${week}`;
  const iframe = $("#report-frame");
  iframe.srcdoc = html;
  modal.classList.add("open");
}

// ── Wire ──
async function refreshAll() {
  await Promise.all([
    loadDashboard().catch((e) => console.error("dashboard", e)),
    loadStatus().catch((e) => console.error("status", e)),
  ]);
  if ($("#weekly-reports-section").open) loadWeeklyReports().catch((e) => console.error("weekly", e));
}

// Leaderboard tab 切换
document.querySelectorAll(".lb-tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".lb-tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    state.lbTab = t.dataset.tab;
    state.lbSource = 0;  // 换 tab 时回到第一个 source
    if (state.lbData) renderLbPanel(state.lbData);
  });
});

// 折叠区 lazy load
$("#weekly-reports-section").addEventListener("toggle", (ev) => {
  if (ev.target.open) loadWeeklyReports().catch((e) => console.error("weekly", e));
});

$("#refresh").addEventListener("click", refreshAll);

refreshAll();
setInterval(refreshAll, 60000);
