"""
GÜVENLİK KAPISI — Kill Switch, Canlı Yetki ve Denetim İzi
==========================================================
Bu modül geri dönüşü olmayan işlemlerin önündeki son kapıdır.

Üç sorumluluğu vardır:

1. **KILL SWITCH** — Tek bir anahtarla tüm yeni emirler reddedilir.
   Ortam değişkeni (`VQ_KILL_SWITCH=true`), dosya kilidi (`KILL_SWITCH` dosyası)
   veya API çağrısıyla açılır. Açıkken raporlama ve izleme sürer, yalnızca
   **yeni risk almak** durur.

2. **CANLI TİCARET YETKİSİ** — Gerçek parayla işlem, kullanıcının açık izni
   olmadan mümkün değildir. İzni veren KULLANICIDIR ve sınırlarını da kendisi
   belirler: sermaye tavanı ve süre isteğe bağlıdır (0 = sınır yok).
   Ajan bu yetkiyi kendi kendine veremez, uzatamaz, limitini yükseltemez.
   Yetki her an tek tıkla iptal edilir.

3. **DENETİM İZİ** — Yetki verme/iptal, mod değişimi ve kill switch olayları
   kalıcı olarak `audit_logs` tablosuna yazılır.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..models import AuditLog, User
from .config import BASE_DIR, settings
from .logging import get_logger

log = get_logger("zumvia.safety")

KILL_SWITCH_FILE = BASE_DIR / "KILL_SWITCH"

# Süre sınırı ARTIK ZORUNLU DEĞİL: 0 verilirse yetki süresiz olur.
# Kullanıcı isterse süreli de verebilir (tatilde bırakırken faydalıdır).
MAX_LIVE_AUTH_HOURS = 0          # 0 = üst sınır yok
DEFAULT_LIVE_AUTH_HOURS = 24 * 7


# --------------------------------------------------------------------------- #
#  1. Kill switch
# --------------------------------------------------------------------------- #


def kill_switch_active() -> bool:
    """Kill switch açık mı? Ortam değişkeni VEYA dosya kilidi yeterlidir."""
    if os.environ.get("VQ_KILL_SWITCH", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return KILL_SWITCH_FILE.exists()


def kill_switch_reason() -> str:
    if os.environ.get("VQ_KILL_SWITCH", "").strip().lower() in ("1", "true", "yes", "on"):
        return "Kill switch ortam değişkeni ile açık (VQ_KILL_SWITCH)."
    if KILL_SWITCH_FILE.exists():
        try:
            note = KILL_SWITCH_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            note = ""
        return f"Kill switch dosya kilidi ile açık{f': {note}' if note else '.'}"
    return ""


def set_kill_switch(db: Session, user: User, active: bool, note: str = "") -> dict[str, Any]:
    """Kill switch'i dosya kilidiyle açar/kapatır ve denetim kaydı bırakır."""
    if active:
        KILL_SWITCH_FILE.write_text(
            note or f"Etkinleştiren: {user.email} · {datetime.now(UTC).isoformat()}",
            encoding="utf-8",
        )
        audit(db, user, "KILL_SWITCH_ON", "user", note or "Tüm yeni emirler durduruldu.")
        log.warning("KILL SWITCH AÇIK — yeni emir kabul edilmiyor.")
    else:
        KILL_SWITCH_FILE.unlink(missing_ok=True)
        audit(db, user, "KILL_SWITCH_OFF", "user", note or "Normal işleyişe dönüldü.")
        log.warning("Kill switch kapatıldı.")

    return {"kill_switch": kill_switch_active(), "reason": kill_switch_reason()}


# --------------------------------------------------------------------------- #
#  2. Canlı ticaret yetkisi
# --------------------------------------------------------------------------- #


def live_authorization_status(user: User, now: datetime | None = None) -> dict[str, Any]:
    """Kullanıcının canlı ticaret yetkisinin güncel durumu."""
    now = now or datetime.now(UTC)

    if settings.force_paper_only:
        return {"authorized": False, "reason": "Sistem genelinde canlı ticaret kapalı "
                                               "(VQ_FORCE_PAPER_ONLY=true).",
                "system_locked": True, "max_capital": 0.0, "expires_at": None}

    expires = user.live_authorized_until
    # Süresiz yetki: kullanıcı süre koymadıysa `live_authorized_until` boş kalır
    # ama `live_authorized_at` dolu olur. Bu, "yetki var, süresi yok" demektir.
    if expires is None:
        if getattr(user, "live_unlimited_time", False) and user.live_authorized_at:
            return {
                "authorized": True,
                "reason": "Canlı ticaret yetkisi aktif (süresiz).",
                "system_locked": False,
                "max_capital": float(user.live_max_capital or 0.0),
                "expires_at": None,
                "unlimited_time": True,
                "granted_at": user.live_authorized_at.isoformat(),
                "note": user.live_authorization_note,
            }
        return {"authorized": False, "reason": "Canlı ticaret yetkisi verilmemiş.",
                "system_locked": False, "max_capital": 0.0, "expires_at": None}

    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)

    if expires <= now:
        return {"authorized": False, "reason": "Canlı ticaret yetkisinin süresi doldu.",
                "system_locked": False, "max_capital": 0.0,
                "expires_at": expires.isoformat(), "expired": True}

    remaining = expires - now
    return {
        "authorized": True,
        "reason": "Canlı ticaret yetkisi aktif.",
        "system_locked": False,
        "max_capital": float(user.live_max_capital or 0.0),
        "expires_at": expires.isoformat(),
        "remaining_hours": round(remaining.total_seconds() / 3600, 1),
        "granted_at": user.live_authorized_at.isoformat() if user.live_authorized_at else None,
        "note": user.live_authorization_note,
    }


