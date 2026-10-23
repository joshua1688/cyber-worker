"""模板匹配：在截图里找一块小图（所有出现的位置），以及录制时从点击处挑一块「认得出来」的目标图。"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

Box = tuple[float, float, float, float]  # x, y, w, h（点）


def gray(img: np.ndarray) -> np.ndarray:
    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


@dataclass(frozen=True)
class Hit:
    x: float  # 左上角（像素）
    y: float
    score: float


def find_all(shot: np.ndarray, ref: np.ndarray, threshold: float, *, limit: int = 20) -> tuple[list[Hit], float]:
    """ref 在 shot 里所有得分 ≥ threshold 的位置（互不重叠，按得分从高到低），以及全图最高分。"""
    s, r = gray(shot), gray(ref)
    if r.shape[0] > s.shape[0] or r.shape[1] > s.shape[1] or r.std() < 1:
        return [], 0.0
    res = cv2.matchTemplate(s, r, cv2.TM_CCOEFF_NORMED)
    res = np.nan_to_num(res, nan=0.0)
    best = float(res.max())
    hits: list[Hit] = []
    rh, rw = r.shape
    for _ in range(limit):
        _, score, _, (x, y) = cv2.minMaxLoc(res)
        if score < threshold:
            break
        hits.append(Hit(x, y, float(score)))
        # 抹掉这一处附近，找下一处（同一个目标挪一两个像素的高分不算第二处）
        res[max(0, y - rh // 2):y + rh // 2 + 1, max(0, x - rw // 2):x + rw // 2 + 1] = -1
    return hits, best


def _texture(g: np.ndarray) -> float:
    gx = cv2.Sobel(g.astype(np.float32), cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(g.astype(np.float32), cv2.CV_32F, 0, 1)
    return float(cv2.magnitude(gx, gy).mean())


# 从小到大试：越小越不容易被旁边变化的内容（时间、未读数）干扰；太小又容易认错
TARGET_SIZES = ((48, 40), (72, 44), (110, 52), (160, 60), (220, 68))


def pick_target(frame: np.ndarray, x: float, y: float, scale: float, *, min_texture: float = 10.0,
                max_similar: float = 0.8) -> Box | None:
    """录制时：以点击处 (x, y)（点）为中心挑一块目标区域。

    要求有纹理（不是空白、纯色输入框），而且在整个窗口里独一无二（别处没有长得差不多的），
    否则返回 None，这一步就保持固定坐标。
    """
    g = gray(frame)
    h, w = g.shape
    for bw, bh in TARGET_SIZES:
        pw, ph = int(bw * scale), int(bh * scale)
        if pw >= w or ph >= h:
            break
        # 尽量以点击处为中心；靠边时整体往里挪，点击处仍在框内
        x0 = int(min(max(0, x * scale - pw / 2), w - pw))
        y0 = int(min(max(0, y * scale - ph / 2), h - ph))
        crop = g[y0:y0 + ph, x0:x0 + pw]
        if _texture(crop) < min_texture:
            continue
        hits, _ = find_all(g, crop, max_similar, limit=3)
        others = [t for t in hits if abs(t.x - x0) > pw / 4 or abs(t.y - y0) > ph / 4]
        if not others:
            return x0 / scale, y0 / scale, pw / scale, ph / scale
    return None
