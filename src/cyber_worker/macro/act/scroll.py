"""人类式滚动：分段滚、段间停顿、偶尔往回滚一格。支持纵向和横向。"""

from __future__ import annotations

import random
from collections.abc import Callable

from cyber_worker.macro.io.base import ScreenIO


def segments(amount: int, rng: random.Random, back_prob: float = 0.1) -> list[int]:
    """把总滚动量切成若干段；回滚段会被补偿，总和恒等于 amount。"""
    if amount == 0:
        return []
    sign = 1 if amount > 0 else -1
    remaining, out = abs(amount), []
    while remaining > 0:
        n = min(remaining, rng.randint(1, 3))
        out.append(sign * n)
        remaining -= n
        if remaining > 0 and rng.random() < back_prob:
            out.append(-sign)
            remaining += 1
    return out


def scroll(
    io: ScreenIO,
    dy: int,
    rng: random.Random,
    sleep: Callable[[float], None],
    *,
    dx: int = 0,
    check: Callable[[], None] = lambda: None,
) -> None:
    for axis, amount in (("y", dy), ("x", dx)):
        for seg in segments(amount, rng):
            check()
            # 一段内逐格滚，模拟惯性：越往后越慢
            step = 1 if seg > 0 else -1
            for i in range(abs(seg)):
                if axis == "y":
                    io.scroll(step, 0)
                else:
                    io.scroll(0, step)
                sleep(0.02 + 0.03 * i)
            sleep(rng.uniform(0.3, 1.2))
