"""唯一装配入口：CLI 和 studio 都从这里拿到组装好的对象，别处不要自己 new 依赖。"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cyber_worker.config import Config
from cyber_worker.infra.sqlite.audit_store import SqliteAuditStore
from cyber_worker.infra.sqlite.db import connect
from cyber_worker.macro.executor import Executor
from cyber_worker.macro.io.base import ActivityMonitor, ScreenIO
from cyber_worker.macro.io.hotkeys import HotkeyManager, NullBackend
from cyber_worker.macro.recorder import RecordingBuilder
from cyber_worker.macro.runner import Runner
from cyber_worker.macro.screenshot.storage import ScreenshotStorage

# 录制监听：阻塞到 stop 被置位，期间把真实输入喂给 builder
Listen = Callable[[RecordingBuilder, threading.Event], None]
# 等用户下一次真实点击，返回系统输入坐标；超时 None（「点一下目标窗口」用）
WaitClick = Callable[[float], "tuple[float, float] | None"]


@dataclass
class App:
    config: Config
    io: ScreenIO
    activity: ActivityMonitor
    storage: ScreenshotStorage
    audit: SqliteAuditStore
    runner: Runner
    executor: Executor
    listen: Listen
    wait_click: WaitClick
    # 全局快捷键（录制开关、急停）；默认不真的注册，build() 换成平台实现
    hotkeys: HotkeyManager = field(default_factory=lambda: HotkeyManager(NullBackend()))
    # 设置里改了快捷键写回哪个文件；None = 只改内存
    config_path: Path | None = None

    @property
    def user_data(self) -> Path:
        return self.config.paths.user_data


def _platform_io() -> ScreenIO:
    if sys.platform == "darwin":
        from cyber_worker.macro.io.macos import MacScreenIO

        return MacScreenIO()
    if sys.platform == "win32":
        from cyber_worker.macro.io.windows import WinScreenIO

        return WinScreenIO()
    raise RuntimeError(f"屏幕操作只支持 macOS 和 Windows（当前：{sys.platform}）")


def build(config_path: str | Path = "config.toml") -> App:
    from cyber_worker.macro.io.hotkeys import platform_backend
    from cyber_worker.macro.io.pynput_input import InputActivityMonitor, wait_for_click
    from cyber_worker.macro.recorder import listen

    config = Config.load(config_path)
    data = config.paths.user_data
    io = _platform_io()
    storage = ScreenshotStorage(data / "screenshots")
    audit = SqliteAuditStore(connect(data / "state.db"))
    executor = Executor(
        InputActivityMonitor(),
        pause_on_user_input=config.executor.pause_on_user_input,
        resume_after_idle_sec=config.executor.resume_after_idle_sec,
    )
    return App(
        config=config,
        io=io,
        activity=executor.activity,
        storage=storage,
        audit=audit,
        runner=Runner(io, storage, audit),
        executor=executor,
        listen=lambda builder, stop: listen(builder, stop, io.input_to_points,
                                            locate=io.window_geometry_at, foreground=io.foreground,
                                            ime=io.ime_active, held=io.held_modifiers),
        wait_click=wait_for_click,
        hotkeys=HotkeyManager(platform_backend()),
        config_path=Path(config_path),
    )
