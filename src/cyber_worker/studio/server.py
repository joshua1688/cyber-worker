"""studio：唯一的 FastAPI 实例。前端是 static/ 下的原生 HTML + JS + Canvas。"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from cyber_worker.bootstrap import App
from cyber_worker.studio.guard import LocalOnly
from cyber_worker.studio.routes import (
    apps,
    audit,
    edit,
    hotkeys,
    packs,
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


def build_id(static: Path = STATIC) -> str:
    """前端文件的指纹（文件名 + 修改时间 + 大小）。页面拿它和自己加载时的比，不一样就是旧页面，该刷新了。
    每次请求现算：改了 JS / CSS 不用重启 studio 也认得出来。"""
    h = hashlib.sha1()
    for f in sorted(static.rglob("*")):
        if f.is_file():
            st = f.stat()
            h.update(f"{f.relative_to(static).as_posix()}|{st.st_mtime_ns}|{st.st_size}\n".encode())
    return h.hexdigest()[:12]


def create_app(app: App, *, run_idle_sec: float | None = None) -> FastAPI:
    """run_idle_sec：点执行后等键鼠停几秒才开始；不给就用配置里的（测试传 0）。"""

    @asynccontextmanager
    async def lifespan(_):
        yield
        app.hotkeys.stop()

    api = FastAPI(title="赛博打工人 Studio", lifespan=lifespan)
    studio_cfg = app.config.studio
    api.state.studio = Studio(app, run_idle_sec=studio_cfg.run_idle_sec if run_idle_sec is None else run_idle_sec,
                              record_delays=studio_cfg.record_delays, record_append=studio_cfg.record_append)
    app.prune_audit(date.today())  # 保留期以前的审计记录和截图
    api.state.studio.migrate_layout()  # 以前的 templates/ 挪成 flows/
    api.state.studio.collect_garbage()  # 流程目录里没人用的旧图（重新录制、换画面留下的，过了几天撤销也用不上了）
    hotkeys.bind(api.state.studio)  # 全局快捷键：配置的键被占用时自动换一个，界面上会提示

    for r in (apps.router, edit.router, record.router, run.router, preview.router, audit.router, hotkeys.router,
              settings.router, packs.router):
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

    @api.get("/apps")
    def apps_page():
        return FileResponse(STATIC / "apps.html")

    @api.get("/api/build")
    def build():
        return {"build": build_id()}

    @api.get("/favicon.ico", include_in_schema=False)
    def favicon():
        # 页面里用的是内联的 🤖 图标；浏览器打开非 HTML 页面时仍会默认请求这个地址
        return Response(FAVICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})

    @api.middleware("http")
    async def no_stale_frontend(request, call_next):
        # 本地工具：前端文件每次都向服务端确认（ETag 未变则 304），升级后不会用到旧的 JS
        response = await call_next(request)
        if request.url.path in ("/", "/audit", "/apps") or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    api.mount("/static", StaticFiles(directory=STATIC), name="static")
    api.add_middleware(LocalOnly)  # 只接受本机 studio 页面发来的请求（防别的网页、DNS 重绑定）
    return api
