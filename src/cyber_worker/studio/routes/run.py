"""执行 / 空跑 / 急停。执行交给全局执行器排队，进度通过 WebSocket 推送。"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from cyber_worker.macro.audit.logger import AuditEntry
from cyber_worker.macro.runner import Cancelled
from cyber_worker.macro.vision.fingerprint import PageBook
from cyber_worker.studio.routes.edit import studio

router = APIRouter(prefix="/api")


class RunRequest(BaseModel):
    dry_run: bool = True
    variables: dict[str, str] = {}


def entry_json(st, entry: AuditEntry) -> dict:
    data = asdict(entry)
    data["at"] = entry.at.isoformat(timespec="seconds")
    data["status"] = entry.status.value
    data["screenshot_url"] = st.file_url(Path(entry.screenshot)) if entry.screenshot else None
    return data


@router.post("/scripts/{script_id:path}/run")
def run(script_id: str, body: RunRequest, request: Request):
    st = studio(request)
    if st.recording:
        raise HTTPException(409, "正在录制，先停止录制")
    script = st.load(script_id)
    pages = PageBook.load(script.pages_path) if script.pages_path else None

    def job(gate):
        # 执行器在调用 job 之前会先等用户松开键鼠；能走到这里就是真正开始了
        st.hub.publish({"type": "run_state", "script_id": script_id, "state": "running",
                        "dry_run": body.dry_run})
        return st.app.runner.run(
            script, pages=pages, variables=body.variables, dry_run=body.dry_run, gate=gate,
            observer=lambda e: st.hub.publish({"type": "run_entry", "script_id": script_id,
                                               "entry": entry_json(st, e)}),
        )

    # 先发「排队」再提交：避免任务立刻开始时「开始」比「排队」先到
    st.hub.publish({"type": "run_state", "script_id": script_id, "state": "queued",
                    "dry_run": body.dry_run, "idle_sec": st.run_idle_sec})
    fut = st.app.executor.submit(
        job, resume_after_idle_sec=st.run_idle_sec,
        # 等你停手期间推剩余秒数：界面显示倒计时，你一动就回到满值
        on_wait=lambda remaining: st.hub.publish({"type": "run_wait", "script_id": script_id,
                                                  "remaining": round(remaining, 2), "total": st.run_idle_sec}),
    )

    def done(f):
        if f.cancelled():
            result = {"status": "cancelled", "reason": "急停（排队中被取消）"}
        elif isinstance(f.exception(), Cancelled):
            result = {"status": "cancelled", "reason": "急停"}
        elif f.exception() is not None:
            result = {"status": "failed", "reason": str(f.exception())}
        else:
            r = f.result()
            result = {"status": r.status.value, "reason": r.reason, "run_id": r.run_id,
                      "steps_done": r.steps_done}
        st.hub.publish({"type": "run_state", "script_id": script_id, "state": "done", **result})

    fut.add_done_callback(done)
    return {"queued": True}


@router.post("/panic")
def panic(request: Request):
    st = studio(request)
    return {"cancelled_queued": st.app.executor.panic()}


@router.get("/status")
def status(request: Request):
    st = studio(request)
    return {
        "busy": st.app.executor.busy,
        "recording": st.recording.script_id if st.recording else None,
        # 实际生效的键（配置的被占用时会自动换成备选），以及换了的说明
        "panic_hotkey": st.app.hotkeys.effective("panic"),
        "record_hotkey": st.app.hotkeys.effective("record"),
        "hotkey_notes": list(st.app.hotkeys.notes.values()),
    }


class Current(BaseModel):
    script_id: str | None


@router.post("/current")
def set_current(body: Current, request: Request):
    """界面切换宏时告诉服务端：按录制快捷键时录的就是它。"""
    studio(request).current_id = body.script_id
    return {"ok": True}
