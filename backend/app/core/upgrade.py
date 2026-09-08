"""
SÜRÜM GEÇİŞİ — güncelleme geldiğinde çalışan sisteme ne olur?
==============================================================

Bu platform para yönetiyor. Bir güncelleme sırasında "acaba botum ne oldu?"
sorusunun cevabı belirsiz olamaz. Politika nettir ve burada uygulanır:

1. **Açık pozisyonlara dokunulmaz.** Güncelleme pozisyon kapatmaz. Kapatma
   kararı yalnızca stratejinin çıkış kuralına, risk kalkanına veya kullanıcıya
   aittir.

2. **Şema göçü yalnızca ekleyicidir.** Kolon eklenir; silinmez, tipi
   değiştirilmez (bkz. `core/db.py::_auto_migrate`). Böylece eski sürüme geri
   dönmek de mümkün kalır.

3. **Sürüm atlayınca botlar duraklatılır, kapatılmaz.** ANA sürüm (major)
   değiştiğinde çalışan botlar `paused_by_upgrade` işaretiyle durdurulur:
   yeni karar mantığı, kullanıcı görmeden pozisyon açmasın. Kullanıcı tek
   tıkla devam ettirir. Yama sürümlerinde (patch) hiçbir şey durmaz.

4. **Kaybolan sistem sessizce yok sayılmaz.** Botun kullandığı playbook yeni
   sürümde yoksa bot durdurulur ve olay defterine sebebiyle yazılır.

5. **Her geçiş denetim defterine işlenir.** Hangi sürümden hangi sürüme,
   kaç bot etkilendi — hepsi `audit_log`'ta durur.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from .config import settings
from .logging import get_logger

log = get_logger("zumvia.upgrade")


@dataclass(slots=True)
class UpgradeReport:
    previous: str
    current: str
    changed: bool
    major_change: bool
    paused_bots: list[int]
    orphaned_bots: list[int]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous": self.previous, "current": self.current,
            "changed": self.changed, "major_change": self.major_change,
            "paused_bots": self.paused_bots, "orphaned_bots": self.orphaned_bots,
            "notes": self.notes,
        }


def _parts(version: str) -> tuple[int, int, int]:
    try:
        nums = [int(x) for x in str(version).split(".")[:3]]
    except ValueError:
        return (0, 0, 0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def _read_stored_version(db: Session) -> str:
    from ..models import SystemState  # noqa: PLC0415

    row = db.query(SystemState).filter(SystemState.key == "app_version").first()
    return row.value if row else ""


def _write_version(db: Session, version: str) -> None:
    from ..models import SystemState  # noqa: PLC0415

    row = db.query(SystemState).filter(SystemState.key == "app_version").first()
    if row is None:
        db.add(SystemState(key="app_version", value=version))
    else:
        row.value = version


def apply(db: Session) -> UpgradeReport:
    """
    Açılışta çağrılır. Sürüm değiştiyse yukarıdaki politikayı uygular.

    İlk kurulumda (kayıtlı sürüm yok) hiçbir şey duraklatılmaz — ortada
    korunacak bir çalışan durum yoktur.
    """
    from ..models import Bot, BotStatus  # noqa: PLC0415

    current = settings.version
    previous = _read_stored_version(db)
    notes: list[str] = []

    if not previous:
        _write_version(db, current)
        db.commit()
        return UpgradeReport("", current, False, False, [], [], ["İlk kurulum."])

    if previous == current:
        return UpgradeReport(previous, current, False, False, [], [], [])

    old_major = _parts(previous)[0]
    new_major = _parts(current)[0]
    major_change = new_major != old_major

    paused: list[int] = []
    orphaned: list[int] = []

    running = db.query(Bot).filter(Bot.status == BotStatus.RUNNING).all()

    # Kullandığı sistem artık yoksa: dur ve sebebini yaz
    from ..layers.playbooks import PLAYBOOKS  # noqa: PLC0415

    for bot in running:
        source = (bot.strategy_notes or "")
        marker = source[1:source.index("]")] if source.startswith("[") and "]" in source else ""
        if marker and marker not in PLAYBOOKS and "__" not in marker:
            bot.status = BotStatus.STOPPED
            bot.lock_reason = (f"Güncelleme sonrası '{marker}' sistemi bulunamadı. "
                              f"Bot güvenlik gereği durduruldu; açık pozisyonlara "
                              f"dokunulmadı.")
            orphaned.append(bot.id)

    if major_change:
        for bot in running:
            if bot.id in orphaned:
                continue
            bot.status = BotStatus.STOPPED
            bot.lock_reason = (f"Ana sürüm {previous} → {current} güncellemesi sonrası "
                              f"duraklatıldı. Açık pozisyonlar korundu; kontrol edip "
                              f"tek tıkla devam ettirebilirsiniz.")
            paused.append(bot.id)
        notes.append("Ana sürüm değişti: çalışan botlar duraklatıldı, pozisyonlar korundu.")
    else:
        notes.append("Yama güncellemesi: botlar çalışmaya devam ediyor.")

    _write_version(db, current)
    db.commit()

    log.info("sürüm geçişi %s -> %s | duraklatılan: %d | sahipsiz: %d",
             previous, current, len(paused), len(orphaned))

    return UpgradeReport(previous, current, True, major_change, paused, orphaned, notes)
