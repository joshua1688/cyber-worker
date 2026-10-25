// 审计页：一次运行一行，有问题的排前面；连续几次同样的失败合成一行（×N）。
// 右边是这次运行的步骤时间线 + 选中那一步的原因和截图。复核按「一次运行」来：
// 「看过了」= 这次运行看过、没问题；「有问题，要修」= 留在「要修」里，点「去修改」直接打开那个宏的那一步。
"use strict";

const A = {
  entries: [],   // 当天的全部记录
  groups: [],    // 一次运行一组
  rows: [],      // 列表上的行：一行 = 连着几次同样结果的运行（通常就一次）
  show: "all",   // all | pending | bad | fix
  selected: -1,  // A.entries 的下标（右边正在看的那一步）
  macros: [],    // 这一天出现过的 {app, app_title, script}：宏筛选下拉按应用分组
};
const TYPE_TEXT = { click: "点击", type: "输入", key: "按键", scroll: "滚动", wait: "延迟", drag: "拖动",
  hover: "悬停", wait_page: "等待页面", start: "开始", calibrate: "找窗口" };

const isBad = (e) => e.status === "failed" || e.status === "aborted";
const pending = (e) => !e.reviewed && e.suspicion >= 0.3;
const stepNo = (e) => { const m = /^s(\d+)$/.exec(e.step_id); return m ? +m[1] : null; };
const stepName = (e) => (stepNo(e) ? `第 ${stepNo(e)} 步 · ` : "") + (TYPE_TEXT[e.action] || e.action);
const flagged = (g) => g.es.some((e) => e.reviewed === 2);   // 标了「有问题，要修」
const seen = (g) => !flagged(g) && g.es.every((e) => e.reviewed === 1);  // 标了「看过了」
// 停下来是正常保护、不用改宏（你动了键鼠、窗口没开）：只留「看过了」，不给「要修」「去修改」
const noFix = (g) => !!g.bad && advise(g.bad)?.edit === false;

// 程序写的原因 → 一句人话。细节（得分、等了几秒）放在右边的「详细」里
function human(detail) {
  const t = (detail || "")
    .replace(/^[A-Za-z_][\w.]*(Error|Exception|Interrupted|Cancelled)?:\s*/, "")
    .replace(/^窗口校准失败：/, "");
  let m;
  if ((m = /^窗口尺寸校准失败：期望 (\d+)x(\d+)，实际 (\d+)x(\d+)/.exec(t))) return `窗口尺寸不对（要 ${m[1]}×${m[2]}，实际 ${m[3]}×${m[4]}）`;
  if ((m = /找不到 \S+（标题含「(.+?)」）\s*的窗口/.exec(t))) return `找不到「${m[1]}」窗口`;
  if ((m = /找不到 (\S+?)\s*的窗口/.exec(t))) return `找不到 ${m[1]} 的窗口`;
  if (/没找到要点的目标/.test(t)) return "没找到要点的东西（按钮、头像…）";
  if ((m = /^页面不符：期望 (.+?)，/.exec(t))) return `页面对不上：不在「${m[1]}」`;
  if (/检测到你在用电脑/.test(t)) return "你在用电脑，已停下";
  if (/中文输入法/.test(t)) return "录到的是输入法按键，要改成实际文字";
  if (/窗口已经关了或最小化了/.test(t)) return "窗口已经关了或最小化了";
  if (/^空跑：跳过发送/.test(t)) return "空跑：到「发送」前停下";
  return t;
}