def grant_live_authorization(db: Session, user: User, *, max_capital: float,
                             hours: int = DEFAULT_LIVE_AUTH_HOURS,
                             note: str = "") -> dict[str, Any]:
    """
    Canlı ticaret yetkisi verir. **Yalnızca kullanıcı arayüzünden çağrılır.**
    Ajan araçlarının bu fonksiyona erişimi yoktur.
    """
    if settings.force_paper_only:
        raise PermissionError("Sistem genelinde canlı ticaret kapalı (VQ_FORCE_PAPER_ONLY).")
    # max_capital = 0  -> sermaye tavanı YOK (kullanıcı sınır koymak istemedi)
    # hours       = 0  -> süre sınırı YOK
    max_capital = max(0.0, float(max_capital))
    hours = max(0, int(hours))
    now = datetime.now(UTC)

    user.live_authorized_at = now
    user.live_authorized_until = (now + timedelta(hours=hours)) if hours > 0 else None
    user.live_unlimited_time = hours <= 0
    user.live_max_capital = max_capital
    user.live_authorization_note = note[:255]
    db.flush()

    limit_text = f"üst limit {max_capital}" if max_capital > 0 else "sermaye tavanı yok"
    time_text = f"{hours} saat" if hours > 0 else "süresiz"
    audit(db, user, "LIVE_GRANT", "user",
          f"Canlı yetki verildi: {limit_text}, {time_text}.",
          {"max_capital": max_capital, "hours": hours,
           "expires_at": (user.live_authorized_until.isoformat()
                          if user.live_authorized_until else None)})
    log.warning("CANLI YETKİ verildi (kullanıcı %s, %s, %s).",
                user.email, limit_text, time_text)
    return live_authorization_status(user, now)


def revoke_live_authorization(db: Session, user: User, note: str = "") -> dict[str, Any]:
    """Yetkiyi anında iptal eder. Her zaman çağrılabilir, hiçbir koşula bağlı değildir."""
    user.live_authorized_at = None
    user.live_authorized_until = None
    user.live_unlimited_time = False
    user.live_max_capital = 0.0
    user.live_authorization_note = note[:255]
    db.flush()
    audit(db, user, "LIVE_REVOKE", "user", note or "Canlı yetki iptal edildi.")
    log.warning("Canlı yetki İPTAL edildi (kullanıcı %s).", user.email)
    return live_authorization_status(user)


def can_enable_live(db: Session, user: User, bot, requested_capital: float) -> dict[str, Any]:
    """
    Bir botun gerçek para moduna geçebilmesi için TÜM kapıları kontrol eder.
    Ajan yalnızca bu fonksiyondan geçerek canlıya alabilir.
    """
    status = live_authorization_status(user)
    if not status["authorized"]:
        return {"allowed": False, "code": "NO_AUTHORIZATION",
                "reason": status["reason"] + " Panelden 'Gerçek Para Yetkisi' vermeniz gerekir.",
                "authorization": status}

    if kill_switch_active():
        return {"allowed": False, "code": "KILL_SWITCH",
                "reason": kill_switch_reason(), "authorization": status}

    # Sermaye tavanı YALNIZCA kullanıcı koyduysa geçerlidir (0 = tavan yok).
    ceiling = status.get("max_capital") or 0.0
    if ceiling > 0 and requested_capital > ceiling + 1e-9:
        return {"allowed": False, "code": "OVER_CAPITAL_LIMIT",
                "reason": (f"İstenen sermaye {requested_capital:,.2f}, sizin koyduğunuz "
                           f"üst limit {ceiling:,.2f}. Bu limiti yalnızca siz "
                           f"yükseltebilirsiniz."),
                "authorization": status}

    if not bot.exchange_credential_id:
        return {"allowed": False, "code": "NO_EXCHANGE_KEY",
                "reason": "Bu bot için borsa API anahtarı seçilmemiş.",
                "authorization": status}

    return {"allowed": True, "code": "OK",
            "reason": "Canlı geçiş için tüm kapılar açık.", "authorization": status}


# --------------------------------------------------------------------------- #
#  3. Denetim izi
# --------------------------------------------------------------------------- #


def audit(db: Session, user: User, action: str, actor: str = "user",
          detail: str = "", data: dict[str, Any] | None = None) -> AuditLog:
    """Kalıcı denetim kaydı bırakır."""
    entry = AuditLog(
        user_id=user.id, action=action, actor=actor, detail=detail[:2000],
        data_json=json.dumps(data or {}, ensure_ascii=False, default=str)[:8000],
    )
    db.add(entry)
    db.flush()
    return entry


def safety_snapshot(db: Session, user: User) -> dict[str, Any]:
    """Arayüz ve ajan için tek bakışta güvenlik durumu."""
    return {
        "kill_switch": kill_switch_active(),
        "kill_switch_reason": kill_switch_reason(),
        "live_authorization": live_authorization_status(user),
        "system_paper_only": settings.force_paper_only,
        "hard_limits": {
            "max_risk_pct": settings.hard_max_risk_pct,
            "max_daily_loss_pct": settings.hard_daily_loss_limit_pct,
            "min_confidence": settings.hard_min_confidence,
            "min_rr": settings.hard_min_rr_ratio,
        },
    }
