"""画面变化检测：两帧之间是不是「真的变了」。

录制时每一步都截一张窗口画面，只有和上一张相比真的变了才另存，没变就共用上一张。
光标闪烁、输入框里多几个字、未读数字跳一下这种小变化不算；点开会话、弹出菜单、出现新消息算。
"""

from __future__ import annotations

import cv2
import numpy as np

PIXEL_DIFF = 24  # 灰度差超过这么多才算这个像素变了（压掉压缩噪声、抗锯齿）
CHANGED_RATIO = 0.005  # 变了的像素超过这个比例才算画面变了（一行字约 0.3%）


def _small_gray(img: np.ndarray) -> np.ndarray:
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)


def changed_ratio(a: np.ndarray, b: np.ndarray) -> float:
    """变了的像素占多少（0~1）；尺寸不一样（窗口被拉伸过）算全变了。"""
    if a.shape[:2] != b.shape[:2]:
        return 1.0
    diff = cv2.absdiff(_small_gray(a), _small_gray(b))
    return float(np.count_nonzero(diff > PIXEL_DIFF)) / diff.size


def frame_changed(a: np.ndarray, b: np.ndarray, ratio: float = CHANGED_RATIO) -> bool:
    return changed_ratio(a, b) > ratio
