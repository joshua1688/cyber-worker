// 画布：窗口截图 + 步骤标记（编号圆点、连线、滚动箭头）+ 页面锚点框 + 区域框选。
// 坐标约定：流程里存的是「点」，截图是像素，像素 = 点 × scale。
"use strict";

// 连线流动：系统设了「减少动态效果」就不动
const FLOW = !matchMedia("(prefers-reduced-motion: reduce)").matches;
// 流动动画每秒最多重画这么多次；一分钟没动键鼠、或者浏览器不在前台就停（停着就是一条静止的虚线），一动就接着走
const FLOW_FPS = 30;
const FLOW_IDLE_MS = 60_000;
let lastInput = performance.now();
const flowing = () => document.hasFocus() && performance.now() - lastInput < FLOW_IDLE_MS;

class Stage {
  constructor(canvas, handlers) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.h = handlers; // onMove(index, x, y) / onSelect(index) / onRegion(rect) / onHover(pt|null)
    this.img = null;
    this.scale = 1;
    this.steps = [];
    this.pages = {};
    this.selected = -1;
    this.anchorMode = false;
    this.drag = null; // {kind: "marker", index, x, y} | {kind: "rect", x0, y0, x1, y1}
    this.region = null; // 框选完成、等待命名的区域

    canvas.addEventListener("mousedown", (e) => this._down(e));
    window.addEventListener("mousemove", (e) => this._move(e));
    window.addEventListener("mouseup", (e) => this._up(e));
    canvas.addEventListener("mouseleave", () => this.h.onHover?.(null));
    const wake = () => {
      const was = flowing();
      lastInput = performance.now();
      if (!was) this.draw();  // 停过：接着流动
    };
    ["pointermove", "keydown", "wheel", "focus"].forEach((t) => window.addEventListener(t, wake, { passive: true }));
    new ResizeObserver(() => this.draw()).observe(canvas);
  }

  get hasImage() {
    return !!this.img;
  }

  setFrame(url, scale) {
    this.scale = scale || 1;
    if (!url) {
      this.img = null;
      this.draw();
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        this.img = img;
        this.canvas.width = img.naturalWidth;
        this.canvas.height = img.naturalHeight;
        this.draw();
        resolve();
      };
      img.onerror = reject;
      img.src = url;
    });
  }

  // win：画面上显示的是哪个窗口（null = 主窗口）。只画这个窗口里的步骤标记和锚点
  setData(steps, pages, selected, win = null) {
    this.steps = steps;
    this.pages = pages || {};
    this.selected = selected;
    this.win = win || null;
    this.draw();
  }

  setAnchorMode(on) {
    this.anchorMode = on;
    this.region = null;
    this.draw();
  }

  clearRegion() {
    this.region = null;
    this.draw();
  }

  // ---- 坐标换算 ----
  _k() {
    // 一个 CSS 像素对应多少画布像素：用来让标记在屏幕上大小一致
    const w = this.canvas.getBoundingClientRect().width || this.canvas.width;
    return this.canvas.width / w;
  }
  _eventPt(e) {
    const r = this.canvas.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * this.canvas.width;
    const py = ((e.clientY - r.top) / r.height) * this.canvas.height;
    return { x: px / this.scale, y: py / this.scale };
  }
  _inside(pt) {
    return this.img && pt.x >= 0 && pt.y >= 0 &&
      pt.x <= this.canvas.width / this.scale && pt.y <= this.canvas.height / this.scale;
  }
  _markers() {
    const out = [];
    const slots = {};  // 同一次点击后面跟着几个输入 / 按键：方块依次往右排，不叠在一起
    this.steps.forEach((s, i) => {
      if ((s.win || null) !== (this.win || null)) return;
      if (MARKED.has(s.action) && s.x != null && s.y != null) {
        out.push({ i, x: s.x, y: s.y, step: s, which: "start" });
      }
      if (s.action === "drag") out.push({ i, x: s.x2, y: s.y2, step: s, which: "end" });
      // 输入 / 按键：标在它打进去的地方（前面那次点击），只显示，不能拖
      if (s.action === "type" || s.action === "key") {
        const k = focusClick(this.steps, i);
        if (k >= 0) {
          const slot = slots[k] = (slots[k] ?? -1) + 1;
          out.push({ i, x: this.steps[k].x, y: this.steps[k].y, step: s, which: "focus", slot });
        }
      }
    });
    return out;
  }
  // 输入 / 按键的标记：方块（和点击的圆区分开），挨着它打进去的那次点击；选中时把那个位置圈出来
  _drawFocus(ctx, m, s, k, selected) {
    const css = getComputedStyle(document.documentElement);
    const info = css.getPropertyValue("--info").trim();
    const accent = css.getPropertyValue("--accent").trim();
    const cx = m.x * s, cy = m.y * s, q = 10 * k;
    const x = cx + (20 + 24 * m.slot) * k, y = cy - 20 * k;  // 往右上错开，不盖住点击的编号；第二个、第三个再往右排
    ctx.save();
    if (selected) {
      ctx.lineWidth = 2 * k;
      ctx.setLineDash([4 * k, 3 * k]);
      ctx.strokeStyle = info;
      ctx.beginPath();
      ctx.arc(cx, cy, 18 * k, 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = `${12 * k}px sans-serif`;
      label(ctx, m.step.action === "type" ? "输入到这里" : "按键发到这里", cx + 34 * k, cy + 4 * k, info, k);
    }
    ctx.lineWidth = 2 * k;
    ctx.fillStyle = selected ? accent : "rgba(20,20,22,.85)";
    ctx.strokeStyle = selected ? "#fff" : info;
    ctx.beginPath();
    ctx.roundRect ? ctx.roundRect(x - q, y - q, q * 2, q * 2, 4 * k) : ctx.rect(x - q, y - q, q * 2, q * 2);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = selected ? "#0b0b0b" : "#fff";
    ctx.font = `bold ${11 * k}px sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(String(actionNo(this.steps, m.i)), x, y + 0.5 * k);
    ctx.restore();
  }

  _hit(pt) {
    const r = (12 * this._k()) / this.scale;
    const ms = this._markers().filter((m) => m.which !== "focus");
    for (let j = ms.length - 1; j >= 0; j--) {
      const m = ms[j];
      if (Math.hypot(m.x - pt.x, m.y - pt.y) <= r) return m;
    }
    return null;
  }

  // ---- 交互 ----
  _down(e) {
    if (!this.img || e.button !== 0) return;
    const pt = this._eventPt(e);
    if (this.anchorMode) {
      this.drag = { kind: "rect", x0: pt.x, y0: pt.y, x1: pt.x, y1: pt.y };
      return;
    }
    // 只有按住编号标记拖动才会改位置：点一下画面不改，看截图时随手一点不会把步骤挪走（改动会自动保存，挪了执行时就点空）
    const m = this._hit(pt);
    if (m) this.drag = { kind: "marker", index: m.i, which: m.which, x: m.x, y: m.y, moved: false };
  }

  _move(e) {
    if (!this.img) return;
    const pt = this._eventPt(e);
    if (this.drag?.kind === "marker") {
      this.drag.x = clamp(pt.x, 0, this.canvas.width / this.scale);
      this.drag.y = clamp(pt.y, 0, this.canvas.height / this.scale);
      this.drag.moved = true;
      this.draw();
    } else if (this.drag?.kind === "rect") {
      this.drag.x1 = clamp(pt.x, 0, this.canvas.width / this.scale);
      this.drag.y1 = clamp(pt.y, 0, this.canvas.height / this.scale);
      this.draw();
    }
    if (e.target === this.canvas) this.h.onHover?.(this._inside(pt) ? pt : null);
  }

  _up() {
    const d = this.drag;
    this.drag = null;
    if (!d) return;
    if (d.kind === "marker") {
      if (d.moved) this.h.onMove?.(d.index, round1(d.x), round1(d.y), d.which);
      else this.h.onSelect?.(d.index);
    } else if (d.kind === "rect") {
      const rect = {
        x: round1(Math.min(d.x0, d.x1)), y: round1(Math.min(d.y0, d.y1)),
        w: round1(Math.abs(d.x1 - d.x0)), h: round1(Math.abs(d.y1 - d.y0)),
      };
      if (rect.w >= 4 && rect.h >= 4) {
        this.region = rect;
        this.h.onRegion?.(rect);
      }
      this.draw();
    }
  }

  // ---- 绘制 ----
  draw() {
    const { ctx, canvas } = this;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!this.img) return;
    ctx.drawImage(this.img, 0, 0);
    const s = this.scale;
    const k = this._k();
    const css = getComputedStyle(document.documentElement);
    const accent = css.getPropertyValue("--accent").trim();
    const info = css.getPropertyValue("--info").trim();
    const danger = css.getPropertyValue("--danger").trim();

    // 页面锚点
    ctx.save();
    ctx.lineWidth = 2 * k;
    ctx.setLineDash([6 * k, 4 * k]);
    ctx.font = `${12 * k}px sans-serif`;
    // 选中「等待页面」时，它等的那个页面的锚点高亮（实线、加粗、淡淡填色）
    const waiting = this.steps[this.selected]?.action === "wait_page" ? this.steps[this.selected].page : null;
    for (const [page, anchors] of Object.entries(this.pages)) {
      for (const a of anchors) {
        if ((a.win || null) !== (this.win || null)) continue;
        const hot = page === waiting;
        ctx.strokeStyle = hot ? accent : info;
        ctx.lineWidth = (hot ? 3 : 2) * k;
        ctx.setLineDash(hot ? [] : [6 * k, 4 * k]);
        if (hot) {
          ctx.save();
          ctx.globalAlpha = 0.15;
          ctx.fillStyle = accent;
          ctx.fillRect(a.x * s, a.y * s, a.w * s, a.h * s);
          ctx.restore();
        }
        ctx.strokeRect(a.x * s, a.y * s, a.w * s, a.h * s);
        // 贴着顶边的锚点，标签画到框里面，免得被裁掉
        const ly = a.y * s - 4 * k >= 14 * k ? a.y * s - 4 * k : a.y * s + 16 * k;
        label(ctx, page, a.x * s, ly, hot ? accent : info, k);
      }
    }
    ctx.restore();

    // 选中的点击步骤：按图找的目标框
    const sel = this.steps[this.selected];
    if ((sel?.action === "click" || sel?.action === "hover") && sel.target && sel.use_target !== false && (sel.win || null) === (this.win || null)) {
      const t = sel.target;
      const orange = css.getPropertyValue("--orange").trim();
      ctx.save();
      ctx.lineWidth = 2 * k;
      ctx.setLineDash([5 * k, 3 * k]);
      ctx.strokeStyle = orange;
      ctx.font = `${12 * k}px sans-serif`;
      ctx.strokeRect(t.x * s, t.y * s, t.w * s, t.h * s);
      const ly = t.y * s - 4 * k >= 14 * k ? t.y * s - 4 * k : t.y * s + t.h * s + 16 * k;
      label(ctx, "目标", t.x * s, ly, orange, k);
      ctx.restore();
    }

    // 鼠标路径（拖动的终点不参与连线，单独画箭头）
    const ms = this._markers().map((m) =>
      this.drag?.kind === "marker" && this.drag.index === m.i && this.drag.which === m.which
        ? { ...m, x: this.drag.x, y: this.drag.y } : m);
    const starts = ms.filter((m) => m.which === "start");
    if (starts.length > 1) {
      // 流动虚线：主题色圆头短划线，沿步骤顺序慢慢往前走，一眼看出先后；底下一圈淡阴影，浅色、深色界面上都清楚
      const dash = [7 * k, 6 * k];
      ctx.save();
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();
      starts.forEach((m, j) => (j ? ctx.lineTo(m.x * s, m.y * s) : ctx.moveTo(m.x * s, m.y * s)));
      ctx.shadowColor = "rgba(0,0,0,.4)";
      ctx.shadowBlur = 3 * k;
      ctx.strokeStyle = accent;
      ctx.lineWidth = 2.6 * k;
      ctx.setLineDash(dash);
      ctx.lineDashOffset = FLOW ? -((performance.now() * 0.02 * k) % (dash[0] + dash[1])) : 0;
      ctx.stroke();
      ctx.restore();
      if (FLOW && !this._flow && flowing()) {  // 有连线才重画；限帧，闲着、不在前台就停
        const due = performance.now() + 1000 / FLOW_FPS - 4;  // 60 Hz 屏幕上隔一帧画一次
        const tick = (t) => {
          if (t < due) return (this._flow = requestAnimationFrame(tick));
          this._flow = 0;
          this.draw();
        };
        this._flow = requestAnimationFrame(tick);
      }
    }
    for (const m of ms.filter((m) => m.which === "end")) {
      const a = ms.find((x) => x.i === m.i && x.which === "start");
      arrow(ctx, a.x * s, a.y * s, m.x * s, m.y * s, m.i === this.selected ? accent : "#f28b3c", k);
    }

    // 标记：选中的最后画，压在别的标记上面
    for (const m of [...ms.filter((m) => m.i !== this.selected), ...ms.filter((m) => m.i === this.selected)]) {
      if (m.which === "focus") {
        this._drawFocus(ctx, m, s, k, m.i === this.selected);
        continue;
      }
      const x = m.x * s, y = m.y * s, r = 11 * k;
      const selected = m.i === this.selected;
      ctx.save();
      ctx.lineWidth = 2 * k;
      ctx.fillStyle = selected ? accent : "rgba(20,20,22,.85)";
      ctx.strokeStyle = m.step.sends ? danger : selected ? "#fff" : accent;
      ctx.beginPath();
      if (m.which === "end") {
        const q = r * 0.7;
        ctx.rect(x - q, y - q, q * 2, q * 2);
      } else if (m.step.action === "scroll") {
        ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y); ctx.closePath();
      } else {
        ctx.arc(x, y, r, 0, Math.PI * 2);
      }
      ctx.fill();
      if (m.step.action === "hover") ctx.setLineDash([3 * k, 2.5 * k]); // 悬停：虚线圈，只停不点
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = selected ? "#0b0b0b" : "#fff";
      ctx.font = `bold ${11 * k}px sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      const text = m.which === "end" ? "" : m.step.action === "scroll"
        ? (m.step.dy > 0 ? "↓" : m.step.dy < 0 ? "↑" : m.step.dx > 0 ? "→" : "←")
        : String(actionNo(this.steps, m.i));
      ctx.fillText(text, x, y + 0.5 * k);
      ctx.restore();
    }

    // 框选中 / 待命名的区域
    const r = this.drag?.kind === "rect"
      ? { x: Math.min(this.drag.x0, this.drag.x1), y: Math.min(this.drag.y0, this.drag.y1),
          w: Math.abs(this.drag.x1 - this.drag.x0), h: Math.abs(this.drag.y1 - this.drag.y0) }
      : this.region;
    if (r) {
      ctx.save();
      ctx.fillStyle = "rgba(47,201,142,.15)";
      ctx.strokeStyle = accent;
      ctx.lineWidth = 2 * k;
      ctx.setLineDash([6 * k, 4 * k]);
      ctx.fillRect(r.x * s, r.y * s, r.w * s, r.h * s);
      ctx.strokeRect(r.x * s, r.y * s, r.w * s, r.h * s);
      ctx.restore();
    }
  }
}

