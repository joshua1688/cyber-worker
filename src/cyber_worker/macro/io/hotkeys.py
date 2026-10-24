"""全局快捷键管理：冲突检查、按候选自动避让、改键立即生效。

后端：Windows 用系统注册（WinHotkeys，独占、能查出被别的程序占用）；
macOS 用 pynput 监听（查不到别的程序占用，只能对照系统保留键）。
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from cyber_worker.config import ExecutorConfig, StudioConfig
from cyber_worker.macro.io.pynput_input import (
    MODIFIERS,
    listen_hotkeys,
    normalize_combo,
)

DEFAULTS = {"record": StudioConfig().record_hotkey, "dry_run": StudioConfig().dry_run_hotkey,
            "panic": ExecutorConfig().panic_hotkey}
# 默认键被占用时依次尝试
CANDIDATES = {
    "record": ["alt+r", "alt+shift+r", "ctrl+alt+f9", "ctrl+alt+f10", "ctrl+shift+f9"],
    "dry_run": ["alt+t", "alt+shift+t", "ctrl+alt+f8", "ctrl+shift+f8"],
    "panic": [DEFAULTS["panic"], "ctrl+alt+f12", "ctrl+shift+f12"],
}
LABELS = {"record": "开始 / 停止录制", "dry_run": "空跑当前宏", "panic": "急停"}

# 系统保留：按了会被系统拦走，或者会破坏日常操作（复制粘贴、切换窗口……）
_RESERVED_COMMON = {
    "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y", "ctrl+a", "ctrl+s", "ctrl+f", "ctrl+w", "ctrl+t",
    "ctrl+n", "ctrl+r", "ctrl+p", "ctrl+tab", "ctrl+shift+tab",
}
_RESERVED = {
    "win32": _RESERVED_COMMON | {
        "alt+tab", "alt+shift+tab", "alt+f4", "alt+esc", "alt+space", "ctrl+esc", "ctrl+shift+esc",
        "ctrl+alt+delete", "alt+enter",
    },
    "darwin": {c.replace("ctrl", "cmd") for c in _RESERVED_COMMON} | {
        "cmd+tab", "cmd+space", "cmd+q", "cmd+h", "cmd+m", "cmd+shift+3", "cmd+shift+4", "cmd+shift+5",
        "alt+cmd+esc", "ctrl+up", "ctrl+down", "ctrl+left", "ctrl+right", "ctrl+cmd+q", "ctrl+cmd+f",
    },
}
_RESERVED = {p: {normalize_combo(c) for c in combos} for p, combos in _RESERVED.items()}
_FUNCTION_KEYS = {f"f{i}" for i in range(1, 25)} | {"pause", "scroll_lock", "insert"}


class Backend(Protocol):
    def available(self, combo: str) -> bool | None:
        """True 空闲 / False 被占用 / None 查不出来。"""
        ...

    def bind(self, bindings: dict[str, Callable[[], None]]) -> dict[str, str | None]: ...

    def stop(self) -> None: ...


class PynputBackend:
    """macOS / 其他：被动监听键盘，查不出别的程序的占用。"""

    def __init__(self) -> None:
        self._listener = None

    def available(self, combo: str) -> bool | None:
        return None

    def bind(self, bindings: dict[str, Callable[[], None]]) -> dict[str, str | None]:
        self.stop()
        self._listener = listen_hotkeys(bindings)
        return {c: None for c in bindings}

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


class NullBackend:
    """不真的注册（测试、没有键盘监听的环境）。"""

    def available(self, combo: str) -> bool | None:
        return None

    def bind(self, bindings: dict[str, Callable[[], None]]) -> dict[str, str | None]:
        return {c: None for c in bindings}

    def stop(self) -> None:
        pass


def platform_backend() -> Backend:
    if sys.platform == "win32":
        from cyber_worker.macro.io.windows import WinHotkeys

        return WinHotkeys()
    return PynputBackend()


@dataclass
class Check:
    status: str  # ok / taken / reserved / duplicate / invalid / unknown
    message: str

    @property
    def blocking(self) -> bool:
        return self.status in ("taken", "reserved", "duplicate", "invalid")


def check_combo(combo: str, *, others: set[str] = frozenset(), own: set[str] = frozenset(),
                backend: Backend | None = None, platform: str = sys.platform) -> Check:
    """这个组合键能不能用。others：另一个快捷键正在用的；own：我们自己此刻注册着的（查占用时算空闲）。"""
    n = normalize_combo(combo or "")
    parts = n.split("+") if n else []
    keys = [p for p in parts if p not in MODIFIERS]
    mods = [p for p in parts if p in MODIFIERS]
    if not keys:
        return Check("invalid", "还没按下按键（只按修饰键不算）")
    if len(keys) > 1:
        return Check("invalid", "只能有一个普通按键")
    if not mods and keys[0] not in _FUNCTION_KEYS:
        return Check("invalid", "单独一个键会影响打字：请加上 Ctrl / Alt / Shift（功能键 F1–F24 除外）")
    if n in {normalize_combo(o) for o in others}:
        return Check("duplicate", "和另一个快捷键重复了")
    if platform == "win32" and "cmd" in mods:
        return Check("reserved", "Win 键组合是留给系统的")
    if n in _RESERVED.get(platform, set()):
        return Check("reserved", "这是系统或日常操作的常用键（复制、粘贴、切换窗口……），不能占用")
    if n in {normalize_combo(o) for o in own}:
        return Check("ok", "可以用（正在使用）")
    free = backend.available(n) if backend else None
    if free is False:
        return Check("taken", "已被其他程序占用")
    if free is None:
        return Check("unknown", "可以用（这个系统上查不到其他软件的占用，按下后没反应就换一个）")
    return Check("ok", "可以用")


@dataclass
class HotkeyManager:
    backend: Backend
    platform: str = sys.platform
    callbacks: dict[str, Callable[[], None]] = field(default_factory=dict)
    active: dict[str, str] = field(default_factory=dict)  # 名字 → 实际生效的组合键
    notes: dict[str, str] = field(default_factory=dict)  # 名字 → 自动避让的说明
    wanted: dict[str, str] = field(default_factory=dict)  # 名字 → 用户配置的组合键
    suspended: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _timer: threading.Timer | None = field(default=None, repr=False)

    def apply(self, wanted: dict[str, str], callbacks: dict[str, Callable[[], None]] | None = None) -> None:
        """按配置注册；配置的键用不了时按候选自动换一个能用的，并记下说明。"""
        with self._lock:
            if callbacks is not None:
                self.callbacks = callbacks
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._apply(wanted)

    def _apply(self, wanted: dict[str, str]) -> None:
        self.wanted = dict(wanted)
        self.backend.stop()  # 先全放掉，查占用时才不会把自己算成「被占用」
        chosen: dict[str, str] = {}
        why: dict[str, str] = {}  # 配置的键为什么用不了
        self.notes = {}
        for name, combo in wanted.items():
            tried = [combo, *[c for c in CANDIDATES.get(name, []) if normalize_combo(c) != normalize_combo(combo)]]
            for c in tried:
                result = check_combo(c, others=set(chosen.values()), backend=self.backend, platform=self.platform)
                if not result.blocking:
                    chosen[name] = normalize_combo(c)
                    break
                why.setdefault(name, result.message)
        results = self.backend.bind({combo: self.callbacks[name] for name, combo in chosen.items()})
        # 查的时候空闲、注册时却失败（极少：刚好被别的程序抢了）：这一项就不生效，记下来
        self.active = {name: combo for name, combo in chosen.items() if results.get(combo) is None}
        for name, combo in wanted.items():
            got = self.active.get(name)
            if got is None:
                self.notes[name] = f"「{LABELS[name]}」的快捷键 {combo} 和备选键都用不了，请在设置里换一个"
            elif got != normalize_combo(combo):
                self.notes[name] = f"{combo} {why.get(name, '已被占用')}，「{LABELS[name]}」已自动改用 {got}"
        self.suspended = False

    def check(self, name: str, combo: str, others: set[str] | None = None) -> Check:
        """others 不给就用另一个快捷键现在生效的键。"""
        if others is None:
            others = {c for n, c in self.active.items() if n != name}
        return check_combo(combo, others=others, own=set(self.active.values()), backend=self.backend,
                           platform=self.platform)

    def suspend(self, timeout: float = 30) -> None:
        """设置面板里录新快捷键时先放开，不然按下现在的键会被系统拦走、浏览器收不到。

        timeout 秒后自动恢复：浏览器中途关掉、没来得及说「恢复」也不会一直失灵。
        """
        with self._lock:
            self.backend.stop()
            self.suspended = True
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(timeout, self.resume)
            self._timer.daemon = True
            self._timer.start()

    def resume(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            if self.suspended:
                self._apply(self.wanted)

    def stop(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self.backend.stop()

    def effective(self, name: str) -> str:
        """现在实际生效的键（没生效时给配置里的，界面上至少有个提示）。"""
        return self.active.get(name) or normalize_combo(self.wanted.get(name, DEFAULTS[name]))
