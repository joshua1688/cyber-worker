"""同一个程序开着好几个窗口、宏里又没记标题时，找对窗口（QQ：主窗口 320 宽，聊天窗口 580 宽）。"""

import random

from cyber_worker.macro.io.base import pick_window
from cyber_worker.macro.runner import Runner, RunStatus
from cyber_worker.macro.schema import Script
from cyber_worker.macro.screenshot.storage import ScreenshotStorage
from fakes import FakeIO, MemoryAudit

CHAT = ("chat", "AIGC研究社", 580, 740)
MAIN = ("main", "QQ", 320, 740)


def test_picks_the_closest_size_not_the_topmost():
    assert pick_window([CHAT, MAIN], (320, 740)) == "main"  # 聊天窗口在最上层也不拿它


def test_avoids_titles_of_the_macros_other_windows():
    same_size_chat = ("chat", "AIGC研究社", 320, 740)
    assert pick_window([same_size_chat, MAIN], (320, 740), avoid=["AIGC研究社"]) == "main"


def test_falls_back_when_everything_is_avoided_or_no_size():
    assert pick_window([CHAT], (320, 740), avoid=["AIGC研究社"]) == "chat"
    assert pick_window([CHAT, MAIN], None) == "chat"  # 不知道尺寸：还是最上层的
    assert pick_window([], (320, 740)) is None


def test_runner_tells_io_which_titles_to_avoid(tmp_path):
    io = FakeIO()
    runner = Runner(io, ScreenshotStorage(tmp_path / "shots"), MemoryAudit(), rng=random.Random(0),
                    sleep=lambda s: None)
    script = Script.model_validate({
        "name": "QQ", "window": {"app": "QQ", "width": 320, "height": 740},
        "windows": {"w2": {"app": "QQ", "title": "AIGC研究社", "width": 580, "height": 740}},
        "steps": [{"id": "s01", "action": "click", "x": 10, "y": 10},
                  {"id": "s02", "win": "w2", "action": "click", "x": 10, "y": 10}],
    })
    assert runner.run(script).status is RunStatus.OK
    # 找主窗口时避开聊天窗口的标题；找聊天窗口时主窗口没有标题，没什么要避开的
    assert io.avoided == [("AIGC研究社",), ()]


def test_step_that_closes_its_window_still_succeeds(tmp_path):
    """点 ✕ 把聊天窗口关了：操作后截不到它，这一步照样算成功，只是没有截图。"""
    from cyber_worker.macro.io.base import CalibrationError

    io = FakeIO()
    shots = {"n": 0}

    def capture(geo):
        shots["n"] += 1
        if any(c[0] == "click" for c in io.calls):
            raise CalibrationError("窗口已经关了或最小化了，截不到它")
        return FakeIO().screen()

    io.capture = capture
    audit = MemoryAudit()
    runner = Runner(io, ScreenshotStorage(tmp_path / "shots"), audit, rng=random.Random(0), sleep=lambda s: None)
    script = Script.model_validate({
        "name": "关窗口", "window": {"app": "QQ", "width": 320, "height": 740},
        "steps": [{"id": "s01", "action": "click", "x": 300, "y": 10}],
    })
    assert runner.run(script).status is RunStatus.OK
    assert audit.entries[-1].screenshot is None
