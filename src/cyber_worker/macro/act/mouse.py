"""人类式鼠标操作：贝塞尔轨迹 + 远距离偶尔过冲 + 落点微抖动；单击 / 双击 / 长按 / 拖动。

安全规则：凡是「按下不放」的（长按、拖动、按住修饰键），一律在 finally 里松开——
急停、你动了鼠标、出错，都不能留下一个一直按着的键。
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

from cyber_worker.macro.io.base import ScreenIO

Point = tuple[float, float]


def bezier_path(start: Point, end: Point, rng: random.Random, steps: int | None = None) -> list[Point]:
    """三次贝塞尔曲线，控制点在起止连线两侧随机偏移；终点精确等于 end。"""
    (x0, y0), (x3, y3) = start, end
    dist = math.hypot(x3 - x0, y3 - y0)
    if steps is None:
        steps = max(8, min(60, int(dist / 12)))
    # 法向量，控制点沿法向偏移，偏移量与距离成比例
    nx, ny = (-(y3 - y0), x3 - x0) if dist else (0.0, 0.0)
    norm = dist or 1.0
    nx, ny = nx / norm, ny / norm

    def ctrl(t: float) -> Point:
        off = rng.uniform(-0.25, 0.25) * dist
        return x0 + (x3 - x0) * t + nx * off, y0 + (y3 - y0) * t + ny * off

    (x1, y1), (x2, y2) = ctrl(rng.uniform(0.2, 0.4)), ctrl(rng.uniform(0.6, 0.8))
    path = []
    for i in range(1, steps + 1):
        t = i / steps
        # ease-in-out：起步和到达都慢，中间快
        t = t * t * (3 - 2 * t)
        u = 1 - t
        path.append((
            u**3 * x0 + 3 * u**2 * t * x1 + 3 * u * t**2 * x2 + t**3 * x3,
            u**3 * y0 + 3 * u**2 * t * y1 + 3 * u * t**2 * y2 + t**3 * y3,
        ))
    path[-1] = end
    return path


def move(
    io: ScreenIO,
    target: Point,
    rng: random.Random,
    sleep: Callable[[float], None],
    *,
    overshoot_prob: float = 0.3,
    duration: float | None = None,
    check: Callable[[], None] = lambda: None,
) -> None:
    start = io.mouse_position()
    dist = math.hypot(target[0] - start[0], target[1] - start[1])
    if duration is None:
        duration = 0.15 + dist / 2500 + rng.uniform(0, 0.1)

    waypoints = [target]
    if dist > 200 and rng.random() < overshoot_prob:
        dx, dy = (target[0] - start[0]) / dist, (target[1] - start[1]) / dist
        over = rng.uniform(4, 12)
        waypoints.insert(0, (target[0] + dx * over, target[1] + dy * over))

    pos = start
    for wp in waypoints:
        path = bezier_path(pos, wp, rng)
        for x, y in path:
            check()
            io.move_mouse(x, y)
            sleep(duration / len(path))
        pos = wp


@contextmanager
def held_keys(io: ScreenIO, keys: Sequence[str]) -> Iterator[None]:
    """按住修饰键（Ctrl+点击 这类），退出时一定松开，顺序与按下相反。"""
    down: list[str] = []
    try:
        for k in keys:
            io.key_down(k)
            down.append(k)
        yield
    finally:
        for k in reversed(down):
            io.key_up(k)


def _hold(io: ScreenIO, button: str, seconds: float, sleep, check) -> None:
    io.mouse_down(button)
    try:
        left = seconds
        while left > 0:  # 分段等，期间也能被急停 / 你的操作打断
            check()
            chunk = min(0.1, left)
            sleep(chunk)
            left -= chunk
    finally:
        io.mouse_up(button)


def click(
    io: ScreenIO,
    target: Point,
    button: str,
    rng: random.Random,
    sleep: Callable[[float], None],
    *,
    jitter: float = 1.0,
    check: Callable[[], None] = lambda: None,
    count: int = 1,
    hold: float = 0.0,
    modifiers: Sequence[str] = (),
) -> None:
    landed = (target[0] + rng.uniform(-jitter, jitter), target[1] + rng.uniform(-jitter, jitter))
    move(io, landed, rng, sleep, check=check)
    sleep(rng.uniform(0.05, 0.15))
    with held_keys(io, modifiers):
        if hold > 0:
            check()
            _hold(io, button, hold * rng.uniform(0.97, 1.03), sleep, check)
            return
        for i in range(count):
            check()
            io.click(button)
            if i < count - 1:
                # 双击：原地、间隔远小于系统双击时间（Windows / macOS 默认约 0.5 秒）
                sleep(rng.uniform(0.06, 0.12))


def drag(
    io: ScreenIO,
    start: Point,
    end: Point,
    button: str,
    rng: random.Random,
    sleep: Callable[[float], None],
    *,
    duration: float = 0.5,
    check: Callable[[], None] = lambda: None,
    modifiers: Sequence[str] = (),
) -> None:
    move(io, start, rng, sleep, check=check)
    sleep(rng.uniform(0.05, 0.12))
    with held_keys(io, modifiers):
        check()
        io.mouse_down(button)
        try:
            sleep(rng.uniform(0.08, 0.15))  # 按下后稍停，很多控件要这一下才进入拖动状态
            move(io, end, rng, sleep, overshoot_prob=0, duration=duration, check=check)
            sleep(rng.uniform(0.05, 0.1))
        finally:
            io.mouse_up(button)
