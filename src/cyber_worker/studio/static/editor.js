// 宏设计页：左「添加步骤」、中步骤列表（勾选 / 拖拽排序 / 撤销重做）、右画布 + 标签页（参数 / 宏设置 / 运行）。
"use strict";

const S = {
  list: [],
  id: null,
  detail: null, // 服务端返回的完整信息（锚点、画面、问题）
  script: null, // 正在编辑的脚本（本地副本）
  selected: -1, // 选中的步骤下标
  checked: new Set(),
  undo: [],
  redo: [],
  dirty: false,
  anchorMode: false,
  regionFor: "anchor", // 框选的用途：anchor = 页面锚点；target = 当前点击步骤的目标图
  frame: "reference", // reference | latest | shot
  win: null, // 画面上显示的是哪个窗口（null = 主窗口）：每个窗口各有参考图和最新截图
  shot: null, // 运行截图 {url}
  tab: "step",
  recording: false,
  recSteps: [],
  running: false, // false | "waiting"（等用户松开键鼠）| "running"
  recHotkey: "", // 开始 / 停止录制的全局快捷键
  dryHotkey: "", // 空跑当前宏的全局快捷键
  seenRecIds: new Set(), // 处理过的录制结果：按钮的响应和推送事件可能各来一次
  runMarks: {}, // step_id → 最近一次运行的状态
  hotkey: "",
  reportedDirty: false, // 上次告诉服务端的「有没保存的修改」
};
// 这个标签页的编号：空跑快捷键遇到没保存的修改时，交给报告它的那个页面去跑
const PAGE_ID = crypto.randomUUID?.() ?? String(Math.random()).slice(2);

const NEEDS_PAGE = (s) => s.action === "type" || !!s.sends;
const VAR_RE = /\{([\p{L}\p{N}_]+)\}/gu;
const varsIn = (text) => [...(text || "").matchAll(VAR_RE)].map((m) => m[1]);
const TYPE = {
  click: { icon: "pointer", name: "点击" },
  type: { icon: "type", name: "输入文本" },
  key: { icon: "keyboard", name: "按键" },
  scroll: { icon: "scroll", name: "滚动" },
  wait: { icon: "clock", name: "延迟" },
  drag: { icon: "move", name: "拖动" },
  hover: { icon: "mouse", name: "悬停" },
  wait_page: { icon: "page", name: "等待页面" },
};

const stage = new Stage($("#canvas"), {
  onMove: (i, x, y, which) =>
    mutate((sc) => Object.assign(sc.steps[i], which === "end" ? { x2: x, y2: y } : { x, y })),
  onPlace: (x, y) => mutate((sc) => Object.assign(sc.steps[S.selected], { x, y })),
  onSelect: (i) => select(i),
  onRegion: (r) => {
    if (S.regionFor === "target") return saveTarget(r);
    $("#anchor-form").hidden = false;
    $("#anchor-hint").textContent = `已框选 ${r.w}×${r.h} 的区域，回车保存（名字可以不改）：`;
    const input = $("#anchor-page");
    if (!input.value) input.value = nextPageName();
    input.focus();
    input.select();
  },
  onHover: (pt) => ($("#coord").textContent = pt ? `x ${pt.x.toFixed(1)}  y ${pt.y.toFixed(1)}` : ""),
});
document.addEventListener("themechange", () => stage.draw());

// ---------------- 加载 ----------------

async function loadList(selectId) {
  S.list = await api("GET", "/api/scripts");
  // 记住的宏可能已经被删了：按顺序找第一个还存在的
  const exists = (x) => x && S.list.some((s) => s.id === x);
  const id = [selectId, S.id, remembered(), S.list[0]?.id].find(exists);
  if (id) {
    await open(id);
  } else {
    S.id = null;
    S.script = null;
    S.detail = null;
    reportCurrent();
    render();
    await showFrame();
  }
}

async function open(id) {
  const d = await api("GET", `/api/scripts/${id}/detail`);
  S.id = id;
  S.undo = [];
  S.redo = [];
  S.runMarks = {};
  S.selected = -1;
  S.shot = null;
  S.checked.clear();
  $("#run-log").innerHTML = "";
  try { localStorage.setItem("cw.script", id); } catch {}
  reportCurrent();
  applyDetail(d);
  S.win = null;
  S.frame = d.frames.reference.main ? "reference" : "latest";
  await showFrame();
}

// 告诉服务端界面上开着哪个宏、填了哪些变量、有没有没保存的修改：按录制 / 空跑快捷键时用的就是它们
function currentBody(dirty = S.dirty) {
  return { script_id: S.id, variables: S.id ? savedVars() : {}, dirty: !!S.id && dirty, page: PAGE_ID };
}
function reportCurrent() {
  S.reportedDirty = S.dirty;
  api("POST", "/api/current", currentBody()).catch(() => {});
}

function remembered() {
  try { return localStorage.getItem("cw.script"); } catch { return null; }
}

function applyDetail(d) {
  S.detail = d;
  S.script = structuredClone(d.script);
  S.dirty = false;
  if (S.selected >= S.script.steps.length) S.selected = -1;
  render();
}

// 选中的步骤录制时的画面（这一步在画面上显示的窗口里才算）
function stepShot() {
  const s = S.script?.steps[S.selected];
  return s && (s.win || null) === S.win ? S.detail?.shots?.[s.id] : undefined;
}

// 当前窗口的参考图 / 最新截图（没有就是 undefined）。选中的步骤有录制时的画面，「参考图」就显示它
function frameOf(kind) {
  if (kind === "reference" && stepShot()) return stepShot();
  return S.detail?.frames[kind]?.[S.win || "main"];
}

// 框锚点 / 目标图时告诉服务端画面上是哪一张
function frameParams() {
  if (S.frame === "reference" && stepShot()) return { frame: "step", step_id: S.script.steps[S.selected].id };
  return { frame: S.frame === "reference" ? "reference" : "latest", win: S.win };
}

// 切换画面上显示的窗口。选中步骤时自动切到它所在的窗口；这个窗口没有当前这种画面就换另一种
function setWin(win, redraw = true) {
  win = win || null;
  if (win === S.win) return;
  S.win = win;
  if (S.frame === "shot" && (S.shot?.win || null) !== win) S.frame = "reference";
  if (redraw) showFrame();
}

// 只列步骤用到的窗口（以前录过、现在没有步骤在用的不列），再加上画面上正显示的那个；不到两个就不显示
function renderWinSeg() {
  const used = new Set([...(S.script?.steps || []).map((s) => s.win || null), S.win]);
  const keys = [null, ...Object.keys(S.script?.windows || {})].filter((k) => used.has(k));
  const seg = $("#win-seg");
  seg.hidden = keys.length < 2;
  if (seg.hidden) return;
  seg.innerHTML = keys.map((k) => `<button type="button" data-win="${k ?? ""}" class="${(k || null) === S.win ? "on" : ""}"
      title="${escapeHtml(windowLabel(k))}">${escapeHtml(windowLabel(k))}</button>`).join("");
}

async function showFrame() {
  // 这个窗口没有当前这种画面（比如聊天窗口只截过「最新截图」）：换另一种
  const other = S.frame === "reference" ? "latest" : "reference";
  if (S.frame !== "shot" && !frameOf(S.frame) && frameOf(other)) S.frame = other;
  // 选中的是延迟：不操作任何窗口，不显示画面（显示了反而像在哪里做了什么）
  const sel = S.script?.steps[S.selected];
  const idle = sel?.action === "wait" && S.frame !== "shot";
  const f = idle ? undefined : S.frame === "shot" ? S.shot : frameOf(S.frame);
  const scale = S.frame === "shot" ? (S.script?.windows?.[S.shot?.win]?.scale ?? S.script?.window.scale) : f?.scale;
  $$("#frame-seg button").forEach((b) => {
    b.classList.toggle("on", b.dataset.frame === S.frame);
    if (b.dataset.frame === "shot") b.hidden = !S.shot;
    if (b.dataset.frame === "latest") b.disabled = !frameOf("latest");
    if (b.dataset.frame === "reference") b.disabled = !frameOf("reference");
  });
  renderWinSeg();
  $("#canvas-empty").innerHTML = idle ? `延迟：等 ${escapeHtml(String(sel.seconds))} 秒，不操作窗口，没有画面`
    : S.win ? `「${escapeHtml(windowLabel(S.win))}」还没有画面<br>打开这个窗口，再点右上角「截取当前窗口」`
    : "还没有画面<br>点右上角「截取当前窗口」";
  const shot = S.frame === "reference" && f && f === stepShot();
  $("#frame-note").textContent = shot ? `第 ${S.selected + 1} 步录制时的画面`
    : sel?.action === "wait_page" && f ? `等待页面「${sel.page}」：高亮的是它的锚点`
    : S.frame === "reference" && f ? `${windowLabel(S.win)}的参考图` : "";
  $("#canvas-empty").hidden = !!f;
  $("#canvas").hidden = !f;
  if (f?.url !== S.shownUrl) {  // 同一张图不重新加载（连着几步共用一张画面时）
    S.shownUrl = f?.url;
    await stage.setFrame(f?.url, scale).catch(() => toast("画面加载失败", true));
  }
  syncStage();
}

// ---------------- 编辑 ----------------

function mutate(fn) {
  if (!S.script) return;
  S.undo.push(JSON.stringify(S.script));
  if (S.undo.length > 200) S.undo.shift();
  S.redo = [];
  fn(S.script);
  S.dirty = true;
  render();
}

function undo() {
  if (!S.undo.length) return;
  S.redo.push(JSON.stringify(S.script));
  S.script = JSON.parse(S.undo.pop());
  afterHistory();
}

function redo() {
  if (!S.redo.length) return;
  S.undo.push(JSON.stringify(S.script));
  S.script = JSON.parse(S.redo.pop());
  afterHistory();
}

function afterHistory() {
  S.dirty = true;
  S.checked.clear();
  if (S.selected >= S.script.steps.length) S.selected = S.script.steps.length - 1;
  render();
}

function nextId(steps) {
  const nums = steps.map((s) => /^s(\d+)$/.exec(s.id)).filter(Boolean).map((m) => +m[1]);
  return "s" + String(Math.max(0, ...nums) + 1).padStart(2, "0");
}

function lastPage(steps, before) {
  for (let i = before; i >= 0; i--) if (steps[i]?.expected_page) return steps[i].expected_page;
  return null;
}

