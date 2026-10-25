import random
from datetime import datetime

import pytest
from fakes import FakeIO, MemoryAudit
from helpers import make_book, pattern, screen_with

from cyber_worker.flow.audit.logger import StepStatus
from cyber_worker.flow.runner import Cancelled, Gate, Runner, RunStatus, UserInterrupted
from cyber_worker.flow.schema import Flow
from cyber_worker.flow.screenshot.storage import ScreenshotStorage

COMMENT = {
    "name": "comment",
    "window": {"app": "WeChat", "width": 400, "height": 300},
    "variables": ["comment_draft"],
    "steps": [
        {"id": "s01", "action": "click", "x": 20, "y": 30},
        {"id": "s02", "action": "type", "text": "{comment_draft}", "expected_page": "compose"},
        {"id": "s03", "action": "click", "x": 380, "y": 280, "sends": True, "expected_page": "compose"},
    ],
}


@pytest.fixture
def env(tmp_path):
    io = FakeIO(lambda: screen_with(pattern()))
    audit = MemoryAudit()
    runner = Runner(io, ScreenshotStorage(tmp_path / "shots"), audit,
                    rng=random.Random(0), sleep=lambda s: None,
                    now=lambda: datetime(2026, 10, 23, 9, 0))
    book = make_book(tmp_path / "pages")
    return io, audit, runner, book


def flow(**over):
    return Flow.model_validate({**COMMENT, **over})


def test_full_run(env):
    io, audit, runner, book = env
    r = runner.run(flow(), pages=book, variables={"comment_draft": "好看"})
    assert r.status is RunStatus.OK and r.steps_done == 3
    assert io.typed() == "好看"
    clicks = [c for c in io.calls if c[0] == "click"]
    assert len(clicks) == 2
    # 窗口原点 (100, 50)，坐标相对窗口
    assert abs(clicks[1][1][0] - 480) <= 1 and abs(clicks[1][1][1] - 330) <= 1
    assert [e.status for e in audit.entries] == [StepStatus.OK] * 3
    assert all(e.screenshot for e in audit.entries)


def test_double_click_clicks_twice_in_place(env):
    io, _, runner, book = env
    s = flow(variables=[], steps=[{"id": "s01", "action": "click", "x": 50, "y": 60, "count": 2}])
    runner.run(s, pages=book)
    clicks = [c for c in io.calls if c[0] == "click"]
    assert len(clicks) == 2 and clicks[0][1] == clicks[1][1]  # 两下之间鼠标没动


def test_wrong_page_aborts_before_typing(env):
    io, audit, runner, book = env
    io.screen = lambda: screen_with(pattern(seed=9))
    r = runner.run(flow(), pages=book, variables={"comment_draft": "好看"})
    assert r.status is RunStatus.ABORTED and r.steps_done == 1
    assert io.typed() == ""
    last = audit.entries[-1]
    assert last.status is StepStatus.ABORTED and last.suspicious and last.screenshot


def test_type_without_expected_page_is_refused(env):
    io, audit, runner, book = env
    s = flow(variables=[], steps=[{"id": "s01", "action": "type", "text": "hi"}])
    r = runner.run(s, pages=book)
    assert r.status is RunStatus.ABORTED and "expected_page" in r.reason
    assert io.typed() == ""


def test_send_without_pages_file_is_refused(env):
    io, audit, runner, _ = env
    r = runner.run(flow(), pages=None, variables={"comment_draft": "x"})
    assert r.status is RunStatus.ABORTED
    assert io.typed() == ""


def test_dry_run_types_but_never_sends(env):
    io, audit, runner, book = env
    r = runner.run(flow(), pages=book, variables={"comment_draft": "好看"}, dry_run=True)
    assert r.status is RunStatus.DRY_RUN_STOPPED
    assert io.typed() == "好看"
    assert len([c for c in io.calls if c[0] == "click"]) == 1  # 只有第一下，没有发送
    assert audit.entries[-1].status is StepStatus.SKIPPED and audit.entries[-1].dry_run


def test_missing_variable_fails_before_touching_anything(env):
    io, audit, runner, book = env
    r = runner.run(flow(), pages=book)
    assert r.status is RunStatus.FAILED and "comment_draft" in r.reason
    assert io.calls == [] and io.calibrations == 0


def test_type_clears_leftover_text_first(env):
    io, _, runner, book = env
    runner.run(flow(), pages=book, variables={"comment_draft": "空跑残留"}, dry_run=True)
    runner.run(flow(), pages=book, variables={"comment_draft": "好看"})
    assert io.typed() == "好看"  # 残留文字没有跟着发出去
    assert io.calls.count(("clear",)) == 2


