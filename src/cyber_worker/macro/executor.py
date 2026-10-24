"""全局执行器：所有操作鼠标键盘的任务都从这里排队，同一时刻只有一个在跑。

- 你在用电脑时（距上次真实输入不足 resume_after_idle_sec 秒），任务开始前和每步之间都会暂停等待；
  步骤进行到一半你开始用电脑，该步骤立即中止（UserInterrupted）。
- panic() 急停：正在跑的任务在下一个检查点抛 Cancelled，排队中的任务全部取消。
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import TypeVar

from cyber_worker.macro.io.base import ActivityMonitor
from cyber_worker.macro.runner import Cancelled, Gate, UserInterrupted

T = TypeVar("T")
WaitCallback = Callable[[float], None]


class _JobGate(Gate):
    def __init__(self, executor: Executor, generation: int, idle_sec: float) -> None:
        self._ex = executor
        self._gen = generation
        self._idle_sec = idle_sec

    def _check_cancel(self) -> None:
        if self._ex._generation != self._gen:
            raise Cancelled()

    def check_cancel(self) -> None:
        self._check_cancel()

    def check(self) -> None:
        self._check_cancel()
        if self._ex._user_active(self._idle_sec):
            raise UserInterrupted()

    def wait(self, on_wait: Callable[[float], None] | None = None) -> bool:
        """on_wait(还要等几秒)：等待期间每次检查都报一次，界面拿去显示倒计时（你一动就回到满值）。"""
        paused = False
        while True:
            self._check_cancel()
            remaining = self._ex._idle_remaining(self._idle_sec)
            if remaining <= 0:
                return paused
            paused = True
            if on_wait:
                on_wait(remaining)
            # 剩得不多时只睡到点，倒计时归零就开始，不用多等一个检查间隔
            self._ex._sleep(min(self._ex.poll_interval, remaining))


class Executor:
    def __init__(
        self,
        activity: ActivityMonitor | None = None,
        *,
        pause_on_user_input: bool = True,
        resume_after_idle_sec: float = 120,
        poll_interval: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.activity = activity
        self.pause_on_user_input = pause_on_user_input
        self.resume_after_idle_sec = resume_after_idle_sec
        self.poll_interval = poll_interval
        self._sleep = sleep
        self._queue: queue.Queue[tuple[Callable[[Gate], object], Future, float, WaitCallback | None] | None] = (
            queue.Queue())
        self._running = False
        # 排队 + 正在跑的任务数。busy 看它：取出任务到标记「在跑」之间队列已空，只看两者会有一瞬间误报空闲
        self._pending = 0
        self._generation = 0
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._loop, name="executor", daemon=True)
        self._worker.start()

    def submit(self, job: Callable[[Gate], T], *, resume_after_idle_sec: float | None = None,
               on_wait: WaitCallback | None = None) -> Future[T]:
        """resume_after_idle_sec 覆盖本任务的空闲阈值：studio 里你刚点完「执行」，只需等几秒。

        on_wait(还要等几秒)：开始前等你停手时反复调用，用来显示倒计时。
        """
        fut: Future[T] = Future()
        idle = self.resume_after_idle_sec if resume_after_idle_sec is None else resume_after_idle_sec
        with self._lock:
            self._pending += 1
        self._queue.put((job, fut, idle, on_wait))
        return fut

    def _finished(self) -> None:
        with self._lock:
            self._pending -= 1

    @property
    def busy(self) -> bool:
        return self._pending > 0

    def panic(self) -> int:
        """急停。返回被取消的排队任务数。"""
        with self._lock:
            self._generation += 1
            dropped = 0
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:  # 别把关闭信号吞掉
                    self._queue.put(None)
                    break
                item[1].cancel()
                self._pending -= 1
                dropped += 1
            return dropped

    def shutdown(self, wait: bool = True) -> None:
        self._queue.put(None)
        if wait:
            self._worker.join()

    def _user_active(self, idle_sec: float) -> bool:
        return self._idle_remaining(idle_sec) > 0

    def _idle_remaining(self, idle_sec: float) -> float:
        """还要再空闲几秒才算「你不在用电脑」；0 表示现在就可以动手。"""
        if not self.pause_on_user_input or self.activity is None:
            return 0.0
        return max(0.0, idle_sec - self.activity.seconds_since_user_input())

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job, fut, idle, on_wait = item
            if not fut.set_running_or_notify_cancel():
                self._finished()
                continue
            with self._lock:
                gate = _JobGate(self, self._generation, idle)
            self._running = True
            try:
                gate.wait(on_wait)
                fut.set_result(job(gate))
            except BaseException as e:  # noqa: BLE001 —— 包括 Cancelled，都交给调用方
                fut.set_exception(e)
            finally:
                self._running = False
                self._finished()
