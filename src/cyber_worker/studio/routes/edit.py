"""脚本的增删改查、页面锚点。"""

from __future__ import annotations

import time

from cyber_worker.macro.recorder import is_studio_title

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from cyber_worker.macro.anchors import add_anchor, remove_anchor, save_target
from cyber_worker.macro.io.base import CalibrationError
from cyber_worker.macro.schema import Click, Script, Window
from cyber_worker.macro.screenshot.storage import read_image, write_image
from cyber_worker.studio.state import Frame, Studio, check_name, platform_slug, safe_stem, window_json

router = APIRouter(prefix="/api")


def studio(request: Request) -> Studio:
    return request.app.state.studio


class NewScript(BaseModel):
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
    frame: str = "latest"  # 从哪一帧截：reference / latest


class NewTarget(BaseModel):
    step_id: str
    x: float
    y: float
    w: float
    h: float
    frame: str = "latest"


@router.get("/scripts")
def list_scripts(request: Request):
    return studio(request).list_scripts()


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
    """当前打开的窗口（含最小化的），新建宏时点选。"""
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


@router.post("/scripts")
def create_script(body: NewScript, request: Request):
    """新建空白宏：校准窗口并截一张参考图，之后录制或手动添加步骤。"""
    st = studio(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "给宏起个名字")
    platform = check_name(body.platform.strip(), "平台名") if body.platform else platform_slug(body.app)
    # 先建成草稿：用户点「保存」才进宏库
    folder = st.drafts / platform / "templates"
    path = folder / f"{st.unique_stem(platform, safe_stem(name))}.yaml"  # 重名自动加序号，不打断用户
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
    Script(name=name, platform=platform, window=window,
           reference=path.with_suffix(".png").name, steps=[]).dump(path)
    return st.describe(st.script_id(path))


@router.get("/scripts/{script_id:path}/detail")
def get_script(script_id: str, request: Request):
    return studio(request).describe(script_id)


@router.put("/scripts/{script_id:path}/detail")
async def save_script(script_id: str, request: Request):
    st = studio(request)
    path = st.script_path(script_id)
    try:
        script = Script.model_validate(await request.json())
    except ValidationError as e:
        raise HTTPException(422, [{"loc": err["loc"], "msg": err["msg"]} for err in e.errors()]) from e
    script.dump(path)
    return st.describe(script_id)


@router.delete("/scripts/{script_id:path}/detail")
def delete_script(script_id: str, request: Request):
    st = studio(request)
    if st.recording and st.recording.script_id == script_id:
        raise HTTPException(409, "正在录制这个宏，先停止录制")
    st.delete(script_id)
    return {"ok": True}


@router.post("/scripts/{script_id:path}/commit")
def commit_script(script_id: str, request: Request):
    """把草稿保存进宏库。"""
    st = studio(request)
    if st.recording and st.recording.script_id == script_id:
        raise HTTPException(409, "正在录制这个宏，先停止录制")
    return st.describe(st.commit(script_id))


@router.post("/scripts/{script_id:path}/capture")
def capture(script_id: str, request: Request, win: str | None = None):
    """校准窗口并截取当前画面，用来对照坐标、框选锚点。win：宏里的哪个窗口（默认主窗口）。"""
    st = studio(request)
    script = st.load(script_id)
    if st.app.executor.busy or st.recording:
        raise HTTPException(409, "正在执行或录制，稍后再试")
    try:
        geo = st.app.io.calibrate(script.window_for(win))
    except (CalibrationError, ValueError) as e:
        raise HTTPException(400, f"窗口校准失败：{e}") from e
    out = st.user_data / "frames" / (script_id.replace("/", "__").removesuffix(".yaml") + ".png")
    write_image(out, st.app.io.capture(geo))
    st.latest[script_id] = Frame(out, geo.scale, None if win in (None, "main") else win)
    return st.describe(script_id)


def _frame(st, script_id: str, script: Script, which: str):
    """画面上显示的那一帧：(图, 缩放, 窗口键)。"""
    if which == "reference":
        if not script.reference_path or not script.reference_path.exists():
            raise HTTPException(400, "这个宏没有参考图")
        return read_image(script.reference_path), script.window.scale, None
    frame = st.latest.get(script_id)
    if frame is None:
        raise HTTPException(400, "先「截取当前窗口」，再在画面上框选")
    return read_image(frame.path), frame.scale, frame.win


@router.post("/scripts/{script_id:path}/anchors")
def create_anchor(script_id: str, body: NewAnchor, request: Request):
    st = studio(request)
    path = st.script_path(script_id)
    script = Script.load(path)
    img, scale, win = _frame(st, script_id, script, body.frame)
    try:
        add_anchor(path, body.page, (body.x, body.y, body.w, body.h), img, scale, win)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return st.describe(script_id)


@router.post("/scripts/{script_id:path}/targets")
def set_target(script_id: str, body: NewTarget, request: Request):
    """在画面上框一块，作为这个点击步骤「按图找」的目标图（重新框选、或者给固定坐标的点击补一张）。"""
    st = studio(request)
    path = st.script_path(script_id)
    script = Script.load(path)
    step = next((s for s in script.steps if s.id == body.step_id), None)
    if not isinstance(step, Click):
        raise HTTPException(400, "只有点击步骤可以按图找")
    img, scale, win = _frame(st, script_id, script, body.frame)
    if (win or None) != (step.win or None):
        raise HTTPException(400, "画面上不是这一步所在的窗口：先切到那个窗口截一张，再框选")
    try:
        target = save_target(path, step.id, img, (body.x, body.y, body.w, body.h), scale)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if step.target is not None:  # 保留用户调过的等待时间和灵敏度
        target.timeout, target.threshold = step.target.timeout, step.target.threshold
    step.target, step.use_target = target, True
    script.dump(path)
    return st.describe(script_id)


@router.delete("/scripts/{script_id:path}/anchors/{page}/{index}")
def delete_anchor(script_id: str, page: str, index: int, request: Request):
    st = studio(request)
    try:
        remove_anchor(st.script_path(script_id), page, index)
    except (KeyError, IndexError, ValueError) as e:
        raise HTTPException(404, "锚点不存在") from e
    return st.describe(script_id)
