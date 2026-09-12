"""
API Bağımlılıkları — Oturum ve kullanıcı çözümleme
"""
from __future__ import annotations

import math
from typing import Any

from fastapi import Depends, HTTPException, Query, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..core.db import get_db
from ..core.security import decode_access_token
from ..models import Bot, User

bearer = HTTPBearer(auto_error=False)


def num(value: Any, default: float = 0.0) -> float:
    """
    Eski satırlardaki NULL sayıları güvenli sayıya çevirir.

    Şema göçü yalnızca ekleyicidir (`ADD COLUMN`); eski satırlarda yeni
    kolonlar NULL kalır. Serileştirici `round(x)` / `x + y` yaparken NULL
    500 üretir ve panel çöker. Bu yardımcı, okuma yolunu sağlamlaştırır —
    veriye YAZMAZ, yalnızca sunumda varsayılan kullanır.
    """
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None or not creds.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Oturum açmanız gerekiyor.")
    try:
        payload = decode_access_token(creds.credentials)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Oturum geçersiz veya süresi dolmuş.") from exc

    user = db.get(User, int(payload.get("sub", 0)))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Kullanıcı bulunamadı.")
    return user


def user_bot(bot_id: int, db: Session = Depends(get_db),
             user: User = Depends(current_user)) -> Bot:
    """Yalnızca kendi botuna erişim."""
    bot = db.get(Bot, bot_id)
    if bot is None or bot.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bot bulunamadı.")
    return bot


async def ws_user(websocket: WebSocket, token: str = Query(default=""),
                  db: Session | None = None) -> User | None:
    """WebSocket için token doğrulama (query parametresi ile)."""
    from ..core.db import SessionLocal  # noqa: PLC0415

    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except Exception:  # noqa: BLE001
        return None

    session = db or SessionLocal()
    try:
        user = session.get(User, int(payload.get("sub", 0)))
        return user if user and user.is_active else None
    finally:
        if db is None:
            session.close()
