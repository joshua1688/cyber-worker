"""页面锚点：从一帧窗口截图里截取一块区域，登记到脚本所属的 pages.yaml。CLI 和 studio 共用。"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from cyber_worker.macro.schema import Script, Target
from cyber_worker.macro.screenshot.storage import write_image
from cyber_worker.macro.vision.fingerprint import Anchor, PageBook, PageFingerprint

_PAGE_NAME = re.compile(r"^[\w\-]+$")


def default_pages_ref(script_path: Path) -> str:
    """每个宏一份页面文件，放在宏文件旁边：不同宏的「页面1」互不冲突，搬动宏时连同它一起搬。"""
    return f"{script_path.stem}.pages.yaml"


def anchors_dir(script_path: Path) -> str:
    return f"{script_path.stem}.anchors"


def ensure_pages(script_path: Path) -> Script:
    script = Script.load(script_path)
    if script.pages is None:
        script.pages = default_pages_ref(script_path)
        script.dump(script_path)
        script = Script.load(script_path)
    return script


def add_anchor(
    script_path: Path,
    page: str,
    region: tuple[float, float, float, float],
    frame: np.ndarray,
    frame_scale: float,
    win: str | None = None,
) -> Anchor:
    page = re.sub(r"\s+", "_", page.strip())  # 「评论 框」这种直接接受，不让用户返工
    if not _PAGE_NAME.match(page):
        raise ValueError(f"页面名只能包含文字、数字、下划线和连字符：{page!r}")
    x, y, w, h = region
    if w < 4 or h < 4:
        raise ValueError("锚点区域太小（至少 4×4 点）")
    s = frame_scale
    crop = frame[int(y * s):int((y + h) * s), int(x * s):int((x + w) * s)]
    if crop.size == 0:
        raise ValueError("锚点区域超出截图范围")

    script = ensure_pages(script_path)
    pages_path = script.pages_path
    book = PageBook.load(pages_path)
    fp = book.pages.setdefault(page, PageFingerprint(anchors=[]))
    folder = anchors_dir(script_path) if script.pages == default_pages_ref(script_path) else "anchors"
    n = len(fp.anchors) + 1
    while (pages_path.parent / f"{folder}/{page}_{n}.png").exists():
        n += 1
    image = f"{folder}/{page}_{n}.png"
    write_image(pages_path.parent / image, crop)
    anchor = Anchor(image=image, x=x, y=y, w=w, h=h, scale=s, win=None if win in (None, "main") else win)
    fp.anchors.append(anchor)
    book.dump(pages_path)
    return anchor


def remove_anchor(script_path: Path, page: str, index: int) -> None:
    script = Script.load(script_path)
    if script.pages_path is None:
        raise ValueError("脚本还没有 pages.yaml")
    book = PageBook.load(script.pages_path)
    anchors = book.pages[page].anchors
    anchor = anchors.pop(index)
    if not anchors:
        del book.pages[page]
    book.dump(script.pages_path)
    (script.pages_path.parent / anchor.image).unlink(missing_ok=True)


def targets_dir(script_path: Path) -> str:
    """按图找的目标图，放在宏文件旁边的 <宏名>.targets/ 里，跟宏一起搬、一起删。"""
    return f"{script_path.stem}.targets"


def save_target(script_path: Path, step_id: str, frame: np.ndarray, box: tuple[float, float, float, float],
                frame_scale: float) -> Target:
    """从一帧窗口截图里截下 box（点）这块，存成这一步的目标图。"""
    x, y, w, h = box
    if w < 6 or h < 6:
        raise ValueError("目标区域太小（至少 6×6 点）")
    s = frame_scale
    crop = frame[int(y * s):int((y + h) * s), int(x * s):int((x + w) * s)]
    if crop.size == 0:
        raise ValueError("目标区域超出截图范围")
    return save_target_crop(script_path, step_id, crop, box, s)


def save_target_crop(script_path: Path, step_id: str, crop: np.ndarray, box: tuple[float, float, float, float],
                     scale: float) -> Target:
    """已经截好的目标图（录制时在后台线程里截的）直接存。"""
    x, y, w, h = box
    s = scale
    image = f"{targets_dir(script_path)}/{step_id}.png"
    write_image(script_path.parent / image, crop)
    return Target(image=image, x=round(x, 1), y=round(y, 1), w=round(w, 1), h=round(h, 1), scale=s)