// 出了问题的那一步怎么修：{ text, edit }。edit = 要到编辑器里改（显示「去修改」）；
// 不用改宏的（你动了键鼠、窗口没开）只给一句说明。认不出的原因返回 null
function advise(e) {
  const t = e.detail || "";
  const no = stepNo(e) ? `第 ${stepNo(e)} 步` : "这一步";
  if (/没有声明 expected_page|没有 pages\.yaml/.test(t)) {
    return { edit: true, text: `${no}还没设「在哪个画面执行」。打字、发送之前，程序要先认出窗口里一块不会变的区域（锚点，`
      + `比如聊天窗口顶部的名字），确认画面对了才动手，所以停下了。点「去修改」打开${no}，点「自动选一个」，`
      + "或者点「我自己框」在画面上框一块。" };
  }
  if (/^页面不符/.test(t)) {
    return { edit: true, text: "窗口里没认出该有的画面：可能窗口没打开、被别的窗口挡住，或者界面变了。"
      + "先看下面的截图；如果界面确实变了，点「去修改」，在当前画面上重新框一下这个页面的锚点。" };
  }
  if (/没找到要点的目标/.test(t)) {
    return { edit: true, text: "要点的按钮 / 头像在窗口里没找到：可能换了位置、换了样式，或者还没出现。"
      + `点「去修改」，截一张当前画面，给${no}点「重新框选」框住要点的东西；如果它只是出现得慢，把「最多等」调长一点。` };
  }
  if (/中文输入法/.test(t)) {
    return { edit: true, text: `录制时开着中文输入法，${no}只录到了按键。点「去修改」，把这一步的文字改成实际要输入的内容。` };
  }
  if (/^缺少变量/.test(t)) {
    return { edit: true, text: "执行前要填的变量没填。点「去修改」，在右边「运行」里把变量填上再执行。" };
  }
  if (/窗口尺寸/.test(t)) {
    return { edit: false, text: "窗口大小和录制时不一样，又没能自动调整。把窗口从最大化还原成普通窗口再执行。" };
  }
  if (/找不到 .*窗口|窗口已经关了或最小化了|窗口校准失败/.test(t)) {
    return { edit: false, text: "要操作的窗口没找到：先把这个程序打开，窗口别最小化，再执行。" };
  }
  if (/检测到你在用电脑/.test(t)) {
    return { edit: false, text: "不用修：执行时你动了鼠标或键盘，程序为了不和你抢，先停下了。执行时别碰键鼠就行。" };
  }
  return null;
}

// 成功了、但按图找的得分离门槛很近（规则同 reviewer.TARGET_WEAK）：提前提醒，免得哪天突然找不到
function weakTarget(e) {
  const m = /按图找到目标.*（得分 ([\d.]+)）/.exec(e.detail || "");
  if (e.status !== "ok" || !m || +m[1] >= 0.85 || /已自动记住新样子/.test(e.detail)) return null;
  const no = stepNo(e) ? `第 ${stepNo(e)} 步` : "这一步";
  return { edit: true, text: `${no}按图找到了，但得分只有 ${m[1]}（门槛 0.80），而且窗口里有好几处像它，没法自动记住新样子。`
    + `点「去修改」，在右边「目标」旁点「重新框选」，框一块只有它才有的区域。` };
}

// 成功了、但页面锚点挪了位置（runner.SHIFT_NOTE）：界面布局变了，程序在整个窗口里找到了它。提醒看一眼，不用改宏
function movedAnchor(e) {
  if (e.status !== "ok" || !/页面锚点挪了位置/.test(e.detail || "")) return null;
  return { edit: false, text: "窗口里的界面挪了位置（比如左边多出一栏会话列表），程序在整个窗口里找到了这个页面，照常执行了。"
    + "看一眼截图，确认这一步做在了对的地方；没问题就点「看过了」。" };
}

// ---------------- 数据 ----------------

async function loadDays() {
  const days = await api("GET", "/api/audit/days");
  const today = new Date().toLocaleDateString("sv-SE"); // YYYY-MM-DD
  if (!days.includes(today)) days.unshift(today);
  $("#f-day").innerHTML = days.map((d) => `<option value="${d}">${d === today ? "今天" : d}</option>`).join("");
}

async function load(keepRun) {
  const params = new URLSearchParams({ day: $("#f-day").value });
  // 下拉的值："" 全部；JSON [应用] 这个应用的全部宏；JSON [应用, 宏名] 这一个宏（没认出应用的旧记录，应用是 ""）
  const pick = $("#f-script").value && JSON.parse($("#f-script").value);
  if (pick) {
    params.set("app", pick[0]);
    if (pick.length > 1) params.set("script", pick[1]);
  }
  const data = await api("GET", `/api/audit?${params}`);
  A.macros = data.macros;
  A.entries = data.entries;
  buildGroups();
  buildRows();
  renderReport(data.report);
  renderScripts();
  const g = keepRun && A.groups.find((x) => x.es[0].run_id === keepRun);
  A.selected = g ? A.entries.indexOf(g.top) : firstSelection();
  render();
}

