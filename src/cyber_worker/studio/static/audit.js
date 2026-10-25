// 审计页：一次运行一行，有问题的排前面；连续几次同样的失败合成一行（×N）。
// 右边是这次运行的步骤时间线 + 选中那一步的原因和截图。复核按「一次运行」来：
// 「看过了」= 这次运行看过、没问题；「有问题，要修」= 留在「要修」里，点「去修改」直接打开那个流程的那一步。
"use strict";

const A = {
  entries: [],   // 当天的全部记录
  groups: [],    // 一次运行一组
  rows: [],      // 列表上的行：一行 = 连着几次同样结果的运行（通常就一次）
  show: "all",   // all | pending | bad | fix
  selected: -1,  // A.entries 的下标（右边正在看的那一步）
  flows: [],    // 这一天出现过的 {platform, platform_title, flow, runs}：流程筛选下拉按应用分组
  days: [],     // 有记录的日子 {day, runs, bad}，新的在前（今天总在）
  day: "",      // 正在看的那天 YYYY-MM-DD
  calMonth: "", // 月历正显示哪个月 YYYY-MM
  pick: "",     // 流程筛选："" 全部；JSON [应用] 这个应用的全部流程；JSON [应用, 流程名] 这一个流程
  report: null, // 后台的日报（卡片上只用它的成功率；数量都在页面里按运行数）
};
const TYPE_TEXT = { click: "点击", type: "输入", key: "按键", scroll: "滚动", wait: "延迟", drag: "拖动",
  hover: "悬停", wait_page: "等待页面", start: "开始", calibrate: "找窗口" };

// 执行时你动了键鼠、程序让给你（runner 记成「中止」）：和急停一样是人为打断，流程没毛病，不算失败
const userStop = (e) => e.status === "aborted" && /检测到你在用电脑/.test(e.detail || "");
const isBad = (e) => (e.status === "failed" || e.status === "aborted") && !userStop(e);
const pending = (e) => !e.reviewed && e.suspicion >= 0.3;
const stepNo = (e) => { const m = /^s(\d+)$/.exec(e.step_id); return m ? +m[1] : null; };
const stepName = (e) => (stepNo(e) ? `第 ${stepNo(e)} 步 · ` : "") + (TYPE_TEXT[e.action] || e.action);
const flagged = (g) => g.es.some((e) => e.reviewed === 2);   // 标了「有问题，要修」
const seen = (g) => !flagged(g) && g.es.every((e) => e.reviewed === 1);  // 标了「看过了」
// 停下来是正常保护、不用改流程（你动了键鼠、窗口没开）：只留「看过了」，不给「要修」「去修改」
const noFix = (g) => (!!g.bad && advise(g.bad)?.edit === false) || (!g.bad && !!stopped(g));

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
// 不用改流程的（你动了键鼠、窗口没开）只给一句说明。认不出的原因返回 null
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

// 成功了、但页面锚点挪了位置（runner.SHIFT_NOTE）：界面布局变了，程序在整个窗口里找到了它。提醒看一眼，不用改流程
function movedAnchor(e) {
  if (e.status !== "ok" || !/页面锚点挪了位置/.test(e.detail || "")) return null;
  return { edit: false, text: "窗口里的界面挪了位置（比如左边多出一栏会话列表），程序在整个窗口里找到了这个页面，照常执行了。"
    + "看一眼截图，确认这一步做在了对的地方；没问题就点「看过了」。" };
}

// ---------------- 数据 ----------------

const dayStr = (d) => d.toLocaleDateString("sv-SE"); // YYYY-MM-DD
// 「今天」「昨天」，更早的「10月22日 周三」
function dayLabel(day) {
  const now = new Date();
  if (day === dayStr(now)) return "今天";
  if (day === dayStr(new Date(now.getTime() - 86400000))) return "昨天";
  const d = new Date(`${day}T00:00:00`);
  return `${d.getMonth() + 1}月${d.getDate()}日 周${"日一二三四五六"[d.getDay()]}`;
}

