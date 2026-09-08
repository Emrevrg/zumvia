"""
OTOMASYONLAR — ajanın kendi zamanlamasını yazması
==================================================

Beceri "nasıl yapılacağını" bilir; otomasyon "ne zaman yapılacağını".
Kullanıcı "her sabah portföyümü kontrol et, bir şey değişirse haber ver"
dediğinde bunu her gün elle tetiklemek zorunda kalmamalı.

Değişmez ilke: **otomasyon YENİ YETKİ VERMEZ.**

Bir otomasyonun çalıştırdığı beceri hangi kısıtlara tabiyse otomasyon da
aynısına tabidir; ajana verilen görev de normal bir tur gibi risk
kalkanından geçer. Otomasyon yalnızca ZAMANLAMAYI otomatikleştirir, izni
değil. Aksi hâlde "gerçek paraya geçişi bir otomasyona gömerim" gibi bir
kaçış yolu doğardı.

İki tür iş çalıştırılabilir:

    BECERİ  yazılmış bir beceri — adım adım, deterministik, ucuz
    GÖREV   ajana verilen serbest metin — yorum gerektiren işler, pahalı

Sıklık sınırlıdır: dakikada bir çalışan bir otomasyon hem sağlayıcı kotasını
hem kullanıcının parasını yakar ve piyasada hiçbir şeyi değiştirmez.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..models import CustomAutomation, User

log = get_logger("zumvia.automations")

#  En sık çalışma aralığı. 15 dakikanın altı, 4 saatlik bir mumda hiçbir şeyi
#  değiştirmez ama kotayı ve parayı yakar.
MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 60 * 24 * 7
MAX_PER_USER = 12                  # sınırsız otomasyon, sessiz bir kota tuzağıdır
MIN_PURPOSE = 20                   # "ne işe yaradığı" yazılmadan kaydedilmez

_SLUG_OK = re.compile(r"^[a-z0-9_]{3,64}$")


class AutomationError(ValueError):
    """Otomasyon tanımı geçersiz. Mesaj doğrudan modele gösterilir."""


def slugify(label: str) -> str:
    from ..core.text import fold  # noqa: PLC0415

    text = "".join(c if c.isalnum() else "_" for c in fold(label).strip())
    text = re.sub(r"_+", "_", text).strip("_")[:64]
    return "".join(c for c in text if c.isascii()) or "otomasyon"


def next_run(row: CustomAutomation, *, now: datetime | None = None) -> datetime:
    """Bir sonraki çalışma anı."""
    moment = now or datetime.now(UTC)
    if row.schedule == "daily":
        target = moment.replace(hour=row.at_hour, minute=row.at_minute,
                                second=0, microsecond=0)
        if target <= moment:
            target += timedelta(days=1)
        return target
    return moment + timedelta(minutes=max(MIN_INTERVAL_MINUTES, row.every_minutes))


# --------------------------------------------------------------------------- #
#  Doğrulama ve kayıt
# --------------------------------------------------------------------------- #

def validate(db: Session, user: User, *, label: str, purpose: str, action: str,
             skill_slug: str, task_prompt: str, schedule: str,
             every_minutes: int, at_hour: int, at_minute: int) -> None:
    """
    Tanımı sınar. Hata mesajları modele NE yapması gerektiğini söyler.
    """
    from . import custom_skills  # noqa: PLC0415

    if len(label.strip()) < 3:
        raise AutomationError("Otomasyon adı çok kısa.")
    if len(purpose.strip()) < MIN_PURPOSE:
        raise AutomationError(
            f"'purpose' en az {MIN_PURPOSE} karakter olmalı. Bu otomasyonun "
            f"NE İŞE YARADIĞINI yaz — altı ay sonra kimse hatırlamayacak.")

    if action not in ("skill", "task"):
        raise AutomationError("'action' ya 'skill' ya 'task' olmalı.")

    if action == "skill":
        if not skill_slug:
            raise AutomationError("'skill' seçtin ama 'skill_slug' vermedin.")
        if custom_skills.find(db, user, skill_slug) is None:
            available = [row.slug for row in custom_skills.listing(db, user)]
            raise AutomationError(
                f"'{skill_slug}' adında bir beceriniz yok. Önce `create_skill` "
                f"ile yaz. Mevcut beceriler: {available or '(hiç yok)'}")
    elif len(task_prompt.strip()) < 15:
        raise AutomationError(
            "'task_prompt' çok kısa. Ajanın ne yapacağını somut yaz; belirsiz "
            "bir görev her çalıştığında farklı sonuç üretir.")

    if schedule not in ("interval", "daily"):
        raise AutomationError("'schedule' ya 'interval' ya 'daily' olmalı.")

    if schedule == "interval":
        if not (MIN_INTERVAL_MINUTES <= every_minutes <= MAX_INTERVAL_MINUTES):
            raise AutomationError(
                f"Çalışma aralığı {MIN_INTERVAL_MINUTES}-{MAX_INTERVAL_MINUTES} "
                f"dakika arasında olmalı. Daha sık çalışmak 4 saatlik bir mumda "
                f"hiçbir şeyi değiştirmez ama kotayı ve parayı yakar.")
    else:
        if not (0 <= at_hour <= 23 and 0 <= at_minute <= 59):
            raise AutomationError("Geçersiz saat.")


def save(db: Session, user: User, *, label: str, purpose: str,
         action: str = "skill", skill_slug: str = "", task_prompt: str = "",
         inputs: dict[str, Any] | None = None, schedule: str = "interval",
         every_minutes: int = 60, at_hour: int = 9, at_minute: int = 0,
         notify: bool = False, author_model: str = "") -> CustomAutomation:
    """Yeni otomasyon kaydeder."""
    validate(db, user, label=label, purpose=purpose, action=action,
             skill_slug=skill_slug, task_prompt=task_prompt, schedule=schedule,
             every_minutes=every_minutes, at_hour=at_hour, at_minute=at_minute)

    existing = listing(db, user)
    if len(existing) >= MAX_PER_USER:
        raise AutomationError(
            f"En fazla {MAX_PER_USER} otomasyon olabilir. Sınırsız otomasyon "
            f"sessiz bir kota tuzağıdır. Önce kullanmadığın birini sil.")

    slug = slugify(label)
    if not _SLUG_OK.match(slug):
        raise AutomationError(f"Geçersiz otomasyon kimliği: {slug!r}")
    if find(db, user, slug) is not None:
        raise AutomationError(
            f"'{slug}' adında bir otomasyonunuz zaten var. Değiştirmek için "
            f"`edit_automation` kullan.")

    row = CustomAutomation(
        user_id=user.id, slug=slug, label=label.strip()[:96],
        purpose=purpose.strip(), action=action, skill_slug=skill_slug,
        task_prompt=task_prompt.strip(),
        inputs_json=json.dumps(inputs or {}, ensure_ascii=False),
        schedule=schedule, every_minutes=every_minutes,
        at_hour=at_hour, at_minute=at_minute, notify=notify,
        author_model=author_model[:120],
    )
    row.next_run_at = next_run(row)
    db.add(row)
    db.commit()
    db.refresh(row)
    log.info("otomasyon yazıldı: %s (%s, %s)", slug, action, schedule)
    return row


def edit(db: Session, user: User, slug: str, changes: dict[str, Any],
         author_model: str = "") -> CustomAutomation:
    """Var olan otomasyonu günceller. Sicil korunur."""
    row = find(db, user, slug)
    if row is None:
        raise AutomationError(f"'{slug}' adında bir otomasyonunuz yok.")

    merged = {
        "label": changes.get("label", row.label),
        "purpose": changes.get("purpose", row.purpose),
        "action": changes.get("action", row.action),
        "skill_slug": changes.get("skill_slug", row.skill_slug),
        "task_prompt": changes.get("task_prompt", row.task_prompt),
        "schedule": changes.get("schedule", row.schedule),
        "every_minutes": int(changes.get("every_minutes", row.every_minutes)),
        "at_hour": int(changes.get("at_hour", row.at_hour)),
        "at_minute": int(changes.get("at_minute", row.at_minute)),
    }
    validate(db, user, **merged)

    for key, value in merged.items():
        setattr(row, key, value)
    if "inputs" in changes:
        row.inputs_json = json.dumps(changes["inputs"] or {}, ensure_ascii=False)
    if "enabled" in changes:
        row.enabled = bool(changes["enabled"])
    if "notify" in changes:
        row.notify = bool(changes["notify"])
    if author_model:
        row.author_model = author_model[:120]

    row.updated_at = datetime.now(UTC)
    row.next_run_at = next_run(row)
    db.commit()
    db.refresh(row)
    log.info("otomasyon güncellendi: %s", slug)
    return row


def find(db: Session, user: User, slug: str) -> CustomAutomation | None:
    return (db.query(CustomAutomation)
            .filter(CustomAutomation.user_id == user.id,
                    CustomAutomation.slug == slug).first())


def listing(db: Session, user: User) -> list[CustomAutomation]:
    return (db.query(CustomAutomation)
            .filter(CustomAutomation.user_id == user.id)
            .order_by(CustomAutomation.updated_at.desc()).all())


def remove(db: Session, user: User, slug: str) -> bool:
    row = find(db, user, slug)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    log.info("otomasyon silindi: %s", slug)
    return True


def describe(row: CustomAutomation) -> dict[str, Any]:
    when = (f"her {row.every_minutes} dakikada"
            if row.schedule == "interval"
            else f"her gün {row.at_hour:02d}:{row.at_minute:02d}")
    runs = row.runs or 0
    return {
        "slug": row.slug, "label": row.label, "purpose": row.purpose,
        "ne_calisir": (f"beceri: {row.skill_slug}" if row.action == "skill"
                       else f"görev: {row.task_prompt[:90]}"),
        "ne_zaman": when, "acik": row.enabled, "bildirim": row.notify,
        "calisma": runs, "hata": row.failures or 0,
        "basari_orani": round((runs - (row.failures or 0)) / runs, 3) if runs else None,
        "son_hata": row.last_error,
        "son_ozet": (row.last_summary or "")[:300],
        "son_calisma": row.last_run_at.isoformat() if row.last_run_at else None,
        "sonraki": row.next_run_at.isoformat() if row.next_run_at else None,
        "yazan": row.author_model,
    }


# --------------------------------------------------------------------------- #
#  Çalıştırma
# --------------------------------------------------------------------------- #

def due(db: Session, *, now: datetime | None = None) -> list[CustomAutomation]:
    """Zamanı gelmiş, açık otomasyonlar."""
    moment = now or datetime.now(UTC)
    rows = (db.query(CustomAutomation)
            .filter(CustomAutomation.enabled.is_(True)).all())
    out = []
    for row in rows:
        due_at = row.next_run_at
        if due_at is None:
            out.append(row)
            continue
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=UTC)
        if due_at <= moment:
            out.append(row)
    return out


def run_one(db: Session, user: User, row: CustomAutomation) -> dict[str, Any]:
    """
    Bir otomasyonu çalıştırır.

    Otomasyon yeni yetki vermez: beceri kendi kısıtlarıyla, görev de normal
    bir ajan turu gibi risk kalkanından geçerek çalışır.
    """
    row.runs = (row.runs or 0) + 1
    row.last_run_at = datetime.now(UTC)
    row.next_run_at = next_run(row)

    try:
        if row.action == "skill":
            result = _run_skill(db, user, row)
        else:
            result = _run_task(db, user, row)
        failed = not result.get("ok", True)
    except Exception as exc:  # noqa: BLE001 — bir otomasyon çökse de diğerleri sürer
        log.exception("otomasyon hatası: %s", row.slug)
        result = {"ok": False, "hata": f"{type(exc).__name__}: {exc}"}
        failed = True

    if failed:
        row.failures = (row.failures or 0) + 1
        row.last_error = str(result.get("hata") or result.get("error") or "")[:400]
    else:
        row.last_error = ""
        row.last_summary = str(result.get("ozet") or "")[:2000]

    db.commit()

    if row.notify and not failed:
        _notify(user, row, result)
    return result


def _run_skill(db: Session, user: User, row: CustomAutomation) -> dict[str, Any]:
    from ..agent.tools import ToolContext  # noqa: PLC0415
    from . import custom_skills  # noqa: PLC0415

    ctx = ToolContext(db=db, user=user, session_id=None)
    inputs = json.loads(row.inputs_json or "{}")
    result = custom_skills.run(ctx, db, user, row.skill_slug, inputs)
    return {"ok": result.get("ok", False),
            "ozet": f"{row.skill_slug}: {result.get('adim_sayisi', 0)} adım",
            **result}


def _run_task(db: Session, user: User, row: CustomAutomation) -> dict[str, Any]:
    """
    Serbest görevi ajana verir.

    Otomasyon için AYRI bir oturum kullanılır: kullanıcının sohbeti
    otomasyon çıktılarıyla dolmasın, ama kayıt yine tutulsun.
    """
    from ..agent.controller import run_agent  # noqa: PLC0415
    from ..models import AgentSession  # noqa: PLC0415

    title = f"Otomasyon: {row.label}"[:96]
    session = (db.query(AgentSession)
               .filter(AgentSession.user_id == user.id,
                       AgentSession.title == title).first())
    if session is None:
        session = AgentSession(user_id=user.id, title=title, control_tool="api",
                               council_mode="solo", status="idle")
        db.add(session)
        db.commit()
        db.refresh(session)

    outcome = run_agent(db, user, session, row.task_prompt)
    return {"ok": bool(outcome.get("ok")),
            "ozet": str(outcome.get("text") or outcome.get("error") or "")[:2000],
            "oturum": session.id, **outcome}


def _notify(user: User, row: CustomAutomation, result: dict[str, Any]) -> None:
    """Sonucu Telegram'a yazar (yapılandırılmışsa)."""
    try:
        from .notifier import send_telegram  # noqa: PLC0415

        summary = str(result.get("ozet") or "")[:600]
        send_telegram(user, f"*{row.label}*\n{summary or 'tamamlandı'}")
    except Exception:  # noqa: BLE001 — bildirim gitmese de otomasyon geçerlidir
        log.warning("otomasyon bildirimi gönderilemedi: %s", row.slug)
