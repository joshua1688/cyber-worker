"""执行特效：每个键鼠动作照做并触发对应的特效；特效按时间画出来、播完消失。"""

import numpy as np
from fakes import FakeIO

from cyber_worker.macro.io.effects import EffectBoard, EffectsIO, pretty_combo


class RecordingOverlay:
    def __init__(self):
        self.shown = []

    def show(self, kind, **kw):
        self.shown.append((kind, kw))

    def close(self):
        pass


def _io(clock):
    io = FakeIO()
    overlay = RecordingOverlay()
    return io, overlay, EffectsIO(io, overlay, clock=lambda: clock[0])


def test_each_action_still_happens_and_shows_its_effect():
    clock = [0.0]
    io, overlay, fx = _io(clock)
    fx.click("left")
    clock[0] = 0.1
    fx.click("left")              # 0.1 秒后同一个键：双击
    clock[0] = 2.0
    fx.click("left")              # 隔了很久：又是单击
    fx.click("right")             # 换了键：不算双击
    clock[0] = 2.1
    io.move_mouse(300, 200)
    fx.click("right")             # 0.1 秒后、但换了地方：两次单击（两个挨着的单击步骤）
    fx.mouse_down("left")
    fx.mouse_up("left")
    fx.scroll(3)
    fx.press_combo("ctrl+a")
    fx.type_char("好")
    fx.clear_field()
    fx.effect("hover", seconds=1.5)
    assert [k for k, _ in overlay.shown] == ["click", "double", "click", "click", "click", "press", "release",
                                             "scroll", "key", "type", "key", "hover"]
    assert overlay.shown[8][1] == {"label": "Ctrl+A"} and overlay.shown[10][1] == {"label": "清空输入框"}
    assert ("click", (0.0, 0.0), "left") in io.calls and ("scroll", 3, 0) in io.calls
    assert ("combo", "ctrl+a") in io.calls and ("char", "好") in io.calls and ("clear",) in io.calls
    assert fx.window_at(0, 0) is not None  # 其余方法原样转给里面的 io


def test_disabled_draws_nothing_and_overlay_errors_never_break_a_run():
    clock = [0.0]
    io, overlay, fx = _io(clock)
    fx.enabled = False
    fx.click("left")
    assert overlay.shown == [] and ("click", (0.0, 0.0), "left") in io.calls

    class Broken:
        def show(self, kind, **kw):
            raise RuntimeError("显卡驱动坏了")

    fx = EffectsIO(io, Broken())
    fx.click("left")  # 不抛
    assert io.calls.count(("click", (0.0, 0.0), "left")) == 2


def test_pretty_combo():
    assert pretty_combo("ctrl+shift+a") == "Ctrl+Shift+A"
    assert pretty_combo("enter") == "Enter"


def _alpha(sprite):
    return int(sprite[2][..., 3].max())


def test_click_ripple_fades_and_disappears():
    b = EffectBoard(1.0)
    b.handle("click", {}, (500, 300), 0.0)
    x, y, img = next(iter(b.frames(0.05, (500, 300)).values()))
    assert img.dtype == np.uint8 and img.shape[2] == 4
    assert x < 500 < x + img.shape[1] and y < 300 < y + img.shape[0]  # 以点击处为中心
    assert (img[..., :3] <= img[..., 3:4]).all()  # 预乘 alpha
    early = _alpha((x, y, img))
    late = _alpha(next(iter(b.frames(0.4, (500, 300)).values())))
    assert early > late > 0
    assert b.frames(0.6, (500, 300)) == {} and not b.active


def test_double_and_right_click_get_a_label():
    b = EffectBoard(1.0)
    b.handle("double", {}, (100, 100), 0.0)
    b.handle("click", {"button": "right"}, (300, 100), 0.0)
    texts = sorted(e.text for e in b.effects.values() if hasattr(e, "text"))
    assert texts == ["双击", "右键"]


