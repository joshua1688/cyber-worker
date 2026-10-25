"""悬停：主窗口里鼠标停在头像上 → 弹出资料卡（一个新窗口）→ 在资料卡上点「发消息」。"""

import random

from fakes import FakeIO, MemoryAudit

from cyber_worker.flow.io.base import WindowGeometry, WindowInfo
from cyber_worker.flow.recorder import RecordingBuilder
from cyber_worker.flow.runner import Runner, RunStatus
from cyber_worker.flow.schema import Flow, Hover, Wait, Window
from cyber_worker.flow.screenshot.storage import ScreenshotStorage

MAIN_GEO = WindowGeometry(100, 50, 320, 740, 1.0)
CARD = (WindowInfo("QQ", "资料卡", 322, 388), WindowGeometry(430, 300, 322, 388, 1.0))


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def recording(record_delays=True):
    clock = Clock()
    b = RecordingBuilder(MAIN_GEO, now=clock, record_delays=record_delays,
                         primary=Window(app="QQ", title="QQ", width=320, height=740))
    return b, clock


def test_hover_is_inserted_where_it_happened_and_splits_the_delay():
    b, clock = recording()
    b.on_click(150, 100, "left")      # s01（t=0）
    clock.t = 4.0
    b.on_click(500, 400, "left", (), CARD)  # 资料卡上点「发消息」
    # 盯悬停的线程发现：t=1.0 时停在头像上，停了 1.5 秒，弹出了资料卡
    b.add_hover(1.0, "main", 60, 600, 1.5)
    renamed = b.finish()
    steps = b.steps
    assert [s.action for s in steps] == ["click", "wait", "hover", "wait", "click"]
    assert [s.id for s in steps] == ["s01", "s02", "s03", "s04", "s05"]
    assert steps[1].seconds == 1.0 and steps[3].seconds == 1.5  # 4 秒的延迟拆成 1 + 悬停 1.5 + 1.5
    h = steps[2]
    assert isinstance(h, Hover) and (h.x, h.y, h.seconds, h.win) == (60, 600, 1.5, None)
    assert b.press_of["s05"] == 2  # 第二次按下：编号跟着改了
    assert renamed["s03"] == "s05" and list(b.hover_marks) == ["s03"]


def test_hover_without_delays_goes_before_the_next_step():
    b, clock = recording(record_delays=False)
    b.on_click(150, 100, "left")
    clock.t = 3.0
    b.on_char("x", CARD)
    b.add_hover(1.0, "main", 60, 600, 1.0)
    b.finish()
    assert [s.action for s in b.steps] == ["click", "hover", "type"]
    assert not any(isinstance(s, Wait) for s in b.steps)


def test_no_hover_no_renumbering():
    b, _ = recording()
    b.on_click(150, 100, "left")
    assert b.finish() == {} and [s.id for s in b.steps] == ["s01"]


def test_hover_window_ignores_other_programs():
    b, _ = recording()
    terminal = (WindowInfo("WindowsTerminal", "PowerShell", 800, 600), WindowGeometry(0, 0, 800, 600, 1.0))
    assert b.hover_window(terminal, 10, 10) is None
    key, x, y, _ = b.hover_window(CARD, 440, 310)
    assert key == "w2" and (x, y) == (10, 10)


def test_runner_hovers_then_finds_popup_without_activating_it(tmp_path):
    """执行：移到头像上停一会儿；资料卡是悬停弹出的窗口，只找位置，不切前台、不改尺寸。"""
    io = FakeIO()
    io.origins["资料卡"] = (430, 300)
    sleeps = []
    runner = Runner(io, ScreenshotStorage(tmp_path / "shots"), MemoryAudit(), rng=random.Random(0),
                    sleep=sleeps.append)
    flow = Flow.model_validate({
        "name": "资料卡",
        "window": {"app": "QQ", "title": "QQ", "width": 320, "height": 740},
        "windows": {"w2": {"app": "QQ", "title": "资料卡", "width": 322, "height": 388, "popup": True}},
        "steps": [
            {"id": "s01", "action": "hover", "x": 60, "y": 600, "seconds": 1.5},
            {"id": "s02", "win": "w2", "action": "click", "x": 250, "y": 338},
        ],
    })
    positions = []
    move = io.move_mouse
    io.move_mouse = lambda x, y: (move(x, y), positions.append((x, y)))
    r = runner.run(flow)
    assert r.status is RunStatus.OK
    assert io.calibrated == ["QQ"]  # 只校准了主窗口
    assert ("locate", "资料卡") in io.calls
    # 移到头像 (100+60, 50+600) 停了 1.5 秒
    assert any(abs(x - 160) <= 1 and abs(y - 650) <= 1 for x, y in positions)
    ticks = [s for s in sleeps if abs(s - 0.1) < 1e-9]
    assert len(ticks) == 15  # 停 1.5 秒：切成 15 段，段间能急停
    clicks = [c for c in io.calls if c[0] == "click"]
    assert len(clicks) == 1 and abs(clicks[0][1][0] - (430 + 250)) <= 1  # 点在资料卡上，没在悬停时点


