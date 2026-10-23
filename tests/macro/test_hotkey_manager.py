"""快捷键冲突检查、被占用时自动换备选、改键前放开 / 之后恢复。"""

from fakes import FakeHotkeys

from cyber_worker.config import Config, save_value
from cyber_worker.macro.io.hotkeys import HotkeyManager, check_combo


def status(combo, **kw):
    return check_combo(combo, platform="win32", **kw).status


def test_check_combo_rules():
    assert status("alt+r", backend=FakeHotkeys()) == "ok"
    assert status("Alt+R", backend=FakeHotkeys(taken={"alt+r"})) == "taken"
    assert status("alt+r", backend=FakeHotkeys(taken={"alt+r"}), own={"alt+r"}) == "ok"  # 我们自己占着
    assert status("alt+r", others={"alt+r"}) == "duplicate"
    assert status("ctrl+c") == "reserved"
    assert status("alt+tab") == "reserved"
    assert status("Alt+Shift+Tab") == "reserved"
    assert status("win+r") == "reserved"
    assert status("r") == "invalid"  # 单个字母会影响打字
    assert status("ctrl+alt") == "invalid"
    assert status("f9", backend=FakeHotkeys()) == "ok"  # 功能键可以单独用
    assert status("alt+r") == "unknown"  # 查不出占用（macOS）：不拦着
    assert check_combo("cmd+space", platform="darwin").status == "reserved"


def test_falls_back_to_first_free_candidate():
    calls = []
    backend = FakeHotkeys(taken={"alt+r"})
    hk = HotkeyManager(backend, platform="win32")
    hk.apply({"record": "alt+r", "panic": "ctrl+alt+shift+."},
             {"record": lambda: calls.append("rec"), "panic": lambda: calls.append("panic")})
    # 组合键统一写成 ctrl+shift+alt+cmd 的顺序
    assert hk.active == {"record": "shift+alt+r", "panic": "ctrl+shift+alt+."}
    assert "alt+r" in hk.notes["record"] and "shift+alt+r" in hk.notes["record"]
    assert "panic" not in hk.notes
    backend.press("shift+alt+r")
    backend.press("ctrl+shift+alt+.")
    assert calls == ["rec", "panic"]


def test_reserved_configured_key_also_falls_back():
    hk = HotkeyManager(FakeHotkeys(), platform="win32")
    hk.apply({"record": "ctrl+c"}, {"record": lambda: None})
    assert hk.active == {"record": "alt+r"} and "ctrl+c" in hk.notes["record"]


def test_suspend_releases_and_resume_rebinds():
    backend = FakeHotkeys()
    hk = HotkeyManager(backend, platform="win32")
    hk.apply({"record": "alt+r"}, {"record": lambda: None})
    hk.suspend(timeout=60)
    assert backend.bound == {} and hk.suspended
    assert hk.check("record", "alt+r").status == "ok"  # 自己的键不算冲突
    hk.resume()
    assert list(backend.bound) == ["alt+r"] and not hk.suspended


def test_suspend_times_out():
    import time

    backend = FakeHotkeys()
    hk = HotkeyManager(backend, platform="win32")
    hk.apply({"record": "alt+r"}, {"record": lambda: None})
    hk.suspend(timeout=0.05)
    time.sleep(0.3)
    assert list(backend.bound) == ["alt+r"] and not hk.suspended


def test_save_value_keeps_comments(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[executor]\ndry_run = false\n\n[studio]\n# record_hotkey = "alt+r"  # 录制开关\n\n'
                 '[paths]\nuser_data = "user_data"\n', encoding="utf-8")
    save_value(p, "studio", "record_hotkey", "ctrl+alt+f9")
    save_value(p, "executor", "panic_hotkey", "ctrl+alt+f12")
    save_value(p, "llm", "provider", "local")
    text = p.read_text(encoding="utf-8")
    assert 'record_hotkey = "ctrl+alt+f9"  # 录制开关' in text
    c = Config.load(p)
    assert c.studio.record_hotkey == "ctrl+alt+f9" and c.executor.panic_hotkey == "ctrl+alt+f12"
    assert c.paths.user_data.name == "user_data" and c.executor.dry_run is False
    save_value(p, "studio", "record_hotkey", "alt+r")  # 再改一次：改原来那行，不重复
    assert p.read_text(encoding="utf-8").count("record_hotkey") == 1


def test_save_value_types_and_commented_section(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[studio]                        # 设置里都能改\n# theme = "dark"  # 外观\n', encoding="utf-8")
    save_value(p, "studio", "theme", "light")
    save_value(p, "studio", "record_delays", False)
    save_value(p, "studio", "run_idle_sec", 2.5)
    text = p.read_text(encoding="utf-8")
    assert text.count("[studio]") == 1 and 'theme = "light"  # 外观' in text
    c = Config.load(p)
    assert c.studio.theme == "light" and c.studio.record_delays is False and c.studio.run_idle_sec == 2.5
