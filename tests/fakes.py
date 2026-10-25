"""测试用假 IO：记录所有操作，截图由测试指定。"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from cyber_worker.flow.io.base import CalibrationError, WindowGeometry, WindowInfo
from cyber_worker.flow.schema import Window


def blank(w: int = 400, h: int = 300) -> np.ndarray:
    return np.full((h, w, 3), 255, dtype=np.uint8)


class FakeIO:
    def __init__(self, screen: Callable[[], np.ndarray] | None = None, *, origin=(100, 50)) -> None:
        self.screen = screen or blank
        self.origin = origin
        self.calls: list[tuple] = []
        self.pos = (0.0, 0.0)
        self.fail_calibrate: str | None = None
        self.calibrations = 0
        self.avoided: list[tuple[str, ...]] = []  # 每次校准时要避开的标题
        self.origins: dict[str, tuple] = {}  # 窗口标题 → 屏幕位置（多窗口测试用）
        self.missing: dict[str, int] = {}  # 窗口标题 → 还要「找不到」几次（模拟窗口还没打开）
        self.calibrated: list[str] = []

    def list_windows(self):
        return [WindowInfo("Demo", "Demo 窗口", 400, 300), WindowInfo("QQ", "会话", 900, 600),
                WindowInfo("QQ", "QQ", 320, 740), WindowInfo("chrome", "流程设计 · 赛博打工人 - Google Chrome", 1200, 800)]

    def window_at(self, x, y):
        if x < 0:  # 约定：负坐标 = 点到了 studio 所在的浏览器
            return WindowInfo("chrome", "流程设计 · 赛博打工人 - Google Chrome", 1200, 800)
        return WindowInfo("QQ", "海绵宝宝大战…等2个会话", 748, 613)

    def window_geometry_at(self, x, y):
        return None  # 测试默认：都算主窗口

    def foreground(self):
        return None

    def ime_active(self):
        return False

    def held_modifiers(self):
        return set()

    def chime(self, kind):
        self.calls.append(("chime", kind))

    def calibrate(self, window: Window, avoid=()) -> WindowGeometry:
        self.calibrations += 1
        self.avoided.append(tuple(avoid))
        key = window.title or window.app
        if self.fail_calibrate:
            raise CalibrationError(self.fail_calibrate)
        if self.missing.get(key, 0) > 0:
            self.missing[key] -= 1
            raise CalibrationError(f"找不到 {key}")
        self.calibrated.append(key)
        return WindowGeometry(*self.origins.get(key, self.origin), window.width, window.height, scale=1.0)

    def locate_window(self, window: Window, avoid=()) -> WindowGeometry:
        key = window.title or window.app
        if self.missing.get(key, 0) > 0:
            self.missing[key] -= 1
            raise CalibrationError(f"找不到 {key}")
        self.calls.append(("locate", key))
        return WindowGeometry(*self.origins.get(key, self.origin), window.width, window.height, scale=1.0)

    def capture(self, geo: WindowGeometry) -> np.ndarray:
        return self.screen()

    def input_to_points(self, x, y):
        return x, y

    def mouse_position(self):
        return self.pos

    def move_mouse(self, x, y):
        self.pos = (x, y)

    def click(self, button):
        self.calls.append(("click", self.pos, button))

    def mouse_down(self, button):
        self.calls.append(("down", self.pos, button))

    def mouse_up(self, button):
        self.calls.append(("up", self.pos, button))

    def key_down(self, name):
        self.calls.append(("key_down", name))

    def key_up(self, name):
        self.calls.append(("key_up", name))

    def type_char(self, ch):
        self.calls.append(("char", ch))

    def press_combo(self, combo):
        self.calls.append(("combo", combo))

    def scroll(self, dy, dx=0):
        self.calls.append(("scroll", dy, dx))

    def clear_field(self):
        self.calls.append(("clear",))

    def typed(self) -> str:
        """按退格还原出最终输入的文本。"""
        out: list[str] = []
        for c in self.calls:
            if c[0] == "char":
                out.append(c[1])
            elif c == ("combo", "backspace") and out:
                out.pop()
            elif c == ("clear",):
                out.clear()
        return "".join(out)


class MemoryAudit:
    def __init__(self) -> None:
        self.entries = []

    def write(self, entry) -> None:
        self.entries.append(entry)


class FakeActivity:
    def __init__(self, idle: float = 1e9) -> None:
        self.idle = idle

    def seconds_since_user_input(self) -> float:
        return self.idle


class FakeHotkeys:
    """假的系统热键注册：taken 里的键算被别的程序占用；press() 模拟按下已注册的键。"""

    def __init__(self, taken=()) -> None:
        self.taken = set(taken)
        self.bound: dict = {}
        self.stops = 0

    def available(self, combo):
        return combo not in self.taken and combo not in self.bound

    def bind(self, bindings):
        self.bound = dict(bindings)
        return {c: ("已被其他程序占用" if c in self.taken else None) for c in bindings}

    def stop(self):
        self.bound = {}
        self.stops += 1

    def press(self, combo):
        self.bound[combo]()