// 一次运行一组：组内按执行顺序；组按「最该看的那一步」排，一样的按时间新的在前
function buildGroups() {
  const by = new Map();
  A.entries.forEach((e) => {
    if (!by.has(e.run_id)) by.set(e.run_id, []);
    by.get(e.run_id).push(e);
  });
  A.groups = [...by.values()].map((es) => {
    es.sort((x, y) => x.id - y.id);
    const bad = es.find(isBad);
    const top = bad || es.reduce((m, e) => (e.suspicion > m.suspicion ? e : m), es[es.length - 1]);
    const { app, app_title: appTitle, macro_id: macroId, script } = es[0];
    return { es, top, bad, at: es[0].at, script, app, appTitle, macroId, dry: es[0].dry_run };
  });
  A.groups.sort((x, y) => y.top.suspicion - x.top.suspicion || y.at.localeCompare(x.at));
}

const tone = (g) => (g.bad ? "bad" : g.es.some(pending) ? "warn" : "ok");

function summary(g) {
  if (g.bad) return `${stepNo(g.bad) ? `第 ${stepNo(g.bad)} 步：` : ""}${human(g.bad.detail) || STATUS_TEXT[g.bad.status]}`;
  const k = g.es.filter(pending).length;
  if (k) return `${g.es.length} 步，有 ${k} 步待看`;
  if (g.es.some((e) => e.status === "skipped")) return "全部正常，空跑到「发送」前停下";
  return `${g.es.length} 步全部成功`;
}

// 列表上的行：按筛选留下的运行；连着的「同一个宏、同样失败」合成一行
function buildRows() {
  const keep = { all: () => true, bad: (g) => !!g.bad, fix: flagged, pending: (g) => g.es.some(pending) }[A.show];
  A.rows = [];
  for (const g of A.groups.filter(keep)) {
    const last = A.rows[A.rows.length - 1];
    const key = `${g.app}|${g.script}|${g.dry}|${summary(g)}`;
    if (g.bad && last?.key === key) last.groups.push(g);
    else A.rows.push({ key, groups: [g] });
  }
}

const firstSelection = () => (A.rows.length ? A.entries.indexOf(A.rows[0].groups[0].top) : -1);
const selectedGroup = () => A.groups.find((g) => g.es.includes(A.entries[A.selected]));
const rowOf = (g) => A.rows.find((r) => r.groups.includes(g));

// ---------------- 渲染 ----------------

function render() {
  renderList();
  renderViewer();
}

function renderReport(r) {
  const rate = r.success_rate == null ? "—" : `${Math.round(r.success_rate * 100)}%`;
  const bad = A.groups.filter((g) => g.bad).length;
  $("#report").innerHTML = `
    <div class="stat"><div class="v">${r.runs}</div><div class="k">运行次数</div></div>
    <div class="stat"><div class="v">${rate}</div><div class="k">成功率</div></div>
    <div class="stat ${bad ? "bad" : ""}"><div class="v">${bad}</div><div class="k">失败</div></div>
    <div class="stat ${r.suspicious_unreviewed ? "alert" : ""}"><div class="v">${r.suspicious_unreviewed}</div><div class="k">待审</div></div>`;
}

// 「全部 QQ 的宏」「全部微信的宏」：英文名两边空一格
const allOf = (title) => (/^[A-Za-z0-9]|[A-Za-z0-9]$/.test(title) ? `全部 ${title} 的宏` : `全部${title}的宏`);

// 宏筛选：按应用分组，每组先是「全部 QQ 的宏」，再是一个个宏；同名的宏在不同应用里也分得清
function renderScripts() {
  const sel = $("#f-script");
  const current = sel.value;
  const groups = new Map();
  A.macros.forEach((m) => {
    const k = m.app || "";
    if (!groups.has(k)) groups.set(k, { title: m.app_title || "其他", names: [] });
    groups.get(k).names.push(m.script);
  });
  const opt = (value, text) => `<option value="${escapeHtml(value)}" ${value === current ? "selected" : ""}>${escapeHtml(text)}</option>`;
  let html = opt("", "全部宏");
  for (const [app, g] of groups) {
    html += `<optgroup label="${escapeHtml(g.title)}">${
      g.names.length > 1 ? opt(JSON.stringify([app]), allOf(g.title)) : ""}${
      g.names.map((n) => opt(JSON.stringify([app, n]), n)).join("")}</optgroup>`;
  }
  // 选中的宏这一天没有记录了（换了日期）：留着它，别悄悄变成「全部」
  if (current && !html.includes(`value="${escapeHtml(current)}"`)) {
    const p = JSON.parse(current);
    html += opt(current, p.length > 1 ? p[1] : allOf(p[0]));
  }
  sel.innerHTML = html;
}

