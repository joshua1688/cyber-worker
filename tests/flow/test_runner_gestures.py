"""执行：长按 / 拖动 / 修饰键 / 连按 / 横向滚动，以及「任何中断都不会留下按着的键」。"""

import random

import pytest
from fakes import FakeIO, MemoryAudit

from cyber_worker.flow.runner import Cancelled, Gate, Runner, RunStatus, UserInterrupted
from cyber_worker.flow.schema import Flow
from cyber_worker.flow.screenshot.storage import ScreenshotStorage


@pytest.fixture
def env(tmp_path):
    io = FakeIO()
    slept = []

    def sleep(t):  # 也记进 io.calls，才能算出「按下到松开」之间等了多久
        slept.append(t)
        io.calls.append(("sleep", t))

    runner = Runner(io, ScreenshotStorage(tmp_path), MemoryAudit(), rng=random.Random(0), sleep=sleep)
    return io, runner, slept


def run(runner, steps, gate=None):
    s = Flow.model_validate({"name": "t", "window": {"app": "A", "width": 400, "height": 300}, "steps": steps})
    return runner.run(s, gate=gate)


def kinds(io):
    return [c[0] for c in io.calls if c[0] != "sleep"]


def actions(io):
    return [c for c in io.calls if c[0] != "sleep"]


def test_long_press_holds_for_the_duration(env):
    io, runner, slept = env
    assert run(runner, [{"id": "s1", "action": "click", "x": 10, "y": 10, "hold": 1.5}]).status is RunStatus.OK
    assert kinds(io) == ["down", "up"]
    down, up = actions(io)
    assert down[1] == up[1]  # 按住期间没有移动
    i, j = io.calls.index(down), io.calls.index(up)
    held = sum(c[1] for c in io.calls[i:j] if c[0] == "sleep")
    assert 1.45 <= held <= 1.55  # 1.5 秒 ± 3%


def test_drag_moves_with_button_down(env):
    io, runner, _ = env
    run(runner, [{"id": "s1", "action": "drag", "x": 10, "y": 10, "x2": 200, "y2": 50, "duration": 0.6}])
    k = kinds(io)
    assert k.count("down") == 1 and k.count("up") == 1
    a = actions(io)
    assert a[k.index("down")][1] == (110, 60)  # 窗口原点 (100, 50) + (10, 10)
    assert a[k.index("up")][1] == (300, 100)  # 松开时在终点
    assert io.pos == (300, 100)


def test_modifier_click_presses_and_releases_in_order(env):
    io, runner, _ = env
    run(runner, [{"id": "s1", "action": "click", "x": 10, "y": 10, "modifiers": ["ctrl", "shift"]}])
    assert [c for c in actions(io) if c[0] != "click"] == [
        ("key_down", "ctrl"), ("key_down", "shift"), ("key_up", "shift"), ("key_up", "ctrl")]
    k = kinds(io)
    assert k.index("key_down") < k.index("click") < k.index("key_up")


def test_key_times(env):
    io, runner, _ = env
    run(runner, [{"id": "s1", "action": "key", "combo": "backspace", "times": 7}])
    assert actions(io) == [("combo", "backspace")] * 7


def test_horizontal_and_vertical_scroll(env):
    io, runner, _ = env
    run(runner, [{"id": "s1", "action": "scroll", "dy": 4, "dx": -3, "x": 50, "y": 50}])
    scrolls = [c for c in io.calls if c[0] == "scroll"]
    assert sum(c[1] for c in scrolls) == 4 and sum(c[2] for c in scrolls) == -3
    assert all(c[1] == 0 or c[2] == 0 for c in scrolls)  # 一次只滚一个方向


class InterruptAfter(Gate):
    """第 n 次 check 时打断（模拟急停 / 你动了鼠标）。"""

    def __init__(self, n, exc):
        self.n, self.exc = n, exc

    def check(self):
        self.n -= 1
        if self.n <= 0:
            raise self.exc()


@pytest.mark.parametrize("step", [
    {"id": "s1", "action": "click", "x": 10, "y": 10, "hold": 5},
    {"id": "s1", "action": "drag", "x": 10, "y": 10, "x2": 300, "y2": 200, "duration": 2},
    {"id": "s1", "action": "click", "x": 10, "y": 10, "hold": 5, "modifiers": ["ctrl"]},
    {"id": "s1", "action": "drag", "x": 10, "y": 10, "x2": 300, "y2": 200, "modifiers": ["shift", "alt"]},
])
@pytest.mark.parametrize("exc", [Cancelled, UserInterrupted])
def test_interruption_never_leaves_anything_pressed(env, step, exc):
    io, runner, _ = env
    # 移动约 10 次 check 之后打断：此时已经按下了
    gate = InterruptAfter(25, exc)
    if exc is Cancelled:
        with pytest.raises(Cancelled):
            run(runner, [step], gate)
    else:
        assert run(runner, [step], gate).status is RunStatus.ABORTED
    k = kinds(io)
    assert "down" in k, "测试设定有误：打断时应当已经按下"
    assert k.count("down") == k.count("up")
    assert k.count("key_down") == k.count("key_up")
    assert k[-1] in ("up", "key_up")  # 最后一个动作是松开