def test_small_jitter_is_not_a_move():
    """手放在鼠标上会抖一两个像素：还算停在原处，停下的时间不重来。"""
    b, clock = recording()
    b.on_move(200, 300)
    first = b.pointer
    clock.t = 0.3
    b.on_move(202, 301)   # 抖了一下
    clock.t = 0.6
    b.on_move(197, 299)
    assert b.pointer.seq == first.seq and b.pointer.t == 0.0
    clock.t = 0.9
    b.on_move(220, 300)   # 真的移开了
    assert b.pointer.seq == first.seq + 1 and b.pointer.t == 0.9


def test_known_window_takes_this_recordings_size():
    """流程里以前录过「全王探测」是 930 宽，这次它是 680 宽：按这次的记，参考图、弹出窗口标记照旧。"""
    old = Window(app="QQ", title="全王探测", width=930, height=613, reference="QQ.refs/w3.png")
    b = RecordingBuilder(MAIN_GEO, now=Clock(), record_delays=False,
                         primary=Window(app="QQ", title="QQ", width=320, height=740), windows={"w3": old})
    chat = (WindowInfo("QQ", "全王探测", 680, 613), WindowGeometry(600, 100, 680, 613, 1.0))
    b.on_click(600 + 584, 100 + 22, "left", (), chat)
    w = b.windows["w3"]
    assert (w.width, w.height, w.reference) == (680, 613, "QQ.refs/w3.png")
    assert old.width == 930  # 没改到流程里原来那份（录制被撤销时还要用）


def test_target_captured_while_hovered_is_found_after_moving_there(tmp_path):
    """目标图是录制时鼠标悬停在按钮上的样子（灰底）。执行时按钮没悬停、不像：先移过去等它亮起来再找。"""
    import numpy as np

    from cyber_worker.flow.screenshot.storage import write_image

    # 最小化按钮：白底上一根横线；悬停时中间一块圆角矩形变灰、外圈还是白（整体变亮变暗模板匹配本来就认得）
    icon = np.full((40, 48, 3), 245, np.uint8)
    icon[19:21, 16:32] = 40
    hovered = icon.copy()
    hovered[6:34, 8:40] = 205
    hovered[19:21, 16:32] = 40

    def screen():
        img = np.full((300, 400, 3), 245, np.uint8)
        over = abs(io.pos[0] - (100 + 300)) < 20 and abs(io.pos[1] - (50 + 60)) < 16
        img[40:80, 276:324] = hovered if over else icon  # 按钮在窗口 (276, 40)
        return img

    io = FakeIO(screen)
    write_image(tmp_path / "t.png", hovered)
    runner = Runner(io, ScreenshotStorage(tmp_path / "shots"), MemoryAudit(), rng=random.Random(0),
                    sleep=lambda s: None)
    flow = Flow.model_validate({
        "name": "t", "window": {"app": "QQ", "width": 400, "height": 300},
        "steps": [{"id": "s01", "action": "click", "x": 300, "y": 60,
                   "target": {"image": "t.png", "x": 276, "y": 40, "w": 48, "h": 40, "timeout": 2}}],
    })
    flow._source = tmp_path / "t.yaml"
    assert runner.run(flow).status is RunStatus.OK
    clicks = [c for c in io.calls if c[0] == "click"]
    assert len(clicks) == 1 and abs(clicks[0][1][0] - 400) <= 1


def test_resting_where_you_just_clicked_is_not_a_hover():
    """双击会话后鼠标停在原处等聊天窗口慢慢打开：冒出来的窗口是那一下点击打开的，不算悬停弹出。
    点完挪到别处再停、或者停了很久以后，照常算。"""
    b, clock = recording()
    clock.t = 10.0
    b.on_mouse_down(200, 300, "left")
    b.on_mouse_up(200, 300, "left")
    clock.t = 10.3
    b.on_move(205, 302)  # 手抖了一下，还在原处附近
    assert b.settling_after_click(b.pointer)
    clock.t = 10.8
    b.on_move(400, 500)  # 挪到别处停住：可能是真悬停
    assert not b.settling_after_click(b.pointer)
    clock.t = 13.0
    b.on_move(203, 301)  # 回到原处，但已经过了好一会儿
    assert not b.settling_after_click(b.pointer)
