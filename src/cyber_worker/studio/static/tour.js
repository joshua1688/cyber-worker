// 新手引导：高亮页面上的某个区域，旁边弹出说明，一步步往下走。
// 首次打开自动开始；顶栏「?」随时重看。
"use strict";

const Tour = {
  steps: [],
  i: 0,
  key: null,
  els: null,
  onEnd: null,

  // steps: [{ el: "#selector", title, body, before?: () => void }]
  start(steps, key, onEnd) {
    this.stop(false);
    this.steps = steps.filter((s) => !s.when || s.when());
    this.key = key;
    this.onEnd = onEnd;
    this.i = 0;
    const block = document.createElement("div");
    block.className = "tour-block";
    const spot = document.createElement("div");
    spot.className = "tour-spot";
    const pop = document.createElement("div");
    pop.className = "tour-pop";
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-live", "polite");
    document.body.append(block, spot, pop);
    this.els = { block, spot, pop };
    block.addEventListener("click", () => this.next());
    pop.addEventListener("click", (e) => {
      const act = e.target.closest("[data-tour]")?.dataset.tour;
      if (act === "next") this.next();
      else if (act === "prev") this.go(this.i - 1);
      else if (act === "skip") this.stop(true);
      else if (act === "go") this.go(+e.target.closest("[data-step]").dataset.step);
    });
    this._key = (e) => {
      if (e.key === "Escape") this.stop(true);
      else if (e.key === "ArrowRight" || e.key === "Enter") this.next();
      else if (e.key === "ArrowLeft") this.go(this.i - 1);
      else return;
      e.preventDefault();
      e.stopPropagation();
    };
    this._place = () => this.place();
    document.addEventListener("keydown", this._key, true);
    window.addEventListener("resize", this._place);
    this.go(0);
  },

  next() {
    if (this.i >= this.steps.length - 1) this.stop(true);
    else this.go(this.i + 1);
  },

  go(i) {
    if (!this.els || i < 0 || i >= this.steps.length) return;
    this.i = i;
    const s = this.steps[i];
    s.before?.();
    const target = $(s.el);
    target?.scrollIntoView({ block: "nearest", inline: "nearest" });
    const last = i === this.steps.length - 1;
    this.els.pop.innerHTML = `
      <div class="step-no">${i + 1} / ${this.steps.length}</div>
      <h4>${s.title}</h4>
      <p>${s.body}</p>
      <div class="tour-dots">${this.steps.map((st, j) =>
          `<button type="button" class="${j === i ? "on" : ""}" data-tour="go" data-step="${j}"
             title="${j + 1}. ${escapeHtml(st.title)}" aria-label="第 ${j + 1} 步：${escapeHtml(st.title)}"
             ${j === i ? 'aria-current="step"' : ""}><i></i></button>`).join("")}</div>
      <div class="foot">
        ${last ? "" : `<button class="btn ghost sm" data-tour="skip">跳过</button>`}
        <span class="grow"></span>
        ${i ? `<button class="btn sm" data-tour="prev">上一步</button>` : ""}
        <button class="btn primary sm" data-tour="next">${last ? "开始使用" : "下一步"}</button>
      </div>`;
    requestAnimationFrame(() => this.place());
    this.els.pop.querySelector('[data-tour="next"]').focus();
  },

  place() {
    if (!this.els) return;
    const s = this.steps[this.i];
    const target = $(s.el);
    const { spot, pop } = this.els;
    const pad = 6;
    const vw = innerWidth, vh = innerHeight;
    let r = target?.getBoundingClientRect();
    if (!r || (r.width === 0 && r.height === 0)) {
      // 目标不可见：居中显示说明，不高亮
      spot.style.cssText = `left:${vw / 2}px;top:${vh / 2}px;width:0;height:0`;
      pop.style.left = `${(vw - pop.offsetWidth) / 2}px`;
      pop.style.top = `${(vh - pop.offsetHeight) / 2}px`;
      return;
    }
    const box = {
      left: Math.max(4, r.left - pad), top: Math.max(4, r.top - pad),
      right: Math.min(vw - 4, r.right + pad), bottom: Math.min(vh - 4, r.bottom + pad),
    };
    spot.style.cssText = `left:${box.left}px;top:${box.top}px;width:${box.right - box.left}px;height:${box.bottom - box.top}px`;

    const pw = pop.offsetWidth, ph = pop.offsetHeight, gap = 12;
    const clampX = (x) => Math.min(Math.max(12, x), vw - pw - 12);
    const clampY = (y) => Math.min(Math.max(12, y), vh - ph - 12);
    let left, top;
    if (vh - box.bottom >= ph + gap) { top = box.bottom + gap; left = clampX(box.left); }
    else if (box.top >= ph + gap) { top = box.top - ph - gap; left = clampX(box.left); }
    else if (vw - box.right >= pw + gap) { left = box.right + gap; top = clampY(box.top); }
    else if (box.left >= pw + gap) { left = box.left - pw - gap; top = clampY(box.top); }
    else { left = clampX((vw - pw) / 2); top = clampY(vh - ph - 24); }
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
  },

  stop(done) {
    if (!this.els) return;
    Object.values(this.els).forEach((el) => el.remove());
    this.els = null;
    document.removeEventListener("keydown", this._key, true);
    window.removeEventListener("resize", this._place);
    if (done && this.key) {
      try { localStorage.setItem(this.key, "1"); } catch {}
    }
    this.onEnd?.();
  },

  seen(key) {
    try { return localStorage.getItem(key) === "1"; } catch { return true; }
  },
};
