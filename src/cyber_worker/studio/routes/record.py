"""录制控制：录到当前宏的末尾，录制过程中通过 WebSocket 实时推送新步骤。

在浏览器里点「停止」不会被录进去——点击落在目标窗口之外的输入都会被忽略。
录制期间后台定时截图；每次鼠标按下时截一块点击处的目标图（按图找）。
每一步开始时截一张它所在窗口的画面（点击在按下那一刻），录完只把真的变了的存下来，没变的几步共用一张。
后台还盯着鼠标：在目标程序里停住时比一比悬停前后，弹出了新窗口（资料卡）或窗口内容变了才记一步「悬停」。
录完后自动：猜「发送」步骤、给认得出来的点击配上目标图、选页面锚点并填给需要的步骤。
"""

from __future__ import annotations

import threading
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from cyber_worker.macro.anchors import add_anchor, save_reference, save_shot, save_target_crop
from cyber_worker.macro.io.base import CalibrationError
from cyber_worker.macro.recorder import RecordingBuilder, guess_send_step
from cyber_worker.macro.io.base import stable_title
from cyber_worker.macro.schema import MAIN, Click, Drag, Hover, Script, Scroll, Wait, needs_page_check
from cyber_worker.macro.screenshot.storage import read_image
from cyber_worker.macro.vision.auto_anchor import pick_stable_region
from cyber_worker.macro.vision.detect import frame_changed
from cyber_worker.macro.vision.template import pick_target
from cyber_worker.macro.vision.fingerprint import PageBook
from cyber_worker.studio.routes.edit import studio
from cyber_worker.studio.state import Recording, next_step_number

router = APIRouter(prefix="/api/record")

SAMPLE_EVERY = 0.8  # 秒
MAX_FRAMES = 24
# 悬停：鼠标停下这么久先截一张「悬停前」，再停到 HOVER_SEC（或移开）看有没有变化
REST_SEC = 0.15
HOVER_MIN = 0.5  # 停不到这么久就移开了：是移动途中顿了一下，不是悬停（资料卡也要停半秒左右才出来）
HOVER_MAX = 3.0  # 停着的时候最多看这么久（资料卡可能一秒多才弹出来）；录下来的悬停时长也不超过它
HOVER_CHECK = 0.2  # 停着的时候多久看一次有没有变化
# 停止录制后这么久之内再按录制快捷键不算「重新开始」：停止要好几秒、又没看到反应时，人会忍不住再按一下
RESTART_COOLDOWN = 1.5  # 秒


class StartRecording(BaseModel):
    script_id: str
    record_delays: bool = True
    record_append: bool | None = None  # 不给就用设置里的（默认替换已有步骤）


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


def _grab_target(st, rec: Recording, seq: int, key: str, geo, x: float, y: float) -> None:
    """鼠标按下那一刻截窗口：整张当这一步的画面；再挑一块点击处认得出来的区域当目标图，
    认不出来（空白、纯色、到处都一样）就算了。"""
    try:
        img = st.app.io.capture(geo)
        rec.press_frames[seq] = (key, geo.scale, img)
        box = pick_target(img, x, y, geo.scale)
        if box is not None:
            s = geo.scale
            bx, by, bw, bh = box
            crop = img[int(by * s):int((by + bh) * s), int(bx * s):int((bx + bw) * s)].copy()
            rec.press_targets[seq] = (s, box, crop)
    except Exception:  # noqa: BLE001 —— 截不到就按固定坐标，不影响录制
        pass


def _grab_step(st, rec: Recording, step_id: str, key: str, geo) -> None:
    """输入、按键、滚动开始时截这一步所在的窗口（窗口刚关掉、最小化截不到就算了）。"""
    try:
        rec.step_frames[step_id] = (key, geo.scale, st.app.io.capture(geo))
    except Exception:  # noqa: BLE001
        pass


def _app_windows(io, builder) -> set[tuple[str, str, int, int]]:
    """目标程序现在开着的窗口。"""
    return {(w.app.lower(), w.title, w.width, w.height) for w in io.list_windows() if builder.is_target_app(w.app)}