function addStep(kind) {
  if (!S.script) return toast("先新建或选择一个宏", true);
  const { width, height } = S.script.window;
  const at = S.selected >= 0 ? S.selected + 1 : S.script.steps.length;
  const base = {
    wait: { action: "wait", seconds: 1 },
    key: { action: "key", combo: "enter" },
    click: { action: "click", x: Math.round(width / 2), y: Math.round(height / 2) },
    scroll: { action: "scroll", dy: 3, x: Math.round(width / 2), y: Math.round(height / 2) },
    hover: { action: "hover", x: Math.round(width / 2), y: Math.round(height / 2), seconds: 1, delay_after: 0 },
    drag: { action: "drag", x: Math.round(width / 2 - 80), y: Math.round(height / 2),
      x2: Math.round(width / 2 + 80), y2: Math.round(height / 2), duration: 0.5 },
    type: { action: "type", text: "", expected_page: lastPage(S.script.steps, at - 1) },
    wait_page: { action: "wait_page", page: lastPage(S.script.steps, at - 1) || Object.keys(S.detail?.pages || {})[0] || "",
      timeout: 10 },
  }[kind];
  // 等待页面：窗口就是那个页面锚点所在的窗口，不用用户再选
  if (kind === "wait_page") base.win = pageWindow(base.page);
  mutate((sc) => sc.steps.splice(at, 0, { id: nextId(sc.steps), ...base }));
  select(at);
}

function deleteSteps() {
  const idx = S.checked.size ? [...S.checked] : S.selected >= 0 ? [S.selected] : [];
  if (!idx.length) return;
  const drop = new Set(idx);
  mutate((sc) => (sc.steps = sc.steps.filter((_, i) => !drop.has(i))));
  S.checked.clear();
  S.selected = -1;
  render();
  toast(`删除了 ${idx.length} 步，可以撤销`);
}

function moveStep(from, to) {
  if (from === to) return;
  mutate((sc) => {
    const [s] = sc.steps.splice(from, 1);
    sc.steps.splice(to, 0, s);
  });
  S.checked.clear();
  select(to);
}

function select(i) {
  S.selected = i;
  if (i >= 0) S.tab = "step";
  const step = S.script?.steps[i];
  // 画面跟着切到这一步所在的窗口；等待页面切到那个页面锚点所在的窗口；延迟不操作窗口，不切
  if (step?.action === "wait_page") setWin(pageWindow(step.page, step.win), false);
  else if (step && step.action !== "wait") setWin(step.win, false);
  if (S.frame !== "shot") {
    if (step && stepShot()) S.frame = "reference"; // 这一步有录制时的画面就显示它：一眼看出在哪操作的
    showFrame();
  }
  render();
  $(`#step-list li[data-i="${i}"]`)?.scrollIntoView({ block: "nearest" });
}

function setTab(tab) {
  S.tab = tab;
  renderTabs();
}

// 把页面上的修改写回（草稿还是草稿）
async function writeBack() {
  const d = await attempt(() => api("PUT", `/api/scripts/${S.id}/detail`, S.script));
  if (d) {
    const keep = S.selected;
    applyDetail(d);
    S.selected = keep < d.script.steps.length ? keep : -1;
    render();
  }
  return d;
}

// 「保存」按钮：写回修改；如果是新建的草稿，再正式存进宏库
async function save() {
  if (!S.script) return;
  if (S.dirty && !(await writeBack())) return;
  if (!S.detail?.draft) {
    toast("已保存");
    return S.detail;
  }
  const d = await attempt(() => api("POST", `/api/scripts/${S.id}/commit`));
  if (!d) return;
  try { localStorage.setItem("cw.script", d.id); } catch {}
  await loadList(d.id);
  toast(`已保存「${d.script.name}」`);
  return d;
}

// 需要服务端读脚本的操作（录制、截图、锚点、执行）之前，先把修改写回（不会把草稿存进宏库）
async function ensureSaved() {
  if (!S.dirty) return true;
  return !!(await writeBack());
}

// ---------------- 问题检查（与服务端 problems() 规则一致） ----------------

function localProblems() {
  const out = {};
  if (!S.script) return out;
  const pages = new Set(Object.keys(S.detail?.pages || {}));
  const seen = new Set();
  for (const s of S.script.steps) {
    const add = (m) => (out[s.id] ||= []).push(m);
    if (seen.has(s.id)) add("步骤编号重复");
    seen.add(s.id);
    if (NEEDS_PAGE(s) && !s.expected_page) add("输入 / 发送前必须指定所在页面");
    if (s.typed_with_ime) add(`录制时开着中文输入法，录到的只是按键「${s.text}」：请改成实际要输入的文字`);
    const otherWin = (p) => `页面「${p}」的锚点截自别的窗口，在这一步的窗口里永远对不上：换一个页面，或在这个窗口里框一个锚点`;
    if (s.expected_page && !pages.has(s.expected_page)) add(`页面「${s.expected_page}」还没有锚点`);
    else if (s.expected_page && !pageWins(s.expected_page).has(s.win || null)) add(otherWin(s.expected_page));
    if (s.action === "wait_page" && !s.page) add("选一个要等的页面");
    else if (s.action === "wait_page" && !pages.has(s.page)) add(`页面「${s.page}」还没有锚点`);
    else if (s.action === "wait_page" && !pageWins(s.page).has(s.win || null)) add(otherWin(s.page));
    if (s.action === "type") {
      for (const v of varsIn(s.text)) {
        if (!S.script.variables.includes(v)) add(`{${v}} 没有声明为变量，会按原文输入`);
      }
    }
  }
  return out;
}

// ---------------- 渲染 ----------------

function render() {
  const has = !!S.script;
  if (S.id && S.dirty !== S.reportedDirty) reportCurrent();
  renderWinSeg(); // 增删步骤后用到的窗口可能变了
  const busy = S.recording;
  $$("[data-add]").forEach((el) => (el.disabled = !has || busy));
  $("#btn-record").disabled = picking;
  $("#btn-record-menu").disabled = !has || busy;
  $("#btn-save").disabled = !has || busy;
  $("#btn-capture").disabled = $("#btn-anchor-mode").disabled = !has || busy;
  $("#btn-undo").disabled = !S.undo.length || busy;
  $("#btn-redo").disabled = !S.redo.length || busy;
  $("#btn-delete").disabled = !has || (!S.checked.size && S.selected < 0) || busy;
  $("#btn-dry").disabled = $("#btn-run").disabled = !has || busy || !!S.running;
  renderRunBusy();
  const unsaved = S.dirty || !!S.detail?.draft;
  $("#btn-save span").textContent = unsaved ? "保存 •" : "保存";
  $("#btn-save").classList.toggle("primary", unsaved && has);
  $("#btn-save").title = S.detail?.draft ? "这个宏还没保存，点这里存进宏库（Ctrl / ⌘ + S）" : "保存（Ctrl / ⌘ + S）";
  const del = $("#btn-delete-macro");
  del.disabled = !has || S.recording;
  if (!del.dataset.armed) del.title = S.detail?.draft ? "丢弃这个还没保存的宏" : "删除这个宏";
  $("#steps-card").classList.toggle("has-checked", S.checked.size > 0);
  renderPickerTrigger();
  renderMeta();
  renderSteps();
  renderTotal();
  renderTabs();
  renderStepPane();
  renderMacroPane();
  renderVars();
  renderStatus();
  syncStage();
}

// 画面上显示的是哪个窗口：参考图是主窗口；最新截图记着自己是哪个窗口；运行截图跟着对应步骤
function frameWin() {
  return S.frame === "shot" ? S.shot?.win || null : S.win;
}

function syncStage() {
  if (!S.script) return stage.setData([], {}, -1);
  stage.setData([...S.script.steps, ...S.recSteps], S.detail?.pages, S.selected, frameWin());
}

function windowLabel(key) {
  if (!key) return "主窗口";
  const w = S.script?.windows?.[key];
  return w ? (w.title || w.app) : key;
}

function renderMeta() {
  const box = $("#meta-chips");
  if (!S.script) {
    box.innerHTML = "";
    return;
  }
  const w = S.script.window;
  const pages = Object.keys(S.detail?.pages || {}).length;
  box.innerHTML = [
    `<span class="chip" title="窗口尺寸（点）">${w.width} × ${w.height}${w.scale !== 1 ? ` @${w.scale}x` : ""}</span>`,
    ...(Object.keys(S.script.windows || {}).length
      ? [`<span class="chip" title="${escapeHtml(Object.values(S.script.windows).map((w) => w.title || w.app).join("、"))}">${icon("window")}${Object.keys(S.script.windows).length + 1} 个窗口</span>`]
      : []),
    `<span class="chip" title="变量">${icon("braces")}${S.script.variables.length} 个变量</span>`,
    `<span class="chip" title="有锚点的页面">${icon("scan")}${pages} 个页面</span>`,
  ].join("");
}

const BUTTON_TEXT = { right: "右键", middle: "中键" };
const MOD_TEXT = { ctrl: "Ctrl", shift: "Shift", alt: "Alt", cmd: "⌘" };
const modsText = (s) => (s.modifiers || []).map((m) => MOD_TEXT[m] + "+").join("");

function summary(s) {
  switch (s.action) {
    case "click": {
      const how = s.hold > 0 ? `长按 ${s.hold} 秒` : s.count === 2 ? "双击" : "点击";
      return `${modsText(s)}${BUTTON_TEXT[s.button] || ""}${how} (${s.x}, ${s.y})`;
    }
    case "drag": return `${modsText(s)}${BUTTON_TEXT[s.button] || ""}拖动 (${s.x}, ${s.y}) → (${s.x2}, ${s.y2})`;
    case "hover": return `悬停 (${s.x}, ${s.y}) ${s.seconds} 秒`;
    case "type": return s.text ? `输入「${s.text}」` : "输入（还没填内容）";
    case "key": return `按 ${s.combo}${s.times > 1 ? ` × ${s.times}` : ""}`;
    case "scroll": {
      const parts = [];
      if (s.dy) parts.push(`向${s.dy > 0 ? "下" : "上"} ${Math.abs(s.dy)} 格`);
      if (s.dx) parts.push(`向${s.dx > 0 ? "右" : "左"} ${Math.abs(s.dx)} 格`);
      return `滚动 ${parts.join("、") || "0 格"}`;
    }
    case "wait": return `等待 ${s.seconds} 秒`;
    case "wait_page": return s.page ? `等待页面「${s.page}」出现（最多 ${s.timeout} 秒）` : "等待页面（还没选）";
  }
  return s.action;
}

