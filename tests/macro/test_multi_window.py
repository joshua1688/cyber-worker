"""跨窗口：QQ 主窗口里双击会话 → 弹出的聊天窗口里打字、点发送。"""

import random

from cyber_worker.macro.io.base import WindowGeometry, WindowInfo
from cyber_worker.macro.recorder import RecordingBuilder
from cyber_worker.macro.runner import Runner, RunStatus
from cyber_worker.macro.schema import Script, Window
from cyber_worker.macro.screenshot.storage import ScreenshotStorage
from fakes import FakeIO, MemoryAudit

MAIN_GEO = WindowGeometry(100, 50, 320, 740, 1.0)
CHAT_GEO = WindowGeometry(300, 100, 998, 613, 1.0)  # 和主窗口有重叠
QQ_MAIN = (WindowInfo("QQ", "QQ", 320, 740), MAIN_GEO)
CHAT = (WindowInfo("QQ", "My Sky等2个会话", 998, 613), CHAT_GEO)
STUDIO = (WindowInfo("chrome", "宏设计 · 赛博打工人 - Google Chrome", 1200, 800),
          WindowGeometry(0, 0, 1200, 800, 1.0))


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_recording_across_windows():
    clock = Clock()
    b = RecordingBuilder(MAIN_GEO, now=clock, record_delays=False,
                         primary=Window(app="QQ", title="QQ", width=320, height=740))
    for _ in range(2):  # 在主窗口双击「My Sky」
        b.on_mouse_down(188, 225, "left", (), QQ_MAIN)
        clock.t += 0.05
        b.on_mouse_up(188, 225, "left")
        clock.t += 0.05
    clock.t += 1
    for ch in "你好":  # 聊天窗口弹出来、拿到焦点，直接打字（没有先点它）
        b.on_char(ch, CHAT)
    clock.t += 1
    b.on_click(350, 150, "left", (), CHAT)  # 点在主窗口矩形范围内，但实际上点的是盖在上面的聊天窗口
    b.on_click(40, 40, "left", (), STUDIO)  # 回 studio 点停止：忽略
    b.on_char("x", STUDIO)

    s = b.build("qq", Window(app="QQ", title="QQ", width=320, height=740))
    got = [(st.action, st.win, getattr(st, "x", None), getattr(st, "y", None), getattr(st, "text", None))
           for st in s.steps]
    assert got == [
        ("click", None, 88, 175, None),
        ("type", "w2", None, None, "你好"),
        ("click", "w2", 50, 50, None),
    ]
    assert s.steps[0].count == 2
    # 聊天窗口按标题认，标题里会变的「等2个会话」去掉
    assert s.windows == {"w2": Window(app="QQ", title="My Sky", width=998, height=613)}


def test_same_window_seen_again_reuses_key():
    clock = Clock()
    b = RecordingBuilder(MAIN_GEO, now=clock, record_delays=False,
                         windows={"w2": Window(app="QQ", title="My Sky", width=998, height=613)})
    b.on_click(350, 150, "left", (), (WindowInfo("QQ", "My Sky等3个会话", 998, 613), CHAT_GEO))
    assert b.steps[0].win == "w2" and list(b.windows) == ["w2"]


def test_runner_switches_windows_and_waits_for_new_one(tmp_path):
    io = FakeIO()
    io.origins = {"QQ": (100, 50), "My Sky": (300, 100)}
    io.missing = {"My Sky": 2}  # 双击后聊天窗口要过一会儿才出来
    runner = Runner(io, ScreenshotStorage(tmp_path), MemoryAudit(), rng=random.Random(0), sleep=lambda s: None)
    s = Script.model_validate({
        "name": "qq", "window": {"app": "QQ", "title": "QQ", "width": 320, "height": 740},
        "windows": {"w2": {"app": "QQ", "title": "My Sky", "width": 998, "height": 613}},
        "steps": [
            {"id": "s1", "action": "click", "x": 88, "y": 175, "count": 2},
            {"id": "s2", "action": "wait", "seconds": 1},
            {"id": "s3", "action": "key", "combo": "a", "win": "w2"},
            {"id": "s4", "action": "click", "x": 50, "y": 50, "win": "w2"},
            {"id": "s5", "action": "click", "x": 10, "y": 10},  # 回到主窗口
        ],
    })
    r = runner.run(s)
    assert r.status is RunStatus.OK
    assert io.calibrated == ["QQ", "My Sky", "QQ"]  # 等待步骤不切窗口；同一窗口连续两步只校准一次
    clicks = [c[1] for c in io.calls if c[0] == "click"]
    assert abs(clicks[0][0] - 188) <= 1 and abs(clicks[0][1] - 225) <= 1  # 主窗口坐标
    assert abs(clicks[2][0] - 350) <= 1 and abs(clicks[2][1] - 150) <= 1  # 聊天窗口坐标
    assert abs(clicks[3][0] - 110) <= 1 and abs(clicks[3][1] - 60) <= 1  # 又回到主窗口


def test_runner_gives_up_if_window_never_appears(tmp_path):
    io = FakeIO()
    io.missing = {"My Sky": 999}
    runner = Runner(io, ScreenshotStorage(tmp_path), MemoryAudit(), rng=random.Random(0), sleep=lambda s: None)
    s = Script.model_validate({
        "name": "qq", "window": {"app": "QQ", "title": "QQ", "width": 320, "height": 740},
        "windows": {"w2": {"app": "QQ", "title": "My Sky", "width": 998, "height": 613}},
        "steps": [{"id": "s1", "action": "key", "combo": "a", "win": "w2"}],
    })
    r = runner.run(s)
    assert r.status is RunStatus.FAILED and "My Sky" in r.reason


def test_ime_typing_is_flagged_and_runner_refuses(tmp_path):
    clock = Clock()
    b = RecordingBuilder(MAIN_GEO, now=clock, record_delays=False)
    for ch in "nihao ":  # 开着中文输入法打「你好」：键盘监听只看到按键
        b.on_char(ch, CHAT, ime=True)
    (step,) = b.steps
    assert step.text == "nihao " and step.typed_with_ime and step.win == "w2"

    io = FakeIO()
    runner = Runner(io, ScreenshotStorage(tmp_path), MemoryAudit(), rng=random.Random(0), sleep=lambda s: None)
    s = b.build("qq", Window(app="QQ", width=320, height=740))
    r = runner.run(s)
    assert r.status is RunStatus.ABORTED and "中文输入法" in r.reason
    assert not any(c[0] == "char" for c in io.calls)  # 一个拼音都没打出去


def test_studio_window_recognized_by_new_and_old_titles():
    from cyber_worker.macro.recorder import is_studio_title

    assert is_studio_title("宏设计 · 赛博打工人 - Google Chrome")
    assert is_studio_title("审计 · 赛博打工人 - Microsoft Edge")
    assert is_studio_title("宏编辑 · cyber-worker studio - Google Chrome")  # 还开着的旧标签页
    assert not is_studio_title("赛博打工人 README - GitHub")  # 别的网页提到这个名字不算
