"""
MUTABAKAT — platformun bildiği ile borsanın gerçeği aynı mı?
=============================================================

Süreç durduğunda piyasa durmaz. Platform kapalıyken:

  * Borsadaki koruyucu stop tetiklenmiş ve pozisyon kapanmış olabilir,
  * Sanal modda fiyat stop/hedef seviyesini geçmiş olabilir,
  * Borsadaki koruyucu stop emri silinmiş/iptal olmuş olabilir.

Bu modül açılışta ve düzenli aralıklarla kaydı gerçeğe eşitler. Amaç tek bir
şeyi imkânsız kılmaktır: **platformun var sandığı ama gerçekte olmayan bir
pozisyona göre karar vermesi.**

TEMEL İLKE — ŞÜPHEDE HİÇBİR ŞEY YAPMA
--------------------------------------
Borsadan okuma başarısız olursa hiçbir kayıt değiştirilmez. "Öğrenemedim" ile
"pozisyon yok" karıştırılırsa, geçici bir ağ hatası gerçek bir pozisyonu
kapatılmış sayar ve sistem korumasız bir varlığı unutur. Bu yüzden okuma
uçları `None` (bilinmiyor) ile `0.0` (gerçekten yok) arasında ayrım yapar.

ASLA OTOMATİK KAPATILMAZ
------------------------
Borsada platformun bilmediği bir pozisyon bulunursa **dokunulmaz**, yalnızca
raporlanır. Kullanıcının elle açtığı bir işlem olabilir; onu kapatmak
platformun yetkisi değildir.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..layers.l1_market_data import fetch_ohlcv
from ..layers.l5_execution import LiveBroker
from ..models import (
    Bot,
    Position,
    PositionStatus,
    Side,
    TradingMode,
    User,
)

log = get_logger("zumvia.reconcile")


@dataclass(slots=True)
class ReconcileReport:
    checked: int = 0
    closed_by_exchange: list[int] = field(default_factory=list)
    stops_replaced: list[int] = field(default_factory=list)
    unknown_at_exchange: list[str] = field(default_factory=list)
    paper_settled: list[int] = field(default_factory=list)
    unreadable: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "closed_by_exchange": self.closed_by_exchange,
            "stops_replaced": self.stops_replaced,
            "unknown_at_exchange": self.unknown_at_exchange,
            "paper_settled": self.paper_settled,
            "unreadable": self.unreadable,
            "notes": self.notes,
        }

    @property
    def changed(self) -> bool:
        return bool(self.closed_by_exchange or self.stops_replaced
                    or self.paper_settled or self.unknown_at_exchange)


def _close_position(db: Session, position: Position, price: float,
                    reason: str) -> None:
    """Pozisyonu kapatır ve PnL'i gerçek çıkış fiyatından hesaplar."""
    direction = 1.0 if position.side == Side.LONG else -1.0
    pnl = (price - position.entry_price) * position.qty * direction - (position.fees or 0.0)

    position.status = PositionStatus.CLOSED
    position.exit_price = price
    position.closed_at = datetime.now(UTC)
    position.pnl = pnl
    position.close_reason = reason[:200]
    if position.risk_amount:
        position.r_multiple = pnl / position.risk_amount
    db.flush()


def _settle_paper_gap(db: Session, bot: Bot, position: Position,
                      report: ReconcileReport, emit: Any = None) -> bool:
    """
    Sanal pozisyonda, platform kapalıyken stop/hedef seviyesi geçildi mi?

    Geçildiyse pozisyon o seviyeden kapatılır — çünkü gerçek hayatta borsadaki
    stop emri tetiklenmiş olurdu. Aksi hâlde sanal sonuçlar gerçeğinden daha
    iyi görünür ve geri test yanıltıcı olur.
    """
    try:
        df = fetch_ohlcv(bot.market, bot.exchange, bot.symbol, bot.timeframe, limit=200)
    except Exception as exc:  # noqa: BLE001
        log.info("sanal mutabakat için veri alınamadı (%s): %s", bot.symbol, exc)
        return False

    opened = position.opened_at
    if opened is None:
        return False
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=UTC)

    # Pozisyon açıldıktan SONRAKİ barlar
    try:
        recent = df[df.index > opened] if hasattr(df.index, "tz") else df.tail(50)
    except Exception:  # noqa: BLE001
        recent = df.tail(50)
    if recent.empty:
        return False

    long_side = position.side == Side.LONG
    stop, target = position.stop_loss, position.take_profit

    for _, bar in recent.iterrows():
        low, high = float(bar["low"]), float(bar["high"])
        # Stop önce kontrol edilir: aynı barda ikisi de dokunulduysa
        # muhafazakâr varsayım, kötü olanın gerçekleştiğidir.
        if stop > 0 and ((long_side and low <= stop) or (not long_side and high >= stop)):
            _close_position(db, position, stop,
                            "Platform kapalıyken stop seviyesi geçildi (mutabakat).")
            report.paper_settled.append(position.id)
            if emit:
                emit(db, bot, "trade", "exec",
                     f"MUTABAKAT · Pozisyon stop seviyesinden kapatıldı ({stop:.6g}). "
                     f"Platform kapalıyken fiyat oraya değmişti.",
                     {"position_id": position.id, "exit": stop})
            return True
        if target > 0 and ((long_side and high >= target) or (not long_side and low <= target)):
            _close_position(db, position, target,
                            "Platform kapalıyken hedef seviyesi geçildi (mutabakat).")
            report.paper_settled.append(position.id)
            if emit:
                emit(db, bot, "trade", "exec",
                     f"MUTABAKAT · Pozisyon hedeften kapatıldı ({target:.6g}).",
                     {"position_id": position.id, "exit": target})
            return True
    return False


