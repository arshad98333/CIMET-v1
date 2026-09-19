from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LiveHub:
    def __init__(self) -> None:
        self._subs: dict[str, set[WebSocket]] = defaultdict(set)
        self._sse: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._log: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def buffer(self, lead_id: str) -> list[dict[str, Any]]:
        return list(self._log.get(lead_id, []))

    async def subscribe(self, lead_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._subs[lead_id].add(websocket)
        for item in self.buffer(lead_id)[-80:]:
            try:
                await websocket.send_json(item)
            except Exception:
                break

    async def unsubscribe(self, lead_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._subs[lead_id].discard(websocket)

    async def subscribe_sse(self, lead_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        async with self._lock:
            self._sse[lead_id].add(queue)
        return queue

    async def unsubscribe_sse(self, lead_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._sse[lead_id].discard(queue)

    async def publish(self, lead_id: str, payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        event = {"id": uuid4().hex[:12], "lead_id": lead_id, "ts": _now(), **payload}
        async with self._lock:
            if persist:
                self._log[lead_id].append(event)
            sockets = set(self._subs.get(lead_id, set())) | set(self._subs.get("*", set()))
            queues = set(self._sse.get(lead_id, set())) | set(self._sse.get("*", set()))
        dead_ws: list[WebSocket] = []
        for socket in sockets:
            try:
                await socket.send_json(event)
            except Exception:
                dead_ws.append(socket)
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue
        if dead_ws:
            async with self._lock:
                for socket in dead_ws:
                    self._subs[lead_id].discard(socket)
                    self._subs["*"].discard(socket)
        return event

    async def publish_typed(self, lead_id: str, role: str, text: str, **extra: Any) -> None:
        message_id = extra.pop("message_id", None) or uuid4().hex[:10]
        i = 0
        while i < len(text):
            step = 1 if text[i] in ".,?!" else 2
            delta = text[i : i + step]
            i += step
            await self.publish(
                lead_id,
                {
                    "kind": "token",
                    "message_id": message_id,
                    "role": role,
                    "delta": delta,
                    "done": False,
                },
                persist=False,
            )
            await asyncio.sleep(0.016)
        await self.publish(
            lead_id,
            {
                "kind": "transcript",
                "message_id": message_id,
                "role": role,
                "text": text,
                "done": True,
                **extra,
            },
        )


hub = LiveHub()


async def live_socket_loop(lead_id: str, websocket: WebSocket) -> None:
    await hub.subscribe(lead_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unsubscribe(lead_id, websocket)


async def sse_event_stream(lead_id: str):
    queue = await hub.subscribe_sse(lead_id)
    try:
        for item in hub.buffer(lead_id)[-80:]:
            yield f"id: {item.get('id')}\ndata: {json.dumps(item, default=str)}\n\n"
        yield "event: ready\ndata: {}\n\n"
        while True:
            item = await queue.get()
            yield f"id: {item.get('id')}\ndata: {json.dumps(item, default=str)}\n\n"
    finally:
        await hub.unsubscribe_sse(lead_id, queue)
