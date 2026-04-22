// ModelRadar Dashboard v3 · 单请求 /api/dashboard → 6 板块渲染。

const $ = (sel) => document.querySelector(sel);

const state = {
  lbTab: "llm",                       // llm | t2i | t2v | i2v | extras
  lbSource: 0,                        // 当前 tab 下的 source 索引
  lbData: null,                       // 缓存最近一次 dashboard.leaderboards
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

// ── Alerts 顶部横条 ──
function renderAlertBar(alerts) {
  const count = alerts?.pending_count || 0;
  const bar = $("#alert-bar");
  const badge = $("#alert-badge");
  $("#alert-bar-count").textContent = count;
  $("#alert-count").textContent = count;
  if (count === 0) {
    bar.hidden = true;
    badge.hidden = true;
    return;
  }
  bar.hidden = false;
  badge.hidden = false;
  if (!bar.dataset.userToggled) bar.open = true;

  const recent = alerts.recent || [];
  $("#alert-list").innerHTML = recent.map((e) => {
    const link = e.detail?.url || e.detail?.html_url;
    const linkHtml = link ? ` <a href="${esc(link)}" target="_blank" rel="noopener">🔗</a>` : "";
    return `
      <div class="alert-item" data-id="${e.id}">
        <span class="sev ${esc(e.severity)}">${esc(e.severity)}</span>
        <div>
          <div>${esc(e.title)}${linkHtml}</div>
          <div class="meta-line">${esc(e.event_type)} · ${esc(e.source)} · ${relTime(e.created_at)}</div>
        </div>
        <button class="ack-btn" data-ack="${e.id}">✓ 已读</button>
      </div>`;
  }).join("");

  $("#alert-list").querySelectorAll("[data-ack]").forEach((b) => {
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const id = b.dataset.ack;
      try {
        await jfetch(`/api/alerts/${id}/ack`, { method: "POST" });
        b.closest(".alert-item").remove();
        const newCount = Math.max(0, (parseInt($("#alert-count").textContent) || 1) - 1);
        $("#alert-count").textContent = newCount;
        $("#alert-bar-count").textContent = newCount;
        if (newCount === 0) { bar.hidden = true; badge.hidden = true; }
      } catch (e) { console.error("ack", e); }
    });
  });
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

  if (!src.items?.length) {
    holder.innerHTML = chipsHtml + `<div class="empty">该平台暂无数据</div>`;
  } else {
    const rows = src.items.map((r) => `
      <div class="lb-row">
        <span class="rank">#${r.rank}</span>
        <span class="name" title="${esc(r.model_name)}">${esc(r.model_name)}</span>
        <span class="score">${r.score != null ? Number(r.score).toFixed(0) : "—"}</span>
        ${deltaSpan(r.delta)}
      </div>`).join("");
    holder.innerHTML = chipsHtml + rows;
  }

  holder.querySelectorAll(".chip-label[data-src-idx]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.lbSource = parseInt(btn.dataset.srcIdx);
      renderLbPanel(state.lbData);
    });
  });
}

// ── Panel 3 · HuggingFace ──
function renderHfPanel(d) {
  setUpdated("#u-hf", d.updated_at);
  const sect = (label, items) => {
    if (!items?.length) return `<div class="hf-section-title">${label}</div><div class="empty" style="padding:16px">—</div>`;
    const rows = items.map((r) => {
      const href = r.model_id ? `https://huggingface.co/${r.model_id}` : null;
      const nameHtml = href
        ? `<a class="name" href="${esc(href)}" target="_blank" rel="noopener" title="${esc(r.model_id)}">${esc(r.model_id)}</a>`
        : `<span class="name" title="${esc(r.model_id)}">${esc(r.model_id)}</span>`;
      return `
      <div class="list-row">
        <span class="rank">#${r.rank}</span>
        ${nameHtml}
        <span class="score">${r.downloads ? (r.downloads/1000).toFixed(0) + "K" : (r.likes ? r.likes + "♥" : "—")}</span>
        <span></span>
      </div>`;
    }).join("");
    return `<div class="hf-section-title">${label}</div>${rows}`;
  };
  $("#p-hf").classList.remove("loading");
  $("#p-hf").innerHTML = sect("🔥 Trending", d.trending) + sect("⬇ Downloads", d.downloads);
}

// ── Panel 4 · OpenRouter ──
function renderOrPanel(d) {
  setUpdated("#u-or", d.updated_at);
  if (!d.items?.length) {
    $("#p-or").classList.remove("loading");
    $("#p-or").innerHTML = `<div class="empty">暂无数据</div>`;
    return;
  }
  const headerLink = `<a href="https://openrouter.ai/rankings" target="_blank" rel="noopener" class="jump-inline">↗ 去 OpenRouter</a>`;
  const header = `<div class="or-header">${d.week_date ? "周 · " + esc(d.week_date) : ""}${headerLink}</div>`;
  const rows = d.items.map((r) => {
    const name = r.matched_model || r.model_permaslug;
    const tok = r.total_tokens ? `${(r.total_tokens/1e9).toFixed(1)}B tok` : "—";
    const chg = r.change_pct != null
      ? (r.change_pct >= 0
          ? `<span class="delta delta-up">+${Number(r.change_pct).toFixed(0)}%</span>`
          : `<span class="delta delta-down">${Number(r.change_pct).toFixed(0)}%</span>`)
      : `<span class="delta delta-flat">—</span>`;
    const href = (r.author && r.model_permaslug)
      ? `https://openrouter.ai/${r.author}/${r.model_permaslug}`
      : null;
    const nameHtml = href
      ? `<a class="name" href="${esc(href)}" target="_blank" rel="noopener" title="${esc(name)}">${esc(name)}</a>`
      : `<span class="name" title="${esc(name)}">${esc(name)}</span>`;
    return `
      <div class="list-row">
        <span class="rank">#${r.rank}</span>
        ${nameHtml}
        <span class="score">${tok}</span>
        ${chg}
      </div>`;
  }).join("");
  $("#p-or").classList.remove("loading");
  $("#p-or").innerHTML = header + rows;
}

