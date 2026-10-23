"""页面指纹：判断当前窗口是不是某个页面。

一个页面由若干锚点组成，每个锚点是「窗口内某块区域应该长这样」的参考图。
全部锚点都命中才算在这个页面上。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import yaml
from pydantic import BaseModel, PrivateAttr

from cyber_worker.macro.screenshot.storage import read_image


class Anchor(BaseModel):
    image: str  # 参考图路径，相对 pages.yaml
    x: float  # 窗口内区域，单位点
    y: float
    w: float
    h: float
    scale: float = 1.0  # 参考图截取时的缩放倍率
    threshold: float = 0.85
    margin: float = 10  # 搜索时向外扩的点数，容忍轻微偏移
    win: str | None = None  # 截自哪个窗口（Script.windows 的键；None = 主窗口），画布上按窗口显示


class PageFingerprint(BaseModel):
    anchors: list[Anchor]


class PageBook(BaseModel):
    pages: dict[str, PageFingerprint] = {}

    _base: Path = PrivateAttr(default_factory=Path.cwd)
    _cache: dict[str, np.ndarray] = PrivateAttr(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> PageBook:
        path = Path(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
        book = cls.model_validate(raw or {})
        book._base = path.parent
        return book

    def dump(self, path: str | Path) -> None:
        Path(path).write_text(
            yaml.safe_dump(self.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def reference(self, anchor: Anchor) -> np.ndarray:
        if anchor.image not in self._cache:
            self._cache[anchor.image] = read_image(self._base / anchor.image)
        return self._cache[anchor.image]


@dataclass(frozen=True)
class PageMatch:
    page: str
    matched: bool
    score: float  # 各锚点得分的最小值


def _gray(img: np.ndarray) -> np.ndarray:
    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def match_anchor(book: PageBook, anchor: Anchor, shot: np.ndarray, scale: float) -> float:
    ref = _gray(book.reference(anchor))
    if anchor.scale != scale:
        f = scale / anchor.scale
        ref = cv2.resize(ref, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)

    # 在锚点区域外扩 margin 的范围内找参考图
    x0 = max(0, int((anchor.x - anchor.margin) * scale))
    y0 = max(0, int((anchor.y - anchor.margin) * scale))
    x1 = min(shot.shape[1], int((anchor.x + anchor.w + anchor.margin) * scale))
    y1 = min(shot.shape[0], int((anchor.y + anchor.h + anchor.margin) * scale))
    region = _gray(shot)[y0:y1, x0:x1]
    if region.shape[0] < ref.shape[0] or region.shape[1] < ref.shape[1]:
        return 0.0
    result = cv2.matchTemplate(region, ref, cv2.TM_CCOEFF_NORMED)
    score = float(result.max())
    return score if np.isfinite(score) else 0.0


def match_page(book: PageBook, page: str, shot: np.ndarray, scale: float) -> PageMatch:
    fp = book.pages.get(page)
    if fp is None or not fp.anchors:
        # 没定义的页面一律视为不匹配：宁可不发
        return PageMatch(page, False, 0.0)
    scores = [(match_anchor(book, a, shot, scale), a.threshold) for a in fp.anchors]
    score = min(s for s, _ in scores)
    return PageMatch(page, all(s >= t for s, t in scores), score)
