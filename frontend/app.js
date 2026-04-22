// ModelRadar Dashboard · 纯 fetch 小应用，无构建。

const $ = (sel) => document.querySelector(sel);

const state = {
  severity: "important",   // important | "" (all) | P0 | P1
  tlOffset: 0,
  tlTotal: 0,
  tlPageSize: 50,
  tlShowAll: false,        // false=过滤 noise+聚合 repo, true=展开原始
  tlAllEvents: [],         // 全量原始数据（跨分页累积）
  tlExpandedAggs: new Set(),
};

async function jfetch(url) {
  const r = await fetch(url, { credentials: "include" });
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

function fmtTime(iso) {
  const d = parseUtc(iso); if (!d) return "—";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
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

// ---------- Noise detection & aggregation ----------

// 判定单个事件是否 patch/dev release 噪音
function isNoise(e) {
  if (e.event_type !== "new_release") return false;
  const tag = String(e.detail?.tag || "").toLowerCase();
  if (!tag) return false;
  if (/\.(post|dev|rc|a|b)\d+/.test(tag)) return true;       // .post1, .dev4, .rc1, .a2
  if (/^nv_dev_/i.test(tag)) return true;                    // nv_dev_4ff3f54
  if (/-[a-f0-9]{6,}$/i.test(tag)) return true;              // 后缀挂长 hash
  return false;
}

// 整版本号（v1.0.0 / v2.0.0 这种 major/minor 发布，一定保留不当噪音）
function isMajorTag(tag) {
  return /^v?\d+\.\d+\.0$/.test(String(tag || ""));
}

// 同 repo 24h 内 ≥2 条 new_release → 聚合成一条 "meta" 事件
function aggregateByRepo(events, windowMs = 24 * 3600 * 1000) {
  // 只对 new_release 做聚合。每个 (org/repo) 独立一组；组内找时间紧邻簇。
  const groups = new Map();
  for (const e of events) {
    if (e.event_type !== "new_release") continue;
    const org = e.detail?.org, repo = e.detail?.repo;
    if (!org || !repo) continue;
    const k = `${org}/${repo}`;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(e);
  }

  const aggIds = new Set();        // 被归入聚合的原始 event id
  const aggregates = [];           // 合成的 meta 行

  for (const [repoKey, list] of groups) {
    if (list.length < 2) continue;
    // 按时间升序
    const sorted = [...list].sort((a, b) => (parseUtc(a.created_at) - parseUtc(b.created_at)));
    const earliest = parseUtc(sorted[0].created_at);
    const latest = parseUtc(sorted[sorted.length - 1].created_at);
    if (latest - earliest > windowMs) continue;

    // 组内如果有 major tag（v1.0.0），把它单独抽出来不折叠
    const majors = sorted.filter((x) => isMajorTag(x.detail?.tag));
    const rest = sorted.filter((x) => !isMajorTag(x.detail?.tag));
    if (rest.length < 2) continue;  // 抽掉 major 后剩不到 2 条就不聚合

    rest.forEach((x) => aggIds.add(x.id));
    const [org, repo] = repoKey.split("/");
    const representativeTs = rest[rest.length - 1].created_at;  // 用最新一条的时间作代表
    aggregates.push({
      id: `agg-${repoKey}`,
      _agg: true,
      _children: rest,
      event_type: "new_release",
      severity: rest.every((x) => x.severity === "P2") ? "P2" : "P1",
      source: "github",
      title: `${org}/${repo} 本周发 ${rest.length} 个版本（${rest[0].detail?.tag || "?"} … ${rest[rest.length - 1].detail?.tag || "?"}）`,
      created_at: representativeTs,
    });
  }

  return { aggIds, aggregates };
}

// ---------- Week-of helpers ----------

// 本周一 00:00（本地时区） → 转成 UTC "YYYY-MM-DD HH:MM:SS" 格式字符串
// 用途：和 DB 里的 created_at (UTC 无 Z) 做字符串比较
function startOfWeekIso() {
  const now = new Date();
  const day = now.getDay();          // 0=Sun, 1=Mon, ...
  const diff = day === 0 ? 6 : day - 1;
  const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - diff, 0, 0, 0, 0);
  return monday.toISOString().replace("T", " ").slice(0, 19);
}

function renderWeekGlance(events) {
  const since = startOfWeekIso();
  const thisWeek = events.filter((e) => e.created_at >= since);
  const nonNoise = thisWeek.filter((e) => !isNoise(e));

  const newModels = new Set(
    nonNoise.filter((e) => e.event_type === "new_model_on_board").map((e) => e.model_name).filter(Boolean)
  ).size;
  const crowned = nonNoise.filter((e) => e.event_type === "rank_crowned").length;
  const blogPosts = nonNoise.filter((e) => e.event_type === "new_blog_post").length;
  // "真正的" release = 非 noise + major tag 或首发
  const releases = nonNoise.filter((e) => e.event_type === "new_release").length;

  const parts = [];
  if (newModels > 0) parts.push(`<strong>${newModels}</strong> 款新模型上榜`);
  if (crowned > 0)   parts.push(`<strong>${crowned}</strong> 次榜单登顶`);
  if (releases > 0)  parts.push(`<strong>${releases}</strong> 个重要 release`);
  if (blogPosts > 0) parts.push(`<strong>${blogPosts}</strong> 条厂商博客`);

  const sentence = parts.length
    ? `本周 7 天：${parts.join(`<span class="sep">·</span>`)}`
    : `本周 7 天：暂无重要信号 <span class="sep">·</span> 系统正在监听 6 个数据源`;
  $("#week-sentence").innerHTML = sentence;
}

// ---------- Day grouping ----------

function dayKey(iso) {
  const d = parseUtc(iso); if (!d) return "unknown";
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function dayLabel(key) {
  const now = new Date();
  const todayKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const y = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  const yKey = `${y.getFullYear()}-${String(y.getMonth() + 1).padStart(2, "0")}-${String(y.getDate()).padStart(2, "0")}`;
  if (key === todayKey) return `今天 · ${key}`;
  if (key === yKey)     return `昨天 · ${key}`;
  return key;
}

// ---------- KPIs & Status ----------
async function loadStatus() {
  const s = await jfetch("/api/status");

  // 系统健康灯（只根据 collector 失败数量定级；先忽略 pending_alerts 那个 bug 数字）
  const fails = s.collectors.filter((c) => c.consecutive_fails > 0).length;
  const dot = $("#health-dot");
  if (fails === 0) { dot.dataset.state = "ok"; dot.title = "所有采集器正常"; }
  else if (fails <= 1) { dot.dataset.state = "warn"; dot.title = `${fails} 个采集器异常`; }
  else { dot.dataset.state = "fail"; dot.title = `${fails} 个采集器异常`; }

  // 折叠区标题右侧的提示数字
  $("#collectors-hint").textContent = fails === 0 ? "全部正常" : `${fails} 项异常`;

  // Footer 累计数字（原来那 5 张大 KPI 的数据挪到这里）
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

  // 折叠区展开后才渲染采集器详情
  if ($("#collectors-details").open) {
    renderCollectors(s.collectors);
  } else {
    // 存起来，展开时再用
    loadStatus._cached = s.collectors;
  }

  $("#last-update").textContent = "更新于 " + new Date().toTimeString().slice(0, 5);
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

// ---------- Alerts (关键信号) ----------
async function loadAlerts() {
  // "important" 预设 = 服务端拿全量后前端再筛
  const wantImportant = state.severity === "important";
  const qsev = (state.severity && !wantImportant) ? `&severity=${state.severity}` : "";
  const rows = await jfetch(`/api/alerts?limit=80${qsev}`);

  let filtered = rows;
  if (wantImportant) {
    filtered = rows.filter((e) => (e.severity === "P0" || e.severity === "P1") && !isNoise(e));
  }
  filtered = filtered.slice(0, 30);

  if (!filtered.length) {
    $("#alerts").innerHTML = `<div class="empty">${wantImportant ? "本周暂无重要信号 · 切换到「全部」查看明细" : "暂无事件"}</div>`;
    return;
  }

  $("#alerts").innerHTML = filtered.map((e) => {
    const link = e.detail && (e.detail.url || e.detail.html_url);
    const linkHtml = link ? `<a href="${esc(link)}" target="_blank" rel="noopener">🔗</a>` : "";
    const statusHtml = e.alert_status === "sent"
      ? `<span class="pill sent">📧 已发</span>`
      : e.alert_status === "suppressed"
      ? `<span class="pill suppressed">🔕 已抑制</span>`
      : "";
    return `
      <div class="signal-row">
        <span class="sev ${esc(e.severity)}">${esc(e.severity)}</span>
        <div class="body">
          <div class="title">${esc(e.title)} ${linkHtml}</div>
          <div class="meta-row">
            <span class="chip">${esc(e.event_type)}</span>
            <span>${esc(e.source)}</span>
            <span>${relTime(e.created_at)}</span>
            ${statusHtml}
          </div>
        </div>
      </div>`;
  }).join("");
}

// ---------- Heat ----------
async function loadHeat() {
  const { date, items } = await jfetch("/api/heat?limit=10");
  $("#heat-date").textContent = date || "—";
  if (!items || !items.length) { $("#heat").innerHTML = `<div style="padding:24px;color:var(--muted);text-align:center;">还没算</div>`; return; }
  const max = items[0].score || 1;
  $("#heat").innerHTML = items.map((m, i) => `
    <div class="heat-row">
      <span class="rank">#${i + 1}</span>
      <div>
        <div class="name" title="${esc(m.model_name)}">${esc(m.model_name)}</div>
        <div class="bar"><span class="fill" style="width:${(m.score / max * 100).toFixed(1)}%"></span></div>
      </div>
      <span class="score">${m.score.toFixed(1)}</span>
    </div>
  `).join("");
}

// ---------- Timeline ----------

function buildTimelineView(events, showAll) {
  // 返回渲染数组：[{type:"day",label}, {type:"row",e}, {type:"row",e,dim:true}, ...]
  const out = [];
  let list = [...events];

  let hiddenNoise = 0;
  let aggregatedCount = 0;

  if (!showAll) {
    // 1) 过滤掉纯 noise
    const kept = [];
    for (const e of list) {
      if (isNoise(e)) { hiddenNoise++; } else { kept.push(e); }
    }
    list = kept;

    // 2) 聚合同 repo
    const { aggIds, aggregates } = aggregateByRepo(list);
    aggregatedCount = aggIds.size;
    const withoutAggChildren = list.filter((e) => !aggIds.has(e.id));
    list = withoutAggChildren.concat(aggregates).sort(
      (a, b) => (parseUtc(b.created_at) - parseUtc(a.created_at))
    );
  }

  // 按天分组
  const byDay = new Map();
  for (const e of list) {
    const k = dayKey(e.created_at);
    if (!byDay.has(k)) byDay.set(k, []);
    byDay.get(k).push(e);
  }
  const dayKeys = [...byDay.keys()].sort().reverse();

  for (const dk of dayKeys) {
    out.push({ type: "day", label: dayLabel(dk) });
    for (const e of byDay.get(dk)) {
      out.push({ type: "row", e, dim: showAll && isNoise(e) });
      if (e._agg && state.tlExpandedAggs.has(e.id)) {
        for (const child of e._children) {
          out.push({ type: "row", e: child, child: true });
        }
      }
    }
  }

  return { view: out, hiddenNoise, aggregatedCount };
}

function renderTimeline() {
  const { view, hiddenNoise, aggregatedCount } = buildTimelineView(state.tlAllEvents, state.tlShowAll);

  if (!view.length) {
    $("#timeline").innerHTML = `<div style="padding:32px;color:var(--muted);text-align:center;">暂无</div>`;
  } else {
    $("#timeline").innerHTML = view.map((item) => {
      if (item.type === "day") {
        return `<div class="timeline-day">${esc(item.label)}</div>`;
      }
      const { e, dim, child } = item;
      const classes = [
        "timeline-row",
        e._agg ? "agg" : "",
        e._agg && state.tlExpandedAggs.has(e.id) ? "expanded" : "",
        child ? "agg-child" : "",
        dim ? "noise-dim" : "",
      ].filter(Boolean).join(" ");
      const dataAttr = e._agg ? ` data-agg="${esc(e.id)}"` : "";
      return `
        <div class="${classes}"${dataAttr}>
          <span class="ts">${fmtTime(e.created_at)}</span>
          <span class="sev ${esc(e.severity)}">${esc(e.severity)}</span>
          <span class="type">${esc(e.event_type)}</span>
          <span class="title" title="${esc(e.title)}">${esc(e.title)}</span>
        </div>`;
    }).join("");

    // 绑定聚合行点击展开
    $("#timeline").querySelectorAll(".timeline-row.agg").forEach((el) => {
      el.addEventListener("click", () => {
        const id = el.dataset.agg;
        if (state.tlExpandedAggs.has(id)) state.tlExpandedAggs.delete(id);
        else state.tlExpandedAggs.add(id);
        renderTimeline();
      });
    });
  }

  const hidden = hiddenNoise + aggregatedCount;
  $("#tl-filter-info").textContent = state.tlShowAll
    ? `已显示全部 ${state.tlAllEvents.length} 条`
    : (hidden > 0 ? `已隐藏 ${hiddenNoise} 条噪音、聚合 ${aggregatedCount} 条 patch release` : "无过滤");
  $("#tl-toggle-all").classList.toggle("on", state.tlShowAll);
  $("#tl-toggle-all").textContent = state.tlShowAll ? "收起过滤后视图" : "显示全部（含已过滤）";
  $("#timeline-total").textContent = `${state.tlAllEvents.length} / ${state.tlTotal} 条`;

  const btn = $("#timeline-more");
  const done = state.tlAllEvents.length >= state.tlTotal;
  btn.textContent = done ? "没有更多了" : "加载更多";
  btn.disabled = done;
}

async function loadTimeline(reset = true) {
  if (reset) { state.tlOffset = 0; state.tlAllEvents = []; state.tlExpandedAggs.clear(); }
  const { total, items } = await jfetch(`/api/timeline?limit=${state.tlPageSize}&offset=${state.tlOffset}`);
  state.tlTotal = total;
  // 补上 detail_json 解析（timeline 接口不带 detail，需要的时候前端也能忍；但 isNoise 靠 detail.tag，所以这里
  // 额外 hit alerts 一次全量拿带 detail 的版本。优先从 /api/alerts 补详）
  const detailMap = await fetchDetailMap(items);
  const enriched = items.map((r) => ({ ...r, detail: detailMap.get(r.id) || {} }));
  state.tlAllEvents = reset ? enriched : state.tlAllEvents.concat(enriched);
  state.tlOffset += items.length;

  renderTimeline();

  // Timeline 拉回来的同时也可算 "本周速览"
  renderWeekGlance(state.tlAllEvents);
}

// 批量拿这些 event 的 detail（timeline 接口没返回 detail_json），走 /api/alerts 合并
async function fetchDetailMap(items) {
  const map = new Map();
  if (!items.length) return map;
  // /api/alerts?limit=200 会覆盖最近 200 条，足够和 timeline 首屏重叠
  try {
    const all = await jfetch(`/api/alerts?limit=200`);
    for (const a of all) map.set(a.id, a.detail || {});
  } catch (_) {}
  return map;
}

// ---------- Weekly reports ----------
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

// ---------- Pending mapping ----------
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

// ---------- Wire ----------
async function refreshAll() {
  // 首屏三个关键接口并行
  await Promise.all([
    loadStatus().catch((e) => console.error("status", e)),
    loadAlerts().catch((e) => console.error("alerts", e)),
    loadHeat().catch((e) => console.error("heat", e)),
    loadTimeline().catch((e) => console.error("timeline", e)),
  ]);
  // 折叠区只有展开过才刷新
  if ($("#weekly-reports-section").open) loadWeeklyReports().catch((e) => console.error("weekly", e));
  if ($("#pending-details").open) loadPending().catch((e) => console.error("pending", e));
}

// Tab 切换 (关键信号)
document.querySelectorAll(".tabs .tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tabs .tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    state.severity = t.dataset.sev;
    loadAlerts();
  });
});

// Show-all toggle (Timeline)
$("#tl-toggle-all").addEventListener("click", () => {
  state.tlShowAll = !state.tlShowAll;
  renderTimeline();
});

// 折叠区展开时 lazy load
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
$("#timeline-more").addEventListener("click", () => loadTimeline(false));
$("#health-dot").addEventListener("click", () => {
  const el = $("#collectors-details");
  el.open = true;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
});

refreshAll();
setInterval(refreshAll, 60000);
