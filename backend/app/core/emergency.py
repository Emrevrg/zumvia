"""
ACİL FREN — durmadan ÖNCE her şeyi kapatan
===========================================

Eski davranış şuydu: acil fren yalnızca YENİ emirleri reddediyordu. Açık
pozisyonlar açık kalıyor, botlar duruyor ve o pozisyonları kimse yönetmiyordu.

Bu, sistemi korur ve kullanıcıyı ortada bırakır.

Çünkü acil frenden sonra sistemin ne zaman kalkacağı bilinmez. Bilinmeyen
bir süre boyunca stopu izlenmeyen bir pozisyon, tanımı gereği SINIRSIZ
risktir: kullanıcı kaldıraçlıysa bakiyesinin altına düşebilir. "Yeni işlem
açmıyoruz" demek, açık olanı korumaz.

Doğru sıra şudur ve bu modül onu uygular:

    1. NİYETİ YAZ      Fren çekildi, sebebi bu. Kayıt önce düşer; sonraki
                       adımlar patlasa bile tarihte iz kalır.
    2. PARÇALARI DURDUR Yarım kalmış büyük emirlerin kalan dilimleri iptal.
                       Yoksa kapatırken aynı anda alım yapmaya devam ederiz.
    3. POZİSYONLARI KAPAT Hepsi, piyasa fiyatından. Kâr da zarar da realize
                       edilir — belirsizlik taşımaktan iyidir.
    4. BOTLARI DURDUR  Zamanlayıcıdan çıkar.
    5. ANAHTARI ÇEVİR  En sona bırakılır: kapatma emirleri kendi frenimize
                       takılmasın.
    6. SONUCU YAZ      Kaç pozisyon kapandı, kaçı KAPANAMADI. Kapanamayan
                       varsa bu SAKLANMAZ — kullanıcı elle müdahale etmeli.

Adım 6'daki dürüstlük kritik: "her şey güvende" diyip bir pozisyonu açıkta
bırakmak, hiç fren çekmemekten daha tehlikelidir, çünkü kullanıcı artık
bakmaz.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..models import Bot, BotStatus, Position, PositionStatus, User
from .logging import get_logger
from .safety import audit, set_kill_switch

log = get_logger("zumvia.emergency")


@dataclass(slots=True)
class FlattenResult:
    """Frenin ne yapabildiğinin dürüst dökümü."""

    closed: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    cancelled_orders: int = 0
    bots_stopped: int = 0
    realized_pnl: float = 0.0

    @property
    def clean(self) -> bool:
        """Gerçekten temiz mi? Tek bir kapanamayan pozisyon bile yeter."""
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "kapatilan": len(self.closed),
            "kapatilamayan": len(self.failed),
            "iptal_edilen_parcali_emir": self.cancelled_orders,
            "durdurulan_bot": self.bots_stopped,
            "gerceklesen_kar_zarar": round(self.realized_pnl, 2),
            "temiz": self.clean,
            "detay_kapatilan": self.closed,
            "detay_kapatilamayan": self.failed,
        }

    def headline(self) -> str:
        if not self.closed and not self.failed:
            return ("Açık pozisyon yoktu; kapatılacak bir şey bulunmadı. "
                    "Yeni işlem açılmıyor.")
        if self.clean:
            return (f"{len(self.closed)} pozisyonun tamamı kapatıldı "
                    f"(gerçekleşen sonuç {self.realized_pnl:+.2f}). "
                    f"Piyasada açık riskiniz kalmadı.")
        return (f"{len(self.closed)} pozisyon kapatıldı ama "
                f"{len(self.failed)} POZİSYON KAPATILAMADI. Bunlar hâlâ "
                f"piyasada ve stopları izlenmiyor — borsanızdan elle "
                f"kapatmanız gerekiyor.")


# --------------------------------------------------------------------------- #
#  Parçalı emirler
# --------------------------------------------------------------------------- #

def _cancel_working_orders(db: Session, user: User) -> int:
    """
    Yarım kalmış büyük emirlerin kalan dilimlerini iptal eder.

    Bunu kapatmadan ÖNCE yapmak şart: aksi hâlde bir yandan pozisyon
    kapatırken zamanlayıcı öbür yandan yeni dilim gönderir ve kullanıcı
    kapattığını sandığı pozisyona geri girer.
    """
    from ..models import WorkingOrder  # noqa: PLC0415

    rows = (db.query(WorkingOrder)
            .join(Bot, WorkingOrder.bot_id == Bot.id)
            .filter(Bot.user_id == user.id,
                    WorkingOrder.status == "working").all())
    now = datetime.now(UTC)
    for row in rows:
        row.status = "cancelled"
        row.finished_at = now
        row.note = ("ACİL FREN: kalan dilimler iptal edildi. Dolan kısım "
                    "korunur ve pozisyon o kadarla kapatılır.")[:1000]
    if rows:
        db.flush()
        log.warning("acil fren: %d parçalı emir iptal edildi", len(rows))
    return len(rows)


# --------------------------------------------------------------------------- #
#  Pozisyon kapatma
# --------------------------------------------------------------------------- #

def _close_bot_positions(db: Session, bot: Bot, user: User,
                         result: FlattenResult) -> None:
    """Tek botun tüm açık pozisyonlarını piyasa fiyatından kapatır."""
    from ..engine.orchestrator import _make_broker, close_position  # noqa: PLC0415
    from ..layers.l1_market_data import fetch_quote  # noqa: PLC0415

    positions = (db.query(Position)
                 .filter(Position.bot_id == bot.id,
                         Position.status == PositionStatus.OPEN).all())
    if not positions:
        return

    # Fiyat bir kez alınır: her pozisyon için ayrı sorgu, frenin kendisini
    # yavaşlatır ve fren yavaş olamaz.
    price = 0.0
    price_error = ""
    try:
        price = float(fetch_quote(bot.market, bot.exchange, bot.symbol).price)
    except Exception as exc:  # noqa: BLE001
        price_error = str(exc)[:200]

    if price <= 0:
        # Fiyat alınamadıysa KAPATMIŞ GİBİ YAPMAYIZ. Uydurma bir fiyatla
        # kapatmak, defterdeki sonucu gerçekle uyumsuz hale getirir ve
        # kullanıcı pozisyonunun kapandığını sanır.
        for pos in positions:
            result.failed.append({
                "bot_id": bot.id, "bot": bot.name, "position_id": pos.id,
                "symbol": pos.symbol, "side": pos.side.value, "qty": pos.qty,
                "sebep": (f"Kapanış fiyatı ölçülemedi: "
                          f"{price_error or 'borsa yanıt vermedi'}"),
            })
        log.error("acil fren: %s için fiyat alınamadı, %d pozisyon açık kaldı",
                  bot.symbol, len(positions))
        return

    try:
        broker = _make_broker(db, bot, user, bot.paper_balance)
    except Exception as exc:  # noqa: BLE001
        broker = None
        log.warning("acil fren: aracı kurulamadı (%s): %s", bot.name, exc)

    for pos in positions:
        try:
            close_position(db, bot, user, pos, price, "EMERGENCY_BRAKE", broker)
            result.closed.append({
                "bot_id": bot.id, "bot": bot.name, "position_id": pos.id,
                "symbol": pos.symbol, "side": pos.side.value,
                "cikis": price, "kar_zarar": round(float(pos.pnl or 0.0), 2),
                "mod": bot.mode.value,
            })
            result.realized_pnl += float(pos.pnl or 0.0)
        except Exception as exc:  # noqa: BLE001 — biri düşse de diğerleri kapansın
            result.failed.append({
                "bot_id": bot.id, "bot": bot.name, "position_id": pos.id,
                "symbol": pos.symbol, "side": pos.side.value, "qty": pos.qty,
                "sebep": f"{type(exc).__name__}: {str(exc)[:180]}",
            })
            log.exception("acil fren: pozisyon %s kapatılamadı", pos.id)


def flatten_all(db: Session, user: User, *, stop_bots: bool = True) -> FlattenResult:
    """
    Kullanıcının TÜM açık riskini kapatır.

    Fren çekmeden de çağrılabilir (örneğin "her şeyi kapat" isteği). Kill
    switch'e dokunmaz — o ayrı bir karardır.
    """
    result = FlattenResult()
    result.cancelled_orders = _cancel_working_orders(db, user)

    bots = db.query(Bot).filter(Bot.user_id == user.id).all()
    for bot in bots:
        _close_bot_positions(db, bot, user, result)

    if stop_bots:
        from ..engine import scheduler as sched  # noqa: PLC0415
        for bot in bots:
            if bot.status != BotStatus.RUNNING:
                continue
            bot.status = BotStatus.STOPPED
            result.bots_stopped += 1
            try:
                sched.stop_bot_job(bot.id)
            except Exception as exc:  # noqa: BLE001 — iş zaten yoksa sorun değil
                log.info("bot işi durdurulamadı (%s): %s", bot.id, exc)

    db.flush()
    return result


# --------------------------------------------------------------------------- #
#  Fren
# --------------------------------------------------------------------------- #

def engage(db: Session, user: User, note: str = "") -> dict[str, Any]:
    """
    ACİL FREN: önce kapat, sonra dur.

    Sıra bilinçlidir ve tersine çevrilemez — anahtar önce çevrilseydi kapatma
    emirlerimiz kendi frenimize takılabilirdi.
    """
    started = datetime.now(UTC)

    # 1. Niyet önce kayda geçer. Sonraki adımlar patlasa bile tarihte
    #    "fren çekildi" izi kalmalı; sessizce kaybolan bir fren, olmamış
    #    bir frendir.
    audit(db, user, "EMERGENCY_BRAKE_START", "user",
          note or "Acil fren çekildi. Açık pozisyonlar kapatılıyor.",
          {"at": started.isoformat()})
    db.flush()

    result = flatten_all(db, user, stop_bots=True)

    # 2. Anahtar EN SONA bırakılır.
    set_kill_switch(db, user, True,
                    note or f"Acil fren · {started.isoformat()}")

    # 3. Sonuç, olduğu gibi yazılır — kapatılamayan varsa gizlenmez.
    audit(db, user, "EMERGENCY_BRAKE_DONE",
          "user" if result.clean else "system",
          result.headline(), result.to_dict())

    for row in result.closed + result.failed:
        _write_bot_event(db, row, clean=row in result.closed)

    _notify(db, user, result, note)
    db.flush()

    level = log.warning if result.clean else log.error
    level("ACİL FREN: %s", result.headline())

    return {
        "engaged": True,
        "kill_switch": True,
        "sonuc": result.to_dict(),
        "kullaniciya_soyle": result.headline(),
        "uyari": ("" if result.clean else
                  "KAPATILAMAYAN POZİSYONLAR VAR. Bunlar hâlâ piyasada ve "
                  "sistem onları izlemiyor. Borsanızın kendi arayüzünden "
                  "elle kapatın."),
        "sure_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
    }


def release(db: Session, user: User, note: str = "") -> dict[str, Any]:
    """
    Freni kaldırır.

    Botlar KENDİLİĞİNDEN başlamaz. Fren, sistemin bir şeyi yanlış yaptığı
    için çekilir; sebebi anlaşılmadan otomatik devam etmek aynı hataya geri
    dönmektir. Kullanıcı hangi botu tekrar çalıştıracağına kendisi karar verir.
    """
    set_kill_switch(db, user, False, note or "Acil fren kaldırıldı.")
    audit(db, user, "EMERGENCY_BRAKE_RELEASE", "user",
          note or "Fren kaldırıldı; botlar elle başlatılacak.")
    db.flush()
    return {
        "engaged": False,
        "kill_switch": False,
        "kullaniciya_soyle": ("Acil fren kaldırıldı. Botlar otomatik "
                              "başlamadı — hangisini çalıştıracağınıza siz "
                              "karar verin."),
    }


# --------------------------------------------------------------------------- #
#  Kayıt ve bildirim
# --------------------------------------------------------------------------- #

def _write_bot_event(db: Session, row: dict[str, Any], *, clean: bool) -> None:
    """Botun kendi geçmişine de yazar: 'bu pozisyona ne oldu' sorusu orada sorulur."""
    from ..models import BotEvent  # noqa: PLC0415

    if clean:
        message = (f"ACİL FREN · {row['symbol']} {row['side'].upper()} kapatıldı "
                   f"@ {row['cikis']:.6g} · sonuç {row['kar_zarar']:+.2f}")
        level = "warn"
    else:
        message = (f"ACİL FREN · {row['symbol']} {row['side'].upper()} "
                   f"KAPATILAMADI · {row['sebep']} · pozisyon hâlâ açık")
        level = "error"

    db.add(BotEvent(bot_id=row["bot_id"], level=level, category="risk",
                    message=message[:1000], data_json="{}"))


def _notify(db: Session, user: User, result: FlattenResult, note: str) -> None:
    """
    Telegram bildirimi — fren sessiz olmamalı.

    Kullanıcının tanımlı ilk Telegram anahtarı kullanılır: fren HESAP
    seviyesinde bir olaydır, tek bir botun olayı değil.
    """
    from ..layers import notifier  # noqa: PLC0415
    from ..models import Credential, CredentialKind  # noqa: PLC0415

    lines = ["🛑 <b>ACİL FREN</b>", ""]
    if note:
        lines.append(f"Sebep: {note}")
        lines.append("")
    lines.append(result.headline())
    if not result.clean:
        lines.append("")
        lines.append("<b>KAPATILAMAYANLAR:</b>")
        for row in result.failed[:8]:
            lines.append(f"• {row['symbol']} {row['side']} — {row['sebep']}")
        lines.append("")
        lines.append("Bunları borsanızdan ELLE kapatın.")
    lines.append("")
    lines.append("Yeni işlem açılmıyor. Botlar durduruldu.")

    try:
        from .creds import read_extra, read_secrets  # noqa: PLC0415

        cred = (db.query(Credential)
                .filter(Credential.user_id == user.id,
                        Credential.kind == CredentialKind.TELEGRAM).first())
        if cred is None:
            return
        secrets = read_secrets(user, cred)
        token = secrets.get("token", "")
        chat_id = secrets.get("chat_id") or read_extra(cred).get("chat_id", "")
        if token and chat_id:
            notifier.send_telegram(token, str(chat_id), "\n".join(lines))
    except Exception as exc:  # noqa: BLE001 — bildirim gitmezse fren yine geçerli
        log.info("acil fren bildirimi gönderilemedi: %s", str(exc)[:160])