def test_press_without_moving_is_a_long_press_moving_is_a_drag():
    b = EffectBoard(1.0)
    b.handle("press", {}, (100, 100), 0.0)
    b.frames(0.5, (100, 100))
    b.handle("release", {}, (100, 100), 1.2)
    assert any(getattr(e, "text", "") == "长按 1.2 秒" for e in b.effects.values())

    b = EffectBoard(1.0)
    b.handle("press", {}, (100, 100), 0.0)
    for i in range(10):
        b.frames(0.05 * i, (100 + 20 * i, 100))  # 拖动时每一帧记下鼠标走过的地方
    _, _, img = b.frames(0.5, (300, 100))[1]
    assert img.shape[1] > 200  # 窗口盖住整条路径
    b.handle("release", {}, (300, 100), 0.6)
    assert any(getattr(e, "text", "") == "拖动" for e in b.effects.values())
    assert b.frames(2.0, (300, 100)) == {}


def test_scrolls_and_keys_merge_into_one_label():
    b = EffectBoard(1.0)
    for i in range(3):
        b.handle("scroll", {"dy": 1}, (0, 0), 0.1 * i)
    b.handle("key", {"label": "Ctrl+A"}, (0, 0), 0.0)
    b.handle("key", {"label": "Backspace"}, (0, 0), 0.1)
    labels = [e.text for e in b.effects.values()]
    assert labels == ["滚动  ↓ 3", "按键  Ctrl+A → Backspace"]
    for ch in "你好":
        b.handle("type", {"text": ch}, (0, 0), 0.2)
    assert [e.text for e in b.effects.values()][-1] == "输入  你好"  # 换成输入：同一个标签改内容
    b.handle("scroll", {"dy": -2}, (0, 0), 0.4)  # 反方向：新的一个
    assert [e.text for e in b.effects.values()].count("滚动  ↑ 2") == 1
    assert b.frames(0.2, (0, 0))  # 带中文的标签画得出来


def test_hover_lasts_as_long_as_the_hover():
    b = EffectBoard(1.0)
    b.handle("hover", {"seconds": 2.0}, (50, 50), 0.0)
    assert len(b.frames(1.9, (50, 50))) == 2
    assert b.frames(2.5, (50, 50)) == {}


def test_runner_through_effects_layer_shows_every_action(tmp_path):
    """runner 用的是特效层包着的 io：点击、悬停、输入都画出来，执行结果不变。"""
    import random

    from fakes import MemoryAudit

    from cyber_worker.macro.runner import Runner, RunStatus
    from cyber_worker.macro.schema import Script
    from cyber_worker.macro.screenshot.storage import ScreenshotStorage

    overlay = RecordingOverlay()
    fx = EffectsIO(FakeIO(), overlay)
    runner = Runner(fx, ScreenshotStorage(tmp_path / "shots"), MemoryAudit(), rng=random.Random(0),
                    sleep=lambda s: None)
    script = Script.model_validate({
        "name": "特效", "window": {"app": "QQ", "width": 320, "height": 740},
        "steps": [
            {"id": "s01", "action": "hover", "x": 60, "y": 600, "seconds": 1.5},
            {"id": "s02", "action": "click", "x": 60, "y": 600, "count": 2},
            {"id": "s03", "action": "scroll", "dy": 2},
        ],
    })
    assert runner.run(script).status is RunStatus.OK
    kinds = [k for k, _ in overlay.shown]
    assert kinds[0] == "hover" and ["click", "double"] == kinds[1:3] and "scroll" in kinds


def test_overlay_loop_never_handles_an_event_twice():
    """等不到新事件的那一帧拿到的是空列表，不是上一批（否则每帧重放一次：「你好」→「你好好好好…」）。"""
    import queue
    import sys

    import pytest

    if sys.platform != "win32":
        pytest.skip("Windows 画法")
    from cyber_worker.macro.io.win_overlay import drain

    q = queue.Queue()
    q.put(("type", {"text": "你"}, 0.0))
    q.put(("type", {"text": "好"}, 0.1))
    assert len(drain(q, 0.01)) == 2
    assert drain(q, 0.01) == []
