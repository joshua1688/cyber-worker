"""执行时的操作特效：单击、双击、长按、拖动、滚动、按键、输入、悬停，在屏幕上画一个短暂的动画，
看得清程序正在做什么（光标本身只会移动，按下去那一下是看不到的）。

- EffectsIO 包在 ScreenIO 外面，只给 runner 用（录制、截图不经过它）：每个键鼠动作照常交给里面的 io，
  再告诉 Overlay 画什么、告诉 Sound 放什么音效（sounds.py）。特效、音效出了错都不影响执行。
- EffectBoard 把这些事件变成一个个特效，按时间算出每一帧的图（预乘 alpha 的 BGRA），和平台无关，可以单测。
- Overlay 是平台的画法：Windows 见 win_overlay（点不到、截不到的置顶透明小窗口）；其他平台暂时不画。

坐标一律是屏幕物理像素，由 Overlay 自己取「此刻鼠标在哪」，这里不换算。
"""

from __future__ import annotations

import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import cv2
import numpy as np

from cyber_worker.flow.io.sounds import NullSound, Sound

DOUBLE_SEC = 0.5  # 同一个键、同一个地方两下点击间隔不超过它算双击（runner 的双击间隔是 60–120 ms）
DOUBLE_SLOP = 4  # 点：两下离得超过这么远就是两次单击（两个挨得很近的单击步骤）
DRAG_SLOP = 8  # 按住时移动超过这么多（96 DPI 下的像素）算拖动，否则是长按

# 颜色（BGR，0–1）：左键绿、右键橙、中键蓝；描边白色，在任何底色上都看得清
_COLORS = {"left": (0.37, 0.77, 0.13), "right": (0.04, 0.62, 0.96), "middle": (0.97, 0.74, 0.22)}
_WHITE = (1.0, 1.0, 1.0)
_BUTTON_TEXT = {"right": "右键", "middle": "中键"}


class Overlay(Protocol):
    def show(self, kind: str, **kw) -> None:
        """画一个特效，不阻塞。kind 见 EffectBoard.handle。"""
        ...

    def close(self) -> None: ...


class NullOverlay:
    """不画（macOS 暂未实现；测试用）。"""

    def show(self, kind: str, **kw) -> None:
        pass

    def close(self) -> None:
        pass


def pretty_combo(combo: str) -> str:
    """ctrl+a → Ctrl+A；和 studio 界面上的写法一致。"""
    names = {"cmd": "⌘" if sys.platform == "darwin" else "Win", "esc": "Esc", "escape": "Esc",
             "page_up": "PageUp", "page_down": "PageDown", "space": "Space"}
    return "+".join(names.get(k, k.upper() if len(k) == 1 else k[:1].upper() + k[1:])
                    for k in combo.lower().split("+"))


class EffectsIO:
    """ScreenIO 的包装：动作照做，再画对应的特效、放对应的音效（sounds.py）。
    enabled / sound_enabled 可随时改（设置里的「显示操作效果」「操作音效」）。"""

    def __init__(self, io, overlay: Overlay, *, sound: Sound | None = None, enabled: bool = True,
                 sound_enabled: bool = True, clock: Callable[[], float] = time.monotonic) -> None:
        self._io = io
        self.overlay = overlay
        self.sound = sound or NullSound()
        self.enabled = enabled
        self.sound_enabled = sound_enabled
        self._clock = clock
        self._last_click: tuple[str, float, tuple[float, float] | None] | None = None

    def __getattr__(self, name):  # 窗口、截图等其余方法原样转给里面的 io
        return getattr(self._io, name)

    def effect(self, kind: str, **kw) -> None:
        """画一个特效、放对应的音效（runner 也用它标悬停这种没有键鼠事件的动作）。"""
        for on, show in ((self.enabled, self.overlay.show), (self.sound_enabled, self.sound.play)):
            if on:
                try:
                    show(kind, **kw)
                except Exception:  # noqa: BLE001, S110 —— 特效、音效只是给人看 / 听的，出错不能打断执行
                    pass

    def click(self, button: str) -> None:
        self._io.click(button)
        now = self._clock()
        try:
            pos = self._io.mouse_position()
        except Exception:  # noqa: BLE001 —— 拿不到位置就只按时间判断
            pos = None
        last = self._last_click
        double = (last is not None and last[0] == button and now - last[1] <= DOUBLE_SEC
                  and (pos is None or last[2] is None or math.dist(pos, last[2]) <= DOUBLE_SLOP))
        self._last_click = None if double else (button, now, pos)
        self.effect("double" if double else "click", button=button)

    def mouse_down(self, button: str) -> None:
        self._io.mouse_down(button)
        self.effect("press", button=button)

    def mouse_up(self, button: str) -> None:
        self._io.mouse_up(button)
        self.effect("release", button=button)

    def scroll(self, dy: int, dx: int = 0) -> None:
        self._io.scroll(dy, dx)
        self.effect("scroll", dy=dy, dx=dx)

    def press_combo(self, combo: str) -> None:
        self._io.press_combo(combo)
        self.effect("key", label=pretty_combo(combo))

    def type_char(self, ch: str) -> None:
        self._io.type_char(ch)
        self.effect("type", text=ch)

    def clear_field(self) -> None:
        self._io.clear_field()
        self.effect("key", label="清空输入框")


