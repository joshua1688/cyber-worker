"""应用总览、应用首页：每个应用（QQ、微信…）和它的流程；删除 / 撤销删除整个应用。流程的增删改走 edit / record。"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from pydantic import BaseModel

from cyber_worker.studio.routes.edit import studio

router = APIRouter(prefix="/api")


class Restore(BaseModel):
    trash: str


@router.get("/apps")
def list_apps(request: Request):
    return studio(request).apps(date.today())


@router.delete("/apps/{platform}")
def delete_app(platform: str, request: Request):
    """删除整个应用：挪进回收站，返回回收站里的名字（撤销用）。"""
    return studio(request).delete_app(platform)


@router.post("/apps/restore")
def restore_app(body: Restore, request: Request):
    return {"platform": studio(request).restore_app(body.trash)}
