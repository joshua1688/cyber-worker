import threading
import time

import pytest
from fakes import FakeActivity

from cyber_worker.flow.executor import Executor
from cyber_worker.flow.runner import Cancelled, UserInterrupted


def test_jobs_never_overlap():
    ex = Executor(poll_interval=0.01)
    running, peak, lock = [0], [0], threading.Lock()

    def job(gate):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.01)
        with lock:
            running[0] -= 1
        return True

    futs = [ex.submit(job) for _ in range(10)]
    assert all(f.result(timeout=5) for f in futs)
    assert peak[0] == 1
    ex.shutdown()


def test_panic_cancels_running_and_queued():
    ex = Executor(poll_interval=0.01)
    started = threading.Event()

    def long_job(gate):
        started.set()
        while True:
            gate.check()
            time.sleep(0.005)

    first = ex.submit(long_job)
    queued = [ex.submit(lambda g: "never") for _ in range(3)]
    assert started.wait(2)
    assert ex.panic() == 3
    with pytest.raises(Cancelled):
        first.result(timeout=2)
    assert all(f.cancelled() for f in queued)
    # 急停之后提交的任务照常执行
    assert ex.submit(lambda g: "after").result(timeout=2) == "after"
    ex.shutdown()


def test_waits_while_user_is_active():
    activity = FakeActivity(idle=0)
    ex = Executor(activity, resume_after_idle_sec=60, poll_interval=0.01)
    fut = ex.submit(lambda g: "done")
    time.sleep(0.1)
    assert not fut.done()
    activity.idle = 61
    assert fut.result(timeout=2) == "done"
    ex.shutdown()


def test_user_input_mid_action_interrupts():
    activity = FakeActivity(idle=1000)
    ex = Executor(activity, resume_after_idle_sec=60, poll_interval=0.01)

    def job(gate):
        activity.idle = 0
        gate.check()

    with pytest.raises(UserInterrupted):
        ex.submit(job).result(timeout=2)
    ex.shutdown()


def test_pause_disabled_ignores_activity():
    ex = Executor(FakeActivity(idle=0), pause_on_user_input=False, poll_interval=0.01)
    assert ex.submit(lambda g: 1).result(timeout=2) == 1
    ex.shutdown()


def test_busy_counts_queued_and_running_jobs():
    """busy 不能有一瞬间误报空闲：空跑快捷键靠它挡住连按。"""
    ex = Executor(poll_interval=0.01)
    release = threading.Event()
    started = threading.Event()

    def slow(gate):
        started.set()
        release.wait(5)

    assert not ex.busy
    first = ex.submit(slow)
    queued = ex.submit(slow)
    assert ex.busy
    assert started.wait(5) and ex.busy
    assert ex.panic() == 1 and queued.cancelled()  # 排队的被取消，正在跑的还在
    assert ex.busy
    release.set()
    first.result(timeout=5)
    for _ in range(100):
        if not ex.busy:
            break
        time.sleep(0.01)
    assert not ex.busy
    ex.shutdown()
