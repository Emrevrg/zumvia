"""
DİSK YÖNETİMİ — sistem kendi ayağını yorganına göre uzatır
===========================================================

Bir ticaret sistemi için disk dolması sıradan bir aksaklık değildir: o an
veritabanına yazılamaz, yani AÇIK POZİSYON KAYDI tutulamaz. Borsada gerçek
bir pozisyon durur ama sistemin haberi olmaz — stop takibi yapılamaz,
mutabakat çalışamaz, kullanıcı ne olduğunu göremez.

Bu yüzden disk, "kullanıcı temizlesin" denip geçilecek bir konu değil;
sistemin kendi sorumluluğudur.

Üç kural:

1. SINIRSIZ BÜYÜYEN HİÇBİR ŞEY OLMAZ.
   Bot olayları, sermaye eğrisi, ajan mesajları, araştırma raporları —
   hepsinin bir saklama sınırı vardır. Sınırsız bir tablo, gecikmeli bir
   arızadır.

2. YER AZALDIĞINDA ÖNCE LÜKS KESİLİR.
   Araştırma raporu yazılamazsa can sıkıcıdır; pozisyon kaydı yazılamazsa
   para kaybettirir. Sıkışınca ilkinden vazgeçilir, ikincisi korunur.

3. DURUM GİZLENMEZ.
   Disk azaldıysa kullanıcı bunu sağlık ekranında görür. Sessizce
   çalışmayı sürdürüp bir gün kayıt tutamamak, uyarmaktan çok daha kötüdür.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .logging import get_logger

log = get_logger("zumvia.storage")

BASE_DIR = Path(__file__).resolve().parents[2]

#  Eşikler. "Kritik" değeri, veritabanının rahatça yazabilmesi ve WAL'ın
#  büyüyebilmesi için gereken asgari alandır.
CRITICAL_MB = 250        # bunun altında lüks yazımlar durur
LOW_MB = 1024            # bunun altında uyarı ve temizlik
TARGET_FREE_MB = 500     # temizlik bu hedefe ulaşmaya çalışır

#  Saklama sınırları. Ölçüye dayanır: 5 dakikalık turlarla çalışan bir bot
#  günde ~900 olay üretir. 30 gün ~27.000 satır eder ve bu, tanı için
#  fazlasıyla yeterlidir; daha eskisi kimse tarafından okunmaz.
KEEP_BOT_EVENTS_DAYS = 30
KEEP_EQUITY_POINTS_PER_BOT = 5_000
KEEP_AGENT_MESSAGES_PER_SESSION = 400
KEEP_RESEARCH_REPORTS = 60
KEEP_EVIDENCE_REPORTS = 30

#  WAL dosyası bu boyutu aşarsa sıkıştırılır. SQLite kendi başına da
#  yapar ama eşiği yüksektir; dolu bir diskte beklemek risklidir.
WAL_CHECKPOINT_MB = 32


@dataclass(slots=True)
class DiskStatus:
    """Diskin o anki durumu ve ne yapılması gerektiği."""

    free_mb: float
    total_mb: float
    level: str               # ok | low | critical
    message: str

    @property
    def ok(self) -> bool:
        return self.level == "ok"

    @property
    def can_write_optional(self) -> bool:
        """Rapor gibi vazgeçilebilir şeyler yazılabilir mi?"""
        return self.level != "critical"

    def to_dict(self) -> dict[str, Any]:
        return {"free_mb": round(self.free_mb, 1),
                "total_mb": round(self.total_mb, 1),
                "used_pct": round((1 - self.free_mb / self.total_mb) * 100, 1)
                if self.total_mb else 0.0,
                "level": self.level, "message": self.message}


def status(path: Path | None = None) -> DiskStatus:
    """Diskin durumunu ölçer ve seviyesini belirler."""
    target = path or BASE_DIR
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        log.warning("disk durumu okunamadı: %s", exc)
        return DiskStatus(0.0, 0.0, "ok", "Disk durumu okunamadı.")

    free_mb = usage.free / 1_048_576
    total_mb = usage.total / 1_048_576

    if free_mb < CRITICAL_MB:
        return DiskStatus(
            free_mb, total_mb, "critical",
            f"Disk kritik seviyede ({free_mb:.0f} MB boş). Yeni rapor yazımı "
            f"durduruldu; pozisyon ve emir kayıtları önceliklidir. Yer açın.")
    if free_mb < LOW_MB:
        return DiskStatus(
            free_mb, total_mb, "low",
            f"Disk azalıyor ({free_mb:.0f} MB boş). Eski kayıtlar otomatik "
            f"temizleniyor.")
    return DiskStatus(free_mb, total_mb, "ok",
                      f"{free_mb / 1024:.1f} GB boş.")


def free_mb(path: Path | None = None) -> float:
    return status(path).free_mb


# --------------------------------------------------------------------------- #
#  Temizlik
# --------------------------------------------------------------------------- #

def housekeeping(db: Any, *, aggressive: bool = False) -> dict[str, Any]:
    """
    Saklama sınırlarını uygular ve yer kazanır.

    `aggressive` verilirse sınırlar yarıya iner: disk kritik seviyedeyken
    tanı verisini korumak, sistemin yazamaz hâle gelmesinden daha az
    önemlidir.

    Hiçbir AÇIK pozisyon, çalışan bot ya da ayar silinmez — yalnızca
    geçmiş kayıtlar budanır.
    """
    factor = 0.5 if aggressive else 1.0
    removed: dict[str, int] = {}

    removed["bot_events"] = _trim_bot_events(db, factor)
    removed["equity_points"] = _trim_equity_points(db, factor)
    removed["agent_messages"] = _trim_agent_messages(db, factor)
    removed["research_reports"] = _trim_reports(
        BASE_DIR / "reports" / "research", int(KEEP_RESEARCH_REPORTS * factor))
    removed["evidence_reports"] = _trim_reports(
        BASE_DIR / "reports" / "evidence", int(KEEP_EVIDENCE_REPORTS * factor))

    checkpointed = checkpoint_wal(db, force=aggressive)
    after = status()

    total = sum(removed.values())
    if total or checkpointed:
        log.info("temizlik: %s kayıt silindi%s — %s",
                 total, " (WAL sıkıştırıldı)" if checkpointed else "",
                 after.message)

    return {"removed": removed, "wal_checkpointed": checkpointed,
            "disk": after.to_dict()}


def _trim_bot_events(db: Any, factor: float) -> int:
    """
    Eski bot olaylarını siler.

    Olaylar tanı içindir: "bot neden işlem açmadı" sorusunun cevabı
    buradadır. Ama bir aydan eski bir cevabı kimse aramaz.
    """
    from ..models import BotEvent  # noqa: PLC0415

    cutoff = datetime.now(UTC) - timedelta(days=int(KEEP_BOT_EVENTS_DAYS * factor))
    count = (db.query(BotEvent).filter(BotEvent.ts < cutoff)
             .delete(synchronize_session=False))
    db.commit()
    return int(count or 0)


def _trim_equity_points(db: Any, factor: float) -> int:
    """
    Sermaye eğrisinin EN ESKİ noktalarını siler, en yenileri korur.

    Grafik için son birkaç bin nokta yeter; daha eskisi ekranda tek bir
    piksele düşer.
    """
    from ..models import Bot, EquityPoint  # noqa: PLC0415

    keep = max(200, int(KEEP_EQUITY_POINTS_PER_BOT * factor))
    total = 0
    for (bot_id,) in db.query(Bot.id).all():
        rows = (db.query(EquityPoint.id)
                .filter(EquityPoint.bot_id == bot_id)
                .order_by(EquityPoint.id.desc()).offset(keep).all())
        if not rows:
            continue
        ids = [r[0] for r in rows]
        total += (db.query(EquityPoint).filter(EquityPoint.id.in_(ids))
                  .delete(synchronize_session=False)) or 0
    db.commit()
    return int(total)


def _trim_agent_messages(db: Any, factor: float) -> int:
    """
    Uzun sohbetlerin en eski mesajlarını siler.

    Ajan zaten son N mesajı bağlam olarak kullanır; daha eskisi yalnızca
    yer kaplar. Oturumun kendisi ve başlığı korunur.
    """
    from ..models import AgentMessage, AgentSession  # noqa: PLC0415

    keep = max(100, int(KEEP_AGENT_MESSAGES_PER_SESSION * factor))
    total = 0
    for (session_id,) in db.query(AgentSession.id).all():
        rows = (db.query(AgentMessage.id)
                .filter(AgentMessage.session_id == session_id)
                .order_by(AgentMessage.id.desc()).offset(keep).all())
        if not rows:
            continue
        ids = [r[0] for r in rows]
        total += (db.query(AgentMessage).filter(AgentMessage.id.in_(ids))
                  .delete(synchronize_session=False)) or 0
    db.commit()
    return int(total)


def _trim_reports(folder: Path, keep: int) -> int:
    """
    Klasördeki en eski raporları siler.

    Raporlar kullanıcının çalışmasıdır; bu yüzden sınır cömert tutulur ve
    yalnızca EN ESKİLER silinir. Her rapor `.md` ve `.json` ikilisidir;
    ikisi birlikte gider.
    """
    if not folder.exists():
        return 0

    stems: dict[str, float] = {}
    for path in folder.iterdir():
        if path.suffix in (".md", ".json"):
            stems[path.stem] = max(stems.get(path.stem, 0.0),
                                   path.stat().st_mtime)

    if len(stems) <= keep:
        return 0

    doomed = sorted(stems, key=lambda s: stems[s])[:len(stems) - keep]
    removed = 0
    for stem in doomed:
        for suffix in (".md", ".json"):
            candidate = folder / f"{stem}{suffix}"
            if candidate.exists():
                try:
                    candidate.unlink()
                    removed += 1
                except OSError as exc:
                    log.warning("rapor silinemedi (%s): %s", candidate.name, exc)
    return removed


def checkpoint_wal(db: Any, *, force: bool = False) -> bool:
    """
    SQLite WAL dosyasını ana veritabanına aktarır.

    WAL, sıkıştırılana kadar büyür. Boş diskte zararsızdır; dolu diskte
    sistemi yazamaz hâle getirir. SQLite kendi eşiğini bekler, biz
    beklemeyiz.
    """
    from .db import engine  # noqa: PLC0415

    wal = Path(str(engine.url.database) + "-wal") if engine.url.database else None
    if wal is None:
        return False

    size_mb = wal.stat().st_size / 1_048_576 if wal.exists() else 0.0
    if not force and size_mb < WAL_CHECKPOINT_MB:
        return False

    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        log.info("WAL sıkıştırıldı (%.1f MB)", size_mb)
        return True
    except Exception as exc:  # noqa: BLE001 — sıkıştırma başarısızsa sistem sürer
        log.warning("WAL sıkıştırılamadı: %s", exc)
        return False


def vacuum(db: Any) -> bool:
    """
    Veritabanını sıkıştırır (silinen satırların yerini geri kazanır).

    Pahalı bir işlemdir ve dosyayı kilitler; yalnızca disk kritikken ve
    temizlikten SONRA çağrılır.
    """
    from .db import engine  # noqa: PLC0415

    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("VACUUM")
        log.info("veritabanı sıkıştırıldı (VACUUM)")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("VACUUM başarısız: %s", exc)
        return False


def ensure_space_for_optional_write() -> DiskStatus:
    """
    Vazgeçilebilir bir yazımdan önce çağrılır (rapor, dışa aktarma).

    Kritik seviyede `can_write_optional` False döner; çağıran yazmaktan
    vazgeçer ve KULLANICIYA SÖYLER — sessizce atlamaz.
    """
    return status()