async function loadDays() {
  const days = await api("GET", "/api/audit/days");
  const today = dayStr(new Date());
  if (!days.some((d) => d.day === today)) days.unshift({ day: today, runs: 0, bad: 0 });
  A.days = days;
  if (!days.some((d) => d.day === A.day)) A.day = today;
  renderDay();
}

const monthOf = (day) => day.slice(0, 7); // YYYY-MM
function shiftMonth(month, n) {
  const d = new Date(`${month}-01T00:00:00`);
  d.setMonth(d.getMonth() + n);
  return dayStr(d).slice(0, 7);
}

// 日期：筛选栏最左边「‹ 今天 ›」，‹ › 一天天翻（只翻有记录的日子）；点中间弹出月历
function renderDay() {
  const i = A.days.findIndex((d) => d.day === A.day);
  $("#day-label").textContent = dayLabel(A.day);
  $("#day-prev").disabled = i >= A.days.length - 1;
  $("#day-next").disabled = i <= 0;
  renderCalendar();
}

// 月历：一周从周一开始。全部成功的日子下面一个绿点，有失败的红点，没有记录的灰着点不了；今天、选中的那天加框
function renderCalendar() {
  A.calMonth ||= monthOf(A.day);
  const today = dayStr(new Date());
  const byDay = Object.fromEntries(A.days.map((x) => [x.day, x]));
  const first = new Date(`${A.calMonth}-01T00:00:00`);
  const blanks = (first.getDay() + 6) % 7;
  const count = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
  let cells = "";
  for (let k = 0; k < blanks; k++) cells += `<span></span>`;
  for (let n = 1; n <= count; n++) {
    const day = `${A.calMonth}-${String(n).padStart(2, "0")}`;
    const rec = byDay[day];
    const cls = [day === A.day && "on", day === today && "today", rec?.runs && "has", rec?.bad && "bad"].filter(Boolean).join(" ");
    const tip = rec?.runs ? `${rec.runs} 次${rec.bad ? `，${rec.bad} 次失败` : ""}` : "没有记录";
    cells += `<button type="button" class="cal-day ${cls}" data-day="${day}" title="${tip}" ${rec ? "" : "disabled"}>${n}<i></i></button>`;
  }
  const oldest = monthOf(A.days[A.days.length - 1].day);
  const [y, m] = A.calMonth.split("-");
  $("#day-menu").innerHTML = `
    <div class="cal-head">
      <button type="button" class="day-arrow" data-cal="-1" aria-label="上个月" ${A.calMonth <= oldest ? "disabled" : ""}>${icon("chevron")}</button>
      <b>${y} 年 ${+m} 月</b>
      <button type="button" class="day-arrow next" data-cal="1" aria-label="下个月" ${A.calMonth >= monthOf(today) ? "disabled" : ""}>${icon("chevron")}</button>
    </div>
    <div class="cal-grid cal-week">${[..."一二三四五六日"].map((w) => `<span>${w}</span>`).join("")}</div>
    <div class="cal-grid">${cells}</div>
    <div class="cal-legend"><span><i class="has"></i>成功</span><span><i class="bad"></i>失败</span></div>`;
}

function setDay(day) {
  A.day = day;
  A.calMonth = monthOf(day);
  closeDropdowns();
  renderDay();
  attempt(() => load());
}

async function load(keepRun) {
  const params = new URLSearchParams({ day: A.day });
  // 没认出应用的旧记录，应用是 ""
  const pick = A.pick && JSON.parse(A.pick);
  if (pick) {
    params.set("platform", pick[0]);
    if (pick.length > 1) params.set("flow", pick[1]);
  }
  const data = await api("GET", `/api/audit?${params}`);
  A.flows = data.flows;
  A.entries = data.entries;
  buildGroups();
  buildRows();
  A.report = data.report;
  renderFlows();
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
    // 时间线上不放延迟（不碰窗口、没有截图，一半的圆点都是它）；延迟本身出了问题（急停在等待中、失败、待看）才放
    const shown = es.filter((e) => e.action !== "wait" || isBad(e) || pending(e) || e.status === "cancelled");
    const top = bad || shown.reduce((m, e) => (e.suspicion > m.suspicion ? e : m), shown[shown.length - 1] || es[es.length - 1]);
    const { platform, platform_title: platformTitle, flow_id: flowId, flow } = es[0];
    return { es, shown, top, bad, at: es[0].at, flow, platform, platformTitle, flowId, dry: es[0].dry_run };
  });
  A.groups.sort((x, y) => y.top.suspicion - x.top.suspicion || y.at.localeCompare(x.at));
}

