"""
WebSocket — Canlı Olay Akışı
=============================
Arayüzdeki şeffaf işlem terminali ve durum göstergeleri bu kanaldan beslenir.
"""
from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..core.db import SessionLocal
from ..core.logging import get_logger
from ..core.security import decode_access_token
from ..engine.hub import hub
from ..models import User

log = get_logger("zumvia.api.ws")

router = APIRouter(tags=["Canlı Akış"])


def _resolve_user(token: str) -> User | None:
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except Exception:  # noqa: BLE001
        return None
    db = SessionLocal()
    try:
        user = db.get(User, int(payload.get("sub", 0)))
        return user if user and user.is_active else None
    finally:
        db.close()


@router.websocket("/ws/stream")
async def stream(websocket: WebSocket, token: str = Query(default="")) -> None:
    user = _resolve_user(token)
    if user is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    queue = await hub.subscribe(user.id)
    await websocket.send_text(hub.encode({"type": "hello", "user_id": user.id}))

    async def heartbeat() -> None:
        """
        Vekil sunucuların boşta duran bağlantıyı kesmemesi için nabız.

        KRİTİK: Bu bağımsız bir görevdir. Ana döngü kapandıktan sonra buradan
        gönderim denenirse hata ANA DÖNGÜNÜN try/except'ine düşmez; doğrudan
        ASGI katmanına sızar ve her sekme kapanışında yığın izi basardı.
        Bu yüzden kendi korumasını taşır.
        """
        try:
            while True:
                await asyncio.sleep(25)
                await websocket.send_text(hub.encode({"type": "ping"}))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # Bağlantı kapanmış: nabız sessizce durur, hata yayılmaz.
            return

    beat = asyncio.create_task(heartbeat())
    try:
        while True:
            event = await queue.get()
            await websocket.send_text(hub.encode(event))
    except WebSocketDisconnect:
        # Tarayıcı sekmesi kapandı — beklenen son.
        pass
    except Exception as exc:  # noqa: BLE001
        # Diğer kopmalar da normaldir (ağ, vekil zaman aşımı) ama sessiz
        # kalmamalı: canlı akışın neden düştüğünü bilmek gerekir.
        log.info("canlı akış kapandı: %s", type(exc).__name__)
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        hub.unsubscribe(user.id, queue)