function renderSteps() {
  const list = $("#step-list");
  const empty = $("#steps-empty");
  const steps = S.script?.steps || [];
  const problems = localProblems();
  const marks = { ok: ["check", "ok"], skipped: ["skip", "skip"], aborted: ["x", "bad"], failed: ["x", "bad"], cancelled: ["stop", "skip"] };
  const row = (s, i, rec) => {
    const probs = problems[s.id] || [];
    const mark = marks[S.runMarks[s.id]];
    const t = TYPE[s.action] || { icon: "info", name: s.action };
    return `<li class="step ${i === S.selected ? "selected" : ""} ${rec ? "recording" : ""}" data-i="${i}" draggable="${!rec}">
      ${rec ? `<span style="width:15px"></span>` : `<input type="checkbox" class="check" data-check="${i}" ${S.checked.has(i) ? "checked" : ""} aria-label="勾选第 ${i + 1} 步">`}
      <span class="num">${i + 1}</span>
      <span class="type-ico sm ${s.action}" title="${t.name}">${icon(t.icon)}</span>
      <span class="body">
        <span class="summary">${escapeHtml(summary(s))}</span>
        ${s.note ? `<span class="sub">${escapeHtml(s.note)}</span>` : ""}
      </span>
      ${s.win ? `<span class="badge" title="在这个窗口里操作">${icon("window")}${escapeHtml(windowLabel(s.win))}</span>` : ""}
      ${s.target && s.use_target !== false ? `<span class="badge find" title="按图找：界面变了也能找到它再点">${icon("image")}按图</span>` : ""}
      ${s.sends ? `<span class="badge send">${icon("send")}发送</span>` : ""}
      ${s.expected_page ? `<span class="badge page" title="执行前核对页面">${icon("scan")}${escapeHtml(s.expected_page)}</span>` : ""}
      ${probs.length ? `<span class="badge warn" title="${escapeHtml(probs.join("\n"))}">${icon("alert")}${probs.length}</span>` : ""}
      ${mark ? `<span class="run-mark ${mark[1]}" title="${STATUS_TEXT[S.runMarks[s.id]]}">${icon(mark[0])}</span>` : ""}
      ${rec ? "" : `<span class="grip" title="拖动排序">${icon("grip")}</span>`}
    </li>`;
  };
  list.innerHTML = steps.map((s, i) => row(s, i, false)).join("")
    + S.recSteps.map((s, j) => row(s, steps.length + j, true)).join("");

  const isEmpty = steps.length + S.recSteps.length === 0;
  empty.hidden = !isEmpty;
  list.hidden = isEmpty;
  if (isEmpty) {
    empty.innerHTML = S.script ? `
      <div class="empty-ico">${icon("sparkles", "lg")}</div>
      <h3>还没有步骤</h3>
      <p>录一遍你在目标窗口里的操作就行，其余的会自动处理。</p>
      <div class="actions">
        <button class="btn primary" data-empty="record"><span class="rec-dot" style="background:currentColor"></span>开始录制</button>
        <button class="btn" data-empty="click">${icon("plus")}添加一次点击</button>
      </div>
      <ol class="steps-guide">
        <li><b>1</b><span>点「开始录制」或按 ${recKbd()}，到目标窗口里像平时一样操作一遍</span></li>
        <li><b>2</b><span>再按一下 ${recKbd()} 停止：「发送」那一步和页面校验会自动设好</span></li>
        <li><b>3</b><span>到「运行」里空跑，看每一步截图对不对</span></li>
        <li><b>4</b><span>没问题再执行</span></li>
      </ol>` : `
      <div class="empty-ico">${icon("layers", "lg")}</div>
      <h3>欢迎使用赛博打工人</h3>
      <p>点下面的按钮，再去点一下你要自动化的窗口（比如某个聊天窗口），然后像平时一样操作一遍，就录好了。</p>
      <div class="actions">
        <button class="btn primary" data-empty="quick">${icon("pointer")}点一下目标窗口，开始录制</button>
        <button class="btn" data-empty="tour">${icon("help")}看看新手引导</button>
      </div>`;
  }
  $("#select-all").checked = steps.length > 0 && S.checked.size === steps.length;
  $("#select-all").disabled = !steps.length;
}

function renderTotal() {
  const steps = S.script?.steps || [];
  const t = steps.reduce((sum, s) => sum + (s.action === "wait" ? s.seconds : s.delay_after ?? 0.5), 0);
  $("#total").innerHTML = `${icon("clock")}${t.toFixed(1)}s · ${steps.length} 步`;
}

function renderStatus() {
  const el = $("#status");
  el.className = "status-pill" + (S.recording ? " rec" : S.running ? " busy" : "");
  const hk = prettyKey(S.recHotkey);
  el.textContent = S.stopping ? "正在保存录制…" : S.recording ? (hk ? `录制中 · 按 ${hk} 停止` : "录制中")
    : S.running === "waiting" ? "等待开始" : S.running ? "执行中" : "空闲";
  el.title = S.hotkey ? `急停：${prettyKey(S.hotkey)}` : "";
  const btn = $("#btn-record");
  btn.classList.toggle("recording", S.recording);
  btn.disabled = !!S.stopping;  // 状态栏写着在保存，几秒就好
  btn.innerHTML = `<span class="rec-dot"></span><span>${S.stopping ? "保存中…" : S.recording ? "停止" : "录制"}</span>`
    + (hk ? `<kbd class="kbd-in">${escapeHtml(hk)}</kbd>` : "");
  const dk = prettyKey(S.dryHotkey);
  $("#btn-dry").title = `完整走一遍，但跳过「发送」${dk ? `（在任何窗口里按 ${dk} 也行）` : ""}`;
  $("#btn-run").title = "真的执行，会发送（Ctrl / ⌘ + Enter 按两次也行）";
  btn.title = S.recording
    ? `停止录制，保存录到的步骤（在任何窗口里按 ${hk} 也行）`
    : `录制你在目标窗口里的操作（在任何窗口里按 ${hk} 开始 / 停止）`;
}

// 执行期间，按钮区换成「在等什么 / 在干什么」+ 取消，不让用户对着灰按钮猜
function renderRunBusy() {
  $("#run-idle").hidden = !!S.running;
  const box = $("#run-busy");
  box.hidden = !S.running;
  box.classList.toggle("running", S.running === "running");
  $("#run-busy-bar").hidden = S.running !== "waiting";
  if (S.running === "waiting") {
    tickCountdown();
  } else if (S.running) {
    $("#run-busy-title").textContent = "正在执行…";
    $("#run-busy-sub").textContent = "动一下鼠标或键盘会立刻停下";
  }
}

// 倒计时：服务端推「还剩几秒」（你一动就回到满值），两次推送之间本地按时间往下走
const WAIT = { until: 0, total: 0, known: false, resetAt: 0, timer: null };
const RESET_HINT_MS = 1500; // 「重新计时」的提示至少留这么久，不然一闪就没了

function onRunWait(ev) {
  const now = performance.now();
  const shown = WAIT.known ? Math.max(0, (WAIT.until - now) / 1000) : null;
  // 比本地算出来的多了一截：说明你刚动过，重新计时
  if (shown !== null && ev.remaining > shown + 0.15) WAIT.resetAt = now;
  WAIT.until = now + ev.remaining * 1000;
  WAIT.total = ev.total || S.idleSec || ev.remaining;
  WAIT.known = true;
  startCountdown();
}

function startCountdown() {
  if (!WAIT.timer) WAIT.timer = setInterval(tickCountdown, 100);
  tickCountdown();
}

function stopCountdown() {
  clearInterval(WAIT.timer);
  Object.assign(WAIT, { timer: null, known: false, resetAt: 0 });
  $("#run-busy").classList.remove("reset");
}

function tickCountdown() {
  if (S.running !== "waiting") return stopCountdown();
  const fill = $("#run-busy-bar i");
  if (!WAIT.known) {
    // 还没收到第一次倒计时（或者根本不用等）
    $("#run-busy-title").textContent = "准备开始…";
    $("#run-busy-sub").textContent = `为了不跟你抢鼠标，键鼠停下 ${S.idleSec} 秒才会开始`;
    fill.style.width = "0%";
    return;
  }
  const now = performance.now();
  const rem = Math.max(0, (WAIT.until - now) / 1000);
  const reset = WAIT.resetAt && now - WAIT.resetAt < RESET_HINT_MS;
  $("#run-busy-title").textContent = rem > 0 ? `鼠标别动，${rem.toFixed(1)} 秒后自动开始` : "马上开始…";
  $("#run-busy-sub").textContent = reset
    ? "检测到你在动，重新计时"
    : `为了不跟你抢鼠标，键鼠停下 ${fmtSec(WAIT.total)} 秒才会开始`;
  $("#run-busy").classList.toggle("reset", !!reset);
  fill.style.width = `${WAIT.total ? Math.min(100, (1 - rem / WAIT.total) * 100) : 100}%`;
}

const fmtSec = (s) => (Number.isInteger(s) ? String(s) : s.toFixed(1));

function renderTabs() {
  $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === S.tab));
  ["step", "macro", "run"].forEach((t) => ($(`#pane-${t}`).hidden = S.tab !== t));
  $("#tab-run").innerHTML = `${icon("play")}运行${S.running ? `<span class="dot-live"></span>` : ""}`;
}

function pageOptions(current) {
  const names = Object.keys(S.detail?.pages || {});
  if (current && !names.includes(current)) names.push(current);
  return `<option value="">不校验</option>` + names.map((n) =>
    `<option ${n === current ? "selected" : ""}>${escapeHtml(n)}</option>`).join("");
}

function field(label, html, help) {
  return `<label>${label}</label><div class="row">${html}</div>${help ? `<span class="help">${help}</span>` : ""}`;
}

function buttonSelect(button) {
  return `<select class="input" data-f="button">${[["left", "左键"], ["right", "右键"], ["middle", "中键"]]
    .map(([v, t]) => `<option value="${v}" ${button === v ? "selected" : ""}>${t}</option>`).join("")}</select>`;
}

function modChips(mods = []) {
  return `<div class="seg" data-seg="mods">${["ctrl", "shift", "alt", "cmd"].map((m) =>
    `<button type="button" data-v="${m}" class="${mods.includes(m) ? "on" : ""}">${MOD_TEXT[m]}</button>`).join("")}</div>`;
}

function num(name, value, step = 1) {
  return `<input type="number" class="input" data-f="${name}" data-t="num" step="${step}" value="${value ?? ""}">`;
}