// 人为打断的运行（急停、你在用电脑）是灰色：没跑完，不能是绿色的「正常」；但流程没毛病，也不是红色的失败
const stopped = (g) => g.es.find((e) => e.status === "cancelled" || userStop(e));
const tone = (g) => (g.bad ? "bad" : stopped(g) ? "stop" : g.es.some(pending) ? "warn" : "ok");

function summary(g) {
  if (g.bad) return `${stepNo(g.bad) ? `第 ${stepNo(g.bad)} 步：` : ""}${human(g.bad.detail) || STATUS_TEXT[g.bad.status]}`;
  const stop = stopped(g);
  if (stop && userStop(stop)) return `${stepNo(stop) ? `第 ${stepNo(stop)} 步：` : ""}${human(stop.detail)}`;
  if (stop) {
    const done = g.es.filter((e) => e.id < stop.id && stepNo(e) && e.status !== "cancelled").pop();
    return done ? `急停：做完第 ${stepNo(done)} 步后停下` : "急停：还没开始做就停下";
  }
  // 「N 步」只数真正的步骤（s01、s02…），不数延迟和找窗口，和编辑器里的「N 步」一样
  const steps = g.es.filter((e) => stepNo(e)).length;
  const k = g.es.filter(pending).length;
  if (k) return `${steps} 步，有 ${k} 步待看`;
  if (g.es.some((e) => e.status === "skipped")) return "全部正常，空跑到「发送」前停下";
  return `${steps} 步全部成功`;
}

// 列表上的行：按筛选留下的运行；连着的「同一个流程、同样失败」合成一行
function buildRows() {
  const keep = { all: () => true, bad: (g) => !!g.bad, fix: flagged, pending: (g) => g.es.some(pending) }[A.show];
  A.rows = [];
  for (const g of A.groups.filter(keep)) {
    const last = A.rows[A.rows.length - 1];
    const key = `${g.platform}|${g.flow}|${g.dry}|${summary(g)}`;
    if (g.bad && last?.key === key) last.groups.push(g);
    else A.rows.push({ key, groups: [g] });
  }
}

const firstSelection = () => (A.rows.length ? A.entries.indexOf(A.rows[0].groups[0].top) : -1);
const selectedGroup = () => A.groups.find((g) => g.es.includes(A.entries[A.selected]));
const rowOf = (g) => A.rows.find((r) => r.groups.includes(g));

// ---------------- 渲染 ----------------

function render() {
  renderReport();
  renderTabs();
  renderList();
  renderViewer();
}

// 卡片和下面的标签用同一套数：都按「运行」数（复核是一次运行一次运行看的），点「看过了」两边一起变。
// 以前卡片的「待审」是后台按步骤数的失败 / 中止，和标签对不上
function counts() {
  return { all: A.groups.length, pending: A.groups.filter((g) => g.es.some(pending)).length,
    bad: A.groups.filter((g) => g.bad).length, fix: A.groups.filter(flagged).length };
}

// 统计卡片只是看的（筛选在下面的标签上）
function renderReport() {
  const r = A.report || {};
  const n = counts();
  const rate = r.success_rate == null ? "—" : `${Math.round(r.success_rate * 100)}%`;
  const tile = (value, label, cls = "") => `<div class="stat ${cls}"><div class="v">${value}</div><div class="k">${label}</div></div>`;
  $("#report").innerHTML = tile(n.all, "运行次数") + tile(rate, "成功率")
    + tile(n.bad, "失败", n.bad ? "bad" : "") + tile(n.pending, "待审", n.pending ? "alert" : "");
}

function setShow(v) {
  A.show = v;
  $$("#f-show button").forEach((x) => x.classList.toggle("on", x.dataset.v === v));
  buildRows();
  if (!rowOf(selectedGroup())) A.selected = firstSelection();
  render();
}

