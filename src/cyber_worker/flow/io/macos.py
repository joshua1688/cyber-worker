"""macOS：screencapture 截图、osascript 定位窗口，键鼠走 pynput。

需要在「系统设置 → 隐私与安全性」中给终端（或 Python）授予：屏幕录制、辅助功能、输入监控。
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

import numpy as np

from cyber_worker.flow.io.base import CalibrationError, WindowGeometry, WindowInfo
from cyber_worker.flow.io.pynput_input import PynputInput
from cyber_worker.flow.schema import Window
from cyber_worker.flow.screenshot.storage import read_image

# 进程名和标题直接拼进 AppleScript，只允许安全字符，防止脚本注入
_APP_NAME = re.compile(r"^[\w .\-]+$")
_TITLE = re.compile(r'^[^"\\]+$')


def _osascript(flow: str) -> str:
    result = subprocess.run(["osascript", "-e", flow], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise CalibrationError(result.stderr.strip() or "osascript 执行失败")
    return result.stdout.strip()


class MacScreenIO(PynputInput):
    input_factor = 1.0  # macOS 上 pynput 坐标就是点

    def __init__(self, *, settle: float = 0.4) -> None:
        super().__init__()
        self._settle = settle

    def list_windows(self) -> list[WindowInfo]:
        out = _osascript(
            'tell application "System Events"\n'
            '  set out to ""\n'
            '  repeat with p in (every process whose background only is false)\n'
            '    set pname to name of p\n'
            '    repeat with w in (every window of p)\n'
            '      try\n'
            '        set {ww, hh} to size of w\n'
            '        set out to out & pname & tab & (name of w) & tab & ww & tab & hh & linefeed\n'
            '      end try\n'
            '    end repeat\n'
            '  end repeat\n'
            '  return out\n'
            'end tell'
        )
        windows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            app, title, w, h = parts[0], parts[1], int(float(parts[2])), int(float(parts[3]))
            if w >= 200 and h >= 150 and _APP_NAME.match(app):
                windows.append(WindowInfo(app=app, title=title if title != "missing value" else "",
                                          width=w, height=h))
        return windows

    def window_at(self, x: float, y: float) -> WindowInfo | None:
        # macOS 上点击会把那个窗口带到最前，直接取最前的窗口
        time.sleep(0.4)
        out = _osascript(
            'tell application "System Events"\n'
            '  set p to first process whose frontmost is true\n'
            '  set w to front window of p\n'
            '  set {ww, hh} to size of w\n'
            '  return (name of p) & tab & (name of w) & tab & ww & tab & hh\n'
            'end tell'
        )
        parts = out.split("\t")
        if len(parts) != 4 or not _APP_NAME.match(parts[0]):
            return None
        title = parts[1] if parts[1] != "missing value" else ""
        return WindowInfo(app=parts[0], title=title, width=int(float(parts[2])), height=int(float(parts[3])))

    # 录制时判断「点到 / 打字到了哪个窗口」：Quartz 的窗口列表按从前到后排好，查起来快，不用 osascript
    def _onscreen(self):
        import Quartz

        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
            if w.get("kCGWindowLayer", 1) != 0:  # 只要普通窗口（菜单栏、Dock 等层级不为 0）
                continue
            b = w.get("kCGWindowBounds") or {}
            app = w.get("kCGWindowOwnerName") or ""
            if not _APP_NAME.match(app) or b.get("Width", 0) < 200 or b.get("Height", 0) < 150:
                continue
            yield app, w.get("kCGWindowName") or "", b

    def _scale(self) -> float:
        from AppKit import NSScreen

        return float(NSScreen.mainScreen().backingScaleFactor())

    def _pack(self, app, title, b):
        info = WindowInfo(app=app, title=title, width=int(b["Width"]), height=int(b["Height"]))
        return info, WindowGeometry(b["X"], b["Y"], b["Width"], b["Height"], scale=self._scale())

    def window_geometry_at(self, x: float, y: float):
        for app, title, b in self._onscreen():
            if b["X"] <= x < b["X"] + b["Width"] and b["Y"] <= y < b["Y"] + b["Height"]:
                return self._pack(app, title, b)
        return None

    def foreground(self):
        for app, title, b in self._onscreen():  # 最前面的普通窗口
            return self._pack(app, title, b)
        return None

    def ime_active(self) -> bool:
        # TODO(macOS 实机)：用 TISCopyCurrentKeyboardInputSource 判断当前是不是中文输入模式
        return False

    def held_modifiers(self) -> set[str]:
        # TODO(macOS 实机)：用 CGEventSourceFlagsState 读修饰键
        return set()

    def chime(self, kind: str) -> None:
        from cyber_worker.flow.io.chime import NOTES, chime_file

        if kind in NOTES:  # 和 Windows 播同一段合成的提示音
            subprocess.Popen(["afplay", str(chime_file(kind))],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def locate_window(self, window: Window, avoid: Iterable[str] = ()) -> WindowGeometry:
        if not _APP_NAME.match(window.app) or (window.title and not _TITLE.match(window.title)):
            raise CalibrationError(f"窗口名不合法：{window.app!r} / {window.title!r}")
        target = (f'(first window whose name contains "{window.title}")'
                  if window.title else "window 1")
        out = _osascript(  # 不 activate、不 AXRaise、不改尺寸；找不到时 _osascript 抛 CalibrationError
            f'tell application "System Events" to tell process "{window.app}"\n'
            f"  get (position of {target}) & (size of {target})\n"
            f"end tell"
        )
        x, y, w, h = (float(v) for v in out.split(","))
        shot = self.capture(WindowGeometry(x, y, w, h, scale=1.0))
        return WindowGeometry(x, y, w, h, scale=shot.shape[1] / w)

    def calibrate(self, window: Window, avoid: Iterable[str] = ()) -> WindowGeometry:
        # TODO(macOS)：没记标题、同一个程序开着好几个窗口时，照 pick_window 避开 avoid、挑尺寸最接近的
        if not _APP_NAME.match(window.app):
            raise CalibrationError(f"进程名不合法：{window.app!r}")
        if window.title and not _TITLE.match(window.title):
            raise CalibrationError(f"窗口标题不能包含引号或反斜杠：{window.title!r}")
        _osascript(f'tell application "{window.app}" to activate')
        time.sleep(self._settle)
        target = (f'(first window whose name contains "{window.title}")'
                  if window.title else "window 1")
        out = _osascript(
            f'tell application "System Events" to tell process "{window.app}"\n'
            f"  set w to {target}\n"
            f'  perform action "AXRaise" of w\n'
            f"  set size of w to {{{window.width}, {window.height}}}\n"
            f"  get (position of w) & (size of w)\n"
            f"end tell"
        )
        x, y, w, h = (float(v) for v in out.split(","))
        if (w, h) != (window.width, window.height):
            raise CalibrationError(
                f"窗口尺寸校准失败：期望 {window.width}x{window.height}，实际 {w:.0f}x{h:.0f}"
            )
        time.sleep(self._settle)
        shot = self.capture(WindowGeometry(x, y, w, h, scale=1.0))
        return WindowGeometry(x, y, w, h, scale=shot.shape[1] / w)

    def scroll(self, dy: int, dx: int = 0) -> None:
        # 直接发「行」单位的滚轮事件，和录制时监听到的单位一致；
        # 投递到 session 层，与监听所在的层相同，系统「自然滚动」的方向处理才会一致
        import Quartz

        ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, -dy, -dx)
        Quartz.CGEventPost(Quartz.kCGSessionEventTap, ev)

    def capture(self, geo: WindowGeometry) -> np.ndarray:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            region = f"{geo.x:.0f},{geo.y:.0f},{geo.width:.0f},{geo.height:.0f}"
            subprocess.run(["screencapture", "-x", "-t", "png", f"-R{region}", str(path)],
                           check=True, timeout=10)
            return read_image(path)
