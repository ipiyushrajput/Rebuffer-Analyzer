"""WebSocket hub.

One topic per job. A client that connects mid-analysis receives the replay buffer first, so
the UI renders everything measured so far before live messages start arriving.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict, deque
from typing import Any

from fastapi import WebSocket

from app.ws.schemas import server_message

logger = logging.getLogger(__name__)

REPLAY_LIMIT = 800
SEND_TIMEOUT_S = 5.0


class Hub:
    """Fan-out of engine events to every socket watching a job."""

    def __init__(self) -> None:
        self._sockets: dict[str, set[WebSocket]] = defaultdict(set)
        self._replay: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=REPLAY_LIMIT)
        )
        self._lock = asyncio.Lock()

    async def connect(self, topic: str, socket: WebSocket) -> None:
        await socket.accept()
        async with self._lock:
            self._sockets[topic].add(socket)
        for message in list(self._replay[topic]):
            with contextlib.suppress(Exception):
                await socket.send_json(message)

    async def disconnect(self, topic: str, socket: WebSocket) -> None:
        async with self._lock:
            self._sockets[topic].discard(socket)
            if not self._sockets[topic]:
                self._sockets.pop(topic, None)

    async def publish(self, topic: str, kind: str, payload: dict[str, Any]) -> None:
        message = server_message(kind, topic, payload)
        # Everything except the high-frequency metric stream is replayed to late joiners.
        if kind != "metric":
            self._replay[topic].append(message)

        for socket in list(self._sockets.get(topic, ())):
            try:
                await asyncio.wait_for(socket.send_json(message), timeout=SEND_TIMEOUT_S)
            except Exception:
                await self.disconnect(topic, socket)

    def clear(self, topic: str) -> None:
        self._replay.pop(topic, None)

    def subscriber_count(self, topic: str) -> int:
        return len(self._sockets.get(topic, ()))


hub = Hub()
