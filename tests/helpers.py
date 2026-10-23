from __future__ import annotations

from pathlib import Path

import numpy as np

from cyber_worker.macro.screenshot.storage import write_image
from cyber_worker.macro.vision.fingerprint import Anchor, PageBook, PageFingerprint


def pattern(w: int = 40, h: int = 30, seed: int = 1) -> np.ndarray:
    """有纹理的小图，模板匹配才有意义（纯色图的相关系数没有定义）。"""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)


def screen_with(patch: np.ndarray | None, at=(50, 40), size=(400, 300), scale: int = 1) -> np.ndarray:
    w, h = size
    img = np.full((h * scale, w * scale, 3), 255, dtype=np.uint8)
    if patch is not None:
        if scale != 1:
            patch = np.kron(patch, np.ones((scale, scale, 1), dtype=np.uint8))
        x, y = at[0] * scale, at[1] * scale
        img[y:y + patch.shape[0], x:x + patch.shape[1]] = patch
    return img


def make_book(tmp: Path, page: str = "compose", patch: np.ndarray | None = None, at=(50, 40)) -> PageBook:
    patch = pattern() if patch is None else patch
    write_image(tmp / "anchors" / f"{page}.png", patch)
    book = PageBook(pages={page: PageFingerprint(anchors=[
        Anchor(image=f"anchors/{page}.png", x=at[0], y=at[1], w=patch.shape[1], h=patch.shape[0])
    ])})
    book.dump(tmp / "pages.yaml")
    return PageBook.load(tmp / "pages.yaml")
