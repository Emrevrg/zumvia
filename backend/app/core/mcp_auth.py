"""
MCP ERİŞİM ANAHTARLARI
=======================
Dış araçların (Claude Code, Claude Desktop, Cursor…) platforma bağlanmak için
kullandığı anahtarları üretir ve doğrular.

Neden tarayıcı oturum anahtarı (JWT) değil?
  * JWT kısa ömürlüdür ve iptal edilemez — sızarsa süresi dolana kadar geçerlidir.
  * Hangi aracın bağlı olduğu anlaşılmaz.
  * Kullanıcı her oturum yenilemesinde bağlantıyı yeniden kurmak zorunda kalır.

Bu modülün anahtarları:
  * `vq_` önekiyle üretilir, 32 bayt rastgeledir,
  * veritabanında yalnızca **SHA-256 özeti** saklanır (düz metin bir kez gösterilir),
  * adlandırılır ("Claude Desktop"), son kullanımı izlenir,
  * tek tıkla iptal edilir ve isteğe bağlı son kullanma tarihi taşır.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from ..models import McpToken, User

PREFIX = "vq_"
MAX_TOKENS_PER_USER = 10


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(db: Session, user: User, name: str,
                 days: int | None = None) -> tuple[McpToken, str]:
    """
    Yeni anahtar üretir. Düz metin YALNIZCA burada döner; tekrar gösterilemez.
    """
    active = (db.query(McpToken)
              .filter(McpToken.user_id == user.id, McpToken.revoked.is_(False))
              .count())
    if active >= MAX_TOKENS_PER_USER:
        raise ValueError(
            f"En fazla {MAX_TOKENS_PER_USER} etkin anahtar olabilir. "
            "Kullanmadıklarınızı iptal edin."
        )

    plain = PREFIX + secrets.token_urlsafe(32)
    record = McpToken(
        user_id=user.id,
        name=(name or "MCP istemcisi").strip()[:64],
        token_hash=_hash(plain),
        prefix=plain[:10],
        expires_at=(datetime.now(UTC) + timedelta(days=days)) if days else None,
    )
    db.add(record)
    db.flush()
    return record, plain


def resolve_token(db: Session, token: str) -> User | None:
    """Anahtarı doğrular ve sahibini döner. Geçersiz/iptal/süresi dolmuşsa None."""
    if not token or not token.startswith(PREFIX):
        return None

    record = (db.query(McpToken)
              .filter(McpToken.token_hash == _hash(token))
              .first())
    if record is None or record.revoked:
        return None

    if record.expires_at:
        expires = record.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            return None

    record.last_used_at = datetime.now(UTC)
    record.calls += 1
    db.flush()

    user = db.get(User, record.user_id)
    return user if user and user.is_active else None


def revoke_token(db: Session, user: User, token_id: int) -> bool:
    record = db.get(McpToken, token_id)
    if record is None or record.user_id != user.id:
        return False
    record.revoked = True
    db.flush()
    return True


def list_tokens(db: Session, user: User) -> list[dict]:
    rows = (db.query(McpToken)
            .filter(McpToken.user_id == user.id)
            .order_by(McpToken.id.desc()).all())
    return [{
        "id": r.id,
        "name": r.name,
        "prefix": r.prefix + "…",
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
        "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        "revoked": r.revoked,
        "calls": r.calls,
    } for r in rows]
