// 设置：齿轮 ⚙ 打开。左边分类（通用 / 快捷键 / 录制与执行 / 关于），右边设置项；改了立即生效并写进 config.toml。
// 页面加载时跟服务端对一次（服务端的配置为准），并在 document 上派发 "settings" 事件（detail 是全部设置）。
// 快捷键改了派发 "hotkeys" 事件（detail 是最新的快捷键列表）。
"use strict";

const Settings = (() => {
  // KeyboardEvent.code → 后端用的键名（按物理键认：macOS 上 ⌥R 打出来是 ®，code 仍是 KeyR）
  const CODE_NAMES = {
    Space: "space", Enter: "enter", Tab: "tab", Backspace: "backspace", Delete: "delete", Insert: "insert",
    Home: "home", End: "end", PageUp: "page_up", PageDown: "page_down", Pause: "pause", ScrollLock: "scroll_lock",
    ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
    Period: ".", Comma: ",", Minus: "-", Equal: "=", Slash: "/", Semicolon: ";", Backquote: "`",
  };
  const MOD_KEYS = new Set(["Control", "Shift", "Alt", "Meta", "AltGraph", "OS"]);
  const STATUS_ICON = { ok: "check", unknown: "info", taken: "alert", reserved: "alert", duplicate: "alert", invalid: "alert" };
  const SECTIONS = [
    ["general", "sliders", "通用"],
    ["hotkeys", "keyboard", "快捷键"],
    ["run", "play", "录制与执行"],
    ["about", "info", "关于"],
  ];
  const OLD_SERVER = "studio 后台是旧版本，请关掉终端里的 studio 重新运行";

  let data = null; // 服务端的设置
  let dlg = null;
  let section = "general";
  const hk = { items: [], msgs: {}, capturing: null };

  // ---------------- 同步 ----------------

  async function load() {
    const r = await api("GET", "/api/settings").catch(() => null);
    if (r) apply(r);
  }

  function apply(r) {
    data = r;
    if (Theme.pref !== r.theme) Theme.set(r.theme);
    document.dispatchEvent(new CustomEvent("settings", { detail: r }));
    if (dlg?.open) render();
  }

  async function update(patch) {
    try {
      apply(await api("PATCH", "/api/settings", patch));
      flashSaved();
    } catch (e) {
      toast(e.message, true);
    }
  }

  // ---------------- 弹窗 ----------------

  function build() {
    dlg = document.createElement("dialog");
    dlg.id = "settings-dialog";
    dlg.className = "settings";
    dlg.innerHTML = `
      <div class="set-shell">
        <nav class="set-nav" aria-label="设置">
          <div class="set-nav-title">设置</div>
          ${SECTIONS.map(([id, ic, name]) =>
            `<button type="button" data-sec="${id}">${icon(ic)}<span>${name}</span></button>`).join("")}
        </nav>
        <section class="set-main">
          <header class="set-head">
            <h3 id="set-title"></h3>
            <span class="set-saved" id="set-saved">${icon("check")}已保存</span>
            <span class="grow"></span>
            <button type="button" class="icon-btn" id="set-close" title="关闭 (Esc)">${icon("x")}</button>
          </header>
          <div class="set-body" id="set-body"></div>
        </section>
      </div>`;
    document.body.appendChild(dlg);
    dlg.querySelector("#set-close").addEventListener("click", () => dlg.close());
    dlg.addEventListener("click", (e) => {
      if (e.target === dlg) dlg.close(); // 点遮罩关闭
      const sec = e.target.closest("[data-sec]");
      if (sec) show(sec.dataset.sec);
    });
    dlg.addEventListener("close", stopCapture);
    dlg.addEventListener("cancel", (e) => {
      if (hk.capturing) e.preventDefault(); // 录键时 Esc 只取消录键，不关窗口
    });
    const body = dlg.querySelector("#set-body");
    body.addEventListener("click", onBodyClick);
    body.addEventListener("change", onBodyChange);
    // 捕获阶段拦下按键：录键时 Ctrl+S、Delete 之类不能再触发页面自己的快捷键
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("keyup", (e) => {
      if (!hk.capturing) return;
      e.preventDefault(); // 单按 Alt 松开时 Windows 会把焦点移到浏览器菜单
      e.stopImmediatePropagation();
      if (MOD_KEYS.has(e.key)) preview(mods(e));
    }, true);
  }

  async function open(sec = "general") {
    if (!dlg) build();
    if (!data) await load();
    if (!data) return toast(OLD_SERVER, true);
    await loadHotkeys();
    section = sec;
    render();
    if (!dlg.open) dlg.showModal();
  }

  function show(sec) {
    stopCapture();
    section = sec;
    render();
  }

  function render() {
    dlg.querySelectorAll("[data-sec]").forEach((b) => b.classList.toggle("on", b.dataset.sec === section));
    dlg.querySelector("#set-title").textContent = SECTIONS.find((x) => x[0] === section)[2];
    dlg.querySelector("#set-body").innerHTML = { general, hotkeys, run, about }[section]();
  }

  function flashSaved() {
    const el = dlg?.querySelector("#set-saved");
    if (!el) return;
    el.classList.remove("show");
    void el.offsetWidth; // 重新触发动画
    el.classList.add("show");
  }

  const row = (title, desc, control, cls = "") => `
    <div class="set-row ${cls}">
      <div class="set-label"><b>${title}</b>${desc ? `<small>${desc}</small>` : ""}</div>
      <div class="set-ctl">${control}</div>
    </div>`;

  const sw = (key) => `<label class="switch"><input type="checkbox" data-set="${key}" ${data[key] ? "checked" : ""}><span></span></label>`;

  const secs = (key, min, max, step) => `<span class="num-unit"><input type="number" class="input" data-set="${key}"
    min="${min}" max="${max}" step="${step}" value="${data[key]}"><span class="muted">秒</span></span>`;

  // ---------------- 通用 ----------------

  function general() {
    const themes = [["dark", "深色"], ["light", "浅色"], ["system", "跟随系统"]];
    return `
      <div class="set-group">
        <div class="set-label"><b>外观</b><small>选「跟随系统」会随系统的深色 / 浅色模式自动切换</small></div>
        <div class="theme-cards" role="radiogroup" aria-label="外观">
          ${themes.map(([v, name]) => `
            <button type="button" class="theme-card ${data.theme === v ? "on" : ""}" data-theme-pref="${v}" role="radio"
                aria-checked="${data.theme === v}">
              <span class="tc-preview ${v}"><i></i><i></i><i></i></span>
              <span class="tc-name">${data.theme === v ? icon("check") : ""}${name}</span>
            </button>`).join("")}
        </div>
      </div>`;
  }

  // ---------------- 快捷键 ----------------

  async function loadHotkeys() {
    const r = await api("GET", "/api/hotkeys").catch(() => null);
    if (r) hk.items = r.items;
  }

  const LOCAL_KEYS = [
    ["Ctrl / ⌘ + K", "打开宏选择器"],
    ["Ctrl / ⌘ + S", "保存"],
    ["Ctrl / ⌘ + Z", "撤销"],
    ["Ctrl / ⌘ + Shift + Z", "重做"],
    ["Delete", "删除勾选（或选中）的步骤"],
    ["↑ / ↓", "上下切换步骤"],
    ["Esc", "退出框选 / 取消选中"],
    ["J / K", "审计页：上下切换记录"],
    ["1 / 2 / 0", "审计页：没问题 / 有问题 / 重置"],
  ];

  function hotkeys() {
    const rows = hk.items.map((it) => {
      const cap = hk.capturing === it.name;
      const m = hk.msgs[it.name] || (it.note ? { status: "unknown", text: it.note } : null);
      return `
        <div class="set-row hk-row">
          <div class="set-label"><b>${escapeHtml(it.label)}</b><small>在任何窗口里都能按</small></div>
          <div class="set-ctl hk-ctl">
            <button type="button" class="hk-field${cap ? " capturing" : ""}${m?.bad ? " bad" : ""}" id="hk-${it.name}"
                data-name="${it.name}" title="点一下，然后按下新的组合键">
              <span class="hk-keys">${cap ? `<span class="muted" data-preview>请按下组合键…（Esc 取消）</span>` : kbds(it.combo)}</span>
              ${it.combo !== it.default && !cap ? `<span class="hk-default" title="默认：${escapeHtml(prettyKey(it.default))}">已改</span>` : ""}
            </button>
            ${m ? `<div class="hk-msg ${m.status}">${icon(STATUS_ICON[m.status] || "info")}<span>${escapeHtml(m.text)}</span></div>` : ""}
          </div>
        </div>`;
    }).join("");
    const changed = hk.items.some((it) => it.combo !== it.default);
    return `
      <div class="set-group">
        <div class="set-group-head"><b>全局快捷键</b>
          <button type="button" class="btn ghost sm" id="hk-reset" ${changed ? "" : "disabled"}>${icon("refresh")}恢复默认</button></div>
        <p class="set-note">点一下输入框，直接按下想要的组合键；能用就立即生效。跟其他软件或系统冲突会当场提示，不会改。</p>
        ${rows}
      </div>
      <div class="set-group">
        <div class="set-group-head"><b>页面内快捷键</b></div>
        <dl class="key-list">${LOCAL_KEYS.map(([k, what]) => `<dt><kbd>${escapeHtml(k)}</kbd></dt><dd>${what}</dd>`).join("")}</dl>
      </div>`;
  }

  function kbds(combo) {
    return combo ? prettyKey(combo).split(IS_MAC ? "" : "+").map((k) => `<kbd>${escapeHtml(k)}</kbd>`).join("") : "";
  }

  function keyName(e) {
    const c = e.code || "";
    if (/^Key[A-Z]$/.test(c)) return c.slice(3).toLowerCase();
    if (/^Digit\d$/.test(c)) return c.slice(5);
    if (/^F\d{1,2}$/.test(c)) return c.toLowerCase();
    return CODE_NAMES[c] || null;
  }

  function mods(e) {
    return [e.ctrlKey && "ctrl", e.shiftKey && "shift", e.altKey && "alt", e.metaKey && "cmd"].filter(Boolean);
  }

  function preview(m) {
    const el = dlg.querySelector("[data-preview]");
    if (el) el.textContent = m.length ? `${prettyKey(m.join("+"))}${IS_MAC ? "" : "+"}…` : "请按下组合键…（Esc 取消）";
  }

  async function startCapture(name) {
    hk.capturing = name;
    delete hk.msgs[name];
    render();
    dlg.querySelector(`#hk-${name}`)?.focus();
    // 先放开全局快捷键：不然按下现在正在用的键会被系统拦走，浏览器收不到
    await api("POST", "/api/hotkeys/suspend").catch(() => {});
  }

  function stopCapture() {
    if (!hk.capturing) return;
    hk.capturing = null;
    api("POST", "/api/hotkeys/resume").catch(() => {});
    if (dlg.open) render();
  }

  function onKey(e) {
    if (!hk.capturing || !dlg?.open) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    if (MOD_KEYS.has(e.key)) return preview(mods(e));
    if (e.code === "Escape" && !mods(e).length) return stopCapture();
    const name = hk.capturing;
    const key = keyName(e);
    stopCapture();
    if (!key) {
      hk.msgs[name] = { status: "invalid", bad: true, text: "这个键不能做全局快捷键，换一个" };
      return render();
    }
    tryHotkey(name, [...mods(e), key].join("+"));
  }

  // 检查一下：能用就立即生效；有冲突只提示，不改
  async function tryHotkey(name, combo) {
    const r = await api("POST", "/api/hotkeys/check", { name, combo }).catch((e) => (toast(e.message, true), null));
    if (!r) return;
    const current = hk.items.find((it) => it.name === name)?.combo;
    if (r.blocking) {
      hk.msgs[name] = { status: r.status, bad: true,
        text: `${prettyKey(r.combo)}：${r.message}。没有改动，仍是 ${prettyKey(current)}` };
      return render();
    }
    if (r.combo === current) return render();
    await saveHotkeys({ [name]: r.combo }, name, r.status === "unknown" ? r.message : null);
  }

  async function saveHotkeys(body, name, hint) {
    try {
      const r = await api("PUT", "/api/hotkeys", body);
      hk.items = r.items;
      hk.msgs = {};
      if (name) hk.msgs[name] = { status: "ok", text: hint || "已生效" };
      announce(r);
      flashSaved();
    } catch (e) {
      toast(e.message, true);
    }
    render();
  }

  function announce(r) {
    document.dispatchEvent(new CustomEvent("hotkeys", { detail: r }));
  }

  // ---------------- 录制与执行 ----------------

  function run() {
    return `
      <div class="set-group">
        <div class="set-group-head"><b>录制</b></div>
        ${row("录制延迟", "把操作之间的间隔录成单独的延迟行，回放节奏和你操作时一样。录制按钮旁的 ▾ 里也能切换", sw("record_delays"))}
        ${row("追加到末尾", "重新录制时接在已有步骤后面。关着时会替换掉旧步骤，录完按 Ctrl+Z 能恢复。录制按钮旁的 ▾ 里也能切换", sw("record_append"))}
      </div>
      <div class="set-group">
        <div class="set-group-head"><b>分享</b></div>
        ${row("导出时带上截图", "导出平台包时带上录制时的窗口截图（参考图、每一步的画面），别人导入后能看到每一步在哪操作。截图里可能有聊天内容，不想分享就关掉；执行不受影响", sw("pack_screenshots"))}
      </div>
      <div class="set-group">
        <div class="set-group-head"><b>执行</b></div>
        ${row("开始前等你停手", "在 studio 里点空跑 / 执行后，等键盘鼠标停下这么久才开始，免得跟你抢鼠标", secs("run_idle_sec", 0, 60, 0.5))}
        ${row("你在用电脑时暂停", "无人值守执行时，检测到你在用键盘鼠标就先停下，等你离开再继续", sw("pause_on_user_input"))}
        ${row("空闲多久后继续", "暂停后，键盘鼠标停下这么久才继续执行", secs("resume_after_idle_sec", 0, 3600, 5),
          data.pause_on_user_input ? "" : "disabled")}
      </div>`;
  }

  // ---------------- 关于 ----------------

  function about() {
    const path = (p) => p ? `<code class="path">${escapeHtml(p)}</code>` : `<span class="muted">没有（只改内存）</span>`;
    return `
      <div class="set-group about-brand">
        <span class="about-logo" aria-hidden="true">🤖</span>
        <b class="about-name">赛博打工人 <span class="brand-tag">Studio</span></b>
        <span class="about-version">版本 ${escapeHtml(data.version)}</span>
        <p class="about-slogan">你的数字分身，自动化一切</p>
      </div>
      <div class="set-group">
        ${row("配置文件", "这里的设置都保存在这个文件里", path(data.config_path))}
        ${row("数据目录", "宏、截图、审计记录都在这里", path(data.user_data))}
        ${row("新手引导", "看过的引导不会再自动出现，可以在这里重新打开",
          `<button type="button" class="btn sm" id="tour-again">${icon("help")}重新看引导</button>`)}
      </div>`;
  }

  // ---------------- 交互 ----------------

  function onBodyClick(e) {
    const theme = e.target.closest("[data-theme-pref]");
    if (theme) {
      Theme.set(theme.dataset.themePref); // 先换上，不等服务端
      return update({ theme: theme.dataset.themePref });
    }
    const field = e.target.closest(".hk-field");
    if (field) return startCapture(field.dataset.name);
    if (e.target.closest("#hk-reset")) {
      return saveHotkeys(Object.fromEntries(hk.items.map((it) => [it.name, it.default])));
    }
    if (e.target.closest("#tour-again")) {
      try {
        Object.keys(localStorage).filter((k) => k.startsWith("cw.tour.")).forEach((k) => localStorage.removeItem(k));
      } catch {}
      dlg.close();
      $("#btn-tour")?.click();
    }
  }

  function onBodyChange(e) {
    const key = e.target.dataset.set;
    if (!key) return;
    if (e.target.type === "checkbox") return update({ [key]: e.target.checked });
    const v = Number(e.target.value);
    const min = Number(e.target.min), max = Number(e.target.max);
    if (e.target.value === "" || Number.isNaN(v) || v < min || v > max) {
      toast(`请填 ${min}–${max} 之间的数`, true);
      e.target.value = data[key];
      return;
    }
    update({ [key]: v });
  }

  return {
    open, update, announce, load,
    applyRemote: (ev) => { const { type, ...rest } = ev; apply(rest); },
    get data() { return data; },
  };
})();

document.addEventListener("DOMContentLoaded", () => {
  document.querySelector("#btn-settings")?.addEventListener("click", () => Settings.open());
  Settings.load();
});
