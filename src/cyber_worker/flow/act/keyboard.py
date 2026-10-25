"""人类式打字：lognormal 按键间隔 + 偶尔打错再退格。"""

from __future__ import annotations

import math
import random
from collections.abc import Callable

from cyber_worker.flow.io.base import ScreenIO

# QWERTY 相邻键，用来模拟手滑
_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
_NEIGHBORS: dict[str, str] = {}
for _r, _row in enumerate(_ROWS):
    for _c, _ch in enumerate(_row):
        _n = [_row[i] for i in (_c - 1, _c + 1) if 0 <= i < len(_row)]
        _NEIGHBORS[_ch] = "".join(_n)


def interval(rng: random.Random, median: float = 0.12, sigma: float = 0.45) -> float:
    return rng.lognormvariate(math.log(median), sigma)


def type_text(
    io: ScreenIO,
    text: str,
    rng: random.Random,
    sleep: Callable[[float], None],
    *,
    typo_prob: float = 0.02,
    check: Callable[[], None] = lambda: None,
) -> None:
    """check 每个字符前调用一次，急停时由它抛异常中断。"""
    for ch in text:
        check()
        lower = ch.lower()
        if lower in _NEIGHBORS and rng.random() < typo_prob:
            wrong = rng.choice(_NEIGHBORS[lower])
            io.type_char(wrong.upper() if ch.isupper() else wrong)
            sleep(interval(rng) * 2)  # 发现打错要愣一下
            io.press_combo("backspace")
            sleep(interval(rng))
        io.type_char(ch)
        sleep(interval(rng))
