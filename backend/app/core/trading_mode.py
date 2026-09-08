"""
BAŞLANGIÇ MODU — yeni bot sanal mı, gerçek parayla mı başlar?
==============================================================

Sanal mod bir "eğitim tekerleği" değil, bir **tercihtir**. Kullanıcı sistemi
gerçek parayla çalıştırmak istiyorsa bunu Kontrol merkezinden seçer ve yeni
botlar doğrudan canlı başlar.

Ancak canlı başlamak için iki şeyin fiilen var olması gerekir; bunlar keyfi
sınır değil, teknik zorunluluktur:

  1. **Borsa API anahtarı** — anahtarsız emir gönderilecek bir yer yoktur.
  2. **Canlı yetki** — parayla işlem yapmanın kullanıcı tarafından bir kez
     onaylanmış olması. Süre ve sermaye tavanı isteğe bağlıdır (0 = sınır yok).

Bu ikisinden biri eksikse bot **sanal başlar ve sebebi kullanıcıya söylenir** —
sessizce gerçek para harcamaya kalkmaz, sessizce de vazgeçmez.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import Credential, CredentialKind, TradingMode, User
from .logging import get_logger
from .safety import live_authorization_status

log = get_logger("zumvia.mode")


def desired_mode(user: User) -> str:
    """Kullanıcının seçtiği başlangıç modu."""
    return (getattr(user, "default_trading_mode", "") or "paper").lower()


def evaluate(db: Session, user: User,
             exchange_credential_id: int | None = None) -> dict[str, Any]:
    """
    İstenen modun uygulanabilir olup olmadığını değerlendirir.

    Her zaman bir sebep döndürür; kullanıcı botunun neden sanal başladığını
    tahmin etmek zorunda kalmaz.
    """
    wanted = desired_mode(user)
    if wanted != "live":
        return {"mode": "paper", "requested": wanted,
                "reason": "Başlangıç modu tercihiniz sanal."}

    exchange = None
    if exchange_credential_id:
        exchange = db.get(Credential, exchange_credential_id)
    if exchange is None:
        exchange = (db.query(Credential)
                    .filter(Credential.user_id == user.id,
                            Credential.kind == CredentialKind.EXCHANGE).first())

    if exchange is None:
        return {"mode": "paper", "requested": "live", "blocked_by": "NO_EXCHANGE_KEY",
                "reason": ("Gerçek para modu seçili ama kayıtlı borsa API anahtarı yok. "
                           "Bot sanal başlatıldı; anahtarı ekleyip canlıya alabilirsiniz.")}

    status = live_authorization_status(user)
    if not status["authorized"]:
        return {"mode": "paper", "requested": "live", "blocked_by": "NO_AUTHORIZATION",
                "reason": (f"Gerçek para modu seçili ama canlı yetki yok "
                           f"({status['reason']}) Bot sanal başlatıldı.")}

    return {"mode": "live", "requested": "live",
            "exchange_credential_id": exchange.id,
            "reason": "Gerçek para modu etkin.",
            "capital_ceiling": status.get("max_capital") or None}


def starting_mode(db: Session, user: User,
                  exchange_credential_id: int | None = None) -> TradingMode:
    """Yeni bot için kullanılacak mod."""
    result = evaluate(db, user, exchange_credential_id)
    if result["mode"] == "live":
        log.warning("Yeni bot GERÇEK PARA modunda başlatılıyor (kullanıcı %s).", user.email)
        return TradingMode.LIVE
    return TradingMode.PAPER