def _watch_hover(st, rec: Recording) -> None:
    """鼠标在目标程序里停住时（手抖几个点不算动，见 RecordingBuilder.on_move）：悬停前截一张、记下开着哪些窗口，
    之后停着的这段时间里每 HOVER_CHECK 秒看一次，最多看 HOVER_MAX 秒：弹出了新窗口、或者窗口内容明显变了，
    就算一次悬停。鼠标移开后只再看一次有没有弹出新窗口，不比画面（移开时悬停高亮消失，画面本来就会变）。
    不算的：停着的时候点了、打字了（变化是那个操作引起的）；停不到 HOVER_MIN 就移开了（移动途中顿了一下）。
    截的是窗口自己的内容，挡在上面的窗口在变不算。"""
    b, io = rec.builder, st.app.io
    seen = 0
    while not rec.stop.wait(0.05):
        p = b.pointer
        if p is None or p.seq == seen or b.pressed or time.monotonic() - p.t < REST_SEC:
            continue
        seen = p.seq
        try:
            hit = b.hover_window(io.window_geometry_at(*p.raw), p.x, p.y)
            if hit is None:
                continue
            key, x, y, geo = hit
            events = b.events
            before, wins = io.capture(geo), _app_windows(io, b)
            popped, changed = set(), False
            while not rec.stop.wait(HOVER_CHECK):
                elapsed = time.monotonic() - p.t
                if b.events != events:
                    break
                resting = b.pointer.seq == p.seq
                if elapsed >= HOVER_MIN:
                    popped = _app_windows(io, b) - wins
                    if not popped and resting:
                        changed = frame_changed(before, io.capture(geo))
                if popped or changed or not resting or elapsed >= HOVER_MAX:
                    break
            if b.events == events and (popped or changed):
                seconds = min(HOVER_MAX, elapsed)
                rec.hover_info[p.t] = (key, geo.scale, before, {(app, title) for app, title, _, _ in popped})
                b.add_hover(p.t, key, x, y, seconds)
        except Exception:  # noqa: BLE001 —— 截不到就不记，不影响录制
            pass


def _save_shots(path, steps, rec: Recording, keep_existing: bool) -> None:
    """每一步的画面：和同一个窗口上一张存下的比，真的变了才另存，没变就共用上一张。"""
    last: dict[str, tuple[object, str]] = {}  # 窗口键 → (上一张存下的图, 它的路径)
    for s in steps:
        if isinstance(s, Wait):
            continue
        if isinstance(s, (Click, Drag)):
            frame = rec.press_frames.get(rec.builder.press_of.get(s.id))
        else:
            frame = rec.step_frames.get(s.id)
        if frame is None:
            continue
        key, _, img = frame
        prev = last.get(key)
        if prev is not None and not frame_changed(prev[0], img):
            s.shot = prev[1]
            continue
        s.shot = save_shot(path, s.id, img, keep_existing=keep_existing)
        last[key] = (img, s.shot)


class RecordError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def start_recording(st, script_id: str, record_delays: bool = True, append: bool | None = None) -> None:
    """append：录到的步骤接在已有步骤后面；否则（默认）替换掉已有步骤，界面上 Ctrl+Z 能恢复。"""
    if not st.record_lock.acquire(blocking=False):
        raise RecordError(409, "正在开始或停止录制，请稍等")
    try:
        _start(st, script_id, record_delays, st.record_append if append is None else append)
    finally:
        st.record_lock.release()


def _start(st, script_id: str, record_delays: bool, append: bool) -> None:
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
    hotkeys = [st.app.hotkeys.effective(n) for n in ("record", "dry_run", "panic")]

    def on_change(steps):
        st.hub.publish({"type": "record", "steps": [s.model_dump(mode="json") for s in steps]})

    rec: Recording | None = None

    def on_press(seq, key, win_geo, x, y):
        # 在键鼠钩子的回调里不能耽搁（太慢系统会把钩子摘掉）：截图和挑目标放到后台线程
        t = threading.Thread(target=_grab_target, args=(st, rec, seq, key, win_geo, x, y), daemon=True)
        rec.press_threads.append(t)
        t.start()

    def on_step(step_id, key, win_geo):
        t = threading.Thread(target=_grab_step, args=(st, rec, step_id, key, win_geo), daemon=True)
        rec.press_threads.append(t)
        t.start()

    builder = RecordingBuilder(geo, on_change=on_change, record_delays=record_delays,
                               first_id=next_step_number(script.steps) if append else 1,
                               primary=script.window, windows=script.windows, ignore_combos=hotkeys,
                               on_press=on_press, on_step=on_step)
    stop = threading.Event()
    thread = threading.Thread(target=st.app.listen, args=(builder, stop), daemon=True, name="recorder")
    rec = Recording(script_id, builder, stop, thread, geo=geo, append=append)
    _grab(st, rec)  # 第一帧：录制开始时主窗口的样子
    rec.sampler = threading.Thread(target=_sample, args=(st, rec), daemon=True, name="frame-sampler")
    rec.hover_thread = threading.Thread(target=_watch_hover, args=(st, rec), daemon=True, name="hover-watch")
    thread.start()
    rec.sampler.start()
    rec.hover_thread.start()
    st.recording = rec
    st.hub.publish({"type": "record_state", "recording": True, "script_id": script_id})
    st.app.io.chime("start")


