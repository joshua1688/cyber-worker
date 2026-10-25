"""图片读取：参考图、最新帧、锚点图、运行截图。只允许读 user_data 下的文件。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from cyber_worker.studio.routes.edit import studio

router = APIRouter(prefix="/api")


IMAGE_TYPES = {".png": "image/png", ".webp": "image/webp"}  # 审计截图是 .webp，宏里的图是 .png


@router.get("/file")
def file(path: str, request: Request):
    st = studio(request)
    target = (st.user_data / path).resolve()
    media = IMAGE_TYPES.get(target.suffix.lower())
    if not target.is_relative_to(st.user_data) or media is None:
        raise HTTPException(400, "非法路径")
    if not target.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target, media_type=media, headers={"Cache-Control": "no-cache"})