# ---------------- 画 ----------------

Sprite = tuple[int, int, np.ndarray]  # 左上角（屏幕像素）+ 预乘 alpha 的 BGRA uint8


def _ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


def _new(w: int, h: int) -> np.ndarray:
    return np.zeros((max(1, h), max(1, w), 4), np.float32)


def _paint(canvas: np.ndarray, mask: np.ndarray, color, opacity: float) -> None:
    """把 mask（0–255）这块涂成 color，叠在 canvas 上（预乘 alpha 的 over 合成）。"""
    a = mask.astype(np.float32) * (opacity / 255)
    canvas[..., :3] = canvas[..., :3] * (1 - a[..., None]) + np.asarray(color, np.float32) * a[..., None]
    canvas[..., 3] = canvas[..., 3] * (1 - a) + a


def _mask(canvas: np.ndarray) -> np.ndarray:
    return np.zeros(canvas.shape[:2], np.uint8)


_SHIFT = 4  # cv2 画图用 1/16 像素精度，圆圈放大时不会一格一格地跳


def _fix(v: float) -> int:
    return round(v * (1 << _SHIFT))


def _ring(canvas, cx, cy, r, thick, color, opacity) -> None:
    """白色描边 + 彩色圆圈。"""
    for col, extra, op in ((_WHITE, 2.0, 0.85), (color, 0.0, 1.0)):
        m = _mask(canvas)
        cv2.circle(m, (_fix(cx), _fix(cy)), _fix(r), 255, max(1, round(thick + extra)), cv2.LINE_AA, _SHIFT)
        _paint(canvas, m, col, opacity * op)


def _dot(canvas, cx, cy, r, color, opacity) -> None:
    m = _mask(canvas)
    cv2.circle(m, (_fix(cx), _fix(cy)), _fix(r + 1.5), 255, -1, cv2.LINE_AA, _SHIFT)
    _paint(canvas, m, _WHITE, opacity * 0.85)
    m = _mask(canvas)
    cv2.circle(m, (_fix(cx), _fix(cy)), _fix(r), 255, -1, cv2.LINE_AA, _SHIFT)
    _paint(canvas, m, color, opacity)


def _to_bgra(canvas: np.ndarray) -> np.ndarray:
    return np.clip(canvas * 255 + 0.5, 0, 255).astype(np.uint8)


_FONT_CACHE: dict[int, object] = {}


def _font(size: int):
    from PIL import ImageFont

    if size not in _FONT_CACHE:
        for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "PingFang.ttc", "Arial Unicode.ttf", "arial.ttf"):
            try:
                _FONT_CACHE[size] = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        else:
            _FONT_CACHE[size] = ImageFont.load_default()
    return _FONT_CACHE[size]