def stop_recording(st) -> dict:
    if not st.record_lock.acquire(blocking=False):
        raise RecordError(409, "正在开始或停止录制，请稍等")
    try:
        if st.recording is None:
            raise RecordError(409, "没有在录制")
        st.stopping = True
        try:
            return _stop(st)
        finally:
            st.stopping = False
            st.stopped_at = time.monotonic()
    finally:
        st.record_lock.release()


def _stop(st) -> dict:
    rec = st.recording
    rec.stop.set()
    # 后面要等线程、挑目标图、选锚点，要好几秒：先让人知道已经在停了
    st.app.io.chime("stop")
    st.hub.publish({"type": "record_state", "recording": True, "stopping": True, "script_id": rec.script_id})
    rec.thread.join(timeout=5)
    if rec.sampler:
        rec.sampler.join(timeout=5)
    if rec.hover_thread:
        rec.hover_thread.join(timeout=5)
    st.recording = None
    _grab(st, rec)
    for t in rec.press_threads:
        t.join(timeout=3)

    path = st.script_path(rec.script_id)
    script = Script.load(path)
    # 悬停插到它发生的时间点上，步骤重新编号：按步骤编号记的画面跟着改
    renamed = rec.builder.finish()
    rec.step_frames = {renamed.get(k, k): v for k, v in rec.step_frames.items()}
    hover_targets = {}
    for sid, mark in rec.builder.hover_marks.items():
        key, scale, img, _ = rec.hover_info[mark.t]
        rec.step_frames[sid] = (key, scale, img)  # 悬停这一步的画面：悬停之前的样子
        box = pick_target(img, mark.x, mark.y, scale)
        if box is not None:
            bx, by, bw, bh = box
            hover_targets[sid] = (scale, box, img[int(by * scale):int((by + bh) * scale),
                                                  int(bx * scale):int((bx + bw) * scale)].copy())
    new = rec.builder.steps
    # 默认替换：重新录一遍就是要这一遍，旧步骤不能留在前面看着像重复（界面上 Ctrl+Z 能恢复）
    kept = script.steps if rec.append else []
    replaced = len(script.steps) - len(kept)

    # 1. 猜「发送」：只在宏里还没有发送步骤时才猜，免得覆盖用户自己的标记
    send_id = None
    if not any(getattr(s, "sends", False) for s in kept):
        i = guess_send_step(new)
        if i is not None:
            new[i].sends = True
            send_id = new[i].id
    # 认得出来的点击 / 悬停配上目标图：执行时按图找，界面变了也能点中、停对地方
    targets = 0
    for s in new:
        if isinstance(s, Click):
            found = rec.press_targets.get(rec.builder.press_of.get(s.id))
        elif isinstance(s, Hover):
            found = hover_targets.get(s.id)
        else:
            continue
        if found is not None and s.target is None:
            scale, box, crop = found
            s.target = save_target_crop(path, s.id, crop, box, scale, keep_existing=bool(replaced))
            targets += 1
    _save_shots(path, new, rec, keep_existing=bool(replaced))
    script.steps = [*kept, *new]
    script.windows = rec.builder.windows  # 录制中新出现的窗口（如双击会话弹出的聊天窗口）
    # 悬停才弹出来的窗口（资料卡）：执行时只找位置，不抢前台、不改尺寸
    popped = {(app, stable_title(title)) for *_, pops in rec.hover_info.values() for app, title in pops}
    for w in script.windows.values():
        if (w.app.lower(), w.title or "") in popped:
            w.popup = True
    # 没有步骤、也没有页面锚点再用到的窗口（以前录过的）一并去掉：留着只会在下次录制时被认出来、带进旧尺寸
    used = {s.win for s in script.steps if s.win}
    if script.pages_path and script.pages_path.exists():
        used |= {a.win for fp in PageBook.load(script.pages_path).pages.values() for a in fp.anchors if a.win}
    script.windows = {k: w for k, w in script.windows.items() if k in used}
    # 每个录到的窗口各存一张参考图（它在这次录制里的第一帧）：画布上选到哪个窗口的步骤就显示哪个窗口。
    # 追加录制时已有的参考图不动（除非窗口尺寸变了，旧图对不上了）；替换录制就换成这一遍的
    for key, (_, img) in _first_frames(rec.frames).items():
        if key != MAIN and key not in script.windows:
            continue
        ref = script.reference_for(key)
        win = script.window_for(key)
        fits = (ref is not None and ref.exists()
                and read_image(ref).shape[:2] == (round(win.height * win.scale), round(win.width * win.scale)))
        if not (rec.append and fits):
            save_reference(path, script, key, img)
    script.dump(path)

    # 2. 自动锚点：宏还没有任何页面、又有需要校验页面的步骤时，从录制帧里挑一块稳定区域
    auto = _auto_anchor(path, rec)

    # 每个窗口的最后一帧当作它的「最新截图」，画面上能直接看到自动选的锚点
    for key, (scale, img) in _first_frames(reversed(rec.frames)).items():
        st.set_latest(rec.script_id, key, img, scale)

    # 每次录制唯一的编号：界面靠它去重（按钮的响应和推送事件各来一次）。不能用计数器：
    # studio 重启后计数从头来，没刷新的页面会把新结果当成处理过的丢掉，屏幕上一直是旧步骤
    result = {"rec_id": uuid.uuid4().hex, "added": len(new), "replaced": replaced,
              "auto_send": send_id, "auto_anchor": auto,
              "auto_targets": targets,
              # 录制结束时在哪个窗口：画面先显示它
              "last_win": None if not rec.frames or rec.frames[-1][0] == MAIN else rec.frames[-1][0],
              **st.describe(rec.script_id)}
    st.hub.publish({"type": "record_state", "recording": False, "script_id": rec.script_id})
    # 用快捷键停止时界面没有发请求，靠这个事件拿到结果
    st.hub.publish({"type": "record_done", **result})
    return result


