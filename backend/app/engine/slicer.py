"""
PARÇALI EMİR YÜRÜTÜCÜ (TWAP) — planı fiilen uygulayan katman
=============================================================

`layers/capital.py` bir emrin kaç parçaya bölünmesi gerektiğini hesaplar.
Bu modül o planı **gerçekten uygular**: her aralıkta bir çocuk emir gönderir,
dolumları biriktirir ve pozisyonun ağırlıklı ortalama giriş fiyatını günceller.

Neden ayrı bir yürütücü?
------------------------
Bot turu periyodiktir (örn. 15 dakikada bir). Emri turun içinde `sleep` ile
bölmek zamanlayıcı iş parçacığını kilitler ve diğer botları durdurur. Bunun
yerine emir **veritabanına yazılır** ve hızlı çalışan ayrı bir iş her birkaç
saniyede bir vadesi gelen parçaları gönderir.

Bu aynı zamanda dayanıklılık sağlar: süreç çökse bile yarım kalmış emrin ne
kadarının dolduğu kayıtlıdır. Bellekte tutulan bir plan, çökme anında
pozisyonu belirsiz bırakırdı.

İPTAL KOŞULLARI (hepsi kodda zorlanır)
--------------------------------------
1. **Fiyat kayması** — referans fiyattan aleyhte `max_drift_pct` kadar
   uzaklaşıldıysa kalan parçalar iptal edilir. Emrin gerekçesi fiyat
   buradayken oluştu; oraya gittiğinde artık aynı işlem değildir.
2. **Acil fren** — kill switch açıksa yeni parça gönderilmez (dolan kısım
   korunur ve yönetilmeye devam eder).
3. **Süre aşımı** — plan `expires_at`'i geçtiyse kalan iptal.

Her durumda **dolan kısım korunur**: pozisyon o miktarla yaşar, stop ve hedef
gerçekleşen ortalama fiyata göre geçerlidir.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..core.safety import kill_switch_active, kill_switch_reason
from ..layers.l1_market_data import fetch_quote
from ..layers.l5_execution import LiveBroker
from ..models import (
    Bot,
    Position,
    PositionStatus,
    Side,
    User,
    WorkingOrder,
)

log = get_logger("zumvia.slicer")

# Bir çalışan emrin yaşayabileceği azami süre (plan süresinin katı olarak)
LIFETIME_MULTIPLIER = 3.0
MIN_LIFETIME_SECONDS = 300


def create(db: Session, bot: Bot, *, side: Side, total_qty: float,
           reference_price: float, slices: int, interval_seconds: int,
           stop_loss: float, take_profit: float, risk_amount: float,
           decision: dict[str, Any], snapshot: dict[str, Any],
           max_drift_pct: float = 0.6) -> WorkingOrder:
    """Parçalı emri kaydeder; ilk parça hemen vadesi gelmiş sayılır."""
    now = datetime.now(UTC)
    lifetime = max(MIN_LIFETIME_SECONDS,
                   int(slices * max(interval_seconds, 1) * LIFETIME_MULTIPLIER))

    order = WorkingOrder(
        bot_id=bot.id, symbol=bot.symbol, side=side, status="working",
        total_qty=float(total_qty), slices=max(1, int(slices)),
        interval_seconds=max(0, int(interval_seconds)),
        reference_price=float(reference_price),
        max_drift_pct=float(max_drift_pct),
        stop_loss=float(stop_loss), take_profit=float(take_profit),
        risk_amount=float(risk_amount),
        decision_json=json.dumps(decision, ensure_ascii=False, default=str)[:8000],
        snapshot_json=json.dumps(snapshot, ensure_ascii=False, default=str)[:16000],
        next_slice_at=now,
        expires_at=now + timedelta(seconds=lifetime),
    )
    db.add(order)
    db.flush()
    log.info("çalışan emir oluşturuldu: bot=%s %s %s parça=%d",
             bot.id, bot.symbol, side.value, slices)
    return order


def due_orders(db: Session, now: datetime | None = None) -> list[WorkingOrder]:
    """Vadesi gelmiş çalışan emirler."""
    now = now or datetime.now(UTC)
    return (db.query(WorkingOrder)
            .filter(WorkingOrder.status == "working",
                    WorkingOrder.next_slice_at <= now)
            .order_by(WorkingOrder.id).all())


def _drift_pct(order: WorkingOrder, price: float) -> float:
    """Referans fiyattan ALEYHTE sapma yüzdesi (lehte sapma 0 sayılır)."""
    if order.reference_price <= 0 or price <= 0:
        return 0.0
    change = (price - order.reference_price) / order.reference_price * 100.0
    return max(0.0, change if order.side == Side.LONG else -change)


def _finish(db: Session, order: WorkingOrder, status: str, note: str) -> None:
    order.status = status
    order.note = note[:1000]
    order.finished_at = datetime.now(UTC)
    db.flush()


def execute_slice(db: Session, order: WorkingOrder,
                  emit: Any = None) -> dict[str, Any]:
    """
    Bir çalışan emrin SIRADAKİ parçasını gönderir.

    Dönen sözlük her zaman `status` taşır; çağıran (zamanlayıcı) sonucu
    yorumlamak zorunda değildir.
    """
    bot = db.get(Bot, order.bot_id)
    if bot is None:
        _finish(db, order, "cancelled", "Bot silinmiş.")
        return {"status": "cancelled", "reason": "Bot yok."}

    user = db.get(User, bot.user_id)
    now = datetime.now(UTC)

    # ---------------------------------------------------------- iptal kapıları
    if kill_switch_active():
        # Beklet, iptal etme: fren kalkınca kalan parçalar devam edebilir.
        order.next_slice_at = now + timedelta(seconds=30)
        db.flush()
        return {"status": "paused", "reason": kill_switch_reason()}

    expires = order.expires_at
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if now > expires:
            _finish(db, order, "cancelled",
                    f"Süre aşıldı; {order.filled_qty:.8g}/{order.total_qty:.8g} doldu. "
                    f"Kalan iptal edildi.")
            _settle(db, order, bot, emit)
            return {"status": "expired", "filled": order.filled_qty}

    # ------------------------------------------------------------ güncel fiyat
    try:
        quote = fetch_quote(bot.market, bot.exchange, bot.symbol)
        price = float(getattr(quote, "price", 0.0) or 0.0)
    except Exception as exc:  # noqa: BLE001 — fiyat alınamazsa parça ertelenir
        order.next_slice_at = now + timedelta(seconds=15)
        db.flush()
        return {"status": "deferred", "reason": f"Fiyat alınamadı: {exc}"}

    if price <= 0:
        order.next_slice_at = now + timedelta(seconds=15)
        db.flush()
        return {"status": "deferred", "reason": "Geçersiz fiyat."}

    drift = _drift_pct(order, price)
    if drift > order.max_drift_pct:
        _finish(db, order, "cancelled",
                f"Fiyat aleyhte %{drift:.2f} kaydı (tavan %{order.max_drift_pct:.2f}). "
                f"Kalan parçalar iptal; {order.filled_qty:.8g} lot korunuyor.")
        _settle(db, order, bot, emit)
        if emit:
            emit(db, bot, "warn", "exec",
                 f"PARÇALI EMİR DURDURULDU · fiyat %{drift:.2f} kaydı, "
                 f"{order.slices_done}/{order.slices} parça gönderildi.",
                 {"drift_pct": round(drift, 3), "filled_qty": order.filled_qty})
        return {"status": "cancelled", "reason": "drift", "drift_pct": drift}

    # ------------------------------------------------------------ parçayı gönder
    remaining = max(0.0, order.total_qty - order.filled_qty)
    slices_left = max(1, order.slices - order.slices_done)
    qty = remaining if slices_left <= 1 else remaining / slices_left
    if qty <= 0:
        _finish(db, order, "done", "Tamamlandı.")
        _settle(db, order, bot, emit)
        return {"status": "done", "filled": order.filled_qty}

    from .orchestrator import _make_broker  # noqa: PLC0415
    broker = _make_broker(db, bot, user, bot.paper_balance)
    side_word = "buy" if order.side == Side.LONG else "sell"
    result = broker.market_order(bot.symbol, side_word, qty, price)

    if not result.ok:
        # Tek bir parçanın reddi emri öldürmez; bir sonraki aralıkta denenir.
        order.slices_done += 1
        order.next_slice_at = now + timedelta(seconds=max(5, order.interval_seconds))
        db.flush()
        if order.slices_done >= order.slices:
            _finish(db, order, "done" if order.filled_qty > 0 else "failed",
                    f"Son parça reddedildi: {result.error}")
            _settle(db, order, bot, emit)
        return {"status": "rejected", "reason": result.error}

    filled_qty = float(result.filled_qty or qty)
    filled_price = float(result.filled_price or price)

    # Ağırlıklı ortalama giriş fiyatı
    total_cost = order.avg_fill_price * order.filled_qty + filled_price * filled_qty
    order.filled_qty += filled_qty
    order.avg_fill_price = total_cost / order.filled_qty if order.filled_qty else filled_price
    order.fees += float(result.fee or 0.0)
    order.slices_done += 1
    order.next_slice_at = now + timedelta(seconds=max(1, order.interval_seconds))
    db.flush()

    _settle(db, order, bot, emit, partial=True)

    finished = order.slices_done >= order.slices or order.filled_qty >= order.total_qty - 1e-12
    if finished:
        _finish(db, order, "done",
                f"{order.slices_done} parçada {order.filled_qty:.8g} lot dolduruldu; "
                f"ortalama {order.avg_fill_price:.6g}.")
        _settle(db, order, bot, emit)
        if emit:
            emit(db, bot, "trade", "exec",
                 f"PARÇALI EMİR TAMAMLANDI · {order.slices_done} parça · "
                 f"ortalama giriş {order.avg_fill_price:.6g} · "
                 f"lot {order.filled_qty:.8g}",
                 {"slices": order.slices_done, "avg_price": order.avg_fill_price,
                  "qty": order.filled_qty})

    return {"status": "filled" if not finished else "done",
            "slice_qty": filled_qty, "avg_price": order.avg_fill_price,
            "slices_done": order.slices_done, "slices": order.slices}


def _settle(db: Session, order: WorkingOrder, bot: Bot, emit: Any = None,
            partial: bool = False) -> Position | None:
    """
    Dolan miktarı pozisyona yansıtır.

    İlk dolumda pozisyon açılır; sonraki dolumlarda miktar ve ortalama giriş
    fiyatı güncellenir. Böylece emir yarıda kalsa bile ortada **gerçek** bir
    pozisyon vardır — hayali bir "tam dolmuş" pozisyon değil.
    """
    if order.filled_qty <= 0:
        return None

    decision = {}
    try:
        decision = json.loads(order.decision_json or "{}")
    except (TypeError, ValueError):
        decision = {}

    position = db.get(Position, order.position_id) if order.position_id else None

    if position is None:
        position = Position(
            bot_id=bot.id, symbol=order.symbol, side=order.side,
            status=PositionStatus.OPEN, mode=bot.mode,
            qty=order.filled_qty, entry_price=order.avg_fill_price,
            stop_loss=order.stop_loss, take_profit=order.take_profit,
            initial_stop=order.stop_loss, risk_amount=order.risk_amount,
            notional=order.filled_qty * order.avg_fill_price,
            fees=order.fees,
            confidence=float(decision.get("confidence", 0.0)),
            reasoning=str(decision.get("reasoning", ""))[:1000],
            snapshot_json=order.snapshot_json,
            original_qty=order.filled_qty,
            decision_models_json=json.dumps(decision.get("decision_models", []),
                                            ensure_ascii=False),
            decision_id=str(decision.get("decision_id", "")),
        )
        db.add(position)
        db.flush()
        order.position_id = position.id
        bot.day_trades += 1

        # Koruyucu stop, ilk dolumdan hemen sonra borsaya bırakılır: emir
        # tamamlanmasa bile pozisyon korumasız kalmaz.
        from .orchestrator import _make_broker  # noqa: PLC0415
        broker = _make_broker(db, bot, db.get(User, bot.user_id), bot.paper_balance)
        if isinstance(broker, LiveBroker) and order.stop_loss > 0:
            side_word = "buy" if order.side == Side.LONG else "sell"
            broker.place_protective_stop(order.symbol, side_word,
                                         position.qty, order.stop_loss)

        if emit:
            emit(db, bot, "trade", "exec",
                 f"POZİSYON AÇILDI (parçalı) · {order.side.value.upper()} "
                 f"{order.symbol} @ {order.avg_fill_price:.6g} · "
                 f"ilk parça {order.filled_qty:.8g} lot · SL {order.stop_loss:.6g}",
                 {"position_id": position.id, "working_order_id": order.id,
                  "entry": order.avg_fill_price, "qty": order.filled_qty})
    else:
        position.qty = order.filled_qty
        position.entry_price = order.avg_fill_price
        position.original_qty = order.filled_qty
        position.notional = order.filled_qty * order.avg_fill_price
        position.fees = order.fees
        db.flush()

    return position
