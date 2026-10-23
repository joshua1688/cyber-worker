"""录制引擎：把你在目标窗口里的键鼠操作转成 Script。

RecordingBuilder 是纯逻辑（可测试）；listen() 用 pynput 监听真实输入，阻塞到 stop 被置位。
规则：
- 鼠标按下落在窗口外的会被忽略，并且之后的按键也忽略，直到你再次点回窗口内（切到终端按 Ctrl+C 不会被录进去）。
- 按下到抬起之间：移动超过 6 点 → 拖动；按住 ≥ 0.5 秒 → 长按；否则是点击。
  0.5 秒内在同一位置（误差 4 点内）同一按键点两下，合并成一个双击步骤。
  点击 / 拖动时按住的 Ctrl、Shift、Alt、Cmd 会一起记下。
- 连续的普通字符合并成一个 type 步骤；带 cmd/ctrl/alt 的组合键和回车等特殊键单独成步。
  同一个键连按（包括按住不放时系统的自动重复）合并成一步，记下次数。
- 滚轮：1 秒内、附近位置、同方向的连续滚动合并成一步；触控板 / 高精度滚轮的小数格数累计成整格。
  支持横向滚动。
- 两次操作的间隔（≥ 0.3 秒）录成单独的 wait 步骤，和雷云一样一眼能看到、能直接改；
  record_delays=False 时不录间隔。
- 跨窗口：每次点击 / 滚动看鼠标下面实际是哪个窗口，每次按键看前台是哪个窗口（窗口会互相叠着，
  不能只看坐标落在哪个矩形里）。主窗口以外的窗口登记成 w2、w3…，坐标相对各自的窗口。
  studio 自己所在的浏览器窗口一律忽略。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from cyber_worker.macro.io.base import WindowGeometry, WindowInfo, stable_title
from cyber_worker.macro.io.pynput_input import key_to_name, normalize_combo
from cyber_worker.macro.schema import MAIN, Click, Drag, Key, Script, Scroll, TypeText, Wait, Window

log = logging.getLogger(__name__)

_MODIFIERS = ("ctrl", "shift", "alt", "cmd")  # 也是记录时的固定顺序
DOUBLE_CLICK_SEC = 0.5  # Windows / macOS 默认的双击间隔
DOUBLE_CLICK_SLOP = 4.0  # 两下之间允许的位移（点）
LONG_PRESS_SEC = 0.5  # 按住超过这么久算长按
DRAG_SLOP = 6.0  # 按住移动超过这么多点算拖动
KEY_REPEAT_SEC = 0.6  # 同一个键两次按下间隔不超过这么久，算连按
SCROLL_MERGE_SEC = 1.0
SCROLL_MERGE_DIST = 60.0
# studio 所在浏览器窗口的标题里有这些（页面标题是「宏设计 · 赛博打工人」），录制时忽略；旧标题也认
STUDIO_TITLES = (" · 赛博打工人", "cyber-worker studio")


def is_studio_title(title: str) -> bool:
    return any(t in title for t in STUDIO_TITLES)

# 「这个事件发生在哪个窗口」：窗口信息 + 它现在的位置；None 表示不知道（按主窗口算）
Where = tuple[WindowInfo, WindowGeometry] | None


def _ordered(mods: Iterable[str]) -> list[str]:
    mods = set(mods)
    return [m for m in _MODIFIERS if m in mods]


def _sign(v: float) -> int:
    return (v > 0) - (v < 0)


@dataclass
class _Press:
    t: float
    x: float
    y: float
    button: str
    modifiers: list[str]
    win: str
    seq: int = 0


def _near(a: WindowGeometry, b: WindowGeometry, tol: float = 3) -> bool:
    return all(abs(p - q) <= tol for p, q in ((a.x, b.x), (a.y, b.y), (a.width, b.width), (a.height, b.height)))


class RecordingBuilder:
    def __init__(
        self,
        geo: WindowGeometry,
        now: Callable[[], float] = time.monotonic,
        on_change: Callable[[list], None] | None = None,
        *,
        record_delays: bool = True,
        first_id: int = 1,
        primary: Window | None = None,
        windows: dict[str, Window] | None = None,
        ignore_combos: Iterable[str] = (),
        on_press: Callable[[int, str, WindowGeometry, float, float], None] | None = None,
    ) -> None:
        """on_press(序号, 窗口键, 窗口位置, x, y)：鼠标按下的那一刻回调，studio 拿去截目标图（按图找用）。
        录完后 press_of[步骤编号] 告诉你这个点击步骤对应哪一次按下。"""
        self.geo = geo
        self.primary = primary
        # 录制 / 急停这些全局快捷键本身不能被录进宏
        self.ignore_combos = {normalize_combo(c) for c in ignore_combos}
        self.windows: dict[str, Window] = dict(windows or {})  # 主窗口以外的窗口（含之前录过的）
        self.geos: dict[str, WindowGeometry] = {MAIN: geo}  # 各窗口现在的位置
        self.focus = MAIN  # 键盘输入当前进的是哪个窗口
        self.now = now
        self.record_delays = record_delays
        self._first_id = first_id  # 追加录制到已有脚本时，编号接着往后排
        self.on_change = on_change  # 每次步骤变化后回调（studio 用来实时推送）
        self.steps: list = []
        self.on_press = on_press
        self.press_of: dict[str, int] = {}
        self._press_seq = 0
        self._focused = True
        self._last_at: float | None = None  # 上一个动作结束的时间
        self._down: _Press | None = None  # 按下还没抬起的鼠标键
        self._last_click_down: float | None = None  # 上一次单击按下的时间（判断双击）
        self._last_key_at: float | None = None
        self._scroll_acc = [0.0, 0.0]  # 还不满一格的滚动量（dx, dy）
        self._last_scroll_at: float | None = None

    # ---- 窗口 ----

    def _resolve(self, where: Where) -> str | None:
        """事件发生在哪个窗口 → 窗口键；studio 自己的窗口返回 None（忽略）。"""
        if where is None:
            return MAIN
        info, geo = where
        if is_studio_title(info.title):
            return None
        title = stable_title(info.title)
        same_app = lambda w: w.app.lower() == info.app.lower()  # noqa: E731
        if _near(geo, self.geos[MAIN]) or (
                self.primary and same_app(self.primary) and self.primary.title and self.primary.title in title):
            key = MAIN
        else:
            key = next((k for k, w in self.windows.items()
                        if same_app(w) and ((w.title or "") == title or (k in self.geos and _near(geo, self.geos[k])))),
                       None)
            if key is None:
                n = 2
                while f"w{n}" in self.windows:
                    n += 1
                key = f"w{n}"
                self.windows[key] = Window(app=info.app, title=title or None, width=round(geo.width),
                                           height=round(geo.height), scale=geo.scale)
        self.geos[key] = geo
        return key

    def focus_geometry(self) -> tuple[str, WindowGeometry]:
        """键盘焦点所在窗口及位置（录制时后台截图用）。"""
        return self.focus, self.geos.get(self.focus, self.geo)

    @staticmethod
    def _win(key: str) -> str | None:
        return None if key == MAIN else key

    def _next_id(self) -> str:
        return f"s{len(self.steps) + self._first_id:02d}"

    def _push(self, step, start: float | None = None, end: float | None = None) -> None:
        """start：动作开始时间（算和上一个动作之间的停顿）；end：动作结束时间。"""
        start = self.now() if start is None else start
        if self.record_delays and self.steps and self._last_at is not None:
            gap = round(min(60.0, start - self._last_at), 2)
            if gap >= 0.3:
                self.steps.append(Wait(id=self._next_id(), seconds=gap))
        self._last_at = start if end is None else end
        step.id = self._next_id()
        self.steps.append(step)
        self._changed()

    def _touch(self, t: float) -> None:
        """合并进上一步时，只更新「上一个动作结束」的时间。"""
        self._last_at = t
        self._changed()

    def _changed(self) -> None:
        if self.on_change:
            self.on_change(self.steps)

    # ---- 鼠标 ----

    def on_mouse_down(self, x: float, y: float, button: str, modifiers: Iterable[str] = (),
                      where: Where = None) -> None:
        if where is None:  # 不知道是哪个窗口：只认主窗口范围内的
            key, pos = MAIN, self.geo.to_window(x, y)
        else:
            key = self._resolve(where)
            pos = None if key is None else (x - self.geos[key].x, y - self.geos[key].y)
        self._focused = pos is not None
        if pos is None:
            self._down = None
            return
        self.focus = key
        self._press_seq += 1
        self._down = _Press(self.now(), pos[0], pos[1], button, _ordered(modifiers), key, self._press_seq)
        if self.on_press:
            self.on_press(self._press_seq, key, self.geos[key], pos[0], pos[1])

    def on_mouse_up(self, x: float, y: float, button: str) -> None:
        down = self._down
        if down is None or down.button != button:
            return
        self._down = None
        t = self.now()
        # 拖动的终点可以在窗口外，不做边界限制
        geo = self.geos[down.win]
        x1, y1 = x - geo.x, y - geo.y
        r = lambda v: round(v, 1)  # noqa: E731
        win = self._win(down.win)

        if math.hypot(x1 - down.x, y1 - down.y) > DRAG_SLOP:
            self._push(Drag(id="", win=win, x=r(down.x), y=r(down.y), x2=r(x1), y2=r(y1), button=button,
                            duration=round(min(30.0, max(0.1, t - down.t)), 2),
                            modifiers=down.modifiers), down.t, t)
            self._last_click_down = None
            return

        held = t - down.t
        if held >= LONG_PRESS_SEC:
            self._push(Click(id="", win=win, x=r(down.x), y=r(down.y), button=button,
                             hold=round(min(60.0, held), 2), modifiers=down.modifiers), down.t, t)
            self.press_of[self.steps[-1].id] = down.seq
            self._last_click_down = None
            return

        last = self.steps[-1] if self.steps else None
        if (isinstance(last, Click) and last.count == 1 and not last.hold and last.button == button
                and last.modifiers == down.modifiers and last.win == win
                and self._last_click_down is not None and down.t - self._last_click_down <= DOUBLE_CLICK_SEC
                and abs(last.x - down.x) <= DOUBLE_CLICK_SLOP and abs(last.y - down.y) <= DOUBLE_CLICK_SLOP):
            last.count = 2
            self._last_click_down = None  # 第三下不再合并
            self._touch(t)
            return
        self._push(Click(id="", win=win, x=r(down.x), y=r(down.y), button=button,
                         modifiers=down.modifiers), down.t, t)
        self.press_of[self.steps[-1].id] = down.seq
        self._last_click_down = down.t

    def on_click(self, x: float, y: float, button: str, modifiers: Iterable[str] = (), where: Where = None) -> None:
        """一次瞬间完成的点击（按下马上抬起）。"""
        self.on_mouse_down(x, y, button, modifiers, where)
        self.on_mouse_up(x, y, button)

    def on_scroll(self, x: float, y: float, dy: float, dx: float = 0, where: Where = None) -> None:
        """dy 正数向下、dx 正数向右；可以是小数（触控板、高精度滚轮）。"""
        if where is None:
            key, pos = MAIN, self.geo.to_window(x, y)
            if not self._focused:
                pos = None
        else:
            key = self._resolve(where)
            pos = None if key is None else (x - self.geos[key].x, y - self.geos[key].y)
        if pos is None or (not dx and not dy):
            return
        win = self._win(key)
        t = self.now()
        if self._last_scroll_at is None or t - self._last_scroll_at > SCROLL_MERGE_SEC:
            self._scroll_acc = [0.0, 0.0]
        self._last_scroll_at = t
        self._scroll_acc[0] += dx
        self._scroll_acc[1] += dy
        ix, iy = int(self._scroll_acc[0]), int(self._scroll_acc[1])  # 向 0 取整
        if not ix and not iy:
            return  # 还不满一格，先攒着
        self._scroll_acc[0] -= ix
        self._scroll_acc[1] -= iy

        last = self.steps[-1] if self.steps else None
        if (isinstance(last, Scroll) and last.win == win and self._last_at is not None
                and t - self._last_at <= SCROLL_MERGE_SEC
                and last.x is not None and math.hypot(last.x - pos[0], last.y - pos[1]) <= SCROLL_MERGE_DIST
                and (not iy or not last.dy or _sign(iy) == _sign(last.dy))
                and (not ix or not last.dx or _sign(ix) == _sign(last.dx))):
            last.dy += iy
            last.dx += ix
            self._touch(t)
            return
        self._push(Scroll(id="", win=win, dy=iy, dx=ix, x=round(pos[0], 1), y=round(pos[1], 1)), t)

    # ---- 键盘 ----

    def _key_window(self, where: Where) -> str | None:
        """按键进了哪个窗口：知道前台窗口就用它，否则沿用最后点过的窗口。"""
        if where is None:
            return self.focus if self._focused else None
        key = self._resolve(where)
        if key is not None:
            self.focus = key
        return key

    def on_char(self, ch: str, where: Where = None, ime: bool = False) -> None:
        """ime：此刻开着中文输入法——录到的是按键，不是最终文字。"""
        key = self._key_window(where)
        if key is None:
            return
        win = self._win(key)
        last = self.steps[-1] if self.steps else None
        if isinstance(last, TypeText) and last.win == win:
            last.text += ch
            last.typed_with_ime = last.typed_with_ime or ime
            self._touch(self.now())
        else:
            self._push(TypeText(id="", win=win, text=ch, typed_with_ime=ime))

    def on_combo(self, combo: str, where: Where = None) -> None:
        if normalize_combo(combo) in self.ignore_combos:
            return
        key = self._key_window(where)
        if key is None:
            return
        win = self._win(key)
        t = self.now()
        last = self.steps[-1] if self.steps else None
        if (isinstance(last, Key) and last.combo == combo and last.win == win and self._last_key_at is not None
                and t - self._last_key_at <= KEY_REPEAT_SEC and last.times < 500):
            last.times += 1
            self._last_key_at = t
            self._touch(t)
            return
        self._push(Key(id="", win=win, combo=combo), t)
        self._last_key_at = t

    def build(self, name: str, window: Window) -> Script:
        window = window.model_copy(update={"scale": self.geo.scale})
        return Script(name=name, window=window, windows=self.windows, steps=self.steps)


def guess_send_step(steps: list) -> int | None:
    """录完后猜哪一步是「发送」：最后一次输入之后，紧跟的第一次点击或回车。猜到就返回下标。"""
    last_type = max((i for i, s in enumerate(steps) if isinstance(s, TypeText)), default=None)
    if last_type is None:
        return None
    for i in range(last_type + 1, len(steps)):
        s = steps[i]
        if isinstance(s, Wait):
            continue
        if (isinstance(s, Click) and not s.hold) or (isinstance(s, Key) and s.combo in ("enter", "return")):
            return i
        return None
    return None


def listen(
    builder: RecordingBuilder,
    stop: threading.Event,
    to_points: Callable[[float, float], tuple[float, float]],
    *,
    locate: Callable[[float, float], Where] | None = None,
    foreground: Callable[[], Where] | None = None,
    ime: Callable[[], bool] | None = None,
    include_injected: bool = False,
) -> None:
    """用 pynput 监听真实输入喂给 builder，直到 stop 被置位。to_points 把系统输入坐标换成点。

    locate(x, y)：系统输入坐标处是哪个窗口；foreground()：前台是哪个窗口。不传就只认主窗口。

    include_injected=True 时程序自己发出的键鼠也会录进去——只用于「录制 → 回放」的自测，正常录制不要打开。
    """
    from pynput import keyboard, mouse

    modifiers: set[str] = set()
    buttons = {mouse.Button.left: "left", mouse.Button.right: "right", mouse.Button.middle: "middle"}
    fg_cache: dict[str, object] = {"at": 0.0, "where": None, "ime": False}

    def at(x, y) -> Where:
        return locate(x, y) if locate else None

    def front() -> Where:
        # 打字时每个键都查一次太浪费：0.3 秒内复用
        if foreground is None:
            return None
        now = time.monotonic()
        if now - fg_cache["at"] > 0.3:
            fg_cache["where"], fg_cache["at"] = foreground(), now
            fg_cache["ime"] = bool(ime and ime())
        return fg_cache["where"]

    def safe(fn):
        # 回调里抛异常会让 pynput 的监听线程直接退出，后面的操作就全录不到了：只记日志，不往外抛
        def wrapper(*args, **kwargs):
            if kwargs.get("injected") and not include_injected:
                return None
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                log.exception("录制时处理输入事件出错")
            return None

        return wrapper

    key_name = key_to_name  # Windows 上 Ctrl+Alt+字母 只有键码没有字符，也能认出来

    @safe
    def on_press(key, injected=False):
        name = key_name(key)
        if name is None:
            return
        if name == "space":
            name = " "
        if name in _MODIFIERS:
            modifiers.add(name)
        elif len(name) == 1 and not (modifiers - {"shift"}):
            where = front()
            builder.on_char(name, where, fg_cache["ime"])
        else:
            mods = _ordered(modifiers)
            if len(name) == 1:  # 带修饰键的字符：shift 已体现在字符本身
                mods = [m for m in mods if m != "shift"]
            builder.on_combo("+".join([*mods, name.lower()]), front())

    @safe
    def on_release(key, injected=False):
        if key_name(key) in _MODIFIERS:
            modifiers.discard(key_name(key))

    @safe
    def on_click(x, y, button, pressed, injected=False):
        name = buttons.get(button)
        if name is None:  # 侧键等不支持
            return
        px, py = to_points(x, y)
        if pressed:
            fg_cache["at"] = 0.0  # 点击可能换了前台窗口
            builder.on_mouse_down(px, py, name, set(modifiers), at(x, y))
        else:
            builder.on_mouse_up(px, py, name)

    @safe
    def on_scroll(x, y, dx, dy, injected=False):
        if dx or dy:
            builder.on_scroll(*to_points(x, y), -dy, dx, at(x, y))  # pynput：dy 正数向上

    def kw(fn, n):
        # pynput ≥ 1.8 在 n 个常规参数之后多传一个 injected；转成关键字参数交给 safe 判断。
        # 不能看最后一个参数是不是 bool：鼠标的 pressed 本身就是 bool。
        def call(*args):
            injected = bool(args[n]) if len(args) > n else False
            return fn(*args[:n], injected=injected)

        return call

    with (
        keyboard.Listener(on_press=kw(on_press, 1), on_release=kw(on_release, 1)),
        mouse.Listener(on_click=kw(on_click, 4), on_scroll=kw(on_scroll, 4)),
    ):
        stop.wait()