def render_pill(text: str, scale: float, accent=None) -> np.ndarray:
    """深色圆角标签 + 白字（预乘 alpha 的 BGRA float）。accent：左边一个彩色小圆点。"""
    from PIL import Image, ImageDraw

    ss = 3  # 放大画再缩小 = 抗锯齿
    font = _font(round(13 * scale * ss))
    pad_x, pad_y, dot = 10 * scale * ss, 5 * scale * ss, (8 * scale * ss if accent else 0)
    gap = 6 * scale * ss if accent else 0
    box = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    w = round(tw + 2 * pad_x + dot + gap)
    h = round(max(th, 13 * scale * ss) + 2 * pad_y + 2 * ss)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=(24, 24, 27, 225),
                        outline=(255, 255, 255, 60), width=max(1, ss))
    x = pad_x
    if accent:
        b, g, r = (round(c * 255) for c in accent)
        cy = h / 2
        d.ellipse((x, cy - dot / 2, x + dot, cy + dot / 2), fill=(r, g, b, 255))
        x += dot + gap
    d.text((x - box[0], (h - th) / 2 - box[1]), text, font=font, fill=(255, 255, 255, 255))
    img = img.resize((max(1, round(w / ss)), max(1, round(h / ss))), Image.LANCZOS)
    rgba = np.asarray(img, np.float32) / 255
    a = rgba[..., 3:4]
    return np.concatenate([rgba[..., 2::-1] * a, a], axis=2)  # RGBA → 预乘 BGRA


# ---------------- 特效 ----------------


@dataclass
class _Effect:
    t0: float
    x: float
    y: float

    def frame(self, now: float, cursor: tuple[float, float], s: float) -> Sprite | None:
        raise NotImplementedError


@dataclass
class _Click(_Effect):
    button: str = "left"
    double: bool = False

    def frame(self, now, cursor, s):
        dur = 0.6 if self.double else 0.5
        t = (now - self.t0) / dur
        if t >= 1:
            return None
        R = round(36 * s)
        c = _new(2 * R, 2 * R)
        color = _COLORS.get(self.button, _COLORS["left"])
        e = _ease_out(t)
        _ring(c, R, R, (8 + 24 * e) * s, 3 * s, color, 1 - t)
        if self.double:  # 第二圈稍晚一点、小一点：两下点击
            t2 = max(0.0, t - 0.25) / 0.75
            _ring(c, R, R, (6 + 16 * _ease_out(t2)) * s, 2.5 * s, color, (1 - t2) if t2 > 0 else 0)
        _dot(c, R, R, 4.5 * s, color, max(0.0, 1 - 2 * t))
        return round(self.x - R), round(self.y - R), _to_bgra(c)


@dataclass
class _Press(_Effect):
    """按住不放：原地是长按（圆圈一圈圈转），移动了就是拖动（画出走过的路径）。松开后淡出。"""

    button: str = "left"
    path: list[tuple[float, float]] = field(default_factory=list)
    released: float | None = None

    def dragged(self, s: float) -> bool:
        return any(math.hypot(px - self.x, py - self.y) > DRAG_SLOP * s for px, py in self.path)

    def frame(self, now, cursor, s):
        if self.released is None:
            if not self.path or math.hypot(cursor[0] - self.path[-1][0], cursor[1] - self.path[-1][1]) >= 1:
                self.path.append(cursor)
            fade = 1.0
        else:
            fade = 1 - (now - self.released) / 0.6
            if fade <= 0:
                return None
        color = _COLORS.get(self.button, _COLORS["left"])
        pts = [(self.x, self.y), *self.path]
        m = round(40 * s)
        x0, y0 = min(p[0] for p in pts) - m, min(p[1] for p in pts) - m
        x1, y1 = max(p[0] for p in pts) + m, max(p[1] for p in pts) + m
        c = _new(round(x1 - x0), round(y1 - y0))
        end = pts[-1]
        if self.dragged(s):
            arr = np.array([[_fix(px - x0), _fix(py - y0)] for px, py in pts], np.int32)
            for col, w, op in ((_WHITE, 7 * s, 0.7), (color, 4 * s, 0.9)):
                mk = _mask(c)
                cv2.polylines(mk, [arr], False, 255, max(1, round(w)), cv2.LINE_AA, _SHIFT)
                _paint(c, mk, col, op * fade)
            _dot(c, self.x - x0, self.y - y0, 5 * s, color, fade)
            _ring(c, end[0] - x0, end[1] - y0, 12 * s, 3 * s, color, fade)
        else:
            held = (self.released or now) - self.t0
            _ring(c, self.x - x0, self.y - y0, 16 * s, 2 * s, color, 0.35 * fade)
            mk = _mask(c)  # 进度弧：每秒转一圈
            angle = 360 * (held % 1.0) if held < 30 else 360
            cv2.ellipse(mk, (_fix(self.x - x0), _fix(self.y - y0)), (_fix(16 * s), _fix(16 * s)),
                        -90, 0, max(1.0, angle), 255, max(1, round(4 * s)), cv2.LINE_AA, _SHIFT)
            _paint(c, mk, color, fade)
            _dot(c, self.x - x0, self.y - y0, 5 * s, color, fade)
        return round(x0), round(y0), _to_bgra(c)