// 「全部 QQ 的流程」「全部微信的流程」：英文名两边空一格
const allOf = (title) => (/^[A-Za-z0-9]|[A-Za-z0-9]$/.test(title) ? `全部 ${title} 的流程` : `全部${title}的流程`);

// 流程筛选：自己画的下拉（浏览器自带的 <select> 改不了样式）。按应用分组，组头带头像，点组头 =
// 「这个应用的全部流程」；每个流程后面是这天跑了几次；同名的流程在不同应用里也分得清
function renderFlows() {
  const current = A.pick;
  const groups = new Map();
  A.flows.forEach((m) => {
    const k = m.platform || "";
    if (!groups.has(k)) groups.set(k, { title: m.platform_title || "其他", flows: [] });
    groups.get(k).flows.push(m);
  });
  const item = (value, label, n, cls = "") => `
    <button type="button" class="ctx-item dd-item ${cls} ${value === current ? "on" : ""}" role="option"
      aria-selected="${value === current}" data-pick="${escapeHtml(value)}"><span>${label}</span>${
      value === current ? icon("check") : n != null ? `<i>${n}</i>` : ""}</button>`;
  let html = item("", "全部流程", null);
  for (const [platform, g] of groups) {
    const total = g.flows.reduce((t, m) => t + m.runs, 0);
    html += `<div class="menu-sep"></div>${item(JSON.stringify([platform]),
      `${platform ? avatar(platform, "xs", g.title) : ""}<b>${escapeHtml(g.title)}</b>`, `全部 ${total}`, "dd-group")}`;
    html += g.flows.map((m) => item(JSON.stringify([platform, m.flow]), escapeHtml(m.flow), m.runs, "dd-sub")).join("");
  }
  $("#flow-menu").innerHTML = html;
  // 选中的流程这一天没有记录了（换了日期）：照样显示它，别悄悄变成「全部」
  const p = current && JSON.parse(current);
  const title = p && (A.flows.find((m) => (m.platform || "") === p[0])?.platform_title || p[0] || "其他");
  $("#flow-label").textContent = !p ? "全部流程" : p.length > 1 ? p[1] : allOf(title);
  fitFlowWidth();
}

// 按钮和展开的列表一样宽，以列表要的宽度为准：最宽的一项再留出名字和数量之间的空隙，至少 220px
// （菜单的常规宽度；只按字算太挤），按钮上的字更长就以按钮为准，最多 320px
function fitFlowWidth() {
  const dd = $("#f-flow"), menu = $("#flow-menu"), btn = $("#flow-btn");
  dd.style.removeProperty("--flow-w");
  const hidden = menu.hidden;
  Object.assign(menu.style, { visibility: "hidden", width: "max-content", right: "auto" });
  menu.hidden = false;
  const listW = menu.getBoundingClientRect().width;
  menu.hidden = hidden;
  Object.assign(menu.style, { visibility: "", width: "", right: "" });
  Object.assign(btn.style, { width: "max-content" });
  const btnW = btn.getBoundingClientRect().width;
  btn.style.width = "";
  dd.style.setProperty("--flow-w", `${Math.ceil(Math.min(320, Math.max(220, listW + 32, btnW)))}px`);
}

// 全部 / 待审 / 失败 / 要修 后面直接带数量
function renderTabs() {
  const n = counts();
  $$("#f-show [data-n]").forEach((el) => {
    el.textContent = n[el.dataset.n];
    el.classList.toggle("zero", !n[el.dataset.n]);
  });
}

function closeDropdowns() {
  $$(".dd-pop").forEach((m) => (m.hidden = true));
  $$("[aria-haspopup]").forEach((b) => b.setAttribute("aria-expanded", "false"));
}
function toggleDropdown(btn, menu) {
  const open = menu.hidden;
  closeDropdowns();
  menu.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (open) menu.querySelector(".on")?.scrollIntoView({ block: "nearest" });
}

