"""自动选页面锚点：从录制期间的多帧截图里，找一块「从头到尾没变过、有纹理、在画面里独一无二」的区域。

用户不用自己框选。选区偏好窗口上部（聊天对象名字、页面标题一般在那里），
避开：录制中变化过的地方（消息列表、输入框）、点击过的位置（按钮悬停会变色）、
右上角的窗口按钮（所有窗口都一样，认不出是哪个页面）。
"""

from __future__ import annotations

from collections.abc import Sequence

import cv2
import numpy as np

Region = tuple[float, float, float, float]  # x, y, w, h（点）


def _gray(img: np.ndarray) -> np.ndarray:
    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def pick_stable_region(
    frames: Sequence[np.ndarray],
    scale: float,
    *,
    avoid: Sequence[tuple[float, float]] = (),
    size: tuple[float, float] = (140, 32),
    step: float = 8,
    min_texture: float = 6.0,
    max_similar: float = 0.85,
) -> Region | None:
    """返回窗口内坐标（点）；找不到合适的返回 None。frames[0] 作为基准帧。"""
    if not frames:
        return None
    base = _gray(frames[0]).astype(np.int16)
    h, w = base.shape
    pw, ph = int(size[0] * scale), int(size[1] * scale)
    if pw >= w or ph >= h:
        return None

    # 录制过程中任何一帧变过的像素都不能用
    changed = np.zeros((h, w), np.uint8)
    for f in frames[1:]:
        g = _gray(f).astype(np.int16)
        if g.shape == base.shape:
            changed |= (np.abs(g - base) > 18).astype(np.uint8)
    changed = cv2.dilate(changed, np.ones((5, 5), np.uint8))

    gx = cv2.Sobel(base.astype(np.float32), cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(base.astype(np.float32), cv2.CV_32F, 0, 1)
    texture = cv2.magnitude(gx, gy)

    # 积分图：O(1) 求任意矩形内的和
    ii_changed = cv2.integral(changed)
    ii_tex = cv2.integral(texture)

    def rect_sum(ii, x, y):
        return ii[y + ph, x + pw] - ii[y, x + pw] - ii[y + ph, x] + ii[y, x]

    margin = int(6 * scale)
    stride = max(1, int(step * scale))
    avoid_px = [(ax * scale, ay * scale) for ax, ay in avoid]
    keep_out = 40 * scale  # 离点击位置至少这么远
    corner_w, corner_h = int(170 * scale), int(44 * scale)  # 右上角窗口按钮

    candidates = []
    for y in range(margin, h - ph - margin, stride):
        for x in range(margin, w - pw - margin, stride):
            if rect_sum(ii_changed, x, y) > 0:
                continue
            if x + pw > w - corner_w and y < corner_h:
                continue
            if any(x - keep_out <= ax <= x + pw + keep_out and y - keep_out <= ay <= y + ph + keep_out
                   for ax, ay in avoid_px):
                continue
            tex = rect_sum(ii_tex, x, y) / (pw * ph)
            if tex < min_texture:
                continue
            candidates.append((tex * (1.6 - y / h), x, y))  # 越靠上越优先
    candidates.sort(reverse=True)

    img = base.astype(np.uint8)
    for _, x, y in candidates[:60]:
        crop = img[y:y + ph, x:x + pw]
        res = cv2.matchTemplate(img, crop, cv2.TM_CCOEFF_NORMED)
        # 抹掉自身附近，看画面里还有没有长得像的地方
        res[max(0, y - ph):y + ph, max(0, x - pw):x + pw] = -1
        if float(res.max()) < max_similar:
            return x / scale, y / scale, pw / scale, ph / scale
    return None