def toggle_recording(st) -> None:
    """全局快捷键：没在录就开始录当前打开的宏，在录就停止。在快捷键监听线程里调用。"""
    if st.record_lock.locked():
        return  # 上一下还在处理（正在开始 / 停止）：连按的这一下不算
    if not st.recording and time.monotonic() - st.stopped_at < RESTART_COOLDOWN:
        return  # 刚停下又按了一下：多半是连按，不算重新开始
    try:
        if st.recording:
            stop_recording(st)
        elif st.current_id:
            start_recording(st, st.current_id, st.record_delays, st.record_append)
        else:
            st.hub.publish({"type": "toast", "error": True, "text": "先在 studio 里选一个宏，再按快捷键录制"})
    except RecordError as e:
        st.hub.publish({"type": "toast", "error": True, "text": str(e)})
    except Exception as e:  # noqa: BLE001 —— 快捷键线程里不能抛出去
        st.hub.publish({"type": "toast", "error": True, "text": f"录制出错：{e}"})


@router.post("/start")
def start(body: StartRecording, request: Request):
    try:
        start_recording(studio(request), body.script_id, body.record_delays, body.record_append)
    except RecordError as e:
        raise HTTPException(e.status, str(e)) from e
    return {"recording": True}


@router.post("/stop")
def stop(request: Request):
    try:
        return stop_recording(studio(request))
    except RecordError as e:
        raise HTTPException(e.status, str(e)) from e


def _first_frames(frames) -> dict[str, tuple[float, object]]:
    """每个窗口最先出现的一帧：窗口键 → (缩放, 图)。"""
    out: dict[str, tuple[float, object]] = {}
    for key, scale, img in frames:
        out.setdefault(key, (scale, img))
    return out


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
