"""
ZUMVIA — Kimlik Doğrulama (Parola + JWT)
-------------------------------------------------
Parolalar `scrypt` (stdlib, hafıza-zor KDF) ile saltlanarak saklanır.
Harici bir hash kütüphanesine bağımlılık yoktur.

Saklama formatı:  scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta

import jwt

from .config import settings

# scrypt parametreleri (RFC 7914 önerisi civarı — ~64 MB bellek)
_N, _R, _P, _DKLEN = 2 ** 15, 8, 1, 32
# OpenSSL varsayilan bellek tavani 32MB'dir; scrypt icin acikca yukseltilir.
_MAXMEM = 192 * 1024 * 1024


def hash_password(password: str) -> str:
    """Parolayı scrypt ile hash'ler."""
    if len(password) < 8:
        raise ValueError("Parola en az 8 karakter olmalıdır.")
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P,
                        dklen=_DKLEN, maxmem=_MAXMEM)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    """Sabit zamanlı parola doğrulaması."""
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p),
            dklen=len(base64.b64decode(hash_b64)),
            maxmem=_MAXMEM,
        )
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:  # noqa: BLE001
        return False


def create_access_token(user_id: int, email: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expire_minutes)).timestamp()),
        "iss": "zumvia",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Token doğrular; geçersizse jwt.PyJWTError fırlatır."""
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        issuer="zumvia",
    )
