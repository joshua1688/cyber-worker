// 审计页：按可疑度排序回看每一步的截图和预期状态，标记「没问题 / 有问题」。
"use strict";

const A = { entries: [], selected: -1 };
const REVIEW_TEXT = { 0: "未审", 1: "没问题", 2: "有问题" };

async function loadDays() {
  const days = await api("GET", "/api/audit/days");
  const today = new Date().toLocaleDateString("sv-SE"); // YYYY-MM-DD
  if (!days.includes(today)) days.unshift(today);
  $("#f-day").innerHTML = days.map((d) => `<option>${d}</option>`).join("");
}

async function load(keepId) {
  const params = new URLSearchParams({ day: $("#f-day").value });
  if ($("#f-script").value) params.set("script", $("#f-script").value);
  if ($("#f-unreviewed").checked) params.set("unreviewed", "true");
  const data = await api("GET", `/api/audit?${params}`);
  A.entries = data.entries;
  renderReport(data.report);
  renderScripts();
  const idx = keepId != null ? A.entries.findIndex((e) => e.id === keepId) : -1;
  A.selected = idx >= 0 ? idx : A.entries.length ? 0 : -1;
  renderList();
  renderViewer();
}

function renderReport(r) {
  const rate = r.success_rate == null ? "—" : `${Math.round(r.success_rate * 100)}%`;
  $("#report").innerHTML = `
    <div class="stat"><div class="v">${r.runs}</div><div class="k">运行次数</div></div>
    <div class="stat"><div class="v">${r.steps}</div><div class="k">步骤</div></div>
    <div class="stat"><div class="v">${rate}</div><div class="k">成功率</div></div>
    <div class="stat ${r.suspicious_unreviewed ? "alert" : ""}"><div class="v">${r.suspicious_unreviewed}</div><div class="k">可疑待审</div></div>`;
}

function renderScripts() {
  const sel = $("#f-script");
  const current = sel.value;
  const names = [...new Set(A.entries.map((e) => e.script))].sort();
  if (current && !names.includes(current)) names.push(current);
  sel.innerHTML = `<option value="">全部宏</option>` + names.map((n) =>
    `<option ${n === current ? "selected" : ""}>${escapeHtml(n)}</option>`).join("");
}

function renderList() {
  $("#list").innerHTML = A.entries.map((e, i) => {
    const level = e.suspicion >= 0.9 ? "high" : e.suspicion >= 0.3 ? "mid" : "";
    return `<li class="audit-row ${i === A.selected ? "selected" : ""} ${e.reviewed ? "done" : ""}" data-i="${i}">
      <span class="bar ${level}" title="可疑度 ${e.suspicion}"></span>
      <span class="st ${e.status}">${STATUS_TEXT[e.status] || e.status}</span>
      <span class="what">
        <span>${escapeHtml(e.script)} · ${escapeHtml(e.step_id)} ${escapeHtml(e.action)}</span>
        <small>${e.at.slice(11)}${e.dry_run ? " · 空跑" : ""}${e.detail ? " · " + escapeHtml(e.detail) : ""}</small>
      </span>
      <span>
        ${e.spot_check ? `<span class="badge warn">抽查</span>` : ""}
        ${e.reviewed ? `<span class="badge ${e.reviewed === 2 ? "send" : "ok"}">${REVIEW_TEXT[e.reviewed]}</span>` : ""}
      </span>
    </li>`;
  }).join("");
  $("#list-empty").hidden = A.entries.length > 0;
}

