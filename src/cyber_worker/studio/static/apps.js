// 应用页：没带 ?app 是应用总览（每个应用一张卡片），带了 ?app=qq 是这个应用的首页（它的流程一张张卡片）。
// 新建应用、录一个新流程都交给流程设计页去做（/?new=1[&app=qq]：点一下目标窗口 → 新建 → 开始录制），这里只展示和跳转
"use strict";

const P = { apps: [], show: "all", q: "" };

const editUrl = (id, extra = "") => `/?flow=${encodeURIComponent(id)}${extra}`;
const appParam = () => new URLSearchParams(location.search).get("app");

async function load() {
  P.apps = await api("GET", "/api/apps");
  render();
}

function render() {
  const key = appParam();
  const app = P.apps.find((a) => a.platform === key);
  if (key && !app) {
    history.replaceState(null, "", "/apps");
    toast("这个应用已经不在了", true);
  }
  document.title = `${app ? app.title : "应用"} · 赛博打工人`;
  $("#apps-main").innerHTML = `<div class="apps-inner">${app ? appHtml(app) : overviewHtml()}</div>`;
  hydrateIcons($("#apps-main"));
}

// 最近一次运行：时间（今天只写几点几分）、结果
function when(at) {
  const today = new Date().toLocaleDateString("sv-SE");
  return at.slice(0, 10) === today ? at.slice(11, 16) : `${at.slice(5, 10)} ${at.slice(11, 16)}`;
}
function lastHtml(m) {
  const r = m.last;
  if (!r) return `<span class="run-state none">还没跑过</span>`;
  const what = r.bad ? "失败" : r.dry_run ? "空跑通过" : "成功";
  return `<span class="run-state ${r.bad ? "bad" : "ok"}">● ${when(r.at)} ${what}</span>`;
}
// 失败的那一步：「第 2 步：没找到要点的目标」
function failHtml(m) {
  const r = m.last;
  if (!r?.bad) return "";
  const no = /^s(\d+)$/.exec(r.step_id || "");
  const why = (r.detail || "").replace(/^[A-Za-z_][\w.]*(Error|Exception)?:\s*/, "").replace(/^s\d+ /, "").split(/[（(]/)[0];
  return `<a class="flow-fail" href="${editUrl(m.id, r.step_id ? `&step=${encodeURIComponent(r.step_id)}` : "")}">
    ${no ? `第 ${+no[1]} 步：` : ""}${escapeHtml(why || "没做成")} · 去看看 →</a>`;
}

// 带放大镜的搜索框
function findBox(id, placeholder, value) {
  return `<label class="find-box">${icon("search")}<input id="${id}" placeholder="${placeholder}" value="${escapeHtml(value)}"
    autocomplete="off" aria-label="${placeholder.replace("…", "")}"></label>`;
}

// ---------------- 应用总览 ----------------

// 搜索和排序一直在（应用多起来时页面不会突然多出一行）；排序默认「最近用过」，记在本机
const SORTS = [["recent", "最近用过"], ["name", "名字"]];
try { P.sort = localStorage.getItem("cw.apps.sort") || "recent"; } catch { P.sort = "recent"; }
P.appQ = "";

function overviewHtml() {
  const q = P.appQ.trim().toLowerCase();
  const apps = P.apps
    .filter((a) => !q || a.title.toLowerCase().includes(q) || a.flows.some((m) => m.name.toLowerCase().includes(q)))
    .sort(P.sort === "name" ? (x, y) => x.title.localeCompare(y.title, "zh")
      : (x, y) => (y.last_at || "").localeCompare(x.last_at || "") || x.title.localeCompare(y.title, "zh"));
  const total = P.apps.reduce((n, a) => n + a.flows.length, 0);
  const cards = apps.map((a) => {
    const t = a.today;
    return `<a class="app-card" href="/apps?app=${encodeURIComponent(a.platform)}">
      <span class="app-card-head">${avatar(a.platform, "lg", a.title)}<span><b>${escapeHtml(a.title)}</b>
        <small>${a.flows.length ? `${a.flows.length} 个流程` : "还没有流程"}</small></span></span>
      <span class="app-card-flows">${a.flows.length ? escapeHtml(a.flows.map((m) => m.name).join(" · "))
        : `点进去录第一个流程`}</span>
      <span class="app-card-foot">${!t.runs ? `<span class="muted">今天还没运行</span>`
        : `<span class="muted">今天 ${t.runs} 次</span>${t.bad ? `<span class="bad-text">${t.bad} 次失败</span>` : `<span class="ok-text">全部成功</span>`}`}</span>
    </a>`;
  }).join("");
  return `
    <div class="apps-head">
      <div><h1>应用</h1><p class="muted">${P.apps.length ? `${P.apps.length} 个应用 · ${total} 个流程`
        : "一个应用就是一个软件或网站（QQ、微信、B站…），里面放它的各种流程。点下面的「新建应用」开始"}</p></div>
      <span class="grow"></span>
      <button class="btn" data-act="import">${icon("upload")}导入应用包</button>
    </div>
    ${P.apps.length ? `<div class="apps-tools">
      ${findBox("apps-find", "搜索应用或流程…", P.appQ)}
      <span class="grow"></span>
      <span class="muted small">排序</span>
      <div class="seg" role="radiogroup" aria-label="排序">${SORTS.map(([k, label]) =>
        `<button type="button" data-sort="${k}" class="${P.sort === k ? "on" : ""}">${label}</button>`).join("")}</div>
    </div>` : ""}
    <div id="apps-results">
    <div class="app-grid">
      ${cards}
      <a class="app-card app-new" href="/?new=1" title="点一下要自动化的程序窗口，名字和尺寸自动识别，接着开始录第一个流程">
        <span class="app-new-ico">${icon("plus")}</span>
        <b>新建应用</b>
      </a>
    </div>
    ${q && !apps.length ? `<p class="muted center">没有找到「${escapeHtml(P.appQ)}」</p>` : ""}
    </div>
    <p class="muted small center">也可以把 .cwpack 文件直接拖进这个页面</p>`;
}

// ---------------- 应用首页 ----------------

const FILTERS = [["all", "全部"], ["bad", "失败"], ["new", "没跑过"]];
const keep = { all: () => true, bad: (m) => m.last?.bad, new: (m) => !m.last };

function appHtml(a) {
  const q = P.q.trim().toLowerCase();
  const shown = a.flows.filter(keep[P.show])
    .filter((m) => !q || m.name.toLowerCase().includes(q) || m.description.toLowerCase().includes(q));
  const t = a.today;
  const cards = shown.map((m) => `
    <article class="flow-card ${m.last?.bad ? "bad" : ""}" data-open="${editUrl(m.id)}">
      <div class="flow-main">
        <span class="flow-title">
          <a class="flow-name" href="${editUrl(m.id)}">${escapeHtml(m.name)}</a>
          <button type="button" class="pencil" data-edit="name" data-id="${escapeHtml(m.id)}" title="改名" aria-label="给「${escapeHtml(m.name)}」改名">${icon("pencil")}</button>
          <span class="grow"></span>${lastHtml(m)}
        </span>
        <span class="flow-desc-row">${m.description
          ? `<span class="flow-desc" title="${escapeHtml(m.description)}">${escapeHtml(m.description)}</span>
             <button type="button" class="pencil" data-edit="description" data-id="${escapeHtml(m.id)}" title="改说明" aria-label="编辑「${escapeHtml(m.name)}」的说明">${icon("pencil")}</button>`
          : `<button type="button" class="add-desc" data-edit="description" data-id="${escapeHtml(m.id)}">＋ 加一句说明</button>`}</span>
        <span class="flow-meta">${m.steps} 步 · 约 ${Math.round(m.seconds)} 秒</span>
      </div>
      ${failHtml(m)}
      <span class="flow-actions">
        <a class="btn sm" href="${editUrl(m.id)}">${icon("sliders")}编辑</a>
        <a class="btn sm" href="${editUrl(m.id, "&dry=1")}" title="打开这个流程，完整走一遍但不发送">${icon("flask")}空跑</a>
      </span>
    </article>`).join("");
  return `
    <nav class="crumbs" aria-label="位置"><a href="/apps">应用</a><span>›</span><b>${escapeHtml(a.title)}</b></nav>
    <div class="apps-head">
      ${avatar(a.platform, "lg", a.title)}
      <div><h1>${escapeHtml(a.title)}</h1><p class="muted">${a.flows.length} 个流程 · ${
        t.runs ? `今天运行 ${t.runs} 次${t.bad ? `，${t.bad} 次失败` : "，全部成功"}` : "今天还没运行"}</p></div>
      <span class="grow"></span>
      <a class="btn primary" href="/?new=1&app=${encodeURIComponent(a.platform)}"><span class="rec-dot"></span>录一个新流程</a>
      <span class="more-group">
        <button class="btn" data-act="more" aria-label="更多操作" aria-haspopup="menu" title="更多操作">${icon("more")}</button>
        <span class="menu app-menu" role="menu" hidden>
          ${a.flows.length ? `<button type="button" class="ctx-item" data-act="export" role="menuitem" title="把这个应用的全部流程打成一个 .cwpack 文件，发给别人拖进页面就能用">${
            icon("download")}<span>导出应用包</span></button>
          <div class="menu-sep"></div>` : ""}
          <button type="button" class="ctx-item danger" data-act="delete-app" role="menuitem">${icon("trash")}<span>删除这个应用</span></button>
        </span>
      </span>
    </div>
    ${a.flows.length ? `
    <div class="apps-tools">
      <div class="seg" role="tablist" aria-label="筛选">${FILTERS.map(([k, label]) => {
        const n = a.flows.filter(keep[k]).length;
        return `<button type="button" data-show="${k}" class="${P.show === k ? "on" : ""}">${label} ${n}</button>`;
      }).join("")}</div>
      <span class="grow"></span>
      ${findBox("apps-q", "搜索流程…", P.q)}
    </div>
    <div id="apps-results">${shown.length ? `<div class="flow-grid">${cards}</div>` : `<p class="muted">没有符合条件的流程</p>`}</div>`
    : `<div class="empty"><div class="empty-ico">${icon("sparkles", "lg")}</div><h3>还没有流程</h3>
       <p>点右上角「录一个新流程」，再去 ${escapeHtml(a.title)} 里像平时一样操作一遍就行。</p></div>`}`;
}

// ---------------- 交互 ----------------

$("#apps-main").addEventListener("click", async (e) => {
  const pencil = e.target.closest("[data-edit]");
  if (pencil) {
    startInlineEdit(pencil);
    return;
  }
  // 点卡片上别的地方（不是按钮、链接、输入框）：进编辑器
  const card = e.target.closest("[data-open]");
  if (card && !e.target.closest("a, button, input")) {
    location.href = card.dataset.open;
    return;
  }
  const sort = e.target.closest("[data-sort]");
  if (sort) {
    P.sort = sort.dataset.sort;
    try { localStorage.setItem("cw.apps.sort", P.sort); } catch {}
    render();
    return;
  }
  const show = e.target.closest("[data-show]");
  if (show) {
    P.show = show.dataset.show;
    render();
    return;
  }
  const actEl = e.target.closest("[data-act]");
  const act = actEl?.dataset.act;
  if (act === "import") $("#pack-file").click();
  else if (act === "export") {
    $(".app-menu").hidden = true;
    await attempt(() => exportPack(appParam()));
  }
  else if (act === "more") $(".app-menu").hidden = !$(".app-menu").hidden;
  else if (act === "delete-app") deleteApp(actEl);
});
// 搜索：只换下面的结果，搜索框本身不动（换掉它的话，中文输入法正在打的拼音就断了，中文根本打不进去）；
// 打拼音的过程中不筛，选好字再筛
function search(input) {
  if (input.id === "apps-q") P.q = input.value;
  else P.appQ = input.value;
  const app = P.apps.find((a) => a.platform === appParam());
  const box = document.createElement("div");
  box.innerHTML = app ? appHtml(app) : overviewHtml();
  $("#apps-results").innerHTML = box.querySelector("#apps-results").innerHTML;
  hydrateIcons($("#apps-results"));
}
$("#apps-main").addEventListener("input", (e) => {
  if ((e.target.id === "apps-q" || e.target.id === "apps-find") && !e.isComposing) search(e.target);
});
$("#apps-main").addEventListener("compositionend", (e) => {
  if (e.target.id === "apps-q" || e.target.id === "apps-find") search(e.target);
});

// 卡片上原地改名 / 写说明：Enter 或点别处保存，Esc 取消；名字空着不改
function startInlineEdit(pencil) {
  const field = pencil.dataset.edit;
  const id = pencil.dataset.id;
  const m = P.apps.flatMap((a) => a.flows).find((x) => x.id === id);
  if (!m) return;
  // 没有说明时点的是「＋ 加一句说明」本身：就地换成输入框
  const shown = pencil.parentElement.querySelector(field === "name" ? ".flow-name" : ".flow-desc") || pencil;
  const input = document.createElement("input");
  input.className = `input inline-edit ${field}`;
  input.value = m[field];
  input.placeholder = field === "name" ? "流程名" : "一句话说它是做什么的，比如：在群里发一条消息";
  shown.replaceWith(input);
  pencil.hidden = true;
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    if (!save || value === m[field] || (field === "name" && !value)) return render();
    const r = await attempt(() => api("PATCH", `/api/flows/${id}/meta`, { [field]: value }));
    if (r) {
      Object.assign(m, r);
      if (field === "name" && r.name !== value) toast(`同一个应用里已经有「${value}」了，改成了「${r.name}」`);
    }
    render();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    else if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}

// 点菜单外面就关掉。用点击那一刻的路径判断：第一次点「删除」时按钮的内容会被换成「确认删除…」，
// 被点中的那个元素已经不在页面上了，用 e.target.closest 会以为点在了外面、把菜单关掉，第二下就点不到了
document.addEventListener("click", (e) => {
  if (!e.composedPath().some((el) => el.classList?.contains("more-group"))) $(".app-menu")?.setAttribute("hidden", "");
});

// 删除整个应用：点两次才删（第一次按钮变红写明删什么），删了挪进回收站，回到总览时能撤销
function deleteApp(btn) {
  const a = P.apps.find((x) => x.platform === appParam());
  if (!a) return;
  confirmClick(btn, `确认删除 ${a.title} 和它的 ${a.flows.length} 个流程`, async () => {
    const r = await attempt(() => api("DELETE", `/api/apps/${encodeURIComponent(a.platform)}`));
    if (r) location.href = `/apps?undo=${encodeURIComponent(r.trash)}&name=${encodeURIComponent(a.title)}`;
  });
}

// 刚删了一个应用：提示一下，能撤销（不会自己消失太快：12 秒）
function undoNotice(trash, name) {
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `${icon("trash")}<span>已删除「${escapeHtml(name)}」</span><button type="button" class="btn sm">撤销</button>`;
  el.querySelector("button").addEventListener("click", async () => {
    const r = await attempt(() => api("POST", "/api/apps/restore", { trash }));
    el.remove();
    if (!r) return;
    toast(`「${name}」放回来了`);
    history.replaceState(null, "", `/apps?app=${encodeURIComponent(r.platform)}`);
    await attempt(load);
  });
  toastBox().append(el);
  setTimeout(() => el.remove(), 12000);
}

async function importPack(file) {
  const r = await attempt(() => api("POST", "/api/packs", file));
  if (!r) return;
  toast(`已导入 ${r.platform}：${r.ids.length} 个流程`);
  history.replaceState(null, "", `/apps?app=${encodeURIComponent(r.platform)}`);
  await attempt(load);
}
$("#pack-file").addEventListener("change", (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (file) importPack(file);
});
// 把 .cwpack 拖进页面就导入
let dragDepth = 0;
const hasFile = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
document.addEventListener("dragenter", (e) => {
  if (!hasFile(e)) return;
  dragDepth++;
  $("#drop-overlay").hidden = false;
});
document.addEventListener("dragleave", (e) => {
  if (hasFile(e) && --dragDepth <= 0) $("#drop-overlay").hidden = true;
});
document.addEventListener("dragover", (e) => hasFile(e) && e.preventDefault());
document.addEventListener("drop", (e) => {
  if (!hasFile(e)) return;
  e.preventDefault();
  dragDepth = 0;
  $("#drop-overlay").hidden = true;
  const file = e.dataTransfer.files[0];
  if (file) importPack(file);
});

// 有新的运行结果（编辑页在跑流程）：卡片上的「最近一次」跟着更新
connectEvents((ev) => {
  if (ev.type === "settings") Settings.applyRemote(ev);
  if (ev.type === "run_state" && ev.state === "done") attempt(load);
});

function startTour() {
  Tour.start([
    { el: "#apps-main", title: "应用和流程",
      body: "一个应用就是一个软件或网站，比如 QQ。点进一个应用，能看到它的各种流程（刷动态、群聊天…）：每个流程是做什么的、最近一次跑得怎么样，点卡片就进去编辑。" },
    { el: ".app-new", title: "新建应用",
      body: "点这张卡片，再去点一下要自动化的程序窗口，就会建好应用并开始录第一个流程。进了某个应用以后，右上角是「录一个新流程」，导出、删除应用在它旁边的 ⋯ 里。" },
  ], "cw.tour.apps");
}
$("#btn-tour").addEventListener("click", startTour);

(async () => {
  const q = new URLSearchParams(location.search);
  if (q.get("undo")) {
    undoNotice(q.get("undo"), q.get("name") || "应用");
    history.replaceState(null, "", "/apps");
  }
  await attempt(load);
  if (!Tour.seen("cw.tour.apps")) startTour();
})();
