// ModelRadar Dashboard · 纯 fetch 小应用，无构建。

const $ = (sel) => document.querySelector(sel);

const state = { severity: "", tlOffset: 0, tlTotal: 0, tlPageSize: 50 };

async function jfetch(url) {
  const r = await fetch(url, { credentials: "include" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

function fmtDate(iso) {
  if (!iso) return "—";
  // 后端给的 UTC 无 TZ 字符串，浏览器当作 UTC 处理
  const d = new Date(iso.replace(" ", "T") + "Z");
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${mm}-${dd} ${hh}:${mi}`;
}

function relTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso.replace(" ", "T") + "Z");
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

// ---------- KPIs & Status ----------
async function loadStatus() {
  const s = await jfetch("/api/status");
  for (const k in s.counts) {
    const el = document.querySelector(`.kpi[data-k="${k}"] .value`);
    if (el) el.textContent = s.counts[k].toLocaleString();
  }
  const holder = $("#collectors");
  holder.innerHTML = s.collectors.map((c) => {
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
  $("#last-update").textContent = "更新于 " + new Date().toTimeString().slice(0, 5);
}

// ---------- Alerts ----------
async function loadAlerts() {
  const q = state.severity ? `?severity=${state.severity}&limit=40` : "?limit=40";
  const rows = await jfetch("/api/alerts" + q);
  if (!rows.length) { $("#alerts").innerHTML = `<div style="padding:24px;color:var(--muted);text-align:center;">暂无事件</div>`; return; }
  $("#alerts").innerHTML = rows.map((e) => {
    const link = e.detail && (e.detail.url || e.detail.html_url);
    const linkHtml = link ? `<a href="${esc(link)}" target="_blank" rel="noopener">🔗</a>` : "";
    const statusMap = {
      sent:       `<span class="pill sent">📧 邮件已发</span>`,
      suppressed: `<span class="pill suppressed">🔕 已抑制</span>`,
      pending:    `<span class="pill pending">⏳ 待处理</span>`,
    };
    const sent = statusMap[e.alert_status] || statusMap.pending;
    return `
      <div class="alert">
        <span class="sev ${esc(e.severity)}">${esc(e.severity)}</span>
        <div class="body">
          <div class="title">${esc(e.title)} ${linkHtml}</div>
          <div class="meta-row">
            <span class="chip">${esc(e.event_type)}</span>
            <span>${esc(e.source)}</span>
            <span>${relTime(e.created_at)}</span>
            ${sent}
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
function renderTimelineRows(rows, append = false) {
  const html = rows.map((r) => `
    <div class="timeline-row">
      <span class="ts">${fmtDate(r.created_at)}</span>
      <span class="sev ${esc(r.severity)}">${esc(r.severity)}</span>
      <span class="type">${esc(r.event_type)}</span>
      <span class="title" title="${esc(r.title)}">${esc(r.title)}</span>
    </div>
  `).join("");
  if (append) $("#timeline").insertAdjacentHTML("beforeend", html);
  else $("#timeline").innerHTML = html;
}

async function loadTimeline(reset = true) {
  if (reset) { state.tlOffset = 0; }
  const { total, items } = await jfetch(`/api/timeline?limit=${state.tlPageSize}&offset=${state.tlOffset}`);
  state.tlTotal = total;
  if (reset && !items.length) {
    $("#timeline").innerHTML = `<div style="padding:24px;color:var(--muted);text-align:center;">暂无</div>`;
  } else {
    renderTimelineRows(items, !reset);
  }
  state.tlOffset += items.length;
  $("#timeline-total").textContent = `${state.tlOffset} / ${total} 条`;
  const btn = $("#timeline-more");
  const done = state.tlOffset >= total;
  btn.textContent = done ? "没有更多了" : "加载更多";
  btn.disabled = done;
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
  if (!rows.length) { $("#pending").innerHTML = `<div style="padding:24px;color:var(--muted);text-align:center;">全部已归一化 🎉</div>`; return; }
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
  await Promise.all([
    loadStatus().catch((e) => console.error("status", e)),
    loadAlerts().catch((e) => console.error("alerts", e)),
    loadHeat().catch((e) => console.error("heat", e)),
    loadTimeline().catch((e) => console.error("timeline", e)),
    loadWeeklyReports().catch((e) => console.error("weekly", e)),
    loadPending().catch((e) => console.error("pending", e)),
  ]);
}

document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    state.severity = t.dataset.sev;
    loadAlerts();
  });
});

$("#refresh").addEventListener("click", refreshAll);
$("#timeline-more").addEventListener("click", () => loadTimeline(false));
refreshAll();
setInterval(refreshAll, 60000);
