"""平台包导出（下载 .cwpack）/ 导入（上传文件原始字节，不需要 multipart）。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from cyber_worker.adapters.pack import EXT
from cyber_worker.studio.routes.edit import studio

router = APIRouter(prefix="/api")

MAX_UPLOAD = 100 * 1024 * 1024


@router.get("/packs/{platform}")
def export_pack(platform: str, request: Request):
    data = studio(request).export_pack(platform)
    name = quote(platform + EXT)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})


@router.post("/packs")
async def import_pack(request: Request):
    data = await request.body()
    if not data:
        raise HTTPException(400, "没有收到文件")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(400, "文件太大")
    return studio(request).import_pack(data)