// 哪个应用：头像 + 「QQ › 群聊天」；认不出应用（宏名在好几个应用里都有的旧记录）只写宏名
function appName(g, link) {
  if (!g.app) return `<b>${escapeHtml(g.script)}</b>`;
  const title = escapeHtml(g.appTitle || g.app);
  return `${avatar(g.app, "xs", g.appTitle || g.app)}${link
    ? `<a class="app-link" href="/apps?app=${encodeURIComponent(g.app)}" title="打开这个应用">${title}</a>`
    : `<span class="app-name">${title}</span>`}<span class="sep">›</span><b>${escapeHtml(g.script)}</b>`;
}

function renderList() {
  const cur = selectedGroup();
  $("#list").innerHTML = A.rows.map((r, i) => {
    const g = r.groups[0];
    return `<li><button type="button" class="run-row ${r.groups.includes(cur) ? "on" : ""} ${seen(g) ? "seen" : ""}" data-row="${i}">
      <span class="dot ${tone(g)}"></span>
      <span class="run-main">
        <span class="run-line">${appName(g)}<span class="muted">${g.dry ? "空跑" : "执行"}</span>
          ${r.groups.length > 1 ? `<span class="times" title="连着 ${r.groups.length} 次一样">×${r.groups.length}</span>` : ""}
          ${flagged(g) ? `<span class="fix-tag">要修</span>` : seen(g) ? `<span class="seen-tag">${icon("check")}看过</span>` : ""}
          <span class="grow"></span><span class="time">${g.at.slice(11, 16)}</span></span>
        <span class="run-sum">${escapeHtml(summary(g))}</span>
      </span>
    </button></li>`;
  }).join("");
  $("#list-empty").hidden = A.rows.length > 0;
  $("#list-empty h3").textContent = !A.entries.length ? "这一天还没有记录"
    : { bad: "没有失败的运行", fix: "没有标了要修的运行", pending: "没有待审的运行" }[A.show] || "没有记录";
}

