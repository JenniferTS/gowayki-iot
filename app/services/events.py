from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class EventHub:
    """Pub/sub in-process para empujar cambios por SSE."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, key: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=16)
        async with self._lock:
            self._subs[key].add(q)
        return q

    async def unsubscribe(self, key: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            subs = self._subs.get(key)
            if not subs:
                return
            subs.discard(q)
            if not subs:
                self._subs.pop(key, None)

    async def publish(self, key: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._subs.get(key, ()))
        for q in targets:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    async def publish_all(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            keys = list(self._subs.keys())
        for key in keys:
            await self.publish(key, payload)

    def subscriber_count(self) -> int:
        return sum(len(v) for v in self._subs.values())


# Alias histórico
ParaderoHub = EventHub