function toggle(name, checked, text) {
  return `<label class="toggle-row"><input type="checkbox" data-f="${name}" data-t="bool" ${checked ? "checked" : ""}> ${text}</label>`;
}

function renderStepPane() {
  const pane = $("#pane-step");
  const s = S.script?.steps[S.selected];
  if (!s) {
    pane.innerHTML = `<div class="empty" style="padding:16px">
      <div class="empty-ico">${icon("sliders", "lg")}</div>
      <h3>选中一个步骤来编辑</h3>
      <p>在中间列表里点一下；点击和滚动步骤也可以直接在画面上拖动编号标记。</p></div>`;
    return;
  }
  const t = TYPE[s.action];
  const probs = localProblems()[s.id] || [];
  let body = "";
  if (s.action === "click") {
    const mode = s.hold > 0 ? "hold" : s.count === 2 ? "2" : "1";
    body += field("位置", `x ${num("x", s.x, 0.1)} y ${num("y", s.y, 0.1)}`, "窗口内坐标（点）；也可以在画面上拖动标记，或点一下画面放到那里")
      + field("方式", `<div class="seg" data-seg="mode">
          <button type="button" data-v="1" class="${mode === "1" ? "on" : ""}">单击</button>
          <button type="button" data-v="2" class="${mode === "2" ? "on" : ""}">双击</button>
          <button type="button" data-v="hold" class="${mode === "hold" ? "on" : ""}">长按</button></div>
        ${mode === "hold" ? `${num("hold", s.hold, 0.1)}<span class="muted">秒</span>` : ""}`,
        mode === "hold" ? "按住不放的时长，比如按住说话" : "打开聊天、打开文件这类操作通常要双击")
      + locateField(s)
      + field("按键", buttonSelect(s.button))
      + field("同时按住", modChips(s.modifiers), "比如按住 Ctrl 多选")
      + field("发送", toggle("sends", s.sends, "这一下就是「发送」"), "空跑时会跳过这一步；必须指定所在页面");
  } else if (s.action === "hover") {
    body += field("位置", `x ${num("x", s.x, 0.1)} y ${num("y", s.y, 0.1)}`, "鼠标停在哪里；也可以在画面上拖动标记")
      + field("停多久", `${num("seconds", s.seconds, 0.1)}<span class="muted">秒</span>`,
        "停在这里等资料卡、菜单这类悬停才出现的东西弹出来")
      + locateField(s);
  } else if (s.action === "drag") {
    body += field("起点", `x ${num("x", s.x, 0.1)} y ${num("y", s.y, 0.1)}`, "画面上圆点是起点、方块是终点，都可以直接拖")
      + field("终点", `x ${num("x2", s.x2, 0.1)} y ${num("y2", s.y2, 0.1)}`)
      + field("用时", `${num("duration", s.duration, 0.1)}<span class="muted">秒</span>`)
      + field("按键", buttonSelect(s.button))
      + field("同时按住", modChips(s.modifiers));
  } else if (s.action === "type") {
    const vars = S.script.variables.length ? S.script.variables : ["评论内容"];
    body += field("内容", `<textarea class="input" data-f="text" rows="3" placeholder="直接写要输入的文字，或点下面插入变量">${escapeHtml(s.text)}</textarea>`)
      + `<div class="var-chips">${vars.map((v) =>
        `<button type="button" data-insert-var="${escapeHtml(v)}" title="插入变量：每次执行时再填内容">+ {${escapeHtml(v)}}</button>`).join("")}</div>`
      + `<span class="help">花括号里的是变量，执行时在「运行」里填，比如每次不同的评论</span>`
      + field("先清空", toggle("clear_first", s.clear_first !== false, "输入前先清空输入框"), "防止空跑或中断后留下的文字跟着一起发出去");
  } else if (s.action === "key") {
    body += field("组合键", `<input type="text" class="input" data-f="combo" value="${escapeHtml(s.combo)}">`, "如 enter、ctrl+v、cmd+shift+z、f5")
      + field("次数", num("times", s.times ?? 1), "按住不放会自动重复，录下来就是这里的次数")
      + field("发送", toggle("sends", s.sends, "这一下就是「发送」"));
  } else if (s.action === "scroll") {
    body += field("纵向", `${num("dy", s.dy)}<span class="muted">格，正数向下</span>`)
      + field("横向", `${num("dx", s.dx ?? 0)}<span class="muted">格，正数向右</span>`)
      + field("位置", `x ${num("x", s.x, 0.1)} y ${num("y", s.y, 0.1)}`, "在哪里滚动；留空则在当前位置");
  } else if (s.action === "wait") {
    body += field("秒数", num("seconds", s.seconds, 0.1));
  } else if (s.action === "wait_page") {
    const names = Object.keys(S.detail?.pages || {});
    if (s.page && !names.includes(s.page)) names.push(s.page);
    body += field("页面", `<select class="input" data-f="page">${names.length ? names.map((n) =>
        `<option ${n === s.page ? "selected" : ""}>${escapeHtml(n)}</option>`).join("") : `<option value="">还没有页面</option>`}</select>`,
        names.length ? "等到这个页面的锚点对上了才往下走" : "先在画面上「框选锚点」建一个页面")
      + field("最多等", `${num("timeout", s.timeout, 1)}<span class="muted">秒，超时就中止</span>`,
        "比固定延迟靠谱：电脑卡的时候多等一会儿，页面出来了马上继续");
  }
  const wins = Object.keys(S.script.windows || {});
  if (wins.length && s.action !== "wait") {  // 延迟不操作窗口
    body += field("窗口", `<select class="input" data-f="win">${[null, ...wins].map((k) =>
      `<option value="${k || ""}" ${(s.win || null) === k ? "selected" : ""}>${escapeHtml(windowLabel(k))}</option>`).join("")}</select>`,
      "执行时会先切到这个窗口；上一步才打开的窗口会等它出现");
  }
  if (s.action !== "wait" && s.action !== "wait_page") {
    body += field("所在页面", `<select class="input" data-f="expected_page">${pageOptions(s.expected_page)}</select>`,
      NEEDS_PAGE(s) ? "输入和发送必须指定：执行前核对锚点，对不上就中止" : "可选：指定后执行前会核对")
      + field("等待", `${num("delay_after", s.delay_after ?? 0.5, 0.1)}<span class="muted">秒后截图</span>`)
      + field("备注", `<input type="text" class="input" data-f="note" value="${escapeHtml(s.note || "")}" placeholder="可选" style="flex:1">`);
  }
  pane.innerHTML = `
    <div class="section-title"><span class="type-ico sm ${s.action}">${icon(t.icon)}</span>第 ${S.selected + 1} 步 · ${t.name}</div>
    ${probs.map((p) => `<div class="problem">${icon("alert")}<span>${escapeHtml(p)}</span></div>`).join("")}
    <div class="fields">${body}</div>`;
}

function renderMacroPane() {
  const pane = $("#pane-macro");
  const sc = S.script;
  if (!sc) {
    pane.innerHTML = `<p class="muted">还没有宏。</p>`;
    return;
  }
  const pages = S.detail?.pages || {};
  const anchors = Object.entries(pages).flatMap(([page, list]) => list.map((a, i) => `
    <div class="anchor-card">
      <div class="thumb"><img src="${a.url}" alt="${escapeHtml(page)}"></div>
      <div class="row"><b>${escapeHtml(page)}</b><span class="muted">#${i + 1}</span></div>
      <div class="row"><span class="muted">${a.w}×${a.h} @ (${a.x}, ${a.y})</span>
        <button class="icon-btn" data-del-anchor="${escapeHtml(page)}" data-index="${i}" title="删除锚点">${icon("trash")}</button></div>
    </div>`)).join("");
  pane.innerHTML = `
    <div class="section-title">${icon("page")}基本信息</div>
    <div class="fields">
      ${field("宏名", `<input type="text" class="input" data-s="name" value="${escapeHtml(sc.name)}">`)}
      ${field("程序", `<input type="text" class="input" data-s="app" value="${escapeHtml(sc.window.app)}">`)}
      ${field("窗口标题", `<input type="text" class="input" data-s="title" value="${escapeHtml(sc.window.title || "")}" placeholder="可选">`, "同一程序有多个窗口时用")}
      ${field("窗口尺寸", `${sc.window.width} × ${sc.window.height}<span class="muted">（缩放 ${sc.window.scale}）</span>`, "坐标都相对这个尺寸；要换尺寸请新建宏")}
      ${field("变量", `<input type="text" class="input" data-s="variables" value="${escapeHtml(sc.variables.join(", "))}" placeholder="comment_draft" style="flex:1">`, "逗号分隔；在文本里写 {变量名} 使用")}
    </div>
    <div class="section-title" style="margin-top:22px">${icon("scan")}页面锚点</div>
    ${anchors ? `<div class="anchors">${anchors}</div>`
      : `<div class="callout">${icon("info")}<span>还没有锚点。让目标窗口停在要校验的页面，点「截取当前窗口」，再点「框选锚点」。</span></div>`}`;
}

function renderVars() {
  const box = $("#run-vars");
  const vars = S.script?.variables || [];
  const saved = savedVars();
  box.innerHTML = vars.map((v) => field(`{${escapeHtml(v)}}`,
    `<input type="text" class="input" data-var="${escapeHtml(v)}" value="${escapeHtml(saved[v] || "")}" style="flex:1">`)).join("");
  box.hidden = !vars.length;
}

function savedVars() {
  try { return JSON.parse(localStorage.getItem(`cw.vars.${S.id}`) || "{}"); } catch { return {}; }
}

// ---------------- 事件 ----------------

// ---------------- 宏选择器：可搜索、按平台分组、草稿置顶、键盘可用 ----------------

const MP = { open: false, q: "", active: 0, items: [] };

// 同一个平台总是同一个颜色
function hueOf(text) {
  let h = 0;
  for (const ch of text || "") h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}

function avatar(platform, cls = "") {
  return `<span class="mp-avatar ${cls}" style="--h:${hueOf(platform)}">${escapeHtml((platform || "?")[0].toUpperCase())}</span>`;
}

function highlight(text, q) {
  const safe = escapeHtml(text);
  if (!q) return safe;
  const i = text.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return safe;
  return escapeHtml(text.slice(0, i)) + `<mark>${escapeHtml(text.slice(i, i + q.length))}</mark>` +
    escapeHtml(text.slice(i + q.length));
}