def run(db: Session, emit: Any = None, *, bot_id: int | None = None) -> ReconcileReport:
    """
    Açık pozisyonları borsayla (canlı) veya fiyat geçmişiyle (sanal) eşitler.

    Açılışta ve düzenli aralıklarla çağrılır. Hiçbir koşulda istisna fırlatmaz:
    mutabakat başarısız olsa bile platform çalışmaya devam etmelidir.
    """
    from .orchestrator import _make_broker  # noqa: PLC0415

    report = ReconcileReport()

    query = db.query(Position).filter(Position.status == PositionStatus.OPEN)
    if bot_id is not None:
        query = query.filter(Position.bot_id == bot_id)

    for position in query.all():
        bot = db.get(Bot, position.bot_id)
        if bot is None:
            continue
        report.checked += 1

        # ------------------------------------------------------------ sanal
        if bot.mode != TradingMode.LIVE:
            try:
                _settle_paper_gap(db, bot, position, report, emit)
            except Exception:  # noqa: BLE001 — tek pozisyon diğerlerini durdurmaz
                log.exception("sanal mutabakat hatası (pozisyon %s)", position.id)
            continue

        # ------------------------------------------------------------- canlı
        user = db.get(User, bot.user_id)
        try:
            broker = _make_broker(db, bot, user, bot.paper_balance)
        except Exception as exc:  # noqa: BLE001
            log.info("mutabakat için broker kurulamadı (bot %s): %s", bot.id, exc)
            report.unreadable.append(position.id)
            continue

        if not isinstance(broker, LiveBroker):
            # Canlı yetki düşmüş olabilir; kayda dokunma, yalnızca not düş.
            report.unreadable.append(position.id)
            continue

        actual = broker.open_position_qty(position.symbol)
        if actual is None:
            # ŞÜPHEDE HİÇBİR ŞEY YAPMA
            report.unreadable.append(position.id)
            continue

        if actual <= 1e-12:
            # Borsada pozisyon yok: koruyucu stop tetiklenmiş demektir.
            exit_price = position.stop_loss or position.entry_price
            _close_position(db, position, exit_price,
                            "Borsada pozisyon bulunamadı — koruyucu stop tetiklenmiş "
                            "(mutabakat ile kapatıldı).")
            report.closed_by_exchange.append(position.id)
            if emit:
                emit(db, bot, "trade", "exec",
                     f"MUTABAKAT · Borsada pozisyon yok; koruyucu stop tetiklenmiş. "
                     f"Kayıt {exit_price:.6g} seviyesinden kapatıldı.",
                     {"position_id": position.id, "exit": exit_price})
            continue

        # Pozisyon duruyor: koruyucu stop hâlâ borsada mı?
        has_stop = broker.has_open_stop(position.symbol)
        if has_stop is False and position.stop_loss > 0:
            side_word = "buy" if position.side == Side.LONG else "sell"
            result = broker.place_protective_stop(position.symbol, side_word,
                                                  position.qty, position.stop_loss)
            if result.ok:
                report.stops_replaced.append(position.id)
                if emit:
                    emit(db, bot, "warn", "risk",
                         f"MUTABAKAT · Borsadaki koruyucu stop kayıptı, yeniden "
                         f"bırakıldı ({position.stop_loss:.6g}).",
                         {"position_id": position.id})
            else:
                report.notes.append(
                    f"{position.symbol}: koruyucu stop yeniden bırakılamadı "
                    f"({result.error}). Motor stop takibini kendisi yapıyor."
                )

    db.commit()

    if report.changed:
        log.warning("mutabakat: %d kapatıldı, %d stop yenilendi, %d okunamadı",
                    len(report.closed_by_exchange), len(report.stops_replaced),
                    len(report.unreadable))
    return report
