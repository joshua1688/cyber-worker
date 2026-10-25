"""设置（快捷键以外的）：外观、录制与执行。改了立即生效，并写回 config.toml。"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from cyber_worker.config import save_value
from cyber_worker.studio.routes.edit import studio

router = APIRouter(prefix="/api/settings")

# 设置项 → config.toml 里的位置
FIELDS = {
    "theme": ("studio", "theme"),
    "record_delays": ("studio", "record_delays"),
    "record_append": ("studio", "record_append"),
    "run_idle_sec": ("studio", "run_idle_sec"),
    "pack_screenshots": ("studio", "pack_screenshots"),
    "record_chime": ("studio", "record_chime"),
    "run_chime": ("studio", "run_chime"),
    "pause_on_user_input": ("executor", "pause_on_user_input"),
    "resume_after_idle_sec": ("executor", "resume_after_idle_sec"),
    "audit_keep_days": ("audit", "keep_days"),
    "show_effects": ("executor", "show_effects"),
    "play_sounds": ("executor", "play_sounds"),
}


class SettingsPatch(BaseModel):
    theme: Literal["dark", "light", "system"] | None = None
    record_delays: bool | None = None
    record_append: bool | None = None
    run_idle_sec: float | None = Field(None, ge=0, le=60)
    pack_screenshots: bool | None = None
    record_chime: bool | None = None
    run_chime: bool | None = None
    pause_on_user_input: bool | None = None
    resume_after_idle_sec: float | None = Field(None, ge=0, le=3600)
    audit_keep_days: int | None = Field(None, ge=1, le=3650)
    show_effects: bool | None = None
    play_sounds: bool | None = None


def _version() -> str:
    try:
        return version("cyber-worker")
    except PackageNotFoundError:
        return "dev"


def payload(st) -> dict:
    cfg = st.app.config
    data = {name: getattr(getattr(cfg, sec), key) for name, (sec, key) in FIELDS.items()}
    data["run_idle_sec"] = st.run_idle_sec
    data["record_delays"] = st.record_delays
    data["record_append"] = st.record_append
    path = st.app.config_path
    data["config_path"] = str(path.resolve()) if path else None
    data["user_data"] = str(st.user_data)
    data["version"] = _version()
    return data


@router.get("")
def get_settings(request: Request):
    return payload(studio(request))


@router.patch("")
def update(body: SettingsPatch, request: Request):
    st = studio(request)
    cfg = st.app.config
    for name, value in body.model_dump(exclude_none=True).items():
        sec, key = FIELDS[name]
        setattr(getattr(cfg, sec), key, value)
        if st.app.config_path is not None:
            save_value(st.app.config_path, sec, key, value)
    # 立即生效：不用重启
    st.run_idle_sec = cfg.studio.run_idle_sec
    st.record_delays = cfg.studio.record_delays
    st.record_append = cfg.studio.record_append
    st.app.executor.pause_on_user_input = cfg.executor.pause_on_user_input
    st.app.executor.resume_after_idle_sec = cfg.executor.resume_after_idle_sec
    if st.app.effects is not None:
        st.app.effects.enabled = cfg.executor.show_effects
        st.app.effects.sound_enabled = cfg.executor.play_sounds
    data = payload(st)
    st.hub.publish({"type": "settings", **data})  # 另一个标签页也跟着换
    return data