// 哪个应用：头像 + 「QQ › 群聊天」；认不出应用（流程名在好几个应用里都有的旧记录）只写流程名
function appName(g, link) {
  if (!g.platform) return `<b>${escapeHtml(g.flow)}</b>`;
  const title = escapeHtml(g.platformTitle || g.platform);
  return `${avatar(g.platform, "xs", g.platformTitle || g.platform)}${link
    ? `<a class="app-link" href="/apps?app=${encodeURIComponent(g.platform)}" title="打开这个应用">${title}</a>`
    : `<span class="app-name">${title}</span>`}<span class="sep">›</span><b>${escapeHtml(g.flow)}</b>`;
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
  const stop = stopped(g);
  const status = g.bad ? STATUS_TEXT[g.bad.status] : stop ? (userStop(stop) ? "已停下" : "已急停")
    : tone(g) === "warn" ? "待看" : "正常";
  const reason = human(e.detail);
  const score = e.page_score == null ? "" : `，得分 ${e.page_score.toFixed(2)}`;
  const id = g.flowId;
  const fixStep = g.bad || g.top;
  const advice = isBad(e) || userStop(e) ? advise(e) : weakTarget(e) || movedAnchor(e);
  v.innerHTML = `
    <div class="card-head viewer-head">
      <span class="pill ${tone(g)}">${status}</span>
      <span class="viewer-title">${appName(g, true)}<b>· ${g.dry ? "空跑" : "执行"}</b></span>
      <span class="muted">${g.at.slice(11, 19)}</span>
      <span class="grow"></span>
      ${twins.length ? `<span class="muted twins">同样的还有 ${twins.map((t) =>
        `<button type="button" class="link" data-run="${escapeHtml(t.es[0].run_id)}">${t.at.slice(11, 16)}</button>`).join(" ")}</span>` : ""}
    </div>
    <ol class="timeline" aria-label="这次运行的步骤">${g.shown.map((x) => `
      <li><button type="button" class="tl ${x === e ? "on" : ""}" data-i="${A.entries.indexOf(x)}" title="${escapeHtml(human(x.detail) || STATUS_TEXT[x.status] || "")}">
        <span class="dot ${userStop(x) ? "" : isBad(x) ? "bad" : pending(x) ? "warn" : x.status === "ok" ? "ok" : ""}"></span>${escapeHtml(stepName(x))}
      </button></li>`).join("")}
    </ol>
    <div class="scroller viewer-body">
      <div class="reason ${isBad(e) ? "bad" : userStop(e) ? "" : pending(e) ? "warn" : ""}">
        <b>${escapeHtml(stepName(e))}：${escapeHtml(userStop(e) ? "已停下" : STATUS_TEXT[e.status] || e.status)}${e.spot_check ? ` <span class="badge warn">抽查</span>` : ""}</b>
        ${reason ? `<span>${escapeHtml(reason)}</span>` : ""}
        <small>${e.expected_page ? `执行前核对页面「${escapeHtml(e.expected_page)}」${score}`
          : /没有声明 expected_page|没有 pages\.yaml/.test(e.detail || "") ? "这一步还没设页面" : "这一步不用核对页面"}${
          e.detail && reason !== e.detail ? ` · 详细：${escapeHtml(e.detail)}` : ""}</small>
      </div>
      ${advice ? `<div class="advice">${icon("info")}<div><b>怎么修</b><p>${escapeHtml(advice.text)}</p>
        ${advice.edit && id ? `<a class="btn sm primary" href="/?flow=${encodeURIComponent(id)}&step=${encodeURIComponent(e.step_id)}">${icon("sliders")}去修改</a>` : ""}</div></div>` : ""}
      ${e.screenshot_url ? `<div class="viewer-shot"><img src="${e.screenshot_url}" alt="操作后截图"></div>`
        : `<p class="no-shot">${icon("image")}这一步没有截图${isBad(e) ? "：没做成，所以没有操作后的画面" : ""}</p>`}
    </div>
    <div class="review-actions">
      <button class="btn ${seen(g) ? "primary" : ""}" data-act="seen"
        title="${seen(g) ? "再点一下撤销" : "这次运行看过了，没问题；自动跳到下一次"}">${icon("check")}${seen(g) ? "已看过" : "看过了"} <kbd>1</kbd></button>
      ${noFix(g) ? "" : `<button class="btn ${flagged(g) ? "danger" : "danger-ghost"}" data-act="fix"
        title="${flagged(g) ? "再点一下撤销" : "这次运行做错了：留在「要修」里，回头去改流程"}">${icon("alert")}${flagged(g) ? "已标为要修" : "有问题，要修"} <kbd>2</kbd></button>`}
      ${flagged(g) && !noFix(g) && !(advice?.edit && id) ? (id ? `<a class="btn" href="/?flow=${encodeURIComponent(id)}&step=${encodeURIComponent(fixStep.step_id)}">${icon("sliders")}去修改</a>`
        : `<button class="btn" disabled title="这个流程已经删了，没法再改">${icon("trash")}流程已删除</button>`) : ""}
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

// ← →：这次运行的上一步 / 下一步（跳过时间线上没放的延迟，到头就停住）
function moveStep(delta) {
  const g = selectedGroup();
  if (!g) return;
  const i = g.shown.indexOf(A.entries[A.selected]) + delta;
  if (i < 0 || i >= g.shown.length) return;
  A.selected = A.entries.indexOf(g.shown[i]);
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
  if (b) setShow(b.dataset.v);
});
$("#day-btn").addEventListener("click", (e) => {
  if ($("#day-menu").hidden) {
    A.calMonth = monthOf(A.day); // 每次打开都从正在看的那个月开始
    renderCalendar();
  }
  toggleDropdown(e.currentTarget, $("#day-menu"));
});
$("#flow-btn").addEventListener("click", (e) => toggleDropdown(e.currentTarget, $("#flow-menu")));
$("#day-prev").addEventListener("click", () => setDay(A.days[A.days.findIndex((d) => d.day === A.day) + 1].day));
$("#day-next").addEventListener("click", () => setDay(A.days[A.days.findIndex((d) => d.day === A.day) - 1].day));
$("#day-menu").addEventListener("click", (e) => {
  const nav = e.target.closest("[data-cal]");
  if (nav) {
    A.calMonth = shiftMonth(A.calMonth, +nav.dataset.cal);
    return renderCalendar();
  }
  const b = e.target.closest("[data-day]");
  if (b) setDay(b.dataset.day);
});
$("#flow-menu").addEventListener("click", (e) => {
  const b = e.target.closest("[data-pick]");
  if (!b) return;
  A.pick = b.dataset.pick;
  closeDropdowns();
  attempt(() => load());
});
// 点下拉外面、按 Esc 收起
document.addEventListener("click", (e) => {
  if (!e.composedPath().some((el) => el.classList?.contains("dd"))) closeDropdowns();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $$(".dd-pop").some((m) => !m.hidden)) {
    closeDropdowns();
    e.stopPropagation();
  }
}, true);
$("#btn-clear-day").addEventListener("click", (e) => {
  const btn = e.currentTarget;
  const day = A.day;
  const label = dayLabel(day);
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
  if (ev.type === "run_state" && ev.state === "done" && A.day === dayStr(new Date())) {
    attempt(async () => { await loadDays(); await load(selectedGroup()?.es[0].run_id); });
  }
});

function startTour() {
  Tour.start([
    { el: "#report", title: "今天的概况",
      body: "运行次数、成功率、失败了几次、还有几次待审。空跑正常停在「发送」前也算成功。" },
    { el: "#list", title: "一次运行一行，有问题的排最前",
      body: "红点是失败，黄点是还有可疑的步骤没看。连着几次一样的失败合成一行，标着 ×N。↑ ↓ 切换运行。" },
    { el: "#viewer", title: "看截图，下结论",
      body: "上面是这次运行的每一步，点哪步看哪步，← → 逐步切换；下面是原因和截图。看完按 1「看过了」，自动到下一次；做错了按 2「有问题，要修」，它会留在「要修」里，点「去修改」直接打开那个流程的那一步。再按一次就是撤销。" },
    { el: "#filters", title: "筛选",
      body: "按日期、按流程筛；「待审」是还没看过的，「失败」是失败的，「要修」是你标了有问题的。" },
  ], "cw.tour.audit");
}
$("#btn-tour").addEventListener("click", startTour);

attempt(async () => {
  await loadDays();
  await load();
  if (!Tour.seen("cw.tour.audit")) startTour();
});