function renderViewer() {
  const e = A.entries[A.selected];
  const g = selectedGroup();
  const v = $("#viewer");
  if (!e || !g) {
    v.innerHTML = `<div class="empty"><div class="empty-ico">${icon("image", "lg")}</div>
      <h3>没有选中的运行</h3><p>左边选一次运行，看它每一步的截图。</p></div>`;
    return;
  }
  const twins = (rowOf(g)?.groups || []).filter((x) => x !== g);
  const status = g.bad ? STATUS_TEXT[g.bad.status] : tone(g) === "warn" ? "待看" : "正常";
  const reason = human(e.detail);
  const score = e.page_score == null ? "" : `，得分 ${e.page_score.toFixed(2)}`;
  const id = g.macroId;
  const fixStep = g.bad || g.top;
  const advice = isBad(e) ? advise(e) : weakTarget(e) || movedAnchor(e);
  v.innerHTML = `
    <div class="card-head viewer-head">
      <span class="pill ${tone(g)}">${status}</span>
      <span class="viewer-title">${appName(g, true)}<b>· ${g.dry ? "空跑" : "执行"}</b></span>
      <span class="muted">${g.at.slice(11, 19)}</span>
      <span class="grow"></span>
      ${twins.length ? `<span class="muted twins">同样的还有 ${twins.map((t) =>
        `<button type="button" class="link" data-run="${escapeHtml(t.es[0].run_id)}">${t.at.slice(11, 16)}</button>`).join(" ")}</span>` : ""}
    </div>
    <ol class="timeline" aria-label="这次运行的步骤">${g.es.map((x) => `
      <li><button type="button" class="tl ${x === e ? "on" : ""}" data-i="${A.entries.indexOf(x)}" title="${escapeHtml(human(x.detail) || STATUS_TEXT[x.status] || "")}">
        <span class="dot ${isBad(x) ? "bad" : pending(x) ? "warn" : x.status === "ok" ? "ok" : ""}"></span>${escapeHtml(stepName(x))}
      </button></li>`).join("")}
    </ol>
    <div class="scroller viewer-body">
      <div class="reason ${isBad(e) ? "bad" : pending(e) ? "warn" : ""}">
        <b>${escapeHtml(stepName(e))}：${escapeHtml(STATUS_TEXT[e.status] || e.status)}${e.spot_check ? ` <span class="badge warn">抽查</span>` : ""}</b>
        ${reason ? `<span>${escapeHtml(reason)}</span>` : ""}
        <small>${e.expected_page ? `执行前核对页面「${escapeHtml(e.expected_page)}」${score}`
          : /没有声明 expected_page|没有 pages\.yaml/.test(e.detail || "") ? "这一步还没设页面" : "这一步不用核对页面"}${
          e.detail && reason !== e.detail ? ` · 详细：${escapeHtml(e.detail)}` : ""}</small>
      </div>
      ${advice ? `<div class="advice">${icon("info")}<div><b>怎么修</b><p>${escapeHtml(advice.text)}</p>
        ${advice.edit && id ? `<a class="btn sm primary" href="/?macro=${encodeURIComponent(id)}&step=${encodeURIComponent(e.step_id)}">${icon("sliders")}去修改</a>` : ""}</div></div>` : ""}
      ${e.screenshot_url ? `<div class="viewer-shot"><img src="${e.screenshot_url}" alt="操作后截图"></div>`
        : `<p class="no-shot">${icon("image")}这一步没有截图${isBad(e) ? "：没做成，所以没有操作后的画面" : ""}</p>`}
    </div>
    <div class="review-actions">
      <button class="btn ${seen(g) ? "primary" : ""}" data-act="seen"
        title="${seen(g) ? "再点一下撤销" : "这次运行看过了，没问题；自动跳到下一次"}">${icon("check")}${seen(g) ? "已看过" : "看过了"} <kbd>1</kbd></button>
      ${noFix(g) ? "" : `<button class="btn ${flagged(g) ? "danger" : "danger-ghost"}" data-act="fix"
        title="${flagged(g) ? "再点一下撤销" : "这次运行做错了：留在「要修」里，回头去改宏"}">${icon("alert")}${flagged(g) ? "已标为要修" : "有问题，要修"} <kbd>2</kbd></button>`}
      ${flagged(g) && !noFix(g) && !(advice?.edit && id) ? (id ? `<a class="btn" href="/?macro=${encodeURIComponent(id)}&step=${encodeURIComponent(fixStep.step_id)}">${icon("sliders")}去修改</a>`
        : `<button class="btn" disabled title="这个宏已经删了，没法再改">${icon("trash")}宏已删除</button>`) : ""}
      <span class="grow"></span>
      <span class="muted" style="font-size:12px"><kbd>↑</kbd> <kbd>↓</kbd> 切换运行 · <kbd>←</kbd> <kbd>→</kbd> 切换步骤</span>
    </div>`;
}

// ---------------- 复核：按一次运行来标 ----------------

// 改几条记录的标记（0 未审 / 1 没问题 / 2 有问题），一起发
async function setMarks(pairs) {
  const ok = await attempt(() => Promise.all(pairs.map(([e, v]) => api("POST", `/api/audit/${e.id}`, { reviewed: v }))));
  if (ok) pairs.forEach(([e, v]) => (e.reviewed = v));
  return ok;
}

// 合成一行的几次同样的运行一起标
const runsOf = (g) => rowOf(g)?.groups || [g];

async function markSeen() {
  const g = selectedGroup();
  if (!g) return;
  const undo = seen(g);
  const es = runsOf(g).flatMap((r) => r.es);
  if (!(await setMarks(es.map((e) => [e, undo ? 0 : 1])))) return;
  if (!undo) return moveRow(1, true);  // 看完一次，自动到下一次
  render();
}

async function markFix() {
  const g = selectedGroup();
  if (!g || noFix(g)) return;
  const undo = flagged(g);
  const pairs = runsOf(g).flatMap((r) => r.es.map((e) => [e, undo ? 0 : e === (r.bad || r.top) ? 2 : 1]));
  if (await setMarks(pairs)) render();
}

// ↑ ↓：上一次 / 下一次运行。refilter：刚标完，在「待审」里这一行会消失，按原位置往下接
function moveRow(delta, refilter = false) {
  const cur = selectedGroup();
  let i = A.rows.findIndex((r) => r.groups.includes(cur));
  if (refilter) {
    buildRows();
    const still = A.rows.some((r) => r.groups.includes(cur));
    i += still ? delta : 0;
  } else {
    i = (i < 0 ? 0 : i) + delta;
  }
  const next = A.rows[Math.max(0, Math.min(A.rows.length - 1, i))];
  A.selected = next ? A.entries.indexOf(next.groups[0].top) : -1;
  render();
  $("#list .run-row.on")?.scrollIntoView({ block: "nearest" });
}

