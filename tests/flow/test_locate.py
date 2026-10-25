"""按图找点击、等页面出现、「等待页面」步骤，以及录制时挑目标图。"""

import random
from datetime import datetime

import numpy as np
import pytest
from fakes import FakeIO, MemoryAudit
from helpers import make_book, pattern, screen_with

from cyber_worker.flow.audit.logger import StepStatus
from cyber_worker.flow.io.base import WindowGeometry
from cyber_worker.flow.recorder import RecordingBuilder
from cyber_worker.flow.runner import Runner, RunStatus
from cyber_worker.flow.schema import Flow
from cyber_worker.flow.screenshot.storage import ScreenshotStorage, write_image
from cyber_worker.flow.vision.template import pick_target

ICON = pattern(40, 30, seed=7)  # 要点的那个「图标」


class Screens:
    """按顺序给出截图（最后一张一直重复），模拟界面在执行过程中变化。"""

    def __init__(self, *frames):
        self.frames = list(frames)
        self.n = 0

    def __call__(self):
        f = self.frames[min(self.n, len(self.frames) - 1)]
        self.n += 1
        return f


class Clock:
    """假的 sleep：记下一共睡了多久。"""

    def __init__(self):
        self.slept = 0.0

    def __call__(self, s):
        self.slept += s


@pytest.fixture
def env(tmp_path):
    def make(screen):
        io = FakeIO(screen)
        audit = MemoryAudit()
        clock = Clock()
        runner = Runner(io, ScreenshotStorage(tmp_path / "shots"), audit, rng=random.Random(0), sleep=clock,
                        now=lambda: datetime(2026, 10, 23, 9, 0))
        return io, audit, runner, clock
    return make


def click_flow(tmp_path, *, timeout=5.0, use_target=True, **step):
    """录制时目标在 (50, 40)，点在目标里面偏右下的 (70, 55)。"""
    write_image(tmp_path / "m.targets" / "s01.png", ICON)
    s = Flow.model_validate({
        "name": "m", "window": {"app": "QQ", "width": 400, "height": 300},
        "steps": [{"id": "s01", "action": "click", "x": 70, "y": 55, "use_target": use_target,
                   "target": {"image": "m.targets/s01.png", "x": 50, "y": 40, "w": 40, "h": 30,
                              "timeout": timeout}, **step}],
    })
    s._source = tmp_path / "m.yaml"
    return s


class clicked:
    """点在了窗口里的哪儿（窗口原点 100, 50）；和期望值比较时容许点击本身 ±2 点的随机抖动。"""

    def __init__(self, io):
        (_, (x, y), _), = [c for c in io.calls if c[0] == "click"]
        self.at = (x - 100, y - 50)

    def __eq__(self, other):
        return all(abs(a - b) <= 2 for a, b in zip(self.at, other))

    def __repr__(self):
        return f"clicked{self.at}"


def test_target_moved_still_clicks_it(env, tmp_path):
    io, audit, runner, _ = env(lambda: screen_with(ICON, at=(150, 120)))
    r = runner.run(click_flow(tmp_path))
    assert r.status is RunStatus.OK
    assert clicked(io) == (170, 135)  # 目标挪了 (+100, +80)，点击处跟着挪
    assert "偏了 +100, +80" in audit.entries[-1].detail


def test_target_in_place(env, tmp_path):
    io, audit, runner, _ = env(lambda: screen_with(ICON, at=(50, 40)))
    runner.run(click_flow(tmp_path))
    assert clicked(io) == (70, 55) and "位置没变" in audit.entries[-1].detail


def test_waits_for_target_to_appear(env, tmp_path):
    blank = screen_with(None)
    screens = Screens(blank, blank, blank, screen_with(ICON, at=(60, 200)))
    io, _, runner, _ = env(screens)
    r = runner.run(click_flow(tmp_path))
    assert r.status is RunStatus.OK and clicked(io) == (80, 215)
    assert screens.n == 5  # 找了 4 次（第 4 次出现了就点）+ 点完截 1 张，没有一直等到超时


