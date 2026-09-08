"""
Şifreli Kimlik Bilgisi Yardımcıları
===================================
Sırlar yalnızca burada çözülür ve bellekte tutulur; loga veya API yanıtına
asla düz metin sızmaz.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..models import Credential, CredentialKind, User
from .crypto import decrypt, encrypt, mask


def save_credential(db: Session, user: User, kind: CredentialKind, provider: str,
                    label: str, secrets: dict[str, str],
                    extra: dict[str, Any] | None = None) -> Credential:
    """Sırları şifreleyip kaydeder; aynı etiket varsa günceller."""
    payload = encrypt(json.dumps(secrets, ensure_ascii=False), user.vault_salt)
    primary = secrets.get("api_key") or secrets.get("token") or ""
    hint = mask(primary) if primary else ""

    cred = (
        db.query(Credential)
        .filter(Credential.user_id == user.id, Credential.kind == kind,
                Credential.label == label)
        .first()
    )
    if cred is None:
        cred = Credential(user_id=user.id, kind=kind, label=label)
        db.add(cred)

    cred.provider = provider
    cred.payload_enc = payload
    cred.hint = hint
    cred.extra_json = json.dumps(extra or {}, ensure_ascii=False)
    db.flush()
    return cred


def read_secrets(user: User, cred: Credential | None) -> dict[str, str]:
    """Şifreli sırları çözer. Kayıt yoksa boş sözlük döner."""
    if cred is None:
        return {}
    try:
        return json.loads(decrypt(cred.payload_enc, user.vault_salt))
    except Exception:  # noqa: BLE001 — bozuk/eski kayıt ticareti durdurmamalı
        return {}


def read_extra(cred: Credential | None) -> dict[str, Any]:
    if cred is None:
        return {}
    try:
        return json.loads(cred.extra_json or "{}")
    except json.JSONDecodeError:
        return {}


def public_view(cred: Credential) -> dict[str, Any]:
    """API yanıtı için güvenli gösterim (sır içermez)."""
    return {
        "id": cred.id,
        "kind": cred.kind.value,
        "provider": cred.provider,
        "label": cred.label,
        "hint": cred.hint,
        "extra": read_extra(cred),
        "created_at": cred.created_at.isoformat() if cred.created_at else None,
    }
