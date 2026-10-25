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

from cyber_worker.flow.screenshot.storage import read_image


class Anchor(BaseModel):
    image: str  # 参考图路径，相对 pages.yaml
    x: float  # 窗口内区域，单位点
    y: float
    w: float
    h: float
    scale: float = 1.0  # 参考图截取时的缩放倍率
    threshold: float = 0.85
    margin: float = 10  # 搜索时向外扩的点数，容忍轻微偏移
    win: str | None = None  # 截自哪个窗口（Flow.windows 的键；None = 主窗口），画布上按窗口显示


class PageFingerprint(BaseModel):
    anchors: list[Anchor]


class PageBook(BaseModel):
    pages: dict[str, PageFingerprint] = {}
    # 没有步骤再用的页面：重新录制时从 pages 里清出来（画面上不再显示、核对时不用），锚点图还留着——
    # 撤销录制回来的步骤用到它时放回 pages（anchors.restore_pages）。retired_at：清出来的时间（秒），
    # studio 启动时过了很久的才真正删掉（state.collect_garbage）
    retired: dict[str, PageFingerprint] = {}
    retired_at: dict[str, float] = {}

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
    shift: tuple[float, float] | None = None  # 有锚点挪了位置（界面布局变了）：挪了多少点；都在原处是 None


@dataclass(frozen=True)
class AnchorHit:
    score: float
    dx: float = 0.0  # 锚点离录制时的位置挪了多少（点）。只有在原位置附近找不到、换成在整个窗口里找到时才不是 0
    dy: float = 0.0

    @property
    def moved(self) -> bool:
        return bool(self.dx or self.dy)


UNIQUE_GAP = 0.05  # 整个窗口里找时，第二像的地方得分离门槛不到这么多：有两处都像，认不准，不认


def _gray(img: np.ndarray) -> np.ndarray:
    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _scores(region: np.ndarray, ref: np.ndarray) -> np.ndarray | None:
    if region.shape[0] < ref.shape[0] or region.shape[1] < ref.shape[1]:
        return None
    return np.nan_to_num(cv2.matchTemplate(region, ref, cv2.TM_CCOEFF_NORMED), nan=0.0)


def locate_anchor(book: PageBook, anchor: Anchor, shot: np.ndarray, scale: float) -> AnchorHit:
    """先在锚点区域外扩 margin 的范围内找（容忍轻微偏移）。找不到再在整个窗口里找：界面布局变了
    （QQ 多开一个会话，左边多出一栏，聊天内容整体右移），锚点还在、只是挪了位置。整个窗口里找的结果
    门槛不降，而且窗口里只能有一处像它：页面核对是防止把字打错地方的，宁可不认，不能认错。"""
    ref = _gray(book.reference(anchor))
    if anchor.scale != scale:
        f = scale / anchor.scale
        ref = cv2.resize(ref, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    gray = _gray(shot)

    x0 = max(0, int((anchor.x - anchor.margin) * scale))
    y0 = max(0, int((anchor.y - anchor.margin) * scale))
    x1 = min(gray.shape[1], int((anchor.x + anchor.w + anchor.margin) * scale))
    y1 = min(gray.shape[0], int((anchor.y + anchor.h + anchor.margin) * scale))
    near = _scores(gray[y0:y1, x0:x1], ref)
    near_score = float(near.max()) if near is not None else 0.0
    if near_score >= anchor.threshold:
        return AnchorHit(near_score)

    whole = _scores(gray, ref)
    if whole is None:
        return AnchorHit(near_score)
    _, best, _, (bx, by) = cv2.minMaxLoc(whole)
    if best < anchor.threshold:
        return AnchorHit(near_score)
    h, w = ref.shape[:2]
    others = whole.copy()
    others[max(0, by - h // 2):by + h // 2 + 1, max(0, bx - w // 2):bx + w // 2 + 1] = 0  # 抹掉找到的那一处
    if float(others.max()) >= anchor.threshold - UNIQUE_GAP:
        return AnchorHit(near_score)
    return AnchorHit(float(best), round(bx / scale - anchor.x), round(by / scale - anchor.y))


def match_anchor(book: PageBook, anchor: Anchor, shot: np.ndarray, scale: float) -> float:
    return locate_anchor(book, anchor, shot, scale).score


def match_page(book: PageBook, page: str, shot: np.ndarray, scale: float) -> PageMatch:
    fp = book.pages.get(page)
    if fp is None or not fp.anchors:
        # 没定义的页面一律视为不匹配：宁可不发
        return PageMatch(page, False, 0.0)
    hits = [(locate_anchor(book, a, shot, scale), a.threshold) for a in fp.anchors]
    score = min(h.score for h, _ in hits)
    moved = next(((h.dx, h.dy) for h, _ in hits if h.moved), None)
    return PageMatch(page, all(h.score >= t for h, t in hits), score, moved)