function renderPickerTrigger() {
  const cur = S.list.find((x) => x.id === S.id);
  const av = $("#mp-avatar");
  if (!cur) {
    av.className = "mp-avatar none";
    av.textContent = "+";
    av.style.removeProperty("--h");
    $("#mp-name").textContent = S.list.length ? "选择一个宏" : "还没有宏";
    $("#mp-sub").textContent = S.list.length ? "" : "点「新建」开始";
    $("#mp-badge").innerHTML = "";
    return;
  }
  av.className = "mp-avatar";
  av.style.setProperty("--h", hueOf(cur.platform));
  av.textContent = (cur.platform[0] || "?").toUpperCase();
  $("#mp-name").textContent = S.script?.name || cur.name;
  const w = S.script?.window;
  $("#mp-sub").textContent = w ? `${cur.platform} · ${w.app}${w.title ? " · " + w.title : ""}` : cur.platform;
  $("#mp-badge").innerHTML = cur.draft ? `<span class="badge warn">未保存</span>` : "";
}

function renderPickerList() {
  const q = MP.q.trim().toLowerCase();
  const hits = S.list.filter((x) => !q || x.name.toLowerCase().includes(q) || x.platform.toLowerCase().includes(q));
  const groups = [];
  const drafts = hits.filter((x) => x.draft);
  if (drafts.length) groups.push(["未保存", drafts]);
  const byPlatform = {};
  hits.filter((x) => !x.draft).forEach((x) => (byPlatform[x.platform] ||= []).push(x));
  Object.keys(byPlatform).sort().forEach((p) => groups.push([p, byPlatform[p]]));

  MP.items = groups.flatMap(([, xs]) => xs);
  MP.active = Math.min(MP.active, Math.max(0, MP.items.length - 1));
  let i = 0;
  $("#mp-list").innerHTML = MP.items.length ? groups.map(([title, xs]) => `
    <div class="mp-group"><span>${escapeHtml(title)}</span><span class="mp-group-r">${xs.length}${xs[0].draft ? ""
      : `<button type="button" class="btn sm mp-export" data-export="${escapeHtml(title)}" title="把这个平台的所有宏导出成一个 .cwpack 文件，别人导入后就能直接使用">${icon("download")}导出</button>`}</span></div>
    ${xs.map((x) => {
      const idx = i++;
      return `<button type="button" class="mp-item ${idx === MP.active ? "active" : ""}" data-mp="${idx}"
          role="option" aria-selected="${x.id === S.id}">
        ${avatar(x.platform, "sm")}
        <span class="t">${highlight(x.name, MP.q.trim())}</span>
        ${x.id === S.id ? `<span class="ok">${icon("check")}</span>` : ""}
      </button>`;
    }).join("")}`).join("")
    : `<div class="mp-empty">${q ? `没有找到「${escapeHtml(MP.q.trim())}」` : "还没有宏"}</div>`;
  $(`#mp-list [data-mp="${MP.active}"]`)?.scrollIntoView({ block: "nearest" });
}

function openPicker() {
  if (MP.open) return;
  MP.open = true;
  MP.q = "";
  MP.active = 0;
  $("#mp-q").value = "";
  $("#mp-pop").hidden = false;
  $("#mp-trigger").setAttribute("aria-expanded", "true");
  renderPickerList();
  MP.active = Math.max(0, MP.items.findIndex((x) => x.id === S.id));
  renderPickerList();
  $("#mp-q").focus();
}

function closePicker() {
  if (!MP.open) return;
  MP.open = false;
  $("#mp-pop").hidden = true;
  $("#mp-trigger").setAttribute("aria-expanded", "false");
}

async function choose(id) {
  closePicker();
  if (!id || id === S.id) return;
  if (S.dirty && !(await ensureSaved())) return;
  await attempt(() => open(id));
}

$("#mp-trigger").addEventListener("click", () => (MP.open ? closePicker() : openPicker()));
$("#mp-trigger").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    openPicker();
  }
});
$("#mp-q").addEventListener("input", (e) => {
  MP.q = e.target.value;
  MP.active = 0;
  renderPickerList();
});
$("#mp-q").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const n = MP.items.length;
    if (n) MP.active = (MP.active + (e.key === "ArrowDown" ? 1 : -1) + n) % n;
    renderPickerList();
  } else if (e.key === "Enter") {
    e.preventDefault();
    choose(MP.items[MP.active]?.id);
  } else if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();
    closePicker();
    $("#mp-trigger").focus();
  }
});
$("#mp-list").addEventListener("mousemove", (e) => {
  const b = e.target.closest("[data-mp]");
  if (b && +b.dataset.mp !== MP.active) {
    MP.active = +b.dataset.mp;
    $$("#mp-list .mp-item").forEach((x) => x.classList.toggle("active", +x.dataset.mp === MP.active));
  }
});
$("#mp-list").addEventListener("click", (e) => {
  const ex = e.target.closest("[data-export]");
  if (ex) return attempt(() => exportPack(ex.dataset.export));
  const b = e.target.closest("[data-mp]");
  if (b) choose(MP.items[+b.dataset.mp]?.id);
});
$("#mp-new").addEventListener("click", () => {
  closePicker();
  openNewDialog();
});

// ---------------- 平台包：一键导出 / 选文件或拖进页面导入，不弹确认 ----------------

async function exportPack(platform) {
  const res = await fetch(`/api/packs/${encodeURIComponent(platform)}`);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(await res.blob());
  a.download = `${platform}.cwpack`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast(`已导出 ${platform}.cwpack`);
  if (res.headers.get("X-Pack-Screenshots") === "1") {
    toast("包里含有录制时的窗口截图（可能有聊天内容），分享前请确认；不想带可以在设置 › 录制与执行里关掉");
  }
}

async function importPack(file) {
  closePicker();
  if (S.dirty && !(await ensureSaved())) return;
  const r = await attempt(() => api("POST", "/api/packs", file));
  if (!r) return;
  toast(`已导入 ${r.platform}：${r.ids.length} 个宏`);
  await attempt(() => loadList(r.ids[0]));
}

$("#mp-import").addEventListener("click", () => $("#pack-file").click());
$("#pack-file").addEventListener("change", (e) => {
  const file = e.target.files[0];
  e.target.value = "";  // 同一个文件可以再导一次
  if (file) importPack(file);
});

// 拖文件进页面（步骤列表里拖动排序不是文件，不受影响）
let dragDepth = 0;
const isFileDrag = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
document.addEventListener("dragenter", (e) => {
  if (!isFileDrag(e)) return;
  dragDepth++;
  $("#drop-overlay").hidden = false;
});
document.addEventListener("dragleave", (e) => {
  if (isFileDrag(e) && --dragDepth <= 0) {
    dragDepth = 0;
    $("#drop-overlay").hidden = true;
  }
});
document.addEventListener("dragover", (e) => {
  if (isFileDrag(e)) e.preventDefault();
});
document.addEventListener("drop", (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault();
  dragDepth = 0;
  $("#drop-overlay").hidden = true;
  const file = e.dataTransfer.files[0];
  if (file) importPack(file);
});
document.addEventListener("mousedown", (e) => {
  if (MP.open && !e.target.closest("#mp")) closePicker();
});
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && !Tour.els) {
    e.preventDefault();
    MP.open ? closePicker() : openPicker();
  }
});

$$("[data-add]").forEach((b) => b.addEventListener("click", () => addStep(b.dataset.add)));
$("#btn-undo").addEventListener("click", undo);
$("#btn-redo").addEventListener("click", redo);
$("#btn-delete").addEventListener("click", deleteSteps);
$("#btn-save").addEventListener("click", save);
$$(".tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));

$("#select-all").addEventListener("change", (e) => {
  S.checked = e.target.checked ? new Set(S.script.steps.map((_, i) => i)) : new Set();
  render();
});

$("#steps-empty").addEventListener("click", (e) => {
  const act = e.target.closest("[data-empty]")?.dataset.empty;
  if (act === "record") $("#btn-record").click();
  else if (act === "click") addStep("click");
  else if (act === "quick") quickStart();
  else if (act === "tour") startTour();
});

// 列表：选中 / 勾选 / 拖拽排序
const list = $("#step-list");
list.addEventListener("click", (e) => {
  const cb = e.target.closest("[data-check]");
  if (cb) {
    const i = +cb.dataset.check;
    cb.checked ? S.checked.add(i) : S.checked.delete(i);
    render();
    return;
  }
  const li = e.target.closest("li");
  if (li && !li.classList.contains("recording")) select(+li.dataset.i === S.selected ? -1 : +li.dataset.i);
});
let dragFrom = null;
list.addEventListener("dragstart", (e) => {
  dragFrom = +e.target.closest("li").dataset.i;
  e.dataTransfer.effectAllowed = "move";
});
list.addEventListener("dragover", (e) => {
  const li = e.target.closest("li");
  if (dragFrom === null || !li) return;
  e.preventDefault();
  $$("#step-list li.drag-over").forEach((x) => x.classList.remove("drag-over"));
  li.classList.add("drag-over");
});
list.addEventListener("drop", (e) => {
  const li = e.target.closest("li");
  if (dragFrom === null || !li) return;
  e.preventDefault();
  moveStep(dragFrom, +li.dataset.i);
  dragFrom = null;
});
list.addEventListener("dragend", () => {
  dragFrom = null;
  $$("#step-list li.drag-over").forEach((x) => x.classList.remove("drag-over"));
});

// 步骤参数：change 时提交一次（进历史）
$("#pane-step").addEventListener("change", (e) => {
  const el = e.target;
  if (!el.dataset.f) return;
  const name = el.dataset.f;
  let v = el.type === "checkbox" ? el.checked : el.value;
  if (el.dataset.t === "num") v = el.value === "" ? null : Number(el.value);
  if ((name === "expected_page" || name === "win") && v === "") v = null;
  mutate((sc) => {
    const step = sc.steps[S.selected];
    if (v === null && (name === "x" || name === "y") && step.action === "scroll") {
      step.x = step.y = null;
    } else if (name.startsWith("target.")) {
      if (step.target) step.target[name.slice(7)] = v;
    } else {
      step[name] = v;
    }
    // 等待页面换了页面：窗口跟着换成新页面锚点所在的窗口
    if (name === "page" && step.action === "wait_page") step.win = pageWindow(v, step.win);
    // 文本里用到的变量自动声明，不用再去「宏设置」里填
    if (name === "text") {
      step.typed_with_ime = false; // 用户亲手改过了，不再是输入法的按键
      for (const x of varsIn(v)) if (!sc.variables.includes(x)) sc.variables.push(x);
    }
  });
});
// 点按钮时输入框会先失焦，所以失焦前记下光标位置
$("#pane-step").addEventListener("focusout", (e) => {
  if (e.target.dataset.f !== "text") return;
  e.target.dataset.cursor = e.target.selectionStart;
  e.target.dataset.cursorEnd = e.target.selectionEnd;
});
$("#pane-step").addEventListener("click", (e) => {
  const mode = e.target.closest('[data-seg="mode"] button');
  if (mode) {
    mutate((sc) => {
      const st = sc.steps[S.selected];
      if (mode.dataset.v === "hold") Object.assign(st, { count: 1, hold: st.hold > 0 ? st.hold : 1 });
      else Object.assign(st, { count: +mode.dataset.v, hold: 0 });
    });
    return;
  }
  const loc = e.target.closest('[data-seg="locate"] button');
  if (loc && !loc.disabled) {
    mutate((sc) => (sc.steps[S.selected].use_target = loc.dataset.v === "image"));
    return;
  }
  if (e.target.closest("[data-reframe]")) {
    setAnchorMode(true, "target");
    return;
  }
  const mod = e.target.closest('[data-seg="mods"] button');
  if (mod) {
    mutate((sc) => {
      const st = sc.steps[S.selected];
      const set = new Set(st.modifiers || []);
      set.has(mod.dataset.v) ? set.delete(mod.dataset.v) : set.add(mod.dataset.v);
      st.modifiers = ["ctrl", "shift", "alt", "cmd"].filter((m) => set.has(m));
    });
    return;
  }
  const chip = e.target.closest("[data-insert-var]");
  if (!chip) return;
  const ta = $("#pane-step [data-f=text]");
  const token = `{${chip.dataset.insertVar}}`;
  // 没点进输入框就追加到末尾；点进去了就插在光标处
  const focused = document.activeElement === ta || ta.dataset.cursor != null;
  const at = focused ? +(ta.dataset.cursor ?? ta.selectionStart) : ta.value.length;
  const end = focused ? +(ta.dataset.cursorEnd ?? ta.selectionEnd) : at;
  ta.value = ta.value.slice(0, at) + token + ta.value.slice(end);
  ta.dispatchEvent(new Event("change", { bubbles: true }));
});

// 宏设置
$("#pane-macro").addEventListener("change", (e) => {
  const el = e.target;
  const name = el.dataset.s;
  if (!name) return;
  mutate((sc) => {
    if (name === "name") sc.name = el.value.trim();
    if (name === "app") sc.window.app = el.value.trim();
    if (name === "title") sc.window.title = el.value.trim() || null;
    if (name === "variables") sc.variables = el.value.split(/[,，\s]+/).map((x) => x.trim()).filter(Boolean);
  });
});
$("#pane-macro").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del-anchor]");
  if (del) {
    if (!(await ensureSaved())) return;
    const d = await attempt(() => api("DELETE", `/api/scripts/${S.id}/anchors/${del.dataset.delAnchor}/${del.dataset.index}`));
    if (d) applyDetail(d);
  }
});

