"""
PARÇALI EMİR YÜRÜTÜCÜ TESTLERİ

Bu testler tek bir iddiayı korur: **plan hesaplanmakla kalmaz, uygulanır.**
Büyük emir gerçekten parçalara bölünür, dolumlar birikir, ağırlıklı ortalama
giriş fiyatı doğru hesaplanır ve iptal koşullarında DOLAN kısım korunur.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.engine import slicer
from app.models import (
    Autonomy,
    Bot,
    BotStatus,
    Position,
    PositionStatus,
    Side,
    TradingMode,
    User,
    WorkingOrder,
)


@pytest.fixture()
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def user(db):
    row = db.query(User).filter(User.email == "slicer@zumvia.com").first()
    if row is None:
        row = User(email="slicer@zumvia.com",
                   password_hash=hash_password("slicertest12345"),
                   vault_salt=new_salt())
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@pytest.fixture()
def bot(db, user):
    row = Bot(
        user_id=user.id, name="Parçalı emir testi", market="demo", exchange="demo",
        symbol="DEMO/USDT", timeframe="1h", mode=TradingMode.PAPER,
        autonomy=Autonomy.FULL, decision_mode="algo_only", status=BotStatus.RUNNING,
        strategies_json=json.dumps(["trend_following"]),
        initial_balance=100_000.0, paper_balance=100_000.0,
        peak_equity=100_000.0, day_start_equity=100_000.0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    yield row

    db.query(WorkingOrder).filter(WorkingOrder.bot_id == row.id).delete()
    db.query(Position).filter(Position.bot_id == row.id).delete()
    fresh = db.get(Bot, row.id)
    if fresh is not None:
        db.delete(fresh)
    db.commit()


def _live_price(bot) -> float:
    """Demo enstrümanının gerçek fiyatı — referans fiyat buradan alınmalı."""
    from app.layers.l1_market_data import fetch_quote  # noqa: PLC0415

    return float(fetch_quote(bot.market, bot.exchange, bot.symbol).price)


def _order(db, bot, *, slices: int = 4, qty: float = 40.0,
           price: float | None = None, drift: float = 5.0) -> WorkingOrder:
    price = price if price is not None else _live_price(bot)
    order = slicer.create(
        db, bot, side=Side.LONG, total_qty=qty, reference_price=price,
        slices=slices, interval_seconds=0,
        stop_loss=price * 0.95, take_profit=price * 1.10,
        risk_amount=100.0, decision={"confidence": 0.8, "reasoning": "test"},
        snapshot={}, max_drift_pct=drift,
    )
    db.commit()
    return order


# --------------------------------------------------------------------------- #
#  Temel akış
# --------------------------------------------------------------------------- #

def test_order_is_persisted_not_kept_in_memory(db, bot) -> None:
    """Süreç çökse bile yarım emrin durumu bilinmeli."""
    order = _order(db, bot)
    assert order.id is not None
    assert db.get(WorkingOrder, order.id).status == "working"


def test_first_slice_opens_the_position(db, bot) -> None:
    order = _order(db, bot, slices=4, qty=40.0)
    result = slicer.execute_slice(db, order)
    db.commit()

    assert result["status"] == "filled"
    assert order.position_id is not None

    position = db.get(Position, order.position_id)
    assert position.status == PositionStatus.OPEN
    assert position.qty == pytest.approx(10.0, rel=0.02)   # 40 / 4


def test_position_grows_slice_by_slice(db, bot) -> None:
    """Pozisyon dolum ilerledikçe büyür — hayali 'tam dolmuş' pozisyon yok."""
    order = _order(db, bot, slices=4, qty=40.0)

    seen = []
    for _ in range(4):
        slicer.execute_slice(db, order)
        db.commit()
        seen.append(db.get(Position, order.position_id).qty)

    assert seen == sorted(seen), "pozisyon miktarı azalmamalı"
    assert seen[-1] == pytest.approx(40.0, rel=0.02)
    assert order.status == "done"
    assert order.slices_done == 4


def test_average_entry_price_is_volume_weighted(db, bot) -> None:
    """Ortalama giriş fiyatı, parçaların ağırlıklı ortalaması olmalı."""
    order = _order(db, bot, slices=3, qty=30.0)
    for _ in range(3):
        slicer.execute_slice(db, order)
        db.commit()

    position = db.get(Position, order.position_id)
    assert order.avg_fill_price > 0
    assert position.entry_price == pytest.approx(order.avg_fill_price, rel=1e-6)
    # Demo fiyatı makul aralıkta olmalı (slipaj payıyla)
    assert 0 < position.entry_price < order.reference_price * 5


def test_completed_order_stops_producing_slices(db, bot) -> None:
    order = _order(db, bot, slices=2, qty=20.0)
    for _ in range(2):
        slicer.execute_slice(db, order)
        db.commit()

    assert order.status == "done"
    again = slicer.execute_slice(db, order)
    db.commit()
    assert again["status"] in ("done", "cancelled")
    assert order.slices_done == 2, "tamamlanmış emir yeni parça göndermemeli"


# --------------------------------------------------------------------------- #
#  İptal koşulları — dolan kısım HER ZAMAN korunur
# --------------------------------------------------------------------------- #

def test_adverse_drift_cancels_rest_but_keeps_filled(db, bot) -> None:
    """
    Fiyat aleyhte kaydıysa kalan parçalar iptal edilir; ama dolan kısım
    silinmez — o pozisyon gerçekten açıldı ve yönetilmeli.
    """
    order = _order(db, bot, slices=4, qty=40.0)
    slicer.execute_slice(db, order)            # ilk parça dolsun
    db.commit()
    filled_before = order.filled_qty
    assert filled_before > 0

    # LONG için aleyhte kayma = fiyatın YÜKSELMESİ (planlanandan pahalıya almak).
    # Referansı düşürmek, aynı etkiyi kontrollü biçimde üretir.
    order.reference_price = order.reference_price / 10.0
    db.commit()

    result = slicer.execute_slice(db, order)
    db.commit()

    assert result["status"] == "cancelled"
    assert order.status == "cancelled"
    assert order.filled_qty == pytest.approx(filled_before)
    position = db.get(Position, order.position_id)
    assert position is not None and position.status == PositionStatus.OPEN
    assert position.qty == pytest.approx(filled_before)


def test_expired_order_is_cancelled_with_filled_kept(db, bot) -> None:
    order = _order(db, bot, slices=4, qty=40.0)
    slicer.execute_slice(db, order)
    db.commit()
    filled = order.filled_qty

    order.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    result = slicer.execute_slice(db, order)
    db.commit()

    assert result["status"] == "expired"
    assert order.status == "cancelled"
    assert db.get(Position, order.position_id).qty == pytest.approx(filled)


def test_kill_switch_pauses_without_cancelling(db, bot) -> None:
    """
    Acil fren emri İPTAL etmez, bekletir: fren kalkınca kalan parçalar
    devam edebilmeli. İptal etmek, yarım pozisyonu kalıcı hâle getirirdi.
    """
    from app.core.safety import KILL_SWITCH_FILE  # noqa: PLC0415

    order = _order(db, bot, slices=4, qty=40.0)
    KILL_SWITCH_FILE.write_text("test", encoding="utf-8")
    try:
        result = slicer.execute_slice(db, order)
        db.commit()
        assert result["status"] == "paused"
        assert order.status == "working", "fren emri iptal etmemeli"
        assert order.slices_done == 0
    finally:
        KILL_SWITCH_FILE.unlink(missing_ok=True)


def test_missing_bot_cancels_order(db, bot) -> None:
    order = _order(db, bot)
    db.expunge(order)
    order.bot_id = 999_999                       # veritabanına yazılmaz
    result = slicer.execute_slice(db, order)
    assert result["status"] == "cancelled"


# --------------------------------------------------------------------------- #
#  Zamanlama
# --------------------------------------------------------------------------- #

def test_due_orders_only_returns_ripe_ones(db, bot) -> None:
    ready = _order(db, bot)
    later = _order(db, bot)
    later.next_slice_at = datetime.now(UTC) + timedelta(minutes=10)
    db.commit()

    due_ids = {o.id for o in slicer.due_orders(db)}
    assert ready.id in due_ids
    assert later.id not in due_ids


def test_interval_is_respected_between_slices(db, bot) -> None:
    order = slicer.create(
        db, bot, side=Side.LONG, total_qty=20.0, reference_price=_live_price(bot),
        slices=4, interval_seconds=60,
        stop_loss=_live_price(bot) * 0.95, take_profit=_live_price(bot) * 1.10,
        risk_amount=50.0,
        decision={}, snapshot={},
    )
    db.commit()

    slicer.execute_slice(db, order)
    db.commit()

    next_at = order.next_slice_at
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=UTC)
    assert next_at > datetime.now(UTC) + timedelta(seconds=30)
    assert order.id not in {o.id for o in slicer.due_orders(db)}
