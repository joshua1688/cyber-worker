// 宏设计页和审计页共用：图标、主题、请求封装、提示、WebSocket。
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ---------------- 图标（24×24 线性图标，stroke 由 CSS 控制） ----------------

const ICON_PATHS = {
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  keyboard: '<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>',
  pointer: '<path d="M5 3l6.5 17 2.4-7.1L21 10.5z"/>',
  scroll: '<path d="M12 3v18M8 7l4-4 4 4M8 17l4 4 4-4"/>',
  mouse: '<rect x="6" y="3" width="12" height="18" rx="6"/><path d="M12 7v4"/>',
  type: '<path d="M4 7V5h16v2M9 19h6M12 5v14"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  undo: '<path d="M9 14L4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
  redo: '<path d="M15 14l5-5-5-5"/><path d="M20 9H9a5 5 0 0 0 0 10h3"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  check: '<path d="M5 12l5 5L20 7"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  skip: '<path d="M5 5l8 7-8 7zM19 5v14"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01"/>',
  camera: '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
  scan: '<path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4"/><rect x="9" y="9" width="6" height="6" rx="1"/>',
  play: '<path d="M7 4l13 8-13 8z"/>',
  flask: '<path d="M9 3h6M10 3v6L4.5 18.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3"/><path d="M7 15h10"/>',
  alert: '<path d="M12 3L2 20h20z"/><path d="M12 10v4M12 17h.01"/>',
  grip: '<path d="M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01" stroke-width="3"/>',
  chevron: '<path d="M6 9l6 6 6-6"/>',
  octagon: '<path d="M8 2h8l6 6v8l-6 6H8l-6-6V8z"/><path d="M12 8v5M12 16h.01"/>',
  layers: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>',
  audit: '<path d="M3 6l2 2 3-3M3 13l2 2 3-3M11 7h10M11 14h10M11 20h10"/>',
  sliders: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  send: '<path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/>',
  page: '<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>',
  sparkles: '<path d="M12 3l1.8 4.6L18 9l-4.2 1.4L12 15l-1.8-4.6L6 9l4.2-1.4zM19 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
  braces: '<path d="M8 4c-2 0-3 1-3 3v2c0 1-1 3-2 3 1 0 2 2 2 3v2c0 2 1 3 3 3M16 4c2 0 3 1 3 3v2c0 1 1 3 2 3-1 0-2 2-2 3v2c0 2-1 3-3 3"/>',
  loop: '<path d="M17 2l4 4-4 4M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 0 1-3 3H3"/>',
  window: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
  move: '<path d="M5 9l-3 3 3 3M9 5l3-3 3 3M15 19l-3 3-3-3M19 9l3 3-3 3M2 12h20M12 2v20"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  logo: '<path d="M7 8l-3 4 3 4M17 8l3 4-3 4M14 5l-4 14"/>',
  download: '<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>',
  upload: '<path d="M12 21V9M7 14l5-5 5 5M5 3h14"/>',
};

function icon(name, cls = "") {
  return `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICON_PATHS[name] || ""}</svg>`;
}

// 静态 HTML 里写 <i data-icon="xxx"></i>，页面加载时替换成 SVG
function hydrateIcons(root = document) {
  $$("i[data-icon]", root).forEach((el) => {
    el.outerHTML = icon(el.dataset.icon, el.className);
  });
}

// ---------------- 主题 ----------------
// 偏好：dark（默认）/ light / system。<head> 里的内联脚本已在首帧前设好 data-theme，这里负责切换和跟随系统。

const Theme = {
  pref: "dark",
  apply() {
    const dark = this.pref === "system" ? matchMedia("(prefers-color-scheme: dark)").matches : this.pref !== "light";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.dispatchEvent(new CustomEvent("themechange"));
  },
  set(pref) {
    this.pref = pref;
    try { localStorage.setItem("cw.theme", pref); } catch {}
    this.apply();
  },
  init() {
    try { this.pref = localStorage.getItem("cw.theme") || "dark"; } catch {}
    this.apply();
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (this.pref === "system") this.apply();
    });
  },
};

