"""全局快捷键匹配：Windows 上 Ctrl+Alt+字母 只有键码没有字符；按住不放只触发一次；默认忽略程序注入。"""

import sys

import pytest
from pynput.keyboard import Key, KeyCode

from cyber_worker.macro.io.pynput_input import HotkeyListener, key_to_name, normalize_combo


def test_normalize_combo():
    assert normalize_combo("Alt+Ctrl+R") == "ctrl+alt+r"
    assert normalize_combo("Control+Shift+Option+.") == "ctrl+shift+alt+."
    assert normalize_combo("win+e") == "cmd+e"


def test_key_names():
    assert key_to_name(KeyCode.from_char("a")) == "a"
    assert key_to_name(KeyCode.from_char("\x16")) == "v"  # 按住 ctrl 时的控制字符
    assert key_to_name(Key.ctrl_l) == "ctrl"
    assert key_to_name(Key.enter) == "enter"


def tap(listener, *keys, injected=False):
    for k in keys:
        listener.on_press(k, injected)
    for k in reversed(keys):
        listener.on_release(k, injected)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 虚拟键码")
def test_ctrl_alt_letter_with_only_vk_code():
    hits = []
    hl = HotkeyListener({"ctrl+alt+r": lambda: hits.append("rec")})
    tap(hl, Key.ctrl_l, Key.alt_l, KeyCode.from_vk(82))  # Windows 实测：R 只有键码 82
    assert hits == ["rec"]


def test_held_key_fires_once_and_injected_is_ignored():
    hits = []
    hl = HotkeyListener({"ctrl+alt+shift+.": lambda: hits.append("panic")})
    hl.on_press(Key.ctrl_l)
    hl.on_press(Key.alt_l)
    hl.on_press(Key.shift)
    for _ in range(5):  # 按住不放：系统自动重复
        hl.on_press(KeyCode.from_char("."))
    hl.on_release(KeyCode.from_char("."))
    hl.on_press(KeyCode.from_char("."))  # 松开后再按：再触发一次
    assert hits == ["panic", "panic"]

    hits.clear()
    tap(hl, Key.ctrl_l, Key.alt_l, Key.shift, KeyCode.from_char("."), injected=True)
    assert hits == []  # 程序自己发的键不触发


def test_other_combos_do_not_fire():
    hits = []
    hl = HotkeyListener({"ctrl+alt+r": lambda: hits.append(1)})
    tap(hl, Key.ctrl_l, KeyCode.from_char("r"))
    tap(hl, KeyCode.from_char("r"))
    tap(hl, Key.ctrl_l, Key.alt_l, KeyCode.from_char("t"))
    assert hits == []