def test_target_never_appears_aborts_without_clicking(env, tmp_path):
    io, audit, runner, clock = env(lambda: screen_with(None))
    r = runner.run(click_flow(tmp_path, timeout=2))
    assert r.status is RunStatus.ABORTED and "没找到要点的目标" in r.reason and "等了 2 秒" in r.reason
    assert not [c for c in io.calls if c[0] == "click"]  # 不会退回去点旧坐标
    assert audit.entries[-1].status is StepStatus.ABORTED and audit.entries[-1].screenshot
    assert 1.9 <= clock.slept <= 2.1


def test_several_matches_picks_nearest(env, tmp_path):
    img = screen_with(ICON, at=(300, 200))
    img[45:75, 60:100] = ICON  # 另一个一样的，离录制位置 (50, 40) 更近
    io, _, runner, _ = env(lambda: img)
    runner.run(click_flow(tmp_path))
    assert clicked(io) == (80, 60)


def test_fixed_coordinates_when_target_off(env, tmp_path):
    io, _, runner, _ = env(lambda: screen_with(ICON, at=(150, 120)))
    runner.run(click_flow(tmp_path, use_target=False))
    assert clicked(io) == (70, 55)


def test_dry_run_still_looks_for_send_target(env, tmp_path):
    """空跑也会找「发送」按钮：没找到就提前报出来，而不是等到真执行才发现。"""
    io, _, runner, _ = env(lambda: screen_with(None))
    s = click_flow(tmp_path, timeout=0.6)
    s.steps[0].sends = True
    s.steps[0].expected_page = None
    book = make_book(tmp_path / "pages", patch=pattern(), at=(200, 100))
    io.screen = lambda: screen_with(pattern(), at=(200, 100))  # 页面对，但目标不在
    s.steps[0].expected_page = "compose"
    r = runner.run(s, pages=book, dry_run=True)
    assert r.status is RunStatus.ABORTED and "没找到" in r.reason


# ---- 等页面 ----

def type_flow():
    return Flow.model_validate({
        "name": "t", "window": {"app": "QQ", "width": 400, "height": 300},
        "steps": [{"id": "s01", "action": "type", "text": "你好", "expected_page": "compose"}],
    })


def test_type_waits_for_page(env, tmp_path):
    book = make_book(tmp_path / "pages")
    io, _, runner, clock = env(Screens(screen_with(None), screen_with(None), screen_with(pattern())))
    r = runner.run(type_flow(), pages=book)
    assert r.status is RunStatus.OK and io.typed() == "你好" and clock.slept > 0


def test_type_aborts_when_page_never_shows(env, tmp_path):
    book = make_book(tmp_path / "pages")
    io, _, runner, clock = env(lambda: screen_with(None))
    r = runner.run(type_flow(), pages=book)
    assert r.status is RunStatus.ABORTED and "页面不符" in r.reason and "还没出现" in r.reason
    assert io.typed() == "" and 4.9 <= clock.slept <= 5.1


def test_wait_page_step(env, tmp_path):
    book = make_book(tmp_path / "pages")
    _, audit, runner, _ = env(Screens(screen_with(None), screen_with(pattern())))
    s = Flow.model_validate({"name": "w", "window": {"app": "QQ", "width": 400, "height": 300},
                               "steps": [{"id": "s01", "action": "wait_page", "page": "compose", "timeout": 3}]})
    r = runner.run(s, pages=book)
    assert r.status is RunStatus.OK and "页面「compose」出现了" in audit.entries[-1].detail
    _, _, runner2, clock = env(lambda: screen_with(None))
    r = runner2.run(s, pages=book)
    assert r.status is RunStatus.ABORTED and 2.9 <= clock.slept <= 3.1


# ---- 录制时挑目标图 ----

