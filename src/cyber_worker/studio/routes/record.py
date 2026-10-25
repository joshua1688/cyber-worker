"""录制控制：录到当前流程的末尾，录制过程中通过 WebSocket 实时推送新步骤。

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

from cyber_worker.flow.anchors import (
    add_anchor,
    free_page_name,
    save_reference,
    save_shot,
    save_target_crop,
)
from cyber_worker.flow.io.base import CalibrationError, stable_title
from cyber_worker.flow.recorder import RecordingBuilder, guess_send_step
from cyber_worker.flow.schema import (
    MAIN,
    Click,
    Drag,
    Flow,
    Hover,
    Key,
    Scroll,
    TypeText,
    Wait,
    WaitPage,
    needs_page_check,
)
from cyber_worker.flow.screenshot.storage import read_image
from cyber_worker.flow.vision.auto_anchor import pick_stable_region
from cyber_worker.flow.vision.detect import frame_changed
from cyber_worker.flow.vision.fingerprint import PageBook, match_page
from cyber_worker.flow.vision.template import look_of, pick_target
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
    flow_id: str
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
        if b.settling_after_click(p):  # 点完在原处停着等窗口打开：不是悬停
            continue
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


def start_recording(st, flow_id: str, record_delays: bool = True, append: bool | None = None) -> None:
    """append：录到的步骤接在已有步骤后面；否则（默认）替换掉已有步骤，界面上 Ctrl+Z 能恢复。"""
    if not st.record_lock.acquire(blocking=False):
        raise RecordError(409, "正在开始或停止录制，请稍等")
    try:
        _start(st, flow_id, record_delays, st.record_append if append is None else append)
    finally:
        st.record_lock.release()


def _start(st, flow_id: str, record_delays: bool, append: bool) -> None:
    if st.recording:
        raise RecordError(409, "已经在录制了")
    if st.app.executor.busy:
        raise RecordError(409, "正在执行，稍后再录")
    flow = st.load(flow_id)
    try:
        geo = st.app.io.calibrate(flow.window, avoid=flow.other_titles(MAIN))
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
                               first_id=next_step_number(flow.steps) if append else 1,
                               primary=flow.window, windows=flow.windows, ignore_combos=hotkeys,
                               on_press=on_press, on_step=on_step)
    stop = threading.Event()
    thread = threading.Thread(target=st.app.listen, args=(builder, stop), daemon=True, name="recorder")
    rec = Recording(flow_id, builder, stop, thread, geo=geo, append=append)
    _grab(st, rec)  # 第一帧：录制开始时主窗口的样子
    rec.sampler = threading.Thread(target=_sample, args=(st, rec), daemon=True, name="frame-sampler")
    rec.hover_thread = threading.Thread(target=_watch_hover, args=(st, rec), daemon=True, name="hover-watch")
    thread.start()
    rec.sampler.start()
    rec.hover_thread.start()
    st.recording = rec
    st.hub.publish({"type": "record_state", "recording": True, "flow_id": flow_id})
    if st.app.config.studio.record_chime:
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
    if st.app.config.studio.record_chime:
        st.app.io.chime("stop")
    st.hub.publish({"type": "record_state", "recording": True, "stopping": True, "flow_id": rec.flow_id})
    rec.thread.join(timeout=5)
    if rec.sampler:
        rec.sampler.join(timeout=5)
    if rec.hover_thread:
        rec.hover_thread.join(timeout=5)
    st.recording = None
    _grab(st, rec)
    for t in rec.press_threads:
        t.join(timeout=3)

    path = st.flow_path(rec.flow_id)
    flow = Flow.load(path)
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
    kept = flow.steps if rec.append else []
    replaced = len(flow.steps) - len(kept)

    # 1. 猜「发送」：只在流程里还没有发送步骤时才猜，免得覆盖用户自己的标记
    send_id = None
    if not any(getattr(s, "sends", False) for s in kept):
        i = guess_send_step(new)
        if i is not None:
            new[i].sends = True
            send_id = new[i].id
    # 认得出来的点击 / 悬停配上目标图：执行时按图找，界面变了也能点中、停对地方
    targets = 0
    for i, s in enumerate(new):
        if isinstance(s, Click):
            found = rec.press_targets.get(rec.builder.press_of.get(s.id))
        elif isinstance(s, Hover):
            found = hover_targets.get(s.id)
        else:
            continue
        if found is not None and s.target is None:
            scale, box, crop = found
            s.target = save_target_crop(path, s.id, crop, box, scale, keep_existing=bool(replaced))
            normal = _normal_look(new, i, rec, box, scale, crop) if isinstance(s, Click) else None
            if normal is not None:
                s.target.alt = save_target_crop(path, f"{s.id}-normal", normal, box, scale,
                                                keep_existing=bool(replaced)).image
            targets += 1
    _save_shots(path, new, rec, keep_existing=bool(replaced))
    flow.steps = [*kept, *new]
    flow.windows = rec.builder.windows  # 录制中新出现的窗口（如双击会话弹出的聊天窗口）
    # 悬停才弹出来的窗口（资料卡）：执行时只找位置，不抢前台、不改尺寸
    # 打过字、按过键的窗口一定不是资料卡这种小窗：以前录的同名窗口带过来的标记也去掉
    popped = {(app, stable_title(title)) for *_, pops in rec.hover_info.values() for app, title in pops}
    typed_in = {s.win for s in [*kept, *new] if isinstance(s, (TypeText, Key))}
    for key, w in flow.windows.items():
        if key in typed_in:
            w.popup = False
        elif (w.app.lower(), w.title or "") in popped:
            w.popup = True
    # 没有步骤、也没有页面锚点再用到的窗口（以前录过的）一并去掉：留着只会在下次录制时被认出来、带进旧尺寸
    used = {s.win for s in flow.steps if s.win}
    if flow.pages_path and flow.pages_path.exists():
        used |= {a.win for fp in PageBook.load(flow.pages_path).pages.values() for a in fp.anchors if a.win}
    flow.windows = {k: w for k, w in flow.windows.items() if k in used}
    # 每个录到的窗口各存一张参考图：画布上选到哪个窗口的步骤就显示哪个窗口。优先用这个窗口第一步的画面
    # （按下鼠标那一刻截的，窗口一定在）；没有步骤画面的窗口才用后台定时截的第一帧。
    # 追加录制时已有的参考图不动（除非窗口尺寸变了，旧图对不上了）；替换录制就换成这一遍的
    firsts = {**_first_frames(rec.frames), **_first_step_frames(new, rec)}
    shots = _step_frames_by_win(new, rec)  # 下面重新编号后就按步骤编号查不到了，先取出来
    for key, (_, img) in firsts.items():
        if key != MAIN and key not in flow.windows:
            continue
        ref = flow.reference_for(key)
        win = flow.window_for(key)
        fits = (ref is not None and ref.exists()
                and read_image(ref).shape[:2] == (round(win.height * win.scale), round(win.width * win.scale)))
        if not (rec.append and fits):
            save_reference(path, flow, key, img)
    # 统一编号：动作 s01、s02…，延迟 d01、d02…（延迟不算「步」）。上面按录制时编号记的画面、目标图都已经存好了
    send_id = flow.renumber().get(send_id, send_id)
    flow.dump(path)

    # 2. 自动锚点：需要校验页面的步骤还没指定页面时，先沿用同一窗口里还对得上的页面，没有才从录制帧里挑一块新的
    auto = _auto_anchor(path, rec, shots)
    # 替换录制：旧步骤用过、现在没有步骤再用的页面一并清掉（不然新旧两个锚点框在同一个地方，标签叠在一起）
    dropped = [] if rec.append else _drop_unused_pages(path)

    # 每个窗口的最后一帧当作它的「最新截图」，画面上能直接看到自动选的锚点
    for key, (scale, img) in _first_frames(reversed(rec.frames)).items():
        st.set_latest(rec.flow_id, key, img, scale)

    # 每次录制唯一的编号：界面靠它去重（按钮的响应和推送事件各来一次）。不能用计数器：
    # studio 重启后计数从头来，没刷新的页面会把新结果当成处理过的丢掉，屏幕上一直是旧步骤
    result = {"rec_id": uuid.uuid4().hex, "added": len(new), "replaced": replaced,
              "auto_send": send_id, "auto_anchor": auto, "dropped_pages": dropped,
              "auto_targets": targets,
              # 录制结束时在哪个窗口：画面先显示它
              "last_win": None if not rec.frames or rec.frames[-1][0] == MAIN else rec.frames[-1][0],
              **st.describe(rec.flow_id)}
    st.hub.publish({"type": "record_state", "recording": False, "flow_id": rec.flow_id})
    # 用快捷键停止时界面没有发请求，靠这个事件拿到结果
    st.hub.publish({"type": "record_done", **result})
    return result


def toggle_recording(st) -> None:
    """全局快捷键：没在录就开始录当前打开的流程，在录就停止。在快捷键监听线程里调用。"""
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
            st.hub.publish({"type": "toast", "error": True, "text": "先在 studio 里选一个流程，再按快捷键录制"})
    except RecordError as e:
        st.hub.publish({"type": "toast", "error": True, "text": str(e)})
    except Exception as e:  # noqa: BLE001 —— 快捷键线程里不能抛出去
        st.hub.publish({"type": "toast", "error": True, "text": f"录制出错：{e}"})


@router.post("/start")
def start(body: StartRecording, request: Request):
    try:
        start_recording(studio(request), body.flow_id, body.record_delays, body.record_append)
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


def _step_frame(s, rec: Recording):
    """这一步动作之前的画面 (窗口键, 缩放, 图)；没截到返回 None。"""
    if isinstance(s, (Click, Drag)):
        return rec.press_frames.get(rec.builder.press_of.get(s.id))
    return rec.step_frames.get(s.id)


def _pointer_at(steps, j: int) -> tuple[float | None, float | None]:
    """第 j 步的画面截下来那一刻鼠标大概在哪：点击、滚动、悬停在它自己的位置，拖动在起点；
    输入、按键这类没有位置的，沿用前面最后一个有位置的步骤（拖动取终点）。"""
    for k in range(j, -1, -1):
        st = steps[k]
        if isinstance(st, Drag):
            return (st.x, st.y) if k == j else (st.x2, st.y2)
        if isinstance(st, (Click, Scroll, Hover)) and st.x is not None:
            return st.x, st.y
    return None, None


def _normal_look(steps, i: int, rec: Recording, box, scale: float, crop):
    """点击的目标图截在按下那一刻：按钮这时多半带着悬停 / 按下的颜色，平时的样子不一样（「关闭」按钮平时只有 0.78 分，
    要靠先把鼠标移过去才找得到）。从这一步之前、同一个窗口、鼠标不在它上面时的画面里截同一块，当作它平时的样子。
    那一块和目标图看起来一样（按钮没有悬停效果）就不用另存，返回 None；内容都变了（列表滚过了）的画面不算。"""
    key = steps[i].win or MAIN
    x, y, w, h = box
    pad = 8

    def pointer_on(j: int) -> bool:
        px, py = _pointer_at(steps, j)
        return px is not None and x - pad <= px <= x + w + pad and y - pad <= py <= y + h + pad

    frames = [_step_frame(steps[j], rec) for j in range(i - 1, -1, -1)
              if (steps[j].win or MAIN) == key and not pointer_on(j)]
    if (first := _first_frames(rec.frames).get(key)) is not None:  # 鼠标在哪不知道，放最后
        frames.append((key, *first))
    for frame in frames:
        if frame is None or frame[1] != scale:
            continue
        img = frame[2]
        region = img[int(y * scale):int(y * scale) + crop.shape[0], int(x * scale):int(x * scale) + crop.shape[1]]
        look = look_of(region, crop)
        if look == "same":
            return None  # 平时也长这样
        if look == "other":
            return region.copy()
    return None


def _first_step_frames(steps, rec: Recording) -> dict[str, tuple[float, object]]:
    """每个窗口第一步的画面（动作之前）：窗口键 → (缩放, 图)。"""
    out: dict[str, tuple[float, object]] = {}
    for s in steps:
        if (frame := _step_frame(s, rec)) is not None:
            key, scale, img = frame
            out.setdefault(key, (scale, img))
    return out


def _step_frames_by_win(steps, rec: Recording) -> dict[str, list[tuple[float, object]]]:
    """每个窗口所有步骤的画面，按顺序：窗口键 → [(缩放, 图)]。"""
    out: dict[str, list[tuple[float, object]]] = {}
    for s in steps:
        if (frame := _step_frame(s, rec)) is not None:
            key, scale, img = frame
            out.setdefault(key, []).append((scale, img))
    return out


def _auto_anchor(path, rec: Recording, shots: dict | None = None) -> dict | None:
    """每个窗口分别处理：这个窗口里有还没指定页面的输入 / 发送步骤时，
    先看这个窗口已有的页面在这次录到的画面里还对不对得上，对得上就沿用；都对不上才从录制帧里挑一块稳定区域新建。
    shots：每个窗口各步骤动作之前的画面（按下鼠标、开始打字那一刻截的，窗口一定在），排在后台定时截的帧前面。
    还是选不出来的步骤放进 missing，界面提示用户自己框。"""
    flow = Flow.load(path)
    needing = [s for s in flow.steps if needs_page_check(s) and not s.expected_page]
    if not needing:
        return None
    book = PageBook.load(flow.pages_path) if flow.pages_path and flow.pages_path.exists() else PageBook()
    existing = book.pages
    pages, ids, reused = [], [], []
    for key in dict.fromkeys(s.win or MAIN for s in needing):
        mine = [*(shots or {}).get(key, []), *((sc, img) for k, sc, img in rec.frames if k == key)]
        if not mine:
            continue
        scale = mine[0][0]
        frames = [img for sc, img in mine if sc == scale]
        page = next((name for name, fp in existing.items()
                     if fp.anchors and all((a.win or MAIN) == key for a in fp.anchors)
                     and match_page(book, name, frames[-1], scale).matched), None)
        if page is not None:
            reused.append(page)
        else:
            in_win = [s for s in flow.steps if (s.win or MAIN) == key]
            avoid = [(s.x, s.y) for s in in_win if isinstance(s, (Click, Scroll, Drag)) and s.x is not None]
            avoid += [(s.x2, s.y2) for s in in_win if isinstance(s, Drag)]
            region = pick_stable_region(frames, scale, avoid=avoid)
            if region is None:
                continue
            page = free_page_name(book, tuple(pages))
            add_anchor(path, page, region, frames[0], scale, key)
        pages.append(page)
        flow = Flow.load(path)  # add_anchor 可能刚写入了 pages 字段
        for s in flow.steps:
            if (s.win or MAIN) == key and needs_page_check(s) and not s.expected_page:
                s.expected_page = page
                ids.append(s.id)
        flow.dump(path)
    missing = [s.id for s in Flow.load(path).steps if needs_page_check(s) and not s.expected_page]
    if not pages and not missing:
        return None
    return {"page": "、".join(pages), "pages": pages, "steps": ids, "reused": reused, "missing": missing}


def _drop_unused_pages(path) -> list[str]:
    """没有步骤再用的页面（核对页面、等待页面都没用到）清出 pages：画面上不再显示。
    锚点图先留着（放进 retired）：Ctrl+Z 撤销这次录制时，旧步骤用到的页面会自动放回来。返回清掉的页面名。"""
    flow = Flow.load(path)
    if not flow.pages_path or not flow.pages_path.exists():
        return []
    used = {s.expected_page for s in flow.steps} | {s.page for s in flow.steps if isinstance(s, WaitPage)}
    book = PageBook.load(flow.pages_path)
    dropped = [name for name in book.pages if name not in used]
    for name in dropped:
        book.retired[name] = book.pages.pop(name)
        book.retired_at[name] = time.time()
    if dropped:
        book.dump(flow.pages_path)
    return dropped
