"""只接受 studio 自己页面发来的请求。

studio 开在本机端口上，浏览器里开着的任何网页都能往它发请求：
- 普通网页能直接发不带内容的 POST（停止录制、急停、导入平台包…），还能连 WebSocket 收录制 / 执行的实时推送；
- 「DNS 重绑定」：恶意域名临时解析到 127.0.0.1，浏览器就当它是同源页面，能调用全部接口（执行流程、读审计截图）。

所以每个 HTTP 请求和 WebSocket 连接都要满足：Host 是本机名字（挡住重绑定：那时 Host 是对方的域名），
带了 Origin 的话 Origin 也必须是本机（挡住别的网页；浏览器跨站请求、WebSocket 都会带 Origin）。
"""

from __future__ import annotations

from urllib.parse import urlsplit

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname(value: str) -> str | None:
    try:
        return urlsplit(value if "//" in value else "//" + value).hostname
    except ValueError:
        return None


def allowed(host: str | None, origin: str | None, hosts=LOCAL_HOSTS) -> bool:
    if _hostname(host or "") not in hosts:
        return False
    return origin is None or _hostname(origin) in hosts  # Origin: null（沙箱 iframe、file://）也不行


class LocalOnly:
    """ASGI 中间件：不是本机页面发来的 HTTP 请求回 403，WebSocket 直接拒绝握手。"""

    def __init__(self, app, hosts=LOCAL_HOSTS) -> None:
        self.app = app
        self.hosts = hosts

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            if not allowed(headers.get("host"), headers.get("origin"), self.hosts):
                if scope["type"] == "websocket":
                    await receive()  # websocket.connect
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = "只接受本机 studio 页面发来的请求".encode()
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
