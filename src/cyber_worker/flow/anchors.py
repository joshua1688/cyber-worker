"""页面锚点：从一帧窗口截图里截取一块区域，登记到流程所属的 pages.yaml。CLI 和 studio 共用。"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from cyber_worker.flow.schema import (
    MAIN,
    Click,
    Drag,
    Flow,
    Scroll,
    Target,
    WaitPage,
    needs_page_check,
)
from cyber_worker.flow.screenshot.storage import read_image, write_image
from cyber_worker.flow.vision.auto_anchor import pick_stable_region
from cyber_worker.flow.vision.fingerprint import (
    Anchor,
    PageBook,
    PageFingerprint,
    match_page,
)

_PAGE_NAME = re.compile(r"^[\w\-]+$")


def default_pages_ref(flow_path: Path) -> str:
    """每个流程一份页面文件，放在流程文件旁边：不同流程的「页面1」互不冲突，搬动流程时连同它一起搬。"""
    return f"{flow_path.stem}.pages.yaml"


def anchors_dir(flow_path: Path) -> str:
    return f"{flow_path.stem}.anchors"


def ensure_pages(flow_path: Path) -> Flow:
    flow = Flow.load(flow_path)
    if flow.pages is None:
        flow.pages = default_pages_ref(flow_path)
        flow.dump(flow_path)
        flow = Flow.load(flow_path)
    return flow


def add_anchor(
    flow_path: Path,
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

    flow = ensure_pages(flow_path)
    pages_path = flow.pages_path
    book = PageBook.load(pages_path)
    book.retired.pop(page, None)  # 同名的旧页面（录制时清出去的）就此作废，免得撤销时把它放回来顶掉这个
    book.retired_at.pop(page, None)
    fp = book.pages.setdefault(page, PageFingerprint(anchors=[]))
    folder = anchors_dir(flow_path) if flow.pages == default_pages_ref(flow_path) else "anchors"
    n = len(fp.anchors) + 1
    while (pages_path.parent / f"{folder}/{page}_{n}.png").exists():
        n += 1
    image = f"{folder}/{page}_{n}.png"
    write_image(pages_path.parent / image, crop)
    anchor = Anchor(image=image, x=x, y=y, w=w, h=h, scale=s, win=None if win in (None, "main") else win)
    fp.anchors.append(anchor)
    book.dump(pages_path)
    return anchor


def free_page_name(book: PageBook, taken: tuple[str, ...] = ()) -> str:
    """下一个没用过的「页面N」。清出去（retired）的名字也不重用：撤销录制时要把它放回来。"""
    n = 1
    while f"页面{n}" in book.pages or f"页面{n}" in book.retired or f"页面{n}" in taken:
        n += 1
    return f"页面{n}"


def auto_page(flow_path: Path, step_id: str, latest: np.ndarray | None = None) -> dict:
    """给一步自动设「在哪个画面执行」：先看这一步所在窗口已有的页面对不对得上这一步录制时的画面，
    对得上就沿用；否则从这个窗口各步骤的录制画面里挑一块稳定、独一无二的区域新建页面。
    同一窗口里还没设页面的输入 / 发送步骤一起填上（调用方不用再 dump）。latest：这个窗口最近截的画面，没有录制画面时用。
    返回 {"page", "reused", "steps"}；选不出来抛 ValueError（中文，说明怎么办）。"""
    flow = Flow.load(flow_path)
    step = next((s for s in flow.steps if s.id == step_id), None)
    if step is None:
        raise ValueError("这一步已经不在了")
    key = step.win
    scale = flow.window_for(key).scale
    # 这一步自己的画面排第一（新页面的锚点从它上面截），再加同一窗口其它步骤的画面，最后是参考图、最近截的画面
    paths = [s.shot for s in [step, *flow.steps] if s.win == key and s.shot]
    ref = flow.reference_for(key)
    if ref is not None:
        paths.append(ref.relative_to(flow.base_dir).as_posix())
    frames = [read_image(flow.base_dir / p) for p in dict.fromkeys(paths) if (flow.base_dir / p).exists()]
    if latest is not None:
        frames.append(latest)
    if not frames:
        raise ValueError("这个窗口还没有画面：先点画布上的「截取当前窗口」，再点「自动选一个」")
    book = PageBook.load(flow.pages_path) if flow.pages_path and flow.pages_path.exists() else PageBook()
    page = next((name for name, fp in book.pages.items()
                 if fp.anchors and all((a.win or None) == key for a in fp.anchors)
                 and match_page(book, name, frames[0], scale).matched), None)
    reused = page is not None
    if not reused:
        in_win = [s for s in flow.steps if s.win == key]
        avoid = [(s.x, s.y) for s in in_win if isinstance(s, (Click, Scroll, Drag)) and s.x is not None]
        avoid += [(s.x2, s.y2) for s in in_win if isinstance(s, Drag)]
        region = pick_stable_region(frames, scale, avoid=avoid)
        if region is None:
            raise ValueError("这个窗口的画面里找不到一块不会变、又认得出来的区域：请点「我自己框」，框一块标题或名字")
        page = free_page_name(book)
        add_anchor(flow_path, page, region, frames[0], scale, key)
    flow = Flow.load(flow_path)  # add_anchor 可能刚写入了 pages 字段
    if isinstance(step, WaitPage):  # 等待页面：等的就是这个页面
        ids = [step_id]
        next(s for s in flow.steps if s.id == step_id).page = page
    else:
        ids = [s.id for s in flow.steps
               if s.win == key and (s.id == step_id or (needs_page_check(s) and not s.expected_page))]
        for s in flow.steps:
            if s.id in ids:
                s.expected_page = page
    flow.dump(flow_path)
    return {"page": page, "reused": reused, "steps": ids}


def remove_anchor(flow_path: Path, page: str, index: int) -> None:
    flow = Flow.load(flow_path)
    if flow.pages_path is None:
        raise ValueError("流程还没有 pages.yaml")
    book = PageBook.load(flow.pages_path)
    anchors = book.pages[page].anchors
    anchor = anchors.pop(index)
    if not anchors:
        del book.pages[page]
    book.dump(flow.pages_path)
    (flow.pages_path.parent / anchor.image).unlink(missing_ok=True)


def refs_dir(flow_path: Path) -> str:
    """主窗口以外的窗口的参考截图，放在流程文件旁边的 <流程名>.refs/<窗口键>.png。"""
    return f"{flow_path.stem}.refs"


def save_reference(flow_path: Path, flow: Flow, key: str | None, img: np.ndarray) -> None:
    """把这一帧存成窗口 key 的参考图，并记进 flow（调用方负责 dump）。
    主窗口是 <流程名>.png（Flow.reference），其他窗口是 <流程名>.refs/<键>.png（Window.reference）。"""
    if key in (None, MAIN):
        flow.reference = flow.reference or f"{flow_path.stem}.png"
        rel = flow.reference
    else:
        rel = f"{refs_dir(flow_path)}/{key}.png"
        flow.window_for(key).reference = rel
    write_image(flow_path.parent / rel, img)


def shots_dir(flow_path: Path) -> str:
    """每一步录制时的窗口画面，放在流程文件旁边的 <流程名>.shots/<步骤编号>.png。"""
    return f"{flow_path.stem}.shots"


def replace_frame(flow_path: Path, flow: Flow, key: str | None, img: np.ndarray,
                  step_id: str | None = None) -> list[str]:
    """把画布上的「录制画面」换成 img（调用方负责 dump）：给了 step_id 换这一步的画面，否则换这个窗口显示的那张
    （Flow.shown_reference：它第一步的画面，没有就是参考图）。和它共用同一张图的步骤一起换。
    存成新文件、旧图不删，界面上撤销能换回来。返回换了画面的步骤编号（换的是参考图就是空列表）。"""
    key = None if key in (None, MAIN) else key
    if step_id is not None:
        step = next((s for s in flow.steps if s.id == step_id), None)
        if step is None or not step.shot:
            raise ValueError("这一步没有录制时的画面")
        old = step.shot
    else:
        shown = flow.shown_reference(key)
        old = shown.relative_to(flow.base_dir).as_posix() if shown is not None else None
    sharing = [s for s in flow.steps if old and s.shot == old]
    if sharing:
        stem = re.sub(r"-\d+$", "", Path(old).stem)  # s01-2 → s01
        base = f"{shots_dir(flow_path)}/{stem}"
    elif old is None or not (flow.base_dir / old).exists():
        save_reference(flow_path, flow, key, img)  # 还没有参考图：直接存成参考图
        return []
    else:
        base = f"{refs_dir(flow_path)}/{key or MAIN}"
    rel, n = f"{base}-2.png", 3
    while (flow_path.parent / rel).exists():
        rel, n = f"{base}-{n}.png", n + 1
    write_image(flow_path.parent / rel, img)
    for s in sharing:
        s.shot = rel
    if not sharing:
        if key is None:
            flow.reference = rel
        else:
            flow.window_for(key).reference = rel
    return [s.id for s in sharing]


def restore_pages(flow: Flow) -> list[str]:
    """步骤用到的页面要是被录制清出去了（retired），放回来：撤销重新录制后旧步骤还能核对页面。返回放回的页面名。"""
    if flow.pages_path is None or not flow.pages_path.exists():
        return []
    used = {s.expected_page for s in flow.steps} | {getattr(s, "page", None) for s in flow.steps}
    book = PageBook.load(flow.pages_path)
    back = [name for name in book.retired if name in used and name not in book.pages]
    for name in back:
        book.pages[name] = book.retired.pop(name)
        book.retired_at.pop(name, None)
    if back:
        book.dump(flow.pages_path)
    return back


def save_shot(flow_path: Path, step_id: str, img: np.ndarray, *, keep_existing: bool = False) -> str:
    """存这一步的画面，返回相对流程文件的路径。keep_existing 同 save_target_crop：重新录制时旧图留给撤销用。"""
    rel, n = f"{shots_dir(flow_path)}/{step_id}.png", 2
    while keep_existing and (flow_path.parent / rel).exists():
        rel, n = f"{shots_dir(flow_path)}/{step_id}-{n}.png", n + 1
    write_image(flow_path.parent / rel, img)
    return rel


def targets_dir(flow_path: Path) -> str:
    """按图找的目标图，放在流程文件旁边的 <流程名>.targets/ 里，跟流程一起搬、一起删。"""
    return f"{flow_path.stem}.targets"


def save_target(flow_path: Path, step_id: str, frame: np.ndarray, box: tuple[float, float, float, float],
                frame_scale: float) -> Target:
    """从一帧窗口截图里截下 box（点）这块，存成这一步的目标图。"""
    x, y, w, h = box
    if w < 6 or h < 6:
        raise ValueError("目标区域太小（至少 6×6 点）")
    s = frame_scale
    crop = frame[int(y * s):int((y + h) * s), int(x * s):int((x + w) * s)]
    if crop.size == 0:
        raise ValueError("目标区域超出截图范围")
    return save_target_crop(flow_path, step_id, crop, box, s)


def learn_look(flow_path: Path, step_id: str, image: str, crop: np.ndarray, scale: float) -> str | None:
    """执行时按图找到了、但得分偏低（按钮样子变了一点）：把这次找到的那块存成目标的另一种样子（Target.alt），
    下次两种都认。录制时的目标图（image）永远不动；alt 用新的换掉旧的，不会越积越多。
    从文件重新读流程：执行期间流程可能被改过（重新录制、重新框选），目标图已经不是 image 了就不算数。返回新的 alt 路径。"""
    flow = Flow.load(flow_path)
    step = next((s for s in flow.steps if s.id == step_id), None)
    t = getattr(step, "target", None)
    if t is None or t.image != image or t.scale != scale:
        return None
    t.alt = save_target_crop(flow_path, f"{step_id}-seen", crop, (t.x, t.y, t.w, t.h), scale,
                             keep_existing=True).image
    flow.dump(flow_path)
    return t.alt


def save_target_crop(flow_path: Path, step_id: str, crop: np.ndarray, box: tuple[float, float, float, float],
                     scale: float, *, keep_existing: bool = False) -> Target:
    """已经截好的目标图（录制时在后台线程里截的）直接存。

    keep_existing：同名的图已经有了就换个名字，不覆盖。重新录制替换旧步骤时编号会从 s01 再来，
    旧步骤的图要留着，界面上撤销回去还能用；重新框同一步的目标图则直接覆盖。"""
    x, y, w, h = box
    s = scale
    image, n = f"{targets_dir(flow_path)}/{step_id}.png", 2
    while keep_existing and (flow_path.parent / image).exists():
        image, n = f"{targets_dir(flow_path)}/{step_id}-{n}.png", n + 1
    write_image(flow_path.parent / image, crop)
    return Target(image=image, x=round(x, 1), y=round(y, 1), w=round(w, 1), h=round(h, 1), scale=s)
