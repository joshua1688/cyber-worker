"""WebSocket 推送：录制时实时推步骤、执行时实时推每一步的审计结果。

事件从执行器线程 / 录制线程发出，这里负责安全地转进 asyncio 事件循环。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect


class Hub:
    def __init__(self) -> None:
        self._clients: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def clients(self) -> int:
        """连着的页面数。"""
        return len(self._clients)

    def publish(self, event: dict[str, Any]) -> None:
        """线程安全，可在任意线程调用。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        for q in list(self._clients):
            loop.call_soon_threadsafe(q.put_nowait, event)

    async def serve(self, ws: WebSocket) -> None:
        await ws.accept()
        self._loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        self._clients.add(q)
        receiver = asyncio.create_task(self._drain(ws))
        try:
            while True:
                getter = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
                if receiver in done:
                    getter.cancel()
                    return
                await ws.send_json(getter.result())
        except WebSocketDisconnect:
            pass
        finally:
            self._clients.discard(q)
            receiver.cancel()

    @staticmethod
    async def _drain(ws: WebSocket) -> None:
        """客户端不发消息，只用来感知断开。"""
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            return
