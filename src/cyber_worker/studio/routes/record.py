"""录制控制：录到当前宏的末尾，录制过程中通过 WebSocket 实时推送新步骤。

在浏览器里点「停止」不会被录进去——点击落在目标窗口之外的输入都会被忽略。
录制期间后台定时截图；每次鼠标按下时截一块点击处的目标图（按图找）。
录完后自动：猜「发送」步骤、给认得出来的点击配上目标图、选页面锚点并填给需要的步骤。
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from cyber_worker.macro.anchors import add_anchor, save_target_crop
from cyber_worker.macro.io.base import CalibrationError
from cyber_worker.macro.recorder import RecordingBuilder, guess_send_step
from cyber_worker.macro.schema import MAIN, Click, Drag, Script, Scroll, needs_page_check
from cyber_worker.macro.screenshot.storage import write_image
from cyber_worker.macro.vision.auto_anchor import pick_stable_region
from cyber_worker.macro.vision.template import pick_target
from cyber_worker.macro.vision.fingerprint import PageBook
from cyber_worker.studio.routes.edit import studio
from cyber_worker.studio.state import Frame, Recording, next_step_number

router = APIRouter(prefix="/api/record")

SAMPLE_EVERY = 0.8  # 秒
MAX_FRAMES = 24


class StartRecording(BaseModel):
    script_id: str
    record_delays: bool = True


def _grab(st, rec: Recording) -> None:
    """截一张「键盘焦点所在窗口」的图。"""
    key, geo = rec.builder.focus_geometry()
    try:
        rec.frames.append((key, geo.scale, st.app.io.capture(geo)))
    except Exception:  # noqa: BLE001 —— 截图失败不影响录制本身
        pass


def _sample(st, rec: Recording) -> None:
    """定时截图（截的是你正在操作的那个窗口）。帧太多时隔一张丢一张，保证覆盖整个录制过程。"""
    while not rec.stop.is_set():
        _grab(st, rec)
        if len(rec.frames) > MAX_FRAMES:
            rec.frames[:] = rec.frames[::2]
        rec.stop.wait(SAMPLE_EVERY)


def _grab_target(st, rec: Recording, seq: int, geo, x: float, y: float) -> None:
    """鼠标按下那一刻截窗口，挑一块点击处认得出来的区域；认不出来（空白、纯色、到处都一样）就算了。"""
    try:
        img = st.app.io.capture(geo)
        box = pick_target(img, x, y, geo.scale)
        if box is not None:
            s = geo.scale
            bx, by, bw, bh = box
            crop = img[int(by * s):int((by + bh) * s), int(bx * s):int((bx + bw) * s)].copy()
            rec.press_targets[seq] = (s, box, crop)
    except Exception:  # noqa: BLE001 —— 截不到就按固定坐标，不影响录制
        pass


class RecordError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def start_recording(st, script_id: str, record_delays: bool = True) -> None:
    if st.recording:
        raise RecordError(409, "已经在录制了")
    if st.app.executor.busy:
        raise RecordError(409, "正在执行，稍后再录")
    script = st.load(script_id)
    try:
        geo = st.app.io.calibrate(script.window)
    except CalibrationError as e:
        raise RecordError(400, f"窗口校准失败：{e}") from e
    st.record_delays = record_delays
    hotkeys = [st.app.hotkeys.effective("record"), st.app.hotkeys.effective("panic")]

    def on_change(steps):
        st.hub.publish({"type": "record", "steps": [s.model_dump(mode="json") for s in steps]})

    rec: Recording | None = None

    def on_press(seq, key, win_geo, x, y):
        # 在键鼠钩子的回调里不能耽搁（太慢系统会把钩子摘掉）：截图和挑目标放到后台线程
        t = threading.Thread(target=_grab_target, args=(st, rec, seq, win_geo, x, y), daemon=True)
        rec.press_threads.append(t)
        t.start()

    builder = RecordingBuilder(geo, on_change=on_change, record_delays=record_delays,
                               first_id=next_step_number(script.steps),
                               primary=script.window, windows=script.windows, ignore_combos=hotkeys,
                               on_press=on_press)
    stop = threading.Event()
    thread = threading.Thread(target=st.app.listen, args=(builder, stop), daemon=True, name="recorder")
    rec = Recording(script_id, builder, stop, thread, geo=geo)
    _grab(st, rec)  # 第一帧：录制开始时主窗口的样子
    rec.sampler = threading.Thread(target=_sample, args=(st, rec), daemon=True, name="frame-sampler")
    thread.start()
    rec.sampler.start()
    st.recording = rec
    st.hub.publish({"type": "record_state", "recording": True, "script_id": script_id})


def stop_recording(st) -> dict:
    rec = st.recording
    if rec is None:
        raise RecordError(409, "没有在录制")
    rec.stop.set()
    rec.thread.join(timeout=5)
    if rec.sampler:
        rec.sampler.join(timeout=5)
    st.recording = None
    _grab(st, rec)
    for t in rec.press_threads:
        t.join(timeout=3)

    path = st.script_path(rec.script_id)
    script = Script.load(path)
    new = rec.builder.steps

    # 1. 猜「发送」：只在宏里还没有发送步骤时才猜，免得覆盖用户自己的标记
    send_id = None
    if not any(getattr(s, "sends", False) for s in script.steps):
        i = guess_send_step(new)
        if i is not None:
            new[i].sends = True
            send_id = new[i].id
    # 认得出来的点击配上目标图：执行时按图找，界面变了也能点中
    targets = 0
    for s in new:
        seq = rec.builder.press_of.get(s.id)
        if isinstance(s, Click) and s.target is None and seq in rec.press_targets:
            scale, box, crop = rec.press_targets[seq]
            s.target = save_target_crop(path, s.id, crop, box, scale)
            targets += 1
    script.steps = [*script.steps, *new]
    script.windows = rec.builder.windows  # 录制中新出现的窗口（如双击会话弹出的聊天窗口）
    script.dump(path)

    # 2. 自动锚点：宏还没有任何页面、又有需要校验页面的步骤时，从录制帧里挑一块稳定区域
    auto = _auto_anchor(path, rec)

    # 最后一帧当作「最新截图」，画面上能直接看到自动选的锚点
    if rec.frames:
        key, scale, img = rec.frames[-1]
        out = st.user_data / "frames" / (rec.script_id.replace("/", "__").removesuffix(".yaml") + ".png")
        write_image(out, img)
        st.latest[rec.script_id] = Frame(out, scale, None if key == MAIN else key)

    st.record_seq += 1
    result = {"rec_id": st.record_seq, "added": len(new), "auto_send": send_id, "auto_anchor": auto,
              "auto_targets": targets,
              **st.describe(rec.script_id)}
    st.hub.publish({"type": "record_state", "recording": False, "script_id": rec.script_id})
    # 用快捷键停止时界面没有发请求，靠这个事件拿到结果
    st.hub.publish({"type": "record_done", **result})
    return result


def toggle_recording(st) -> None:
    """全局快捷键：没在录就开始录当前打开的宏，在录就停止。在快捷键监听线程里调用。"""
    try:
        if st.recording:
            stop_recording(st)
        elif st.current_id:
            start_recording(st, st.current_id, st.record_delays)
        else:
            st.hub.publish({"type": "toast", "error": True, "text": "先在 studio 里选一个宏，再按快捷键录制"})
    except RecordError as e:
        st.hub.publish({"type": "toast", "error": True, "text": str(e)})
    except Exception as e:  # noqa: BLE001 —— 快捷键线程里不能抛出去
        st.hub.publish({"type": "toast", "error": True, "text": f"录制出错：{e}"})


@router.post("/start")
def start(body: StartRecording, request: Request):
    try:
        start_recording(studio(request), body.script_id, body.record_delays)
    except RecordError as e:
        raise HTTPException(e.status, str(e)) from e
    return {"recording": True}


@router.post("/stop")
def stop(request: Request):
    try:
        return stop_recording(studio(request))
    except RecordError as e:
        raise HTTPException(e.status, str(e)) from e


def _auto_anchor(path, rec: Recording) -> dict | None:
    """每个窗口分别处理：这个窗口里有还没指定页面的输入 / 发送步骤，就从这个窗口的录制帧里挑一块稳定区域。"""
    script = Script.load(path)
    needing = [s for s in script.steps if needs_page_check(s) and not s.expected_page]
    if not needing or not rec.frames:
        return None
    existing = PageBook.load(script.pages_path).pages if script.pages_path else {}
    pages, ids = [], []
    for key in dict.fromkeys(s.win or MAIN for s in needing):
        frames = [img for k, _, img in rec.frames if k == key]
        if not frames:
            continue
        scale = next(sc for k, sc, _ in rec.frames if k == key)
        in_win = [s for s in script.steps if (s.win or MAIN) == key]
        avoid = [(s.x, s.y) for s in in_win if isinstance(s, (Click, Scroll, Drag)) and s.x is not None]
        avoid += [(s.x2, s.y2) for s in in_win if isinstance(s, Drag)]
        region = pick_stable_region(frames, scale, avoid=avoid)
        if region is None:
            continue
        n = 1
        while f"页面{n}" in existing or f"页面{n}" in pages:
            n += 1
        page = f"页面{n}"
        add_anchor(path, page, region, frames[0], scale, key)
        pages.append(page)
        script = Script.load(path)  # add_anchor 可能刚写入了 pages 字段
        for s in script.steps:
            if (s.win or MAIN) == key and needs_page_check(s) and not s.expected_page:
                s.expected_page = page
                ids.append(s.id)
        script.dump(path)
    if not pages:
        return None
    return {"page": "、".join(pages), "pages": pages, "steps": ids}
