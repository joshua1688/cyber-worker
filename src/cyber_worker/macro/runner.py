"""执行单个脚本：校准窗口 → 逐步操作（输入/发送前校验页面）→ 每步操作后截图 → 写审计。

安全规则：
- 输入和发送之前必须核对页面指纹；没有声明 expected_page、没有 pages.yaml 一律中止；
  页面还没出现就等（最多 PAGE_WAIT 秒），等不到才中止——电脑卡一下不至于失败。
- 按图找的点击：先在窗口里找目标图，找到才点；找不到就等（最多 target.timeout 秒），超时中止，
  不会退回去点录制时的旧坐标（宁可不点，也不点错）。
- 空跑时遇到「发送」步骤：截图、记为 skipped，然后结束本次运行（发送之后的步骤依赖发送后的页面状态）。
"""

from __future__ import annotations

import math
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from cyber_worker.macro.act import keyboard, mouse
from cyber_worker.macro.act import scroll as scroll_act
from cyber_worker.macro.audit.logger import AuditEntry, AuditSink, StepStatus
from cyber_worker.macro.io.base import CalibrationError, ScreenIO, WindowGeometry
from cyber_worker.macro.locator.icon import Located, locate_click
from cyber_worker.macro.schema import (
    MAIN,
    Click,
    Drag,
    Hover,
    Key,
    Script,
    Scroll,
    TypeText,
    Wait,
    WaitPage,
    needs_page_check,
)
from cyber_worker.macro.anchors import learn_look
from cyber_worker.macro.screenshot.storage import ScreenshotStorage, read_image
from cyber_worker.macro.vision.fingerprint import PageBook, match_page

TICK = 0.1  # 长时间等待（延迟、悬停）切成这么长一段，每段之间检查急停
WINDOW_WAIT = 6.0  # 换到别的窗口时，最多等它出现这么久（秒）
PAGE_WAIT = 5.0  # 输入 / 发送前页面还没出现时，最多等这么久（秒）
POLL = 0.3  # 等页面、等目标时多久重新截一次图


class Cancelled(Exception):
    """急停。由 Gate 抛出，runner 记审计后向上传递给执行器。"""


class UserInterrupted(Exception):
    """动作进行到一半时你开始用电脑了。步骤之间会暂停等待，步骤中途只能中止，避免和你的输入混在一起。"""


class Gate:
    """执行器交给 runner 的闸门。默认实现什么都不拦。"""

    def wait(self) -> bool:
        """每步之前调用：你在用电脑时阻塞；返回 True 表示刚暂停过（窗口可能被动过，需重新校准）。"""
        return False

    def check(self) -> None:
        """动作内部频繁调用：急停抛 Cancelled，你开始用电脑抛 UserInterrupted。"""

    def check_cancel(self) -> None:
        """只查急停（延迟期间用：你动一下鼠标不该让整次运行中止，下一步之前自然会暂停等你）。"""


class RunStatus(str, Enum):
    OK = "ok"
    ABORTED = "aborted"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DRY_RUN_STOPPED = "dry_run_stopped"  # 空跑走到了「发送」，正常结束


@dataclass
class RunResult:
    run_id: str
    status: RunStatus
    steps_done: int
    reason: str = ""


