"""设置 → 快捷键：查看、检查冲突、改键（写回 config.toml 并立即生效）。"""

from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from cyber_worker.config import save_value
from cyber_worker.macro.io.hotkeys import DEFAULTS, LABELS
from cyber_worker.macro.io.pynput_input import normalize_combo
from cyber_worker.studio.routes.edit import studio
from cyber_worker.studio.routes.record import toggle_recording
from cyber_worker.studio.routes.run import hotkey_dry_run

router = APIRouter(prefix="/api/hotkeys")

# 名字 → config.toml 里的位置
CONFIG_KEYS = {"record": ("studio", "record_hotkey"), "dry_run": ("studio", "dry_run_hotkey"),
               "panic": ("executor", "panic_hotkey")}


def configured(config) -> dict[str, str]:
    return {name: getattr(getattr(config, sec), key) for name, (sec, key) in CONFIG_KEYS.items()}


def bind(st) -> None:
    """按配置注册全局快捷键（启动时、改键后）。"""

    def panic():
        n = st.app.executor.panic()
        st.hub.publish({"type": "toast", "error": True, "text": f"已急停（取消了 {n} 个排队任务）"})

    st.app.hotkeys.apply(configured(st.app.config), {
        # 开始录制要校准窗口、截图，耗时约 1 秒：放到单独线程，别卡住快捷键监听
        "record": lambda: threading.Thread(target=toggle_recording, args=(st,), daemon=True).start(),
        "dry_run": lambda: threading.Thread(target=hotkey_dry_run, args=(st,), daemon=True).start(),
        "panic": panic,
    })
    publish(st)


def payload(st) -> dict:
    hk = st.app.hotkeys
    return {
        "items": [{"name": n, "label": LABELS[n], "combo": hk.effective(n), "active": n in hk.active,
                   "configured": normalize_combo(hk.wanted.get(n, DEFAULTS[n])),
                   "default": normalize_combo(DEFAULTS[n]), "note": hk.notes.get(n)} for n in CONFIG_KEYS],
        "suspended": hk.suspended,
    }


def publish(st) -> None:
    st.hub.publish({"type": "hotkeys", **payload(st)})


@router.get("")
def get_hotkeys(request: Request):
    return payload(studio(request))


class CheckBody(BaseModel):
    name: str
    combo: str
    other: str | None = None  # 设置面板里另一个快捷键当前填的值（还没保存）


@router.post("/check")
def check(body: CheckBody, request: Request):
    st = studio(request)
    if body.name not in CONFIG_KEYS:
        raise HTTPException(404, "没有这个快捷键")
    others = {body.other} if body.other else None
    result = st.app.hotkeys.check(body.name, body.combo, others=others)
    return {"combo": normalize_combo(body.combo), "status": result.status, "message": result.message,
            "blocking": result.blocking}


@router.put("")
def save(body: dict[str, str], request: Request):
    """{"record": "alt+r", "panic": "..."}：有冲突就整体拒绝；通过就写回配置并立即重新注册。"""
    st = studio(request)
    hk = st.app.hotkeys
    new = {**configured(st.app.config), **{n: normalize_combo(c) for n, c in body.items() if n in CONFIG_KEYS}}
    for name, combo in new.items():
        others = {c for n, c in new.items() if n != name}
        result = hk.check(name, combo, others=others)
        if result.blocking:
            raise HTTPException(400, f"「{LABELS[name]}」{combo}：{result.message}")
    for name, combo in new.items():
        sec, key = CONFIG_KEYS[name]
        setattr(getattr(st.app.config, sec), key, combo)
        if st.app.config_path is not None:
            save_value(st.app.config_path, sec, key, combo)
    bind(st)
    return payload(st)


@router.post("/suspend")
def suspend(request: Request):
    """设置面板正在录新键：先放开全局快捷键，按下去浏览器才收得到。"""
    st = studio(request)
    st.app.hotkeys.suspend()
    return {"suspended": True}


@router.post("/resume")
def resume(request: Request):
    st = studio(request)
    st.app.hotkeys.resume()
    return {"suspended": False}
