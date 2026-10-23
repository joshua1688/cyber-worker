"""按图定位：在窗口截图里找目标图，算出这次该点哪里（点击处相对目标的位置和录制时一样）。"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from cyber_worker.macro.schema import Click
from cyber_worker.macro.vision.template import find_all


@dataclass(frozen=True)
class Located:
    found: bool
    x: float = 0.0  # 这次要点的位置（窗口内，点）
    y: float = 0.0
    dx: float = 0.0  # 目标相对录制时挪了多少（点）
    dy: float = 0.0
    score: float = 0.0  # 找到时是那一处的得分，没找到时是全图最高分
    count: int = 0  # 窗口里有几处像目标


def locate_click(step: Click, ref: np.ndarray, shot: np.ndarray, scale: float) -> Located:
    """有好几处都像（几个一样的按钮）时，选离录制时位置最近的那个。"""
    t = step.target
    if t.scale != scale:
        f = scale / t.scale
        ref = cv2.resize(ref, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    hits, best = find_all(shot, ref, t.threshold)
    if not hits:
        return Located(False, score=best)
    hit = min(hits, key=lambda h: math.hypot(h.x / scale - t.x, h.y / scale - t.y))
    dx, dy = hit.x / scale - t.x, hit.y / scale - t.y
    return Located(True, round(step.x + dx, 1), round(step.y + dy, 1), round(dx, 1), round(dy, 1),
                   hit.score, len(hits))