// 删除和新建、保存放在一起；点两次确认
$("#btn-delete-macro").addEventListener("click", (e) => {
  const draft = !!S.detail?.draft;
  confirmClick(e.currentTarget, draft ? "再点一次，丢弃" : "再点一次，确认删除", async () => {
    await attempt(async () => {
      await api("DELETE", `/api/scripts/${S.id}/detail`);
      S.id = null;
      S.dirty = false;
      try { localStorage.removeItem("cw.script"); } catch {}
      await loadList();
      toast(draft ? "已丢弃" : "已删除");
    });
  });
});

$("#run-vars").addEventListener("change", (e) => {
  const v = savedVars();
  v[e.target.dataset.var] = e.target.value;
  try { localStorage.setItem(`cw.vars.${S.id}`, JSON.stringify(v)); } catch {}
  reportCurrent();
});

// 画面
$("#frame-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-frame]");
  if (!b || b.disabled) return;
  S.frame = b.dataset.frame;
  showFrame();
});
$("#win-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-win]");
  if (b) setWin(b.dataset.win);
});
$("#btn-capture").addEventListener("click", async () => {
  if (!(await ensureSaved())) return;
  const win = S.win;
  const d = await attempt(() => api("POST", `/api/scripts/${S.id}/capture${win ? `?win=${encodeURIComponent(win)}` : ""}`));
  if (d) {
    applyDetail(d);
    S.frame = "latest";
    await showFrame();
    toast("已截取当前窗口");
  }
});
$("#btn-anchor-mode").addEventListener("click", () => setAnchorMode(!S.anchorMode || S.regionFor !== "anchor"));
function setAnchorMode(on, purpose = "anchor") {
  S.anchorMode = on;
  S.regionFor = on ? purpose : "anchor";
  stage.setAnchorMode(on);
  $("#btn-anchor-mode").classList.toggle("primary", on && purpose === "anchor");
  $("#anchor-bar").hidden = !on;
  $("#anchor-form").hidden = true;
  $("#anchor-hint").textContent = purpose === "target"
    ? "在画面上框出这一步要点的东西（头像、按钮、文字），执行时会在窗口里找到它再点"
    : "在画面上拖出一块这个页面独有、不会变化的区域（按钮、标题），别选头像和正文";
  $("#page-names").innerHTML = Object.keys(S.detail?.pages || {}).map((n) => `<option value="${escapeHtml(n)}">`).join("");
  if (on && S.frame === "shot") {
    S.frame = frameOf("latest") ? "latest" : "reference";
    showFrame();
  }
}
$("#anchor-cancel").addEventListener("click", () => setAnchorMode(false));
$("#anchor-page").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#anchor-ok").click();
});
$("#anchor-ok").addEventListener("click", async () => {
  const page = $("#anchor-page").value.trim();
  if (!page) return toast("给页面起个名字", true);
  if (!(await ensureSaved())) return;
  const d = await attempt(() => api("POST", `/api/scripts/${S.id}/anchors`, { page, ...stage.region, ...frameParams() }));
  if (d) {
    applyDetail(d);
    setAnchorMode(false);
    $("#anchor-page").value = "";
    toast(`已添加锚点：${page}`);
    await autoAssignPage(page);
  }
});

// 「重新框选目标」框好了：直接存成这一步的目标图
async function saveTarget(r) {
  const step = S.script.steps[S.selected];
  if (!step || (step.action !== "click" && step.action !== "hover")) return setAnchorMode(false);
  if (!(await ensureSaved())) return;
  const d = await attempt(() => api("POST", `/api/scripts/${S.id}/targets`,
    { ...frameParams(), step_id: step.id, shot_of: frameParams().step_id, ...r }));
  setAnchorMode(false);
  if (d) {
    const keep = S.selected;
    applyDetail(d);
    S.selected = keep;
    render();
    toast(`第 ${keep + 1} 步改成按这块图找`);
  }
}

// 点击步骤的「定位」：按图找 / 固定坐标，目标图缩略图、最多等几秒、重新框选
function locateField(s) {
  const has = !!s.target;
  const on = has && s.use_target !== false;
  const url = S.detail?.targets?.[s.id];
  const seg = `<div class="seg" data-seg="locate">
      <button type="button" data-v="image" class="${on ? "on" : ""}" ${has ? "" : "disabled title=\"还没有目标图，先在画面上框一块\""}>按图找</button>
      <button type="button" data-v="fixed" class="${on ? "" : "on"}">固定坐标</button></div>`;
  const help = on ? "执行时先在窗口里找这块图，找到了点在它上面；界面变了（列表多了一条）也能点中，找不到就等，超时中止"
    : has ? "按录制时的坐标点，界面一变就可能点错" : "录制时这里认不出来（空白、纯色），所以按坐标点；也可以自己框一块目标";
  let html = field("定位", seg, help);
  if (on) {
    html += field("目标", `<span class="target-thumb">${url ? `<img src="${url}" alt="目标图">` : ""}</span>
        <button type="button" class="btn sm" data-reframe>${icon("scan")}重新框选</button>`)
      + field("最多等", `${num("target.timeout", s.target.timeout ?? 5, 1)}<span class="muted">秒，找不到就中止</span>`);
  } else if (!has) {
    html += field("", `<button type="button" class="btn sm" data-reframe>${icon("scan")}在画面上框一块目标</button>`);
  }
  return html;
}

function nextPageName() {
  const used = new Set(Object.keys(S.detail?.pages || {}));
  let n = 1;
  while (used.has(`页面${n}`)) n++;
  return `页面${n}`;
}

// 页面的锚点截自哪些窗口（null = 主窗口）：页面只能用在同一个窗口的步骤上
function pageWins(page) {
  return new Set((S.detail?.pages?.[page] || []).map((a) => a.win || null));
}

// 页面锚点所在的窗口；这个页面还没有锚点就用 fallback
function pageWindow(page, fallback = null) {
  const wins = [...pageWins(page)];
  return wins.length ? wins[0] : fallback;
}

// 同一窗口里的输入 / 发送步骤还没指定页面的，自动填上 page 并保存。别的窗口的步骤不填：
// 主窗口的锚点拿去核对聊天窗口，永远对不上
async function autoAssignPage(page) {
  const wins = pageWins(page);
  const idx = S.script.steps
    .map((s, i) => (NEEDS_PAGE(s) && !s.expected_page && wins.has(s.win || null) ? i : -1))
    .filter((i) => i >= 0);
  if (!idx.length) return;
  mutate((sc) => idx.forEach((i) => (sc.steps[i].expected_page = page)));
  if (await save()) toast(`已把「${page}」设为第 ${idx.map((i) => i + 1).join("、")} 步的所在页面`);
}

// 新建：点一下目标窗口（或从列表点一个）就直接创建，名字、标题、尺寸全部自动
const NF = { windows: [], picked: null };

async function openNewDialog() {
  NF.picked = null;
  ["#nf-name", "#nf-platform", "#nf-title", "#nf-width", "#nf-height"].forEach((s) => ($(s).value = ""));
  $("#nf-advanced").open = false;
  syncCreateButton();
  setPickCard(false);
  $("#new-dialog").showModal();
  await loadWindows();
}

async function loadWindows() {
  $("#win-list").innerHTML = `<div class="win-empty">正在读取窗口…</div>`;
  NF.windows = (await attempt(() => api("GET", "/api/windows"))) || [];
  renderWindows();
}

