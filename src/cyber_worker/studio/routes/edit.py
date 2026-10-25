"""流程的增删改查、页面锚点。"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from cyber_worker.flow.anchors import (
    add_anchor,
    auto_page,
    remove_anchor,
    replace_frame,
    restore_pages,
    save_reference,
    save_target,
)
from cyber_worker.flow.io.base import CalibrationError
from cyber_worker.flow.recorder import is_studio_title
from cyber_worker.flow.schema import MAIN, Click, Flow, Hover, Window
from cyber_worker.flow.screenshot.storage import read_image, write_image
from cyber_worker.studio.state import (
    Studio,
    check_name,
    platform_slug,
    renamed,
    safe_stem,
    window_json,
)

router = APIRouter(prefix="/api")


def studio(request: Request) -> Studio:
    return request.app.state.studio


class NewFlow(BaseModel):
    name: str  # 随便写，可以是中文
    app: str
    title: str | None = None
    width: int
    height: int
    platform: str | None = None  # 不填就按程序名生成


class NewAnchor(BaseModel):
    page: str
    x: float
    y: float
    w: float
    h: float
    frame: str = "latest"  # 从哪一帧截：reference / latest / step（某一步录制时的画面）
    win: str | None = None  # 哪个窗口的画面（None = 主窗口）
    step_id: str | None = None  # frame=step 时：哪一步的画面


class FlowMeta(BaseModel):
    name: str | None = None
    description: str | None = None


class AutoPage(BaseModel):
    step_id: str


class NewTarget(BaseModel):
    step_id: str
    x: float
    y: float
    w: float
    h: float
    frame: str = "latest"
    win: str | None = None
    shot_of: str | None = None  # frame=step 时：用哪一步的画面框（默认就是这一步）


@router.get("/flows")
def list_flows(request: Request):
    return studio(request).list_flows()


def _is_studio(w) -> bool:
    return is_studio_title(w.title)


def _shared_apps(items) -> set[str]:
    seen, shared = set(), set()
    for w in items:
        key = w.app.lower()
        (shared if key in seen else seen).add(key)
    return shared


@router.get("/windows")
def windows(request: Request):
    """当前打开的窗口（含最小化的），新建流程时点选。"""
    st = studio(request)
    items = [w for w in st.app.io.list_windows() if not _is_studio(w)]
    shared = _shared_apps(items)
    return [window_json(w, shared) for w in items]


PICK_TIMEOUT = 20.0


@router.post("/windows/pick")
def pick_window(request: Request):
    """等用户去点一下目标窗口，返回那个窗口。点到 studio 自己不算，继续等。"""
    st = studio(request)
    if st.recording or st.app.executor.busy:
        raise HTTPException(409, "正在执行或录制，稍后再试")
    deadline = time.monotonic() + PICK_TIMEOUT
    while (left := deadline - time.monotonic()) > 0:
        pos = st.app.wait_click(left)
        if pos is None:
            break
        w = st.app.io.window_at(*pos)
        if w and not _is_studio(w):
            items = [x for x in st.app.io.list_windows() if not _is_studio(x)]
            return window_json(w, _shared_apps([*items, w] if w not in items else items))
    raise HTTPException(408, f"{PICK_TIMEOUT:.0f} 秒内没有点到其他窗口，再试一次")


@router.post("/flows")
def create_flow(body: NewFlow, request: Request):
    """新建空白流程：校准窗口并截一张参考图，之后录制或手动添加步骤。"""
    st = studio(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "给流程起个名字")
    platform = check_name(body.platform.strip(), "平台名") if body.platform else platform_slug(body.app)
    # 先建成草稿：用户点「保存」才进流程库
    folder = st.drafts / platform / "flows"
    base = safe_stem(name)
    stem = st.unique_stem(platform, base)  # 重名自动加序号，不打断用户
    name = renamed(name, base, stem)
    path = folder / f"{stem}.yaml"
    window = Window(app=body.app, title=body.title or None, width=body.width, height=body.height)
    if st.app.executor.busy or st.recording:
        raise HTTPException(409, "正在执行或录制，稍后再试")
    try:
        geo = st.app.io.calibrate(window)
    except CalibrationError as e:
        raise HTTPException(400, f"窗口校准失败：{e}") from e
    path.parent.mkdir(parents=True, exist_ok=True)
    write_image(path.with_suffix(".png"), st.app.io.capture(geo))
    window.scale = geo.scale
    Flow(name=name, platform=platform, window=window,
           reference=path.with_suffix(".png").name, steps=[]).dump(path)
    return st.describe(st.flow_id(path))


@router.get("/flows/{flow_id:path}/detail")
def get_flow(flow_id: str, request: Request):
    return studio(request).describe(flow_id)


@router.put("/flows/{flow_id:path}/detail")
async def save_flow(flow_id: str, request: Request):
    st = studio(request)
    path = st.flow_path(flow_id)
    try:
        flow = Flow.model_validate(await request.json())
    except ValidationError as e:
        raise HTTPException(422, [{"loc": err["loc"], "msg": err["msg"]} for err in e.errors()]) from e
    flow.renumber()  # 增删、挪动步骤后编号跟着位置走：界面、日志、审计里的「第 N 步」一致
    st.rename_runs(Flow.load(path).name, flow.name, path.parent.parent.name)  # 在流程设置里改了名：以前的运行记录跟着改
    flow.dump(path)
    restore_pages(Flow.load(path))  # 撤销重新录制：旧步骤用到的页面被清出去了，放回来
    return st.describe(flow_id)


@router.patch("/flows/{flow_id:path}/meta")
def update_meta(flow_id: str, body: FlowMeta, request: Request):
    """应用首页卡片上改名、写说明（不用打开编辑器）。"""
    return studio(request).update_meta(flow_id, name=body.name, description=body.description)


@router.delete("/flows/{flow_id:path}/detail")
def delete_flow(flow_id: str, request: Request):
    st = studio(request)
    if st.recording and st.recording.flow_id == flow_id:
        raise HTTPException(409, "正在录制这个流程，先停止录制")
    st.delete(flow_id)
    return {"ok": True}


@router.post("/flows/{flow_id:path}/commit")
def commit_flow(flow_id: str, request: Request):
    """把草稿保存进流程库。"""
    st = studio(request)
    if st.recording and st.recording.flow_id == flow_id:
        raise HTTPException(409, "正在录制这个流程，先停止录制")
    return st.describe(st.commit(flow_id))


@router.post("/flows/{flow_id:path}/capture")
def capture(flow_id: str, request: Request, win: str | None = None, replace: bool = False,
            step_id: str | None = None):
    """校准窗口并截取当前画面，用来对照坐标、框选锚点。win：流程里的哪个窗口（默认主窗口）。
    这个窗口还没有参考图（比如以前录的流程，聊天窗口没存过）就顺便存成它的参考图。
    replace：用这一张换掉画布上的「录制画面」（录错了的时候）：给了 step_id 换那一步的，否则换这个窗口显示的那张。"""
    st = studio(request)
    path = st.flow_path(flow_id)
    flow = Flow.load(path)
    win = None if win in (None, MAIN) else win
    if st.app.executor.busy or st.recording:
        raise HTTPException(409, "正在执行或录制，稍后再试")
    try:
        geo = st.app.io.calibrate(flow.window_for(win), avoid=flow.other_titles(win))
    except (CalibrationError, ValueError) as e:
        raise HTTPException(400, f"窗口校准失败：{e}") from e
    img = st.app.io.capture(geo)
    st.set_latest(flow_id, win, img, geo.scale)
    replaced = None
    if replace:
        try:
            replaced = replace_frame(path, flow, win, img, step_id)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        flow.dump(path)
    else:
        ref = flow.reference_for(win)
        if ref is None or not ref.exists():
            save_reference(path, flow, win, img)
            flow.dump(path)
    out = st.describe(flow_id)
    if replaced is not None:
        out["replaced_steps"] = replaced
    return out


def _frame(st, flow_id: str, flow: Flow, which: str, win: str | None, step_id: str | None = None):
    """画面上显示的那一帧：(图, 缩放, 窗口键)。win：哪个窗口（None = 主窗口）；which=step 时用 step_id 那一步的画面。"""
    if which == "step":
        step = next((s for s in flow.steps if s.id == step_id), None)
        if step is None or not step.shot or not (flow.base_dir / step.shot).exists():
            raise HTTPException(400, "这一步没有录制时的画面")
        return read_image(flow.base_dir / step.shot), flow.window_for(step.win).scale, step.win
    win = None if win in (None, MAIN) else win
    try:
        window = flow.window_for(win)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if which == "reference":
        ref = flow.shown_reference(win)
        if ref is None or not ref.exists():
            raise HTTPException(400, "这个窗口还没有参考图：打开它后点「截取当前窗口」")
        return read_image(ref), window.scale, win
    frame = st.latest.get(flow_id, {}).get(win or MAIN)
    if frame is None:
        raise HTTPException(400, "先「截取当前窗口」，再在画面上框选")
    return read_image(frame.path), frame.scale, win


@router.post("/flows/{flow_id:path}/anchors")
def create_anchor(flow_id: str, body: NewAnchor, request: Request):
    st = studio(request)
    path = st.flow_path(flow_id)
    flow = Flow.load(path)
    img, scale, win = _frame(st, flow_id, flow, body.frame, body.win, body.step_id)
    try:
        add_anchor(path, body.page, (body.x, body.y, body.w, body.h), img, scale, win)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return st.describe(flow_id)


@router.post("/flows/{flow_id:path}/auto-page")
def create_auto_page(flow_id: str, body: AutoPage, request: Request):
    """步骤上「自动选一个」：替这一步选好「在哪个画面执行」（沿用对得上的页面，或从录制画面里挑一块新建）。"""
    st = studio(request)
    path = st.flow_path(flow_id)
    flow = Flow.load(path)
    step = next((s for s in flow.steps if s.id == body.step_id), None)
    frame = st.latest.get(flow_id, {}).get(step.win or MAIN) if step is not None else None
    try:
        got = auto_page(path, body.step_id, read_image(frame.path) if frame is not None else None)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {**st.describe(flow_id), "auto_page": got}


@router.post("/flows/{flow_id:path}/targets")
def set_target(flow_id: str, body: NewTarget, request: Request):
    """在画面上框一块，作为这个点击 / 悬停步骤「按图找」的目标图（重新框选、或者给固定坐标的补一张）。"""
    st = studio(request)
    path = st.flow_path(flow_id)
    flow = Flow.load(path)
    step = next((s for s in flow.steps if s.id == body.step_id), None)
    if not isinstance(step, (Click, Hover)):
        raise HTTPException(400, "只有点击、悬停步骤可以按图找")
    img, scale, win = _frame(st, flow_id, flow, body.frame, body.win, body.shot_of or body.step_id)
    if (win or None) != (step.win or None):
        raise HTTPException(400, "画面上不是这一步所在的窗口：先切到那个窗口截一张，再框选")
    try:
        target = save_target(path, step.id, img, (body.x, body.y, body.w, body.h), scale)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if step.target is not None:  # 保留用户调过的等待时间和灵敏度
        target.timeout, target.threshold = step.target.timeout, step.target.threshold
    step.target, step.use_target = target, True
    flow.dump(path)
    return st.describe(flow_id)


@router.delete("/flows/{flow_id:path}/anchors/{page}/{index}")
def delete_anchor(flow_id: str, page: str, index: int, request: Request):
    st = studio(request)
    try:
        remove_anchor(st.flow_path(flow_id), page, index)
    except (KeyError, IndexError, ValueError) as e:
        raise HTTPException(404, "锚点不存在") from e
    return st.describe(flow_id)