// ---------------- 请求 / 提示 ----------------

// body 是 File / Blob 时原样上传（导入平台包），否则按 JSON 发
async function api(method, url, body) {
  const raw = body instanceof Blob;
  const res = await fetch(url, {
    method,
    headers: body === undefined ? {} : { "Content-Type": raw ? "application/octet-stream" : "application/json" },
    body: body === undefined || raw ? body : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.detail ?? res.statusText;
    if (Array.isArray(msg)) msg = msg.map((e) => `${(e.loc || []).join(".")}: ${e.msg}`).join("；");
    // 前端是新的、后台还是升级前的旧进程（没重启）：说人话
    if (res.status === 404 && msg === "Not Found") msg = "studio 后台是旧版本，请关掉终端里的 studio 重新运行";
    throw new Error(msg);
  }
  return data;
}

function toast(msg, isError = false) {
  let box = $(".toasts");
  if (!box) {
    box = document.createElement("div");
    box.className = "toasts";
    box.setAttribute("role", "status");
    document.body.append(box);
  }
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.innerHTML = icon(isError ? "alert" : "check") + `<span>${escapeHtml(msg)}</span>`;
  box.append(el);
  while (box.children.length > 4) box.firstChild.remove();
  setTimeout(() => el.remove(), isError ? 6000 : 2600);
}

// 出错统一弹提示，返回 undefined
async function attempt(fn) {
  try {
    return await fn();
  } catch (e) {
    toast(e.message || String(e), true);
  }
}

// onOpen：每次（重新）连上时调用。断线期间的事件会丢，调用方应借此跟服务端对一次状态
function connectEvents(onEvent, onOpen) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let retry = 500;
  const open = () => {
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onmessage = (m) => onEvent(JSON.parse(m.data));
    ws.onopen = () => {
      retry = 500;
      onOpen?.();
    };
    ws.onclose = () => setTimeout(open, (retry = Math.min(retry * 2, 8000)));
  };
  open();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// 两次点击确认（不用 confirm() 弹窗）
function confirmClick(btn, text, action) {
  if (btn.dataset.armed) {
    delete btn.dataset.armed;
    btn.classList.remove("armed");
    btn.innerHTML = btn.dataset.label;
    action();
    return;
  }
  btn.dataset.armed = "1";
  btn.dataset.label = btn.innerHTML;
  btn.classList.add("armed");
  btn.innerHTML = escapeHtml(text);
  setTimeout(() => {
    if (btn.dataset.armed) {
      delete btn.dataset.armed;
      btn.classList.remove("armed");
      btn.innerHTML = btn.dataset.label;
    }
  }, 4000);
}

// 快捷键显示：Windows 写 Ctrl+Alt+R，macOS 用 ⌃⌥⇧⌘ 符号
const IS_MAC = /Mac/i.test(navigator.platform || navigator.userAgent);
function prettyKey(combo) {
  if (!combo) return "";
  const mac = { ctrl: "⌃", alt: "⌥", shift: "⇧", cmd: "⌘" };
  const win = { ctrl: "Ctrl", alt: "Alt", shift: "Shift", cmd: "Win" };
  const parts = combo.toLowerCase().split("+").map((k) =>
    (IS_MAC ? mac : win)[k] || (k.length === 1 ? k.toUpperCase() : k[0].toUpperCase() + k.slice(1)));
  return parts.join(IS_MAC ? "" : "+");
}

const STATUS_TEXT = {
  ok: "成功", aborted: "中止", failed: "失败", skipped: "空跑跳过", cancelled: "急停",
  dry_run_stopped: "空跑完成",
};

document.addEventListener("DOMContentLoaded", () => {
  hydrateIcons();
  Theme.init();
});
