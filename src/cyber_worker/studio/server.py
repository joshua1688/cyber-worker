"""studio：唯一的 FastAPI 实例。前端是 static/ 下的原生 HTML + JS + Canvas。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from cyber_worker.bootstrap import App
from cyber_worker.studio.routes import (
    audit,
    edit,
    hotkeys,
    preview,
    record,
    run,
    settings,
)
from cyber_worker.studio.state import Studio

STATIC = Path(__file__).parent / "static"
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text x="50" y="54" text-anchor="middle" '
    'dominant-baseline="central" font-size="86" '
    "font-family=\"'Segoe UI Emoji','Apple Color Emoji','Noto Color Emoji',sans-serif\">🤖</text></svg>"
)


def create_app(app: App, *, run_idle_sec: float | None = None) -> FastAPI:
    """run_idle_sec：点执行后等键鼠停几秒才开始；不给就用配置里的（测试传 0）。"""

    @asynccontextmanager
    async def lifespan(_):
        yield
        app.hotkeys.stop()

    api = FastAPI(title="赛博打工人 Studio", lifespan=lifespan)
    studio_cfg = app.config.studio
    api.state.studio = Studio(app, run_idle_sec=studio_cfg.run_idle_sec if run_idle_sec is None else run_idle_sec,
                              record_delays=studio_cfg.record_delays)
    hotkeys.bind(api.state.studio)  # 全局快捷键：配置的键被占用时自动换一个，界面上会提示

    for r in (edit.router, record.router, run.router, preview.router, audit.router, hotkeys.router,
              settings.router):
        api.include_router(r)

    @api.websocket("/ws")
    async def ws(socket: WebSocket):
        await api.state.studio.hub.serve(socket)

    @api.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @api.get("/audit")
    def audit_page():
        return FileResponse(STATIC / "audit.html")

    @api.get("/favicon.ico", include_in_schema=False)
    def favicon():
        # 页面里用的是内联的 🤖 图标；浏览器打开非 HTML 页面时仍会默认请求这个地址
        return Response(FAVICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})

    @api.middleware("http")
    async def no_stale_frontend(request, call_next):
        # 本地工具：前端文件每次都向服务端确认（ETag 未变则 304），升级后不会用到旧的 JS
        response = await call_next(request)
        if request.url.path == "/" or request.url.path == "/audit" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    api.mount("/static", StaticFiles(directory=STATIC), name="static")
    return api