// ← →：这次运行的上一步 / 下一步（到头就停住）
function moveStep(delta) {
  const g = selectedGroup();
  if (!g) return;
  const i = g.es.indexOf(A.entries[A.selected]) + delta;
  if (i < 0 || i >= g.es.length) return;
  A.selected = A.entries.indexOf(g.es[i]);
  renderViewer();
  $("#viewer .tl.on")?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function selectRun(runId) {
  const g = A.groups.find((x) => x.es[0].run_id === runId);
  if (!g) return;
  A.selected = A.entries.indexOf(g.top);
  render();
}

$("#list").addEventListener("click", (e) => {
  const b = e.target.closest("[data-row]");
  if (b) selectRun(A.rows[+b.dataset.row].groups[0].es[0].run_id);
});
$("#viewer").addEventListener("click", (e) => {
  const act = e.target.closest("[data-act]");
  if (act) return act.dataset.act === "seen" ? markSeen() : markFix();
  const step = e.target.closest(".tl[data-i]");
  if (step) {
    A.selected = +step.dataset.i;
    return renderViewer();
  }
  const run = e.target.closest("[data-run]");
  if (run) selectRun(run.dataset.run);
});
$("#f-show").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-v]");
  if (!b) return;
  A.show = b.dataset.v;
  $$("#f-show button").forEach((x) => x.classList.toggle("on", x === b));
  buildRows();
  if (!rowOf(selectedGroup())) A.selected = firstSelection();
  render();
});
["#f-day", "#f-script"].forEach((s) => $(s).addEventListener("change", () => attempt(() => load())));
$("#btn-clear-day").addEventListener("click", (e) => {
  const btn = e.currentTarget;
  const day = $("#f-day").value;
  const label = $("#f-day").selectedOptions[0]?.textContent || day;
  confirmClick(btn, "确认清空", async () => {
    const r = await attempt(() => api("DELETE", `/api/audit/days/${day}`));
    if (!r) return;
    toast(r.deleted ? `已清空${label}的 ${r.deleted} 条记录` : `${label}没有记录`);
    await attempt(async () => { await loadDays(); await load(); });
  });
});
$("#btn-refresh").addEventListener("click", () => attempt(() => load(selectedGroup()?.es[0].run_id)));

document.addEventListener("keydown", (e) => {
  if (Tour.els || e.target.matches("input, select, textarea") || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "ArrowDown") { e.preventDefault(); moveRow(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); moveRow(-1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); moveStep(1); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); moveStep(-1); }
  else if (e.key === "1") markSeen();
  else if (e.key === "2") markFix();
});

// 有新的执行结果时自动刷新（停留在今天时）
connectEvents((ev) => {
  if (ev.type === "settings") Settings.applyRemote(ev);
  if (ev.type === "run_state" && ev.state === "done" && $("#f-day").selectedIndex === 0) {
    attempt(() => load(selectedGroup()?.es[0].run_id));
  }
});

function startTour() {
  Tour.start([
    { el: "#report", title: "今天的概况",
      body: "运行次数、成功率、失败了几次、还有几步待审。空跑正常停在「发送」前也算成功。" },
    { el: "#list", title: "一次运行一行，有问题的排最前",
      body: "红点是失败，黄点是还有可疑的步骤没看。连着几次一样的失败合成一行，标着 ×N。↑ ↓ 切换运行。" },
    { el: "#viewer", title: "看截图，下结论",
      body: "上面是这次运行的每一步，点哪步看哪步，← → 逐步切换；下面是原因和截图。看完按 1「看过了」，自动到下一次；做错了按 2「有问题，要修」，它会留在「要修」里，点「去修改」直接打开那个宏的那一步。再按一次就是撤销。" },
    { el: "#filters", title: "筛选",
      body: "按日期、按宏筛；「待审」是还没看过的，「失败」是失败的，「要修」是你标了有问题的。" },
  ], "cw.tour.audit");
}
$("#btn-tour").addEventListener("click", startTour);

attempt(async () => {
  await loadDays();
  await load();
  if (!Tour.seen("cw.tour.audit")) startTour();
});
