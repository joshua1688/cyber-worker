import threading
import time

import pytest

from cyber_worker.macro.executor import Executor
from cyber_worker.macro.runner import Cancelled, UserInterrupted
from fakes import FakeActivity


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
