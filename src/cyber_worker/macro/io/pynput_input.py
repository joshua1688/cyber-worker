"""基于 pynput 的键鼠操作、真实输入监听和全局热键，macOS 和 Windows 共用。

坐标约定：对外一律是「点」。pynput 用的是系统输入坐标——macOS 上就是点，
Windows（进程开启 DPI 感知后）是物理像素，两者之间差一个 input_factor。
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable

_SPECIAL_KEYS = {
    "enter": "enter", "return": "enter", "tab": "tab", "space": "space",
    "backspace": "backspace", "delete": "delete", "esc": "esc", "escape": "esc",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "page_up": "page_up", "page_down": "page_down",
    "cmd": "cmd", "command": "cmd", "win": "cmd", "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt", "shift": "shift",
    **{f"f{i}": f"f{i}" for i in range(1, 13)},
}


def _key(name: str):
    from pynput.keyboard import Key, KeyCode

    name = name.lower()
    if name in _SPECIAL_KEYS:
        return getattr(Key, _SPECIAL_KEYS[name])
    if len(name) == 1:
        return KeyCode.from_char(name)
    raise ValueError(f"未知按键：{name}")


def _button(name: str):
    from pynput.mouse import Button

    return {"left": Button.left, "right": Button.right, "middle": Button.middle}[name]


class PynputInput:
    """ScreenIO 中与键鼠相关的那一半。子类负责窗口与截图，并维护 input_factor。"""

    input_factor: float = 1.0  # 输入坐标 = 点 × input_factor

    def __init__(self) -> None:
        from pynput import keyboard, mouse

        self._mouse = mouse.Controller()
        self._kb = keyboard.Controller()

    def input_to_points(self, x: float, y: float) -> tuple[float, float]:
        return x / self.input_factor, y / self.input_factor

    def mouse_position(self) -> tuple[float, float]:
        x, y = self._mouse.position
        return self.input_to_points(float(x), float(y))

    def move_mouse(self, x: float, y: float) -> None:
        f = self.input_factor
        self._mouse.position = (round(x * f), round(y * f))

    def click(self, button: str) -> None:
        self._mouse.click(_button(button))

    def mouse_down(self, button: str) -> None:
        self._mouse.press(_button(button))

    def mouse_up(self, button: str) -> None:
        self._mouse.release(_button(button))

    def type_char(self, ch: str) -> None:
        self._kb.type(ch)

    def press_combo(self, combo: str) -> None:
        keys = [_key(k) for k in combo.split("+")]
        pressed = []
        try:
            for k in keys:
                self._kb.press(k)
                pressed.append(k)
        finally:
            for k in reversed(pressed):
                self._kb.release(k)

    def key_down(self, name: str) -> None:
        self._kb.press(_key(name))

    def key_up(self, name: str) -> None:
        self._kb.release(_key(name))

    def scroll(self, dy: int, dx: int = 0) -> None:
        # pynput：dy 正数向上、dx 正数向右；我们约定 dy 正数向下
        self._mouse.scroll(dx, -dy)

    def clear_field(self) -> None:
        self.press_combo("cmd+a" if sys.platform == "darwin" else "ctrl+a")
        self.press_combo("backspace")


class InputActivityMonitor:
    """监听真实键鼠输入，忽略程序自己注入的事件（pynput ≥ 1.8 的 injected 参数）。"""

    def __init__(self) -> None:
        from pynput import keyboard, mouse

        self._last = time.monotonic()
        self._mouse = mouse.Listener(
            on_move=self._on_event, on_click=self._on_event, on_scroll=self._on_event
        )
        self._kb = keyboard.Listener(on_press=self._on_event)
        self._mouse.start()
        self._kb.start()

    def _on_event(self, *args) -> None:
        if args and args[-1] is True:  # injected
            return
        self._last = time.monotonic()

    def seconds_since_user_input(self) -> float:
        return time.monotonic() - self._last

    def stop(self) -> None:
        self._mouse.stop()
        self._kb.stop()


def wait_for_click(timeout: float) -> tuple[float, float] | None:
    """等用户的下一次真实左键点击（忽略程序注入的），返回系统输入坐标；超时返回 None。"""
    from pynput import mouse

    got: dict[str, tuple[float, float]] = {}
    done = threading.Event()

    def on_click(x, y, button, pressed, injected=False):
        if pressed and not injected and button == mouse.Button.left:
            got["pos"] = (float(x), float(y))
            done.set()
            return False
        return None

    with mouse.Listener(on_click=on_click):
        done.wait(timeout)
    return got.get("pos")


def to_pynput_hotkey(combo: str) -> str:
    """"ctrl+alt+cmd+." → "<ctrl>+<alt>+<cmd>+."。"""
    parts = []
    for k in combo.lower().split("+"):
        parts.append(f"<{_SPECIAL_KEYS[k]}>" if k in _SPECIAL_KEYS else k)
    return "+".join(parts)


MODIFIERS = ("ctrl", "shift", "alt", "cmd")  # 也是组合键里修饰键的固定顺序

# Windows 虚拟键码 → 键名。按住 Ctrl+Alt 时，pynput 给出的字母键只有键码、没有字符（比如 R 是 <82>）
_WIN_VK = {
    **{0x30 + i: str(i) for i in range(10)},
    **{0x41 + i: chr(0x61 + i) for i in range(26)},
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`",
    0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}


def normalize_combo(combo: str) -> str:
    """"Alt+Ctrl+R" → "ctrl+alt+r"：修饰键按固定顺序、全小写，录制和快捷键匹配都用这个写法。"""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    alias = {"control": "ctrl", "option": "alt", "command": "cmd", "win": "cmd"}
    parts = [alias.get(p, p) for p in parts]
    mods = set(p for p in parts if p in MODIFIERS)
    return "+".join([*(m for m in MODIFIERS if m in mods), *(p for p in parts if p not in MODIFIERS)])


def key_to_name(key) -> str | None:
    """pynput 的按键 → 键名：普通字符原样（区分大小写）；特殊键用名字（enter、cmd…）；拿不到就 None。"""
    from pynput import keyboard

    if isinstance(key, keyboard.KeyCode):
        ch = key.char
        if ch is not None and len(ch) == 1 and ord(ch) < 32:  # 按住 ctrl 时给出控制字符（ctrl+v → ）
            return chr(ord(ch) + 96)
        if ch is not None:
            return ch
        if sys.platform == "win32":
            return _WIN_VK.get(getattr(key, "vk", None))
        return None
    return key.name.split("_")[0]  # ctrl_l → ctrl，cmd_r → cmd


class HotkeyListener:
    """全局快捷键：自己跟踪修饰键、用 key_to_name 解析，Windows 上 Ctrl+Alt+字母 也能认。

    默认忽略程序注入的按键（自己发出的键不会误触发）；include_injected 只给自测用。
    按住不放时系统的自动重复只触发一次，松开后才能再触发。
    """

    def __init__(self, bindings: dict[str, Callable[[], None]], *, include_injected: bool = False) -> None:
        self.bindings = {normalize_combo(c): cb for c, cb in bindings.items()}
        self.include_injected = include_injected
        self._mods: set[str] = set()
        self._fired: set[str] = set()

    def on_press(self, key, injected: bool = False) -> None:
        if injected and not self.include_injected:
            return
        name = key_to_name(key)
        if not name:
            return
        if name in MODIFIERS:
            self._mods.add(name)
            return
        combo = normalize_combo("+".join([*self._mods, name.lower()]))
        cb = self.bindings.get(combo)
        if cb and combo not in self._fired:
            self._fired.add(combo)
            cb()

    def on_release(self, key, injected: bool = False) -> None:
        if injected and not self.include_injected:
            return
        name = key_to_name(key)
        if name in MODIFIERS:
            self._mods.discard(name)
        self._fired.clear()

    def start(self):
        from pynput import keyboard

        listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release)
        listener.start()
        return listener


def listen_hotkey(combo: str, callback: Callable[[], None]) -> threading.Thread:
    return listen_hotkeys({combo: callback})


def listen_hotkeys(bindings: dict[str, Callable[[], None]], *, include_injected: bool = False):
    """一次注册多个全局快捷键。macOS 上 Option 会把字母变成特殊符号，用 pynput 自带的（按键码匹配）。"""
    if sys.platform == "darwin" and not include_injected:
        from pynput import keyboard

        listener = keyboard.GlobalHotKeys({to_pynput_hotkey(c): cb for c, cb in bindings.items()})
        listener.start()
        return listener
    return HotkeyListener(bindings, include_injected=include_injected).start()