function arrow(ctx, x0, y0, x1, y1, color, k) {
  const ang = Math.atan2(y1 - y0, x1 - x0), head = 10 * k;
  ctx.save();
  ctx.strokeStyle = ctx.fillStyle = color;
  ctx.lineWidth = 3 * k;
  ctx.beginPath();
  ctx.moveTo(x0, y0);
  ctx.lineTo(x1 - Math.cos(ang) * head * 0.6, y1 - Math.sin(ang) * head * 0.6);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x1 - head * Math.cos(ang - 0.45), y1 - head * Math.sin(ang - 0.45));
  ctx.lineTo(x1 - head * Math.cos(ang + 0.45), y1 - head * Math.sin(ang + 0.45));
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

function label(ctx, text, x, y, color, k) {
  const w = ctx.measureText(text).width + 8 * k;
  ctx.fillStyle = "rgba(10,10,12,.8)";
  ctx.fillRect(x, y - 14 * k, w, 16 * k);
  ctx.fillStyle = color;
  ctx.setLineDash([]);
  ctx.fillText(text, x + 4 * k, y - 2 * k);
}

const round1 = (v) => Math.round(v * 10) / 10;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
// 在画面上有编号标记、可以拖动的步骤
const MARKED = new Set(["click", "hover", "scroll", "drag"]);