// ── Panel 5 · 社区声音 (opinions) ──
function renderOpinionsPanel(d) {
  setUpdated("#u-opinions", d.generated_at);
  const models = d.payload?.models;
  if (!models?.length) {
    $("#p-opinions").classList.remove("loading");
    $("#p-opinions").innerHTML = `<div class="empty">聚合缓存为空<br><span style="font-size:11px">每 12h 自动生成</span></div>`;
    return;
  }
  $("#p-opinions").classList.remove("loading");
  $("#p-opinions").innerHTML = models.slice(0, 3).map((m) => {
    const name = m.name || m.model_name || "—";
    const summary = m.summary || m.opinion_summary || m.text || "";
    const quote = Array.isArray(m.quotes) && m.quotes.length ? m.quotes[0].text || m.quotes[0] : "";
    return `
      <div class="opinion-item">
        <div class="model">${esc(name)}</div>
        <div class="summary">${esc(summary)}</div>
        ${quote ? `<div class="quote">「${esc(String(quote).slice(0, 140))}」</div>` : ""}
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
  renderAlertBar(d.alerts);
  renderReleasesPanel(d.releases);
  renderLbPanel(d.leaderboards);
  renderHfPanel(d.hf);
  renderOrPanel(d.openrouter);
  renderOpinionsPanel(d.opinions);
  renderThemesPanel(d.themes);
}

// ── 系统健康（折叠区） ──
async function loadStatus() {
  const s = await jfetch("/api/status");
  const fails = s.collectors.filter((c) => c.consecutive_fails > 0).length;
  const dot = $("#health-dot");
  if (fails === 0) { dot.dataset.state = "ok"; dot.title = "所有采集器正常"; }
  else if (fails <= 1) { dot.dataset.state = "warn"; dot.title = `${fails} 个采集器异常`; }
  else { dot.dataset.state = "fail"; dot.title = `${fails} 个采集器异常`; }

  $("#collectors-hint").textContent = fails === 0 ? "全部正常" : `${fails} 项异常`;

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

  if ($("#collectors-details").open) renderCollectors(s.collectors);
  else loadStatus._cached = s.collectors;

  $("#last-update").textContent = "刷新 " + new Date().toTimeString().slice(0, 5);
}

function renderCollectors(collectors) {
  $("#collectors").innerHTML = collectors.map((c) => {
    const ok = !c.last_error && c.consecutive_fails === 0;
    const stale = !ok && c.last_success_at;
    const cls = ok ? "ok" : (stale ? "stale" : "fail");
    return `
      <div class="collector-row">
        <div class="left">
          <span class="status-dot ${cls}"></span>
          <span class="name">${esc(c.collector)}</span>
        </div>
        <span class="time">${relTime(c.last_run_at)} · ${c.consecutive_fails ? `失败 x${c.consecutive_fails}` : "ok"}</span>
      </div>`;
  }).join("");
}

// ── 历史周报 ──
async function loadWeeklyReports() {
  const rows = await jfetch("/api/weekly-reports?limit=12");
  const holder = $("#weekly-reports");
  if (!rows.length) {
    holder.innerHTML = `<div style="padding:24px;color:var(--muted);text-align:center;">还没有周报（下周一 09:00 自动生成）</div>`;
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

// ── 待归一化 ──
async function loadPending() {
  const rows = await jfetch("/api/pending-mapping?limit=20");
  $("#pending-hint").textContent = rows.length ? `${rows.length} 条待处理` : "全部已归一化";
  if (!rows.length) {
    $("#pending").innerHTML = `<div style="padding:24px;color:var(--muted);text-align:center;">全部已归一化 🎉</div>`;
    return;
  }
  $("#pending").innerHTML = rows.map((r) => `
    <div class="pending-row">
      <span>${esc(r.raw_name)}</span>
      <span class="src">${esc(r.source)}</span>
      <span class="count">x${r.seen_count}</span>
    </div>
  `).join("");
}

// ── Wire ──
async function refreshAll() {
  await Promise.all([
    loadDashboard().catch((e) => console.error("dashboard", e)),
    loadStatus().catch((e) => console.error("status", e)),
  ]);
  if ($("#weekly-reports-section").open) loadWeeklyReports().catch((e) => console.error("weekly", e));
  if ($("#pending-details").open) loadPending().catch((e) => console.error("pending", e));
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

// 告警条用户手动关闭过一次，就不再自动展开
$("#alert-bar").addEventListener("toggle", (ev) => {
  ev.target.dataset.userToggled = "1";
});

// 折叠区 lazy load
$("#collectors-details").addEventListener("toggle", (ev) => {
  if (ev.target.open) {
    if (loadStatus._cached) renderCollectors(loadStatus._cached);
    else loadStatus();
  }
});
$("#weekly-reports-section").addEventListener("toggle", (ev) => {
  if (ev.target.open) loadWeeklyReports().catch((e) => console.error("weekly", e));
});
$("#pending-details").addEventListener("toggle", (ev) => {
  if (ev.target.open) loadPending().catch((e) => console.error("pending", e));
});

$("#refresh").addEventListener("click", refreshAll);
$("#health-dot").addEventListener("click", () => {
  const el = $("#collectors-details");
  el.open = true;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
});

refreshAll();
setInterval(refreshAll, 60000);
