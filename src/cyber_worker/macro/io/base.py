"""系统 IO 接口。runner、executor、recorder 只依赖这里，具体实现在 macos.py，测试用假实现。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from cyber_worker.macro.schema import Window


@dataclass(frozen=True)
class WindowGeometry:
    x: float  # 窗口左上角在屏幕上的位置（点）
    y: float
    width: float
    height: float
    scale: float  # 当前屏幕缩放倍率：截图像素 = 点 × scale
    # 系统的窗口句柄（Windows 的 HWND；0 = 不知道）：有它就能截窗口自己的内容，被别的窗口挡住也不怕
    handle: int = field(default=0, compare=False)

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        return self.x + x, self.y + y

    def to_window(self, x: float, y: float) -> tuple[float, float] | None:
        """屏幕坐标 → 窗口坐标；落在窗口外返回 None。"""
        wx, wy = x - self.x, y - self.y
        if 0 <= wx < self.width and 0 <= wy < self.height:
            return wx, wy
        return None


class CalibrationError(RuntimeError):
    pass


# 标题里会变的部分：QQ「…等2个会话」、未读数「(3) 」
_TITLE_NOISE = [re.compile(r"(…|\.{3})?等\d+个会话$"), re.compile(r"^\(\d+\)\s*")]


def stable_title(title: str) -> str:
    """去掉标题里会随时间变化的部分，下次还能按标题认出这个窗口。"""
    for pat in _TITLE_NOISE:
        title = pat.sub("", title)
    return title.strip()


@dataclass(frozen=True)
class WindowInfo:
    """当前打开的一个窗口，新建宏时让用户直接点选，不用记进程名。"""

    app: str  # 进程名，可直接用作 Window.app
    title: str
    width: int  # 点
    height: int
    minimized: bool = False


class ScreenIO(Protocol):
    def list_windows(self) -> list[WindowInfo]:
        """当前打开的可见窗口（排除太小的工具窗口），最上层的在前。"""
        ...

    def window_at(self, x: float, y: float) -> WindowInfo | None:
        """用户刚点击的位置（系统输入坐标）所在的窗口。macOS 上取点击后的最前窗口。"""
        ...

    def window_geometry_at(self, x: float, y: float) -> tuple[WindowInfo, WindowGeometry] | None:
        """系统输入坐标处最上层的窗口及它现在的位置（录制时判断点到了哪个窗口，不调整窗口）。"""
        ...

    def foreground(self) -> tuple[WindowInfo, WindowGeometry] | None:
        """当前前台窗口及位置（录制时判断键盘输入进了哪个窗口）。"""
        ...

    def ime_active(self) -> bool:
        """前台窗口的中文（等）输入法是否处于「输入母语」模式：此时按键会先进输入法，录到的不是最终文字。"""
        ...

    def held_modifiers(self) -> set[str]:
        """此刻按着的修饰键（ctrl / shift / alt / cmd）。录制启动时用：按快捷键开始录制时 Alt 还没松开，
        监听看不到它按下，不问一下就会把后面的字母当成普通输入录进去。"""
        ...

    def chime(self, kind: str) -> None:
        """提示音，不阻塞。kind：start（开始录制）/ stop（停止录制）。人在目标窗口里也能知道快捷键生效了。"""
        ...

    def calibrate(self, window: Window) -> WindowGeometry:
        """激活窗口并调整到录制时的尺寸；做不到就抛 CalibrationError。"""
        ...

    def locate_window(self, window: Window) -> WindowGeometry:
        """只找窗口现在在哪，不激活、不改尺寸（悬停弹出的资料卡：一抢焦点就消失）；找不到抛 CalibrationError。"""
        ...

    def capture(self, geo: WindowGeometry) -> np.ndarray:
        """窗口截图，BGR，单位像素。"""
        ...

    def input_to_points(self, x: float, y: float) -> tuple[float, float]:
        """键鼠监听拿到的系统坐标 → 点（录制时用）。"""
        ...

    def mouse_position(self) -> tuple[float, float]: ...

    def move_mouse(self, x: float, y: float) -> None:
        """屏幕坐标（点）。"""
        ...

    def click(self, button: str) -> None: ...

    def mouse_down(self, button: str) -> None:
        """按下不放（长按、拖动用）。调用方必须保证最后调用 mouse_up。"""
        ...

    def mouse_up(self, button: str) -> None: ...

    def type_char(self, ch: str) -> None: ...

    def press_combo(self, combo: str) -> None:
        """如 "enter"、"cmd+v"、"backspace"。"""
        ...

    def key_down(self, name: str) -> None:
        """按住一个键不放（Ctrl+点击这类用）。调用方必须保证最后调用 key_up。"""
        ...

    def key_up(self, name: str) -> None: ...

    def scroll(self, dy: int, dx: int = 0) -> None:
        """滚轮格数：dy 正数向下，dx 正数向右。"""
        ...

    def clear_field(self) -> None:
        """清空当前输入框：全选（macOS ⌘A / Windows Ctrl+A）后删除。"""
        ...


class ActivityMonitor(Protocol):
    def seconds_since_user_input(self) -> float:
        """距离你上一次真实键鼠输入过了多久（忽略程序注入的事件）。"""
        ...