function renderWindows() {
  $("#win-list").innerHTML = NF.windows.length ? NF.windows.map((w, i) => `
    <button type="button" class="win-item ${NF.picked === i ? "on" : ""}" data-win="${i}" role="option" aria-selected="${NF.picked === i}">
      <span class="avatar">${escapeHtml((w.app[0] || "?").toUpperCase())}</span>
      <span><span class="t">${escapeHtml(w.title || w.app)}${w.minimized ? `<span class="tag">已最小化</span>` : ""}</span>
        <span class="s">${escapeHtml(w.app)} · ${w.width} × ${w.height}</span></span>
    </button>`).join("")
    : `<div class="win-empty">没有找到窗口。先把要操作的程序打开，再点「刷新」。</div>`;
}

// 自定义面板打开时，点列表只是选中（方便改名字再创建）；否则点了就建
function syncCreateButton() {
  const custom = $("#nf-advanced").open;
  $("#nf-create").hidden = !custom;
  $("#nf-create").disabled = NF.picked === null;
}

function fillAdvanced(w) {
  $("#nf-name").placeholder = w.suggested_name;
  $("#nf-platform").placeholder = w.platform;
  $("#nf-title").placeholder = w.match_title || "不需要";
  $("#nf-width").value = w.width;
  $("#nf-height").value = w.height;
}

async function createFromWindow(w, { record = false } = {}) {
  const custom = $("#new-dialog").open && $("#nf-advanced").open;
  const body = {
    name: (custom && $("#nf-name").value.trim()) || w.suggested_name,
    app: w.app,
    title: (custom && $("#nf-title").value.trim()) || w.match_title,
    width: (custom && +$("#nf-width").value) || w.width,
    height: (custom && +$("#nf-height").value) || w.height,
    platform: (custom && $("#nf-platform").value.trim()) || null,
  };
  const d = await attempt(() => api("POST", "/api/scripts", body));
  if (!d) return false;
  if ($("#new-dialog").open) $("#new-dialog").close();
  await loadList(d.id);
  if (record) {
    await startRecording();
  } else {
    toast(`已新建「${body.name}」（还没保存），点「录制」开始`);
  }
  return true;
}

// 等用户去点目标窗口。期间顶部一直有提示，点到 studio 自己不算
let picking = false;
async function pickByClick() {
  if (picking) return null;
  picking = true;
  const banner = document.createElement("div");
  banner.className = "pick-banner";
  banner.innerHTML = `${icon("pointer")}现在切到要操作的窗口，点一下它（20 秒内）`;
  document.body.append(banner);
  try {
    return await attempt(() => api("POST", "/api/windows/pick"));
  } finally {
    banner.remove();
    picking = false;
  }
}

function setPickCard(waiting) {
  $("#win-pick").classList.toggle("waiting", waiting);
  $("#win-pick-text").textContent = waiting ? "正在等你点击…" : "点一下目标窗口";
  $("#win-pick-sub").textContent = waiting
    ? "切到那个窗口（比如聊天窗口）点一下就行"
    : "点这里，然后切过去点一下那个窗口（比如聊天窗口）";
}

$("#btn-new").addEventListener("click", openNewDialog);
$("#win-refresh").addEventListener("click", loadWindows);
$("#nf-advanced").addEventListener("toggle", syncCreateButton);
$("#win-pick").addEventListener("click", async () => {
  setPickCard(true);
  const w = await pickByClick();
  setPickCard(false);
  if (!w) return;
  if ($("#nf-advanced").open) {
    // 自定义模式：把点到的窗口放进列表并选中，让用户改完再创建
    NF.windows = [w, ...NF.windows.filter((x) => !(x.app === w.app && x.title === w.title))];
    NF.picked = 0;
    fillAdvanced(w);
    renderWindows();
    syncCreateButton();
  } else {
    await createFromWindow(w);
  }
});
$("#win-list").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-win]");
  if (!b) return;
  const w = NF.windows[+b.dataset.win];
  if ($("#nf-advanced").open) {
    NF.picked = +b.dataset.win;
    fillAdvanced(w);
    renderWindows();
    syncCreateButton();
  } else {
    await createFromWindow(w);
  }
});
$("#new-form").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "ok") return;
  e.preventDefault();
  const w = NF.windows[NF.picked];
  if (w) await createFromWindow(w);
});

// 还没有宏时的一键流程：点窗口 → 建宏 → 开始录制
async function quickStart() {
  const w = await pickByClick();
  if (w) await createFromWindow(w, { record: true });
}

// 录制
$("#btn-record-menu").addEventListener("click", (e) => {
  e.stopPropagation();
  $("#record-menu").hidden = !$("#record-menu").hidden;
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#record-group")) $("#record-menu").hidden = true;
});

async function startRecording() {
  if (!(await ensureSaved())) return;
  // 先清空再发请求：录制线程推送的步骤可能比这个请求的响应先到
  S.recSteps = [];
  const ok = await attempt(() => api("POST", "/api/record/start",
    { script_id: S.id, record_delays: $("#opt-delays").checked, record_append: $("#opt-append").checked }));
  // 推送的「开始录制」事件可能比响应先到，那边已经提示过了就不再重复
  if (ok && !S.recording) {
    S.recording = true;
    render();
    toast(recStartHint());
  }
}

function recStartHint() {
  const hk = prettyKey(S.recHotkey);
  return hk ? `开始录制：到目标窗口里像平时一样操作，做完按 ${hk} 停止，不用切回来`
    : "开始录制：到目标窗口里像平时一样操作，做完回来点「停止」";
}

async function stopRecording() {
  S.stopping = true;  // 停止要等好几秒：先让人看到已经在停了
  renderStatus();
  const d = await attempt(() => api("POST", "/api/record/stop"));
  S.stopping = false;
  if (d) await handleRecordDone(d);
  else {
    S.recording = false;
    S.recSteps = [];
    render();
  }
}

async function handleRecordDone(d) {
  if (d.rec_id && S.seenRecIds.has(d.rec_id)) return; // 同一次录制的结果已经处理过
  if (d.rec_id) S.seenRecIds.add(d.rec_id);
  S.recording = false;
  S.stopping = false;
  S.recSteps = [];
  if (d.id !== S.id) return render(); // 录的是别的宏（比如切走了）：不覆盖当前画面
  // 替换掉了旧步骤：旧的放进撤销历史，Ctrl+Z 就回来
  if (d.replaced && S.script) {
    S.undo.push(JSON.stringify(S.script));
    S.redo = [];
  }
  applyDetail(d);
  // 告诉用户替他自动做了什么
  const done = [`录入 ${d.added} 步`];
  if (d.replaced) done.push(`替换了原来的 ${d.replaced} 步（Ctrl+Z 恢复）`);
  if (d.auto_targets) done.push(`${d.auto_targets} 次点击会按图找`);
  if (d.auto_send) {
    const i = S.script.steps.findIndex((s) => s.id === d.auto_send);
    done.push(`第 ${i + 1} 步标为「发送」`);
  }
  if (d.auto_anchor) {
    done.push(`自动生成页面「${d.auto_anchor.page}」并用于 ${d.auto_anchor.steps.length} 步`);
    S.win = d.last_win || null; // 录制结束时所在的窗口，自动选的锚点多半就在这里
    S.frame = frameOf("latest") ? "latest" : "reference";
    await showFrame();
  } else {
    const pages = Object.keys(S.detail.pages || {});
    if (pages.length === 1) await autoAssignPage(pages[0]);
    await showFrame(); // 参考图可能换成了这一遍录的
  }
  toast(done.join(" · "));
  const imeAt = S.script.steps.findIndex((s) => s.typed_with_ime);
  if (imeAt >= 0) {
    select(imeAt);
    toast(`第 ${imeAt + 1} 步是用中文输入法打的，只录到了按键，请在右边填上实际要输入的文字`, true);
    setTimeout(() => $("#pane-step [data-f=text]")?.select(), 50);
  }
  if (S.detail.draft) toast("录好了。空跑看看没问题，再点「保存」存进宏库");
  if (!localProblems() || !Object.keys(localProblems()).length) {
    toast("可以去「运行」里先空跑一遍看看");
  }
}

$("#btn-record").addEventListener("click", () =>
  S.recording ? stopRecording() : S.script ? startRecording() : quickStart());

// 执行
$("#btn-dry").addEventListener("click", () => run(true));
$("#btn-run").addEventListener("click", (e) => confirmClick(e.currentTarget, "确认执行（会真的发送）", () => run(false)));

let runStarting = false; // 写回、排队这一小段时间里再按一次（快捷键连按两下）不重复跑
async function run(dryRun) {
  if (runStarting || S.running) return;
  runStarting = true;
  try {
    if (!(await ensureSaved())) return;
    const variables = {};
    $$("[data-var]").forEach((el) => (variables[el.dataset.var] = el.value));
    const missing = S.script.variables.filter((v) => !variables[v]);
    if (missing.length) {
      setTab("run");
      return toast(`先填变量：${missing.join(", ")}`, true);
    }
    await attempt(() => api("POST", `/api/scripts/${S.id}/run`, { dry_run: dryRun, variables }));
  } finally {
    runStarting = false;
  }
}

$("#btn-panic").addEventListener("click", async () => {
  const r = await attempt(() => api("POST", "/api/panic"));
  if (r) toast(`已急停，取消了 ${r.cancelled_queued} 个排队任务`);
});

$("#run-log").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-url]");
  if (!li) return;
  S.shot = { url: li.dataset.url, win: S.script.steps.find((s) => s.id === li.dataset.step)?.win || null };
  S.win = S.shot.win;
  S.frame = "shot";
  showFrame();
  const i = S.script.steps.findIndex((s) => s.id === li.dataset.step);
  if (i >= 0) {
    S.selected = i;
    renderSteps();
    syncStage();
  }
});

// 快捷键
document.addEventListener("keydown", (e) => {
  if (Tour.els) return;
  const typing = e.target.matches("input, textarea, select");
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); save(); return; }
  if (mod && e.key === "Enter") {
    // 执行会真的发送：和点按钮一样，第一下只是准备，4 秒内再按一下才执行。没有全局快捷键
    e.preventDefault();
    const b = $("#btn-run");
    if (b.disabled) return;
    b.click();
    if (b.dataset.armed) toast(`再按一次 ${IS_MAC ? "⌘" : "Ctrl"}+Enter 确认执行（会真的发送）`);
    return;
  }
  if (typing || $("#new-dialog").open) return;
  if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); }
  else if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); }
  else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSteps(); }
  else if (e.key === "ArrowDown" && S.script) { e.preventDefault(); select(Math.min(S.selected + 1, S.script.steps.length - 1)); }
  else if (e.key === "ArrowUp" && S.script) { e.preventDefault(); select(Math.max(S.selected - 1, 0)); }
  else if (e.key === "Escape") { if (S.anchorMode) setAnchorMode(false); else select(-1); }
});

