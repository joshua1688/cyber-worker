"""录制：单击 / 双击 / 长按 / 拖动 / 修饰键 / 滚轮 / 连按 的判定。时间由测试手动拨。"""

from cyber_worker.macro.io.base import WindowGeometry
from cyber_worker.macro.recorder import RecordingBuilder


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(record_delays=False):
    clock = Clock()
    # 窗口在屏幕 (100, 50)，大小 400×300
    return RecordingBuilder(WindowGeometry(100, 50, 400, 300, 1.0), now=clock,
                            record_delays=record_delays), clock


def press(b, clock, x, y, *, hold=0.05, to=None, button="left", mods=()):
    b.on_mouse_down(x, y, button, mods)
    clock.t += hold
    tx, ty = to or (x, y)
    b.on_mouse_up(tx, ty, button)


def test_click_double_click_and_third_click():
    b, c = make()
    press(b, c, 200, 150)
    c.t += 0.1
    press(b, c, 201, 151)  # 0.15 秒内、1 点之内 → 双击
    c.t += 0.1
    press(b, c, 201, 151)  # 第三下：新的单击
    assert [(s.action, s.count) for s in b.steps] == [("click", 2), ("click", 1)]


def test_slow_second_click_is_not_double():
    b, c = make()
    press(b, c, 200, 150)
    c.t += 0.6
    press(b, c, 200, 150)
    assert [s.count for s in b.steps] == [1, 1]


def test_long_press():
    b, c = make()
    press(b, c, 200, 150, hold=1.2)
    (s,) = b.steps
    assert s.action == "click" and s.hold == 1.2 and (s.x, s.y) == (100, 100)


def test_long_press_never_merges_into_double_click():
    b, c = make()
    press(b, c, 200, 150)
    c.t += 0.05
    press(b, c, 200, 150, hold=0.8)
    assert [(s.count, s.hold) for s in b.steps] == [(1, 0.0), (1, 0.8)]


def test_drag_including_end_outside_window():
    b, c = make()
    press(b, c, 150, 100, hold=0.7, to=(600, 120))  # 拖到窗口外
    (s,) = b.steps
    assert s.action == "drag"
    assert (s.x, s.y, s.x2, s.y2) == (50, 50, 500, 70)
    assert s.duration == 0.7


def test_small_jitter_while_pressing_is_still_a_click():
    b, c = make()
    press(b, c, 200, 150, to=(203, 152))
    assert b.steps[0].action == "click"


def test_right_and_middle_buttons_and_modifiers():
    b, c = make()
    press(b, c, 200, 150, button="right")
    c.t += 1
    press(b, c, 220, 150, button="middle")
    c.t += 1
    press(b, c, 240, 150, mods={"shift", "ctrl"})
    assert [(s.button, s.modifiers) for s in b.steps] == [
        ("right", []), ("middle", []), ("left", ["ctrl", "shift"])]


def test_press_outside_window_is_ignored():
    b, c = make()
    press(b, c, 900, 900)
    assert b.steps == []


def test_wheel_merges_while_continuous():
    b, c = make()
    for _ in range(5):
        b.on_scroll(200, 150, 1)
        c.t += 0.1
    (s,) = b.steps
    assert (s.dy, s.dx) == (5, 0)


def test_wheel_pause_or_direction_change_starts_new_step():
    b, c = make(record_delays=True)
    b.on_scroll(200, 150, 1)
    c.t += 0.1
    b.on_scroll(200, 150, 1)
    c.t += 2.0  # 停了 2 秒
    b.on_scroll(200, 150, 1)
    c.t += 0.1
    b.on_scroll(200, 150, -1)  # 换方向
    assert [(s.action, getattr(s, "dy", None)) for s in b.steps] == [
        ("scroll", 2), ("wait", None), ("scroll", 1), ("scroll", -1)]


def test_fractional_touchpad_scroll_accumulates():
    b, c = make()
    for _ in range(10):
        b.on_scroll(200, 150, 0.25)  # 触控板：每次 1/4 格
        c.t += 0.02
    (s,) = b.steps
    assert s.dy == 2  # 10 × 0.25 = 2.5 → 2 整格，余 0.5 等后续


def test_horizontal_scroll():
    b, c = make()
    b.on_scroll(200, 150, 0, 3)
    assert (b.steps[0].dx, b.steps[0].dy) == (3, 0)


def test_held_key_auto_repeat_becomes_times():
    b, c = make()
    for _ in range(20):  # 按住退格：系统每 33ms 重复一次
        b.on_combo("backspace")
        c.t += 0.033
    c.t += 1
    b.on_combo("backspace")
    assert [(s.combo, s.times) for s in b.steps] == [("backspace", 20), ("backspace", 1)]


def test_delays_measured_from_end_of_previous_action():
    b, c = make(record_delays=True)
    press(b, c, 200, 150, hold=1.0)  # 长按 1 秒，结束于 t=1
    c.t += 0.5
    press(b, c, 250, 150)  # 开始于 t=1.5
    assert [s.action for s in b.steps] == ["click", "wait", "click"]
    assert b.steps[1].seconds == 0.5


def test_hotkeys_are_not_recorded():
    from cyber_worker.macro.recorder import normalize_combo

    assert normalize_combo("Alt+Ctrl+R") == "ctrl+alt+r"
    assert normalize_combo("Control+Shift+Option+.") == "ctrl+shift+alt+."
    clock = Clock()
    b = RecordingBuilder(WindowGeometry(100, 50, 400, 300, 1.0), now=clock, record_delays=False,
                         ignore_combos=["alt+r", "ctrl+alt+shift+."])
    b.on_combo("alt+r")  # 按快捷键开始 / 停止录制
    b.on_combo("ctrl+shift+alt+.")  # 急停
    b.on_combo("ctrl+v")
    assert [s.combo for s in b.steps] == ["ctrl+v"]