def test_pick_target():
    img = screen_with(ICON, at=(50, 40))
    box = pick_target(img, 70, 55, 1.0)
    assert box is not None
    x, y, w, h = box
    assert x <= 70 <= x + w and y <= 55 <= y + h  # 点击处在框里
    assert pick_target(screen_with(None), 70, 55, 1.0) is None  # 空白处认不出来，保持固定坐标


def test_pick_target_rejects_lookalikes():
    tile = pattern(60, 50, seed=3)
    img = np.full((300, 400, 3), 255, np.uint8)
    for x in (20, 120, 220, 320):  # 一排一模一样的按钮
        img[100:150, x:x + 60] = tile
    assert pick_target(img, 150, 125, 1.0) is None


def test_recorder_reports_presses():
    presses = []
    b = RecordingBuilder(WindowGeometry(100, 50, 400, 300, 1.0), now=iter(range(100)).__next__,
                         record_delays=False, on_press=lambda *a: presses.append(a))
    b.on_click(170, 105, "left")
    b.on_click(300, 200, "left")
    assert [(p[0], p[3], p[4]) for p in presses] == [(1, 70, 55), (2, 200, 150)]
    assert b.press_of == {"s01": 1, "s02": 2}


def test_target_with_two_looks_matches_the_closer_one():
    """目标图截在按下那一刻（按钮变了色），平时的样子另存一张：两张都比，取更像的。"""
    from cyber_worker.flow.locator.icon import locate_click
    from cyber_worker.flow.schema import Click, Target
    from cyber_worker.flow.vision.template import look_of

    normal = ICON
    pressed = ICON.copy()
    pressed[:, :14] = 200  # 按下去左边一截盖上了高亮底色（整体变暗变亮不影响得分，局部变了才会）
    screen = screen_with(normal, at=(100, 80))
    step = Click(id="s01", x=120, y=95, target=Target(image="t.png", x=100, y=80, w=40, h=30))
    only_pressed = locate_click(step, [pressed], screen, 1.0)
    both = locate_click(step, [pressed, normal], screen, 1.0)
    assert only_pressed.score < 0.9  # 只认按下时的样子：勉强甚至认不出来
    assert both.found and both.score > 0.99
    assert (both.x, both.y) == (120, 95)
    assert look_of(normal, normal) == "same" and look_of(pressed, normal) == "other"
    assert look_of(pattern(40, 30, seed=99), normal) == "different"


def test_run_remembers_a_changed_look_by_itself(env, tmp_path):
    """按钮样子变了一点（得分 0.84：找到了，但偏低）：执行时自动记住新样子，下次两种都认，满分找到；
    录制时的目标图不动。不用用户手动去更新。"""
    changed = ICON.copy()
    changed[:, :5] = 200
    io, audit, runner, _ = env(lambda: screen_with(changed, at=(50, 40)))
    s = click_flow(tmp_path)
    s.dump(tmp_path / "m.yaml")
    assert runner.run(s).status is RunStatus.OK
    assert "已自动记住新样子" in audit.entries[-1].detail
    saved = Flow.load(tmp_path / "m.yaml").steps[0].target
    assert saved.image == "m.targets/s01.png" and saved.alt and (tmp_path / saved.alt).exists()

    again = Flow.load(tmp_path / "m.yaml")
    runner.run(again)
    assert "得分 1.00" in audit.entries[-1].detail and "已自动记住" not in audit.entries[-1].detail


def test_run_does_not_learn_when_several_places_look_alike(env, tmp_path):
    """窗口里有两处都像它：分不清是哪个，不学（免得把认错的东西记下来）。"""
    changed = ICON.copy()
    changed[:, :5] = 200
    screen = screen_with(changed, at=(50, 40))
    screen[200:230, 250:290] = changed
    io, audit, runner, _ = env(lambda: screen)
    s = click_flow(tmp_path)
    s.dump(tmp_path / "m.yaml")
    runner.run(s)
    assert Flow.load(tmp_path / "m.yaml").steps[0].target.alt is None