function renderViewer() {
  const e = A.entries[A.selected];
  const v = $("#viewer");
  if (!e) {
    v.innerHTML = `<div class="empty"><div class="empty-ico">${icon("image", "lg")}</div>
      <h3>没有选中的记录</h3><p>左边选一条，查看那一步操作后的截图。</p></div>`;
    return;
  }
  const score = e.page_score == null ? `<span class="muted">没有校验</span>` : e.page_score.toFixed(3);
  v.innerHTML = `
    <div class="card-head">
      <span class="st ${e.status}">${STATUS_TEXT[e.status] || e.status}</span>
      <span class="muted">${escapeHtml(e.script)} · ${escapeHtml(e.step_id)}（${escapeHtml(e.action)}）</span>
      <span class="grow"></span>
      ${e.reviewed ? `<span class="badge ${e.reviewed === 2 ? "send" : "ok"}">${REVIEW_TEXT[e.reviewed]}</span>` : `<span class="badge">未审</span>`}
    </div>
    <div class="viewer-shot">
      ${e.screenshot_url ? `<img src="${e.screenshot_url}" alt="操作后截图">` : `<div class="canvas-empty">这一步没有截图</div>`}
    </div>
    <div class="scroller" style="flex:1;padding:14px 16px">
      <dl class="dl">
        <dt>预期页面</dt><dd>${e.expected_page ? escapeHtml(e.expected_page) : `<span class="muted">未指定</span>`}</dd>
        <dt>页面得分</dt><dd>${score}</dd>
        <dt>说明</dt><dd>${e.detail ? escapeHtml(e.detail) : `<span class="muted">—</span>`}</dd>
        <dt>可疑度</dt><dd>${e.suspicion}${e.spot_check ? "（随机抽查）" : ""}</dd>
        <dt>时间</dt><dd>${e.at}${e.dry_run ? "（空跑）" : ""}</dd>
        <dt>运行编号</dt><dd class="muted">${escapeHtml(e.run_id)}</dd>
      </dl>
    </div>
    <div class="review-actions" id="review-actions">
      <button class="btn primary" data-mark="1">${icon("check")}没问题 <kbd>1</kbd></button>
      <button class="btn danger-ghost" data-mark="2">${icon("x")}有问题 <kbd>2</kbd></button>
      <button class="btn ghost" data-mark="0">重置 <kbd>0</kbd></button>
    </div>`;
}

async function mark(value) {
  const e = A.entries[A.selected];
  if (!e) return;
  const ok = await attempt(() => api("POST", `/api/audit/${e.id}`, { reviewed: value }));
  if (!ok) return;
  e.reviewed = value;
  // 标完自动跳到下一条
  if (value && A.selected < A.entries.length - 1) A.selected++;
  renderList();
  renderViewer();
  $(`#list li[data-i="${A.selected}"]`)?.scrollIntoView({ block: "nearest" });
}

function move(delta) {
  if (!A.entries.length) return;
  A.selected = Math.max(0, Math.min(A.entries.length - 1, A.selected + delta));
  renderList();
  renderViewer();
  $(`#list li[data-i="${A.selected}"]`)?.scrollIntoView({ block: "nearest" });
}

$("#list").addEventListener("click", (e) => {
  const li = e.target.closest("li");
  if (!li) return;
  A.selected = +li.dataset.i;
  renderList();
  renderViewer();
});
$("#viewer").addEventListener("click", (e) => {
  const b = e.target.closest("[data-mark]");
  if (b) mark(+b.dataset.mark);
});
["#f-day", "#f-script", "#f-unreviewed"].forEach((s) => $(s).addEventListener("change", () => attempt(() => load())));
$("#btn-refresh").addEventListener("click", () => attempt(() => load(A.entries[A.selected]?.id)));

document.addEventListener("keydown", (e) => {
  if (Tour.els || e.target.matches("input, select, textarea") || e.ctrlKey || e.metaKey) return;
  if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); move(1); }
  else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); move(-1); }
  else if (["0", "1", "2"].includes(e.key)) mark(+e.key);
});

// 有新的执行结果时自动刷新（停留在当天时）
connectEvents((ev) => {
  if (ev.type === "settings") Settings.applyRemote(ev);
  if (ev.type === "run_state" && ev.state === "done" && $("#f-day").selectedIndex === 0) {
    attempt(() => load(A.entries[A.selected]?.id));
  }
});

function startTour() {
  Tour.start([
    { el: "#report", title: "今天的概况",
      body: "运行次数、成功率，以及还没看过的可疑记录。空跑正常停在「发送」前也算成功。" },
    { el: "#list", title: "最可疑的排在最前",
      body: "执行出错、页面对不上而中止的排最前面；颜色条越红越该先看。标了「抽查」的是随机挑出来的成功记录，也值得瞄一眼。" },
    { el: "#viewer", title: "看截图，下结论",
      body: "右边是那一步操作后的截图和预期页面。按 1 = 没问题，2 = 有问题，标完自动跳到下一条；J / K 上下切换。" },
    { el: "#filters", title: "筛选",
      body: "按日期、按宏筛；勾上「只看未审」，就只剩还没处理的。" },
  ], "cw.tour.audit");
}
$("#btn-tour").addEventListener("click", startTour);

attempt(async () => {
  await loadDays();
  await load();
  if (!Tour.seen("cw.tour.audit")) startTour();
});