class Runner:
    def __init__(
        self,
        io: ScreenIO,
        storage: ScreenshotStorage,
        audit: AuditSink,
        *,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.io = io
        self.storage = storage
        self.audit = audit
        self.rng = rng or random.Random()
        self.sleep = sleep
        self.now = now

    def run(
        self,
        script: Script,
        *,
        pages: PageBook | None = None,
        variables: dict[str, str] | None = None,
        dry_run: bool = False,
        gate: Gate | None = None,
        observer: Callable[[AuditEntry], None] | None = None,
    ) -> RunResult:
        """observer 在每条审计写入后被调用（studio 用来实时推送进度）。"""
        variables = variables or {}
        gate = gate or Gate()
        run_id = uuid.uuid4().hex[:12]

        def log(step_id: str, action: str, status: StepStatus, **kw) -> None:
            entry = AuditEntry(
                run_id=run_id, at=self.now(), script=script.name, step_id=step_id,
                action=action, status=status, dry_run=dry_run, **kw,
            )
            self.audit.write(entry)
            if observer:
                observer(entry)

        missing = script.missing_variables(variables)
        if missing:
            reason = f"缺少变量：{', '.join(missing)}"
            log("-", "start", StepStatus.FAILED, detail=reason)
            return RunResult(run_id, RunStatus.FAILED, 0, reason)

        try:
            geo = self.io.calibrate(script.window, avoid=script.other_titles(MAIN))
        except CalibrationError as e:
            log("-", "calibrate", StepStatus.FAILED, detail=str(e))
            return RunResult(run_id, RunStatus.FAILED, 0, f"窗口校准失败：{e}")

        current = MAIN  # 当前操作的是哪个窗口
        done = 0
        targets: dict[str, object] = {}  # 目标图缓存
        for step in script.steps:
            try:
                paused = gate.wait()
                key = step.win or MAIN
                if not isinstance(step, Wait) and (key != current or paused):
                    # 换窗口（或者你刚用过电脑，窗口可能被挪了）：把这一步的窗口调到前台、校准尺寸。
                    # 主窗口以外的窗口常常是上一步才打开的（比如双击会话弹出的聊天窗口），给它一点时间出现
                    geo = self._calibrate(script.window_for(key), gate, wait=0 if key == MAIN else WINDOW_WAIT,
                                          avoid=script.other_titles(key))
                    current = key

                if isinstance(step, TypeText) and step.typed_with_ime:
                    reason = (f"{step.id} 是开着中文输入法录的，录到的只是按键「{step.text}」，"
                              "请先把它改成实际要输入的文字")
                    log(step.id, step.action, StepStatus.ABORTED, detail=reason)
                    return RunResult(run_id, RunStatus.ABORTED, done, reason)

                if isinstance(step, WaitPage):
                    ok, reason, shot_path, page_score, waited = self._wait_page(
                        step.id, step.page, geo, pages, gate, step.timeout)
                    status = StepStatus.OK if ok else StepStatus.ABORTED
                    detail = (f"页面「{step.page}」出现了" + (f"（等了 {waited:.1f} 秒）" if waited else "")) if ok else reason
                    log(step.id, step.action, status, screenshot=shot_path, expected_page=step.page,
                        page_score=page_score, detail=detail)
                    if not ok:
                        return RunResult(run_id, RunStatus.ABORTED, done, reason)
                    done += 1
                    continue

                if needs_page_check(step):
                    ok, reason, shot_path, page_score, _ = self._wait_page(
                        step.id, step.expected_page, geo, pages, gate, PAGE_WAIT)
                    if not ok:
                        log(step.id, step.action, StepStatus.ABORTED, screenshot=shot_path,
                            expected_page=step.expected_page, page_score=page_score, detail=reason)
                        return RunResult(run_id, RunStatus.ABORTED, done, reason)
                else:
                    page_score = None

                # 按图找：先找到目标再说（空跑也找，提前发现「找不到」）
                where: Located | None = None
                before = None  # 动作之前的画面：这一步把窗口关掉（点「关闭」、按 Esc）时，拿它当证据
                if isinstance(step, (Click, Hover)) and step.finds_target:
                    where, shot_path, before = self._find_target(step, geo, script, gate, targets)
                    if not where.found:
                        reason = (f"{step.id} 没找到要点的目标（等了 {step.target.timeout:g} 秒，"
                                  f"最像的地方得分 {where.score:.2f}）")
                        log(step.id, step.action, StepStatus.ABORTED, screenshot=shot_path,
                            expected_page=step.expected_page, page_score=page_score, detail=reason)
                        return RunResult(run_id, RunStatus.ABORTED, done, reason)

                if dry_run and getattr(step, "sends", False):
                    shot = self.storage.save(self.io.capture(geo), at=self.now(),
                                             step_id=step.id, action="dry_run")
                    log(step.id, step.action, StepStatus.SKIPPED, screenshot=str(shot),
                        expected_page=step.expected_page, page_score=page_score,
                        detail="空跑：跳过发送")
                    return RunResult(run_id, RunStatus.DRY_RUN_STOPPED, done)

                if before is None and isinstance(step, (Click, Drag, Key)):
                    before = self._before_shot(geo)
                self._perform(step, geo, script, variables, gate, where)
                if isinstance(step, Wait):
                    log(step.id, step.action, StepStatus.OK)
                else:
                    self.sleep(step.delay_after)
                    shot = self._after_shot(geo, step)
                    detail = _found_text(where) if where else ""
                    if where is not None and self._learn(script, step, where, before, geo.scale):
                        detail += f"，{LEARNED_NOTE}"
                    if shot is None and before is not None:
                        shot = str(self.storage.save(before, at=self.now(), step_id=step.id, action=step.action))
                        detail = "，".join(x for x in (detail, CLOSED_NOTE) if x)
                    log(step.id, step.action, StepStatus.OK, screenshot=shot,
                        expected_page=step.expected_page, page_score=page_score, detail=detail)
                done += 1
            except Cancelled:
                log(step.id, step.action, StepStatus.CANCELLED, detail="急停")
                raise
            except UserInterrupted:
                reason = "检测到你在用电脑，已中止当前步骤"
                log(step.id, step.action, StepStatus.ABORTED, detail=reason)
                return RunResult(run_id, RunStatus.ABORTED, done, reason)
            except Exception as e:  # noqa: BLE001 —— 任何异常都要留审计，不能让执行器吞掉
                log(step.id, step.action, StepStatus.FAILED, detail=f"{type(e).__name__}: {e}")
                return RunResult(run_id, RunStatus.FAILED, done, str(e))

        return RunResult(run_id, RunStatus.OK, done)

    def _learn(self, script: Script, step, where: Located, frame, scale: float) -> bool:
        """按图找到了、但得分偏低：自动记住目标现在的样子（见 anchors.learn_look）。
        只在确实找到（过了门槛）、窗口里只有一处像它、这一步做成了的时候学，免得把认错的东西记下来。"""
        t = step.target
        if not (where.found and where.count == 1 and where.score < LEARN_BELOW) or frame is None or script.path is None:
            return False
        x, y = (t.x + where.dx) * scale, (t.y + where.dy) * scale
        crop = frame[int(y):int(y + t.h * scale), int(x):int(x + t.w * scale)]
        if crop.size == 0:
            return False
        try:
            return learn_look(script.path, step.id, t.image, crop.copy(), scale) is not None
        except Exception:  # noqa: BLE001 —— 记不下来不影响执行
            return False

    def _before_shot(self, geo: WindowGeometry):
        """动作之前截一张（只留在内存里）；窗口已经没了就算了。"""
        try:
            return self.io.capture(geo)
        except CalibrationError:
            return None

    def _after_shot(self, geo: WindowGeometry, step) -> str | None:
        """操作后截图留证。这一步本身就可能把窗口关掉（点 ✕、最小化），截不到不算失败。"""
        try:
            return str(self.storage.save(self.io.capture(geo), at=self.now(), step_id=step.id, action=step.action))
        except CalibrationError:
            return None

    def _calibrate(self, window, gate: Gate, *, wait: float, avoid=()) -> WindowGeometry:
        """校准窗口；找不到时每 0.5 秒重试，最多等 wait 秒。
        悬停弹出的窗口（资料卡）只找位置：切前台、改尺寸都会让它消失。"""
        left = wait
        while True:
            try:
                if window.popup:
                    return self.io.locate_window(window, avoid=avoid)
                return self.io.calibrate(window, avoid=avoid)
            except CalibrationError:
                if left <= 0:
                    raise
            gate.check()
            self.sleep(0.5)
            left -= 0.5

    def _poll(self, attempt: Callable[[], bool], gate: Gate, timeout: float) -> float:
        """反复 attempt()，直到它返回 True 或超时；返回等了多久（秒）。期间急停 / 你动了照样生效。"""
        start, slept = time.monotonic(), 0.0
        while True:
            if attempt():
                return max(slept, time.monotonic() - start)
            waited = max(slept, time.monotonic() - start)
            if waited >= timeout:
                return waited
            gate.check()
            pause = min(POLL, timeout - waited)
            self.sleep(pause)
            slept += pause

    def _wait_page(self, step_id: str, page: str | None, geo: WindowGeometry, pages: PageBook | None,
                   gate: Gate, timeout: float):
        """等页面出现。返回 (是否出现, 原因, 截图路径, 得分, 等了几秒)；只保存最后一次的截图。"""
        if page is None:
            return False, "输入/发送步骤没有声明 expected_page", None, None, 0.0
        if pages is None:
            path = str(self.storage.save(self.io.capture(geo), at=self.now(), step_id=step_id, action="check"))
            return False, "脚本没有 pages.yaml，无法校验页面", path, None, 0.0
        last: dict = {}

        def attempt() -> bool:
            last["shot"] = self.io.capture(geo)
            last["m"] = match_page(pages, page, last["shot"], geo.scale)
            return last["m"].matched

        waited = self._poll(attempt, gate, timeout)
        path = str(self.storage.save(last["shot"], at=self.now(), step_id=step_id, action="check"))
        m = last["m"]
        if not m.matched:
            return (False, f"页面不符：期望 {page}，得分 {m.score:.2f}"
                    + (f"（等了 {waited:.1f} 秒还没出现）" if timeout else ""), path, m.score, waited)
        return True, "", path, m.score, round(waited, 1) if waited >= 0.05 else 0.0

    def _find_target(self, step: Click | Hover, geo: WindowGeometry, script: Script, gate: Gate, cache: dict):
        """在窗口里找这一步的目标图；返回 (定位结果, 截图路径, 最后一次截的画面)。找不到时截图留给审计。"""
        t = step.target
        images = [t.image, *(a for a in [t.alt] if a and (script.base_dir / a).exists())]  # 平时的样子丢了不影响
        for image in images:
            if image not in cache:
                cache[image] = read_image(script.base_dir / image)
        refs = [cache[image] for image in images]
        last: dict = {}
        hovered = False

        def attempt() -> bool:
            nonlocal hovered
            last["shot"] = self.io.capture(geo)
            loc = last["loc"] = locate_click(step, refs, last["shot"], geo.scale)
            if not loc.found and loc.near and not hovered:
                # 目标图是录制时鼠标悬停在上面的样子（按钮带高亮底色），现在没悬停所以不像：
                # 先把鼠标移到最像的那一处，等它亮起来再找。还不像就照样等、照样超时中止，不会点
                hovered = True
                mouse.move(self.io, geo.to_screen(loc.x, loc.y), self.rng, self.sleep, check=gate.check)
            return loc.found

        self._poll(attempt, gate, t.timeout)
        path = None
        if not last["loc"].found:
            path = str(self.storage.save(last["shot"], at=self.now(), step_id=step.id, action="find"))
        return last["loc"], path, last["shot"]

    def _perform(self, step, geo: WindowGeometry, script: Script, variables, gate: Gate,
                 where: Located | None = None) -> None:
        if isinstance(step, Click):
            x, y = (where.x, where.y) if where else (step.x, step.y)
            mouse.click(self.io, geo.to_screen(x, y), step.button, self.rng, self.sleep,
                        check=gate.check, count=step.count, hold=step.hold, modifiers=step.modifiers)
        elif isinstance(step, Hover):
            x, y = (where.x, where.y) if where else (step.x, step.y)
            mouse.move(self.io, geo.to_screen(x, y), self.rng, self.sleep, check=gate.check)
            effect = getattr(self.io, "effect", None)  # 执行特效（EffectsIO）：悬停没有键鼠事件，单独告诉它
            if effect is not None:
                effect("hover", seconds=step.seconds)
            self._pause(step.seconds, gate.check)  # 停住，等资料卡、菜单出来；这时你一动鼠标它就没了，只能中止
        elif isinstance(step, Drag):
            mouse.drag(self.io, geo.to_screen(step.x, step.y), geo.to_screen(step.x2, step.y2),
                       step.button, self.rng, self.sleep, duration=step.duration,
                       check=gate.check, modifiers=step.modifiers)
        elif isinstance(step, TypeText):
            text = script.render_text(step.text, variables)
            if step.clear_first:
                gate.check()
                self.io.clear_field()
                self.sleep(keyboard.interval(self.rng))
            keyboard.type_text(self.io, text, self.rng, self.sleep, check=gate.check)
        elif isinstance(step, Key):
            for i in range(step.times):
                gate.check()
                self.io.press_combo(step.combo)
                if i < step.times - 1:
                    self.sleep(self.rng.uniform(0.03, 0.07))  # 和系统按住不放时的自动重复差不多快
        elif isinstance(step, Scroll):
            if step.x is not None and step.y is not None:
                mouse.move(self.io, geo.to_screen(step.x, step.y), self.rng, self.sleep, check=gate.check)
            scroll_act.scroll(self.io, step.dy, self.rng, self.sleep, dx=step.dx, check=gate.check)
        elif isinstance(step, Wait):
            self._pause(step.seconds, gate.check_cancel)
        else:  # pragma: no cover
            raise TypeError(f"未知步骤：{step!r}")


    def _pause(self, seconds: float, check: Callable[[], None]) -> None:
        """等 seconds 秒，切成 TICK 一段，每段之前 check()：延迟设得再长，按急停也是马上停。"""
        n = max(1, math.ceil(seconds / TICK - 1e-9))
        for _ in range(n if seconds > 0 else 0):
            check()
            self.sleep(seconds / n)


CLOSED_NOTE = "点完窗口就关了，截图是点之前的画面"
LEARN_BELOW = 0.9  # 按图找的得分低于这个（但过了门槛）：按钮样子变了一点，自动记住新样子
LEARNED_NOTE = "按钮样子变了，已自动记住新样子"


def _found_text(where: Located) -> str:
    if not where.dx and not where.dy:
        return f"按图找到目标，位置没变（得分 {where.score:.2f}）"
    return f"按图找到目标，位置偏了 {where.dx:+g}, {where.dy:+g}（得分 {where.score:.2f}）"