@dataclass
class _Hover(_Effect):
    seconds: float = 1.0

    def frame(self, now, cursor, s):
        age = now - self.t0
        if age >= self.seconds + 0.3:
            return None
        fade = min(1.0, age / 0.15, (self.seconds + 0.3 - age) / 0.3)
        R = round(30 * s)
        c = _new(2 * R, 2 * R)
        r = (13 + 4 * math.sin(age * 2 * math.pi / 1.2)) * s  # 呼吸
        _ring(c, R, R, r, 2.5 * s, _COLORS["middle"], 0.8 * fade)
        _dot(c, R, R, 3.5 * s, _COLORS["middle"], 0.8 * fade)
        return round(self.x - R), round(self.y - R), _to_bgra(c)


@dataclass
class _Label(_Effect):
    """鼠标旁边的小标签（双击、右键、滚动、按键、输入…）。until 之后 0.3 秒淡出；内容变了重画。"""

    text: str = ""
    until: float = 0.0
    accent: tuple | None = None
    _img: np.ndarray | None = None
    _drawn: str | None = None

    def frame(self, now, cursor, s):
        if now >= self.until + 0.3:
            return None
        if self._drawn != self.text:
            self._img, self._drawn = render_pill(self.text, s, self.accent), self.text
        fade = min(1.0, (now - self.t0) / 0.1 + 0.2, (self.until + 0.3 - now) / 0.3)
        return round(self.x), round(self.y), _to_bgra(self._img * max(0.0, fade))


