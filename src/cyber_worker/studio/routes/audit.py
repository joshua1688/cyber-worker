"""审计页：按可疑度排序回看每一步的截图和预期状态，标记「没问题 / 有问题」。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from cyber_worker.macro.audit.reporter import daily_report
from cyber_worker.macro.audit.reviewer import rank, sample
from cyber_worker.studio.routes.edit import studio

router = APIRouter(prefix="/api/audit")


class Mark(BaseModel):
    reviewed: int  # 0 未审 / 1 没问题 / 2 有问题


@router.get("/days")
def days(request: Request):
    return studio(request).app.audit.days()


@router.get("")
def entries(request: Request, day: date | None = None, script: str | None = None,
            unreviewed: bool = False, spot_check: int = 5):
    st = studio(request)
    rows = st.app.audit.entries(day=day, script=script, unreviewed_only=unreviewed)
    for r in rows:
        r["screenshot_url"] = st.file_url(Path(r["screenshot"])) if r["screenshot"] else None
    ranked = rank(rows)
    spot_ids = {r["id"] for r in sample(rows, spot_check)}
    for r in ranked:
        r["spot_check"] = r["id"] in spot_ids
    return {"report": daily_report(rows), "entries": ranked}


@router.post("/{entry_id}")
def mark(entry_id: int, body: Mark, request: Request):
    try:
        ok = studio(request).app.audit.mark(entry_id, body.reviewed)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not ok:
        raise HTTPException(404, "记录不存在")
    return {"ok": True}