window.addEventListener("beforeunload", (e) => {
  if (S.dirty) e.preventDefault();
});
// 关掉页面时撤回「有没保存的修改」，不然空跑快捷键会一直交给这个已经不在的页面
window.addEventListener("pagehide", () => {
  if (!S.dirty) return;
  const body = new Blob([JSON.stringify(currentBody(false))], { type: "application/json" });
  navigator.sendBeacon("/api/current", body);
});

// 执行中定期跟服务端对一次状态：万一「完成」事件在断线时丢了，按钮也不会一直灰着
let runWatch = null;
function watchRun() {
  clearInterval(runWatch);
  runWatch = setInterval(syncStatus, 3000);
}
async function syncStatus() {
  const st = await api("GET", "/api/status").catch(() => null);
  if (!st) return;
  S.hotkey = st.panic_hotkey;
  S.recHotkey = st.record_hotkey;
  S.dryHotkey = st.dry_run_hotkey;
  const wasRecording = S.recording;
  S.recording = !!st.recording;
  S.stopping = !!st.stopping;
  if (S.running && !st.busy) S.running = false;
  if (!S.running) clearInterval(runWatch);
  if (wasRecording !== S.recording || !S.running) render();
}

$("#btn-cancel-run").addEventListener("click", async () => {
  const r = await attempt(() => api("POST", "/api/panic"));
  if (r) toast("已取消");
  syncStatus();
});

// 实时事件
connectEvents((ev) => {
  if (ev.type === "record") {
    S.recSteps = ev.steps;
    renderSteps();
    syncStage();
  } else if (ev.type === "record_state") {
    const started = ev.recording && !S.recording;
    S.recording = ev.recording;
    S.stopping = !!ev.stopping;  // 按了快捷键停止：在保存录到的步骤
    if (started) {
      S.recSteps = [];
      if (ev.script_id === S.id) toast(recStartHint());
    }
    render();
  } else if (ev.type === "record_done") {
    handleRecordDone(ev);
  } else if (ev.type === "toast") {
    toast(ev.text, !!ev.error);
  } else if (ev.type === "hotkeys") {
    applyHotkeys(ev.items);
  } else if (ev.type === "settings") {
    Settings.applyRemote(ev);
  } else if (ev.type === "run_wait" && ev.script_id === S.id) {
    if (S.running === "waiting") onRunWait(ev);
  } else if (ev.type === "dry_run_request" && ev.page === PAGE_ID && ev.script_id === S.id) {
    run(true); // 按了空跑快捷键、这里有没保存的修改：和点「空跑」一样，先写回再跑
  } else if (ev.type === "run_state" && ev.script_id === S.id) {
    if (ev.state === "queued") {
      S.running = "waiting";
      S.idleSec = ev.idle_sec ?? 2;
      stopCountdown();
      S.runMarks = {};
      S.tab = "run";
      $("#run-log").innerHTML = "";
      watchRun();
    } else if (ev.state === "running") {
      S.running = "running";
    } else {
      S.running = false;
      const good = ["ok", "dry_run_stopped"].includes(ev.status);
      const text = `${STATUS_TEXT[ev.status] || ev.status}${ev.reason ? "：" + ev.reason : ""}`;
      toast(text, !good);
      $("#run-log").insertAdjacentHTML("beforeend",
        `<li class="summary-row"><span class="st ${good ? "ok" : "failed"}">${escapeHtml(text)}</span></li>`);
    }
    render();
  } else if (ev.type === "run_entry" && ev.script_id === S.id) {
    const en = ev.entry;
    S.runMarks[en.step_id] = en.status;
    $("#run-log .summary-row.muted")?.remove();
    const ico = { ok: "check", skipped: "skip", aborted: "x", failed: "x", cancelled: "stop" }[en.status] || "info";
    $("#run-log").insertAdjacentHTML("beforeend",
      `<li ${en.screenshot_url ? `data-url="${en.screenshot_url}"` : ""} data-step="${escapeHtml(en.step_id)}" title="${en.screenshot_url ? "点击查看截图" : ""}">
        <span class="st ${en.status}">${icon(ico)}</span>
        <span class="st ${en.status}">${STATUS_TEXT[en.status] || en.status}</span>
        <span class="detail">${escapeHtml(en.step_id)} · ${escapeHtml(en.action)}${en.detail ? " · " + escapeHtml(en.detail) : ""}</span>
        <span class="muted">${en.page_score != null ? `页面 ${en.page_score.toFixed(2)}` : ""}</span>
      </li>`);
    renderSteps();
  }
}, onReconnect);

// 连上（包括 studio 重启后自动重连）：断线期间的事件都丢了，跟服务端对一次，屏幕上的宏以文件为准
async function onReconnect() {
  await syncStatus();
  if (!S.id || S.dirty || S.recording) return;  // 有没保存的修改就不覆盖
  const d = await api("GET", `/api/scripts/${S.id}/detail`).catch(() => null);
  if (d && !S.dirty) applyDetail(d);
}

// 快捷键在设置里改了（这个页面或别的页面）：按钮、状态栏、引导里的提示跟着换
function applyHotkeys(items) {
  const by = Object.fromEntries(items.map((it) => [it.name, it.combo]));
  S.recHotkey = by.record ?? S.recHotkey;
  S.dryHotkey = by.dry_run ?? S.dryHotkey;
  S.hotkey = by.panic ?? S.hotkey;
  render();
}
document.addEventListener("hotkeys", (e) => applyHotkeys(e.detail.items));

// 「录制延迟」「追加到末尾」：▾ 菜单里和设置里是同一个开关，改了记进配置
document.addEventListener("settings", (e) => {
  $("#opt-delays").checked = e.detail.record_delays;
  $("#opt-append").checked = !!e.detail.record_append;
});
$("#opt-delays").addEventListener("change", (e) => Settings.update({ record_delays: e.target.checked }));
$("#opt-append").addEventListener("change", (e) => Settings.update({ record_append: e.target.checked }));

// ---------------- 新手引导 ----------------

function startTour() {
  const rec = recKbd();
  Tour.start([
    { el: "#macro-picker", title: "宏在这里切换、新建、保存、删除",
      body: "一个宏 = 在某个程序窗口里的一串操作。新建时去点一下那个窗口就行，程序、名字、尺寸全部自动识别。新建的宏先是草稿（标着「未保存」），点「保存」才存进宏库。下拉里每个平台旁边的「导出」能把这个平台的整套宏打包成一个 .cwpack 文件，别人把文件拖进页面就导入好了。" },
    { el: "#record-group", title: "最快的方式：录制",
      body: `点「录制」或在任何窗口里按 ${rec}，到目标窗口里像平时一样操作，做完再按一下同一个快捷键就停止，不用切回来。录完会自动标出「发送」那一步、自动生成页面锚点，大多数时候直接就能空跑。` },
    { el: "#palette", title: "也可以手动添加",
      body: "点一下就插在当前选中的步骤后面。点击、输入、按键、滚动、拖动、延迟，还有「等待页面」：等某个页面出来再往下走，比固定延迟靠谱。" },
    { el: "#steps-card", title: "步骤列表",
      body: "点一行编辑，拖动排序，勾选后批量删除；Ctrl/⌘+Z 撤销。黄色 ⚠ 表示这一步还缺东西，鼠标放上去能看到原因。" },
    { el: "#canvas-area", title: "画面：坐标和锚点",
      body: "编号标记就是点击位置，可以直接拖。蓝色虚线框是页面锚点（录制时自动选的），程序靠它认出「现在在哪个页面」；选中一个点击步骤时，橙色框是它的目标图：执行时按图找到它再点，界面变了也不怕。宏用到好几个窗口（比如点开会话弹出的聊天窗口）时，每个窗口各有自己的参考图：选中哪一步就显示它所在的窗口，上方也能手动切换。",
      before: () => setTab("step") },
    { el: "#pane-step", title: "关键两项：所在页面、发送",
      body: "输入和发送步骤执行前会核对「所在页面」，对不上就停下，宁可不发也不发错；框选锚点后会自动填上。录制时最后那一下点击或回车会被自动标成「发送」，不对可以在这里改。",
      before: () => { if (S.script?.steps.length && S.selected < 0) select(0); setTab("step"); } },
    { el: "#pane-run", title: "先空跑，再执行",
      body: `空跑会完整走一遍但跳过「发送」，每一步都有截图可点开看；录完在目标窗口里直接按 <kbd>${escapeHtml(prettyKey(S.dryHotkey) || "Alt+T")}</kbd> 就能空跑，不用切回来。确认没问题再点执行（要点两次确认，也可以按两次 Ctrl+Enter）。执行会真的发送，所以没有全局快捷键。`,
      before: () => setTab("run") },
    { el: "#btn-panic", title: "随时可以停",
      body: `点「急停」或在任何窗口里按 <kbd>${escapeHtml(prettyKey(S.hotkey) || "急停热键")}</kbd> 立刻中止。执行时你一动键鼠，它也会自己停下来。` },
    { el: "#btn-settings", title: "设置都在齿轮里",
      body: `深色 / 浅色外观、快捷键、录制与执行的细节都在这里。录制开关现在是 ${rec}，想换就在「快捷键」里直接按下新的组合键；跟其他软件或系统冲突会当场提示。` },
    { el: "#nav-audit", title: "事后回看",
      body: "每一步都留了截图和记录；审计页把最可疑的排在最前面，按 1 / 2 标记就行。" },
  ], "cw.tour.editor", () => setTab(S.selected >= 0 ? "step" : S.tab));
}

// 引导、空状态里显示的录制快捷键
function recKbd() {
  return `<kbd>${escapeHtml(prettyKey(S.recHotkey) || "Alt+R")}</kbd>`;
}
$("#btn-tour").addEventListener("click", startTour);

(async () => {
  const st = await attempt(() => api("GET", "/api/status"));
  if (st) {
    S.hotkey = st.panic_hotkey;
    S.recHotkey = st.record_hotkey;
    S.dryHotkey = st.dry_run_hotkey;
    S.recording = !!st.recording;
    S.stopping = !!st.stopping;
    // 配置的快捷键被别的软件占了、自动换成了备选：告诉用户
    (st.hotkey_notes || []).forEach((n) => toast(n));
  }
  await attempt(() => loadList());
  if (!Tour.seen("cw.tour.editor")) startTour();
})();