def test_clear_first_can_be_disabled(env):
    io, _, runner, book = env
    s = flow(variables=[], steps=[
        {"id": "s01", "action": "type", "text": "a", "expected_page": "compose", "clear_first": False},
    ])
    runner.run(s, pages=book)
    assert ("clear",) not in io.calls


def test_undeclared_braces_are_literal(env):
    io, _, runner, book = env
    s = flow(variables=[], steps=[
        {"id": "s01", "action": "type", "text": "{哈}", "expected_page": "compose"},
    ])
    runner.run(s, pages=book)
    assert io.typed() == "{哈}"


def test_calibration_failure(env):
    io, audit, runner, book = env
    io.fail_calibrate = "窗口太小"
    r = runner.run(flow(), pages=book, variables={"comment_draft": "x"})
    assert r.status is RunStatus.FAILED and audit.entries[-1].action == "calibrate"


def test_recalibrates_after_pause(env):
    io, _, runner, book = env

    class PausingGate(Gate):
        def wait(self):
            return True

    runner.run(flow(), pages=book, variables={"comment_draft": "x"}, gate=PausingGate())
    assert io.calibrations == 1 + 3


def test_panic_is_audited_and_propagates(env):
    io, audit, runner, book = env

    class PanicGate(Gate):
        def check(self):
            raise Cancelled()

    with pytest.raises(Cancelled):
        runner.run(flow(), pages=book, variables={"comment_draft": "x"}, gate=PanicGate())
    assert audit.entries[-1].status is StepStatus.CANCELLED


def test_panic_stops_a_long_delay_right_away(tmp_path):
    """延迟 30 秒：中途按急停马上停，不用等完；你动一下鼠标不会让延迟中止（下一步之前自然会暂停等你）。"""
    io = FakeIO(lambda: screen_with(pattern()))
    slept = []
    runner = Runner(io, ScreenshotStorage(tmp_path / "shots"), MemoryAudit(), rng=random.Random(0),
                    sleep=slept.append)
    s = flow(variables=[], steps=[{"id": "d01", "action": "wait", "seconds": 30}])

    class PanicAfter(Gate):
        def check(self):
            raise UserInterrupted()  # 延迟期间不看这个

        def check_cancel(self):
            if sum(slept) >= 1.0:
                raise Cancelled()

    with pytest.raises(Cancelled):
        runner.run(s, gate=PanicAfter())
    assert 1.0 <= sum(slept) < 1.2


def test_user_input_mid_step_aborts_without_sending(env):
    io, audit, runner, book = env
    calls = {"n": 0}

    class InterruptingGate(Gate):
        def check(self):
            calls["n"] += 1
            if calls["n"] > 40:  # 第一下点击之后、打字中途
                raise UserInterrupted()

    r = runner.run(flow(), pages=book,
                   variables={"comment_draft": "很长的一段评论" * 5}, gate=InterruptingGate())
    assert r.status is RunStatus.ABORTED
    assert len([c for c in io.calls if c[0] == "click"]) == 1
    assert audit.entries[-1].status is StepStatus.ABORTED


def test_closing_click_keeps_the_frame_before_it(env):
    """这一下把窗口关了（点「关闭」）：操作后截不到，就把点之前的画面留作证据，审计里照样能点开看。"""
    from cyber_worker.flow.io.base import CalibrationError
    from cyber_worker.flow.runner import CLOSED_NOTE

    io, audit, runner, book = env
    screen = screen_with(pattern())

    def capture():
        if any(c[0] == "click" for c in io.calls):
            raise CalibrationError("窗口已经关了或最小化了")
        return screen

    io.screen = capture
    s = flow(variables=[], steps=[{"id": "s01", "action": "click", "x": 380, "y": 280}])
    r = runner.run(s, pages=book)
    assert r.status is RunStatus.OK
    (e,) = audit.entries
    assert e.status is StepStatus.OK and e.screenshot and CLOSED_NOTE in e.detail


def test_type_step_on_a_shifted_page_runs_and_notes_it(env):
    """输入前核对页面：锚点挪了位置也认得出来，照常输入，记一笔「页面锚点挪了位置」。"""
    io, audit, runner, book = env
    io.screen = lambda: screen_with(pattern(), at=(250, 40))
    r = runner.run(flow(), pages=book, variables={"comment_draft": "好看"})
    assert r.status is RunStatus.OK and io.typed() == "好看"
    typed = next(e for e in audit.entries if e.step_id == "s02")
    assert "页面锚点挪了位置（+200, +0）" in typed.detail