class EffectBoard:
    """事件 → 特效；frames() 给出每个还在播的特效此刻的图。和平台无关，Overlay 负责真正画到屏幕上。

    事件（handle 的 kind）：click / double / press / release（button）、scroll（dy, dx）、
    key（label）、type（text）、hover（seconds）。kw 里的 at=(x, y) 可以指定位置（演示用），否则用 cursor。
    """

    LABEL_DX, LABEL_DY = 20, 18  # 标签在鼠标右下方（96 DPI 下的像素），不挡住点的地方

    def __init__(self, scale: float = 1.0) -> None:
        self.scale = scale
        self.effects: dict[int, _Effect] = {}
        self._next = 0
        self._press: dict[str, int] = {}  # 按着的键 → 特效编号
        self._scroll: int | None = None
        self._kbd: int | None = None
        self._kbd_mode = ""
        self._kbd_items: list[str] = []
        self._scroll_sum = [0, 0]

    @property
    def active(self) -> bool:
        return bool(self.effects)

    def _add(self, eff: _Effect) -> int:
        self._next += 1
        self.effects[self._next] = eff
        return self._next

    def _label_at(self, x, y, row: int = 0) -> tuple[float, float]:
        s = self.scale
        return x + self.LABEL_DX * s, y + (self.LABEL_DY + row * 30) * s

    def _alive(self, key: int | None, now: float, within: float) -> _Label | None:
        eff = self.effects.get(key) if key is not None else None
        return eff if isinstance(eff, _Label) and now - eff.until <= within else None

    def handle(self, kind: str, kw: dict, cursor: tuple[float, float], now: float) -> None:
        x, y = kw.get("at") or cursor
        button = kw.get("button", "left")
        if kind in ("click", "double"):
            self._add(_Click(now, x, y, button=button, double=kind == "double"))
            text = "双击" if kind == "double" else _BUTTON_TEXT.get(button)
            if kind == "double" and button in _BUTTON_TEXT:
                text = _BUTTON_TEXT[button] + "双击"
            if text:
                lx, ly = self._label_at(x, y)
                self._add(_Label(now, lx, ly, text=text, until=now + 0.6, accent=_COLORS.get(button)))
        elif kind == "press":
            self._press[button] = self._add(_Press(now, x, y, button=button))
        elif kind == "release":
            eff = self.effects.get(self._press.pop(button, -1))
            if isinstance(eff, _Press) and eff.released is None:
                eff.released = now
                if eff.path and (end := kw.get("at")):
                    eff.path.append(end)
                ex, ey = (eff.path or [(x, y)])[-1]
                held = now - eff.t0
                text = "拖动" if eff.dragged(self.scale) else f"长按 {held:.1f} 秒"
                lx, ly = self._label_at(ex, ey)
                self._add(_Label(now, lx, ly, text=text, until=now + 0.7, accent=_COLORS.get(button)))
        elif kind == "scroll":
            dy, dx = int(kw.get("dy", 0)), int(kw.get("dx", 0))
            label = self._alive(self._scroll, now, 0.6)
            same = label is not None and (self._scroll_sum[0] * dy >= 0) and (self._scroll_sum[1] * dx >= 0)
            if not same:
                self._scroll_sum = [0, 0]
                lx, ly = self._label_at(x, y)
                label = _Label(now, lx, ly, accent=_COLORS["middle"])
                self._scroll = self._add(label)
            self._scroll_sum[0] += dy
            self._scroll_sum[1] += dx
            label.text = self._scroll_text()
            label.until = now + 0.8
        elif kind in ("key", "type"):
            label = self._alive(self._kbd, now, 1.0)
            if label is None or self._kbd_mode != kind:
                self._kbd_items = []
                if label is None:
                    lx, ly = self._label_at(x, y, row=1)
                    label = _Label(now, lx, ly, accent=(0.9, 0.9, 0.9))
                    self._kbd = self._add(label)
                self._kbd_mode = kind
            if kind == "key":
                self._kbd_items = [*self._kbd_items, kw.get("label", "")][-3:]
                label.text = "按键  " + " → ".join(self._kbd_items)
            else:
                self._kbd_items.append(kw.get("text", ""))
                typed = "".join(self._kbd_items).replace("\n", "⏎")
                label.text = "输入  " + (typed if len(typed) <= 16 else "…" + typed[-15:])
            label.until = now + 1.2
        elif kind == "hover":
            seconds = min(30.0, float(kw.get("seconds", 1.0)))
            self._add(_Hover(now, x, y, seconds=seconds))
            lx, ly = self._label_at(x, y)
            self._add(_Label(now, lx, ly, text="悬停", until=now + seconds, accent=_COLORS["middle"]))

    def _scroll_text(self) -> str:
        dy, dx = self._scroll_sum
        parts = []
        if dy:
            parts.append(f"{'↓' if dy > 0 else '↑'} {abs(dy)}")
        if dx:
            parts.append(f"{'→' if dx > 0 else '←'} {abs(dx)}")
        return "滚动  " + "  ".join(parts or ["0"])

    def frames(self, now: float, cursor: tuple[float, float]) -> dict[int, Sprite]:
        """每个还在播的特效此刻的图；播完的删掉。"""
        out: dict[int, Sprite] = {}
        for key, eff in list(self.effects.items()):
            sprite = eff.frame(now, cursor, self.scale)
            if sprite is None:
                del self.effects[key]
            else:
                out[key] = sprite
        return out
