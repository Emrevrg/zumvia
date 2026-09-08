"""
Gerçek Zamanlı Yayın Merkezi (WebSocket Hub)
============================================
Bot motoru arka plan iş parçacıklarında (APScheduler) çalışır; arayüz ise
asyncio WebSocket üzerinden dinler. Bu köprü, iş parçacığı güvenli şekilde
olayları abonelere dağıtır.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict, deque
from typing import Any

from ..core.logging import get_logger

log = get_logger("zumvia.hub")


class EventHub:
    """Kullanıcı bazlı yayın merkezi + son olay tamponu."""

    def __init__(self, buffer_size: int = 200) -> None:
        self._subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._recent: dict[int, deque] = defaultdict(lambda: deque(maxlen=buffer_size))
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Uygulama açılışında ana olay döngüsü kaydedilir."""
        self._loop = loop

    # -- abonelik ----------------------------------------------------------- #
    async def subscribe(self, user_id: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers[user_id].add(queue)
        for event in list(self._recent[user_id])[-40:]:
            queue.put_nowait(event)
        return queue

    def unsubscribe(self, user_id: int, queue: asyncio.Queue) -> None:
        self._subscribers[user_id].discard(queue)

    # -- yayın -------------------------------------------------------------- #
    def publish(self, user_id: int, event: dict[str, Any]) -> None:
        """Herhangi bir iş parçacığından güvenle çağrılabilir."""
        self._recent[user_id].append(event)
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._fanout, user_id, event)
        except RuntimeError:  # pragma: no cover — kapanış anı
            pass

    def _fanout(self, user_id: int, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(user_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Yavaş istemci tüm sistemi yavaşlatmasın
                # Yavaş istemcide en eski olay düşürülür; kuyruk yarışı
                # nedeniyle bu da başarısız olabilir ve sorun değildir.
                with contextlib.suppress(Exception):
                    queue.get_nowait()
                    queue.put_nowait(event)

    def recent(self, user_id: int, limit: int = 80) -> list[dict[str, Any]]:
        return list(self._recent[user_id])[-limit:]

    @staticmethod
    def encode(event: dict[str, Any]) -> str:
        return json.dumps(event, ensure_ascii=False, default=str)


hub = EventHub()
