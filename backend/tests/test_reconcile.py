"""
MUTABAKAT TESTLERİ

Korunan iki ilke:

1. **Şüphede hiçbir şey yapma.** Borsadan okuma başarısızsa ("bilinmiyor")
   hiçbir kayıt değişmez. Geçici bir ağ hatası, gerçek bir pozisyonu
   "kapanmış" saymamalıdır.
2. **Platform kapalıyken olanlar kayda geçer.** Sanal modda stop/hedef
   seviyesi geçildiyse pozisyon o seviyeden kapanır — aksi hâlde sonuçlar
   gerçeğinden iyi görünür.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.engine import reconcile
from app.models import (
    Autonomy,
    Bot,
    BotStatus,
    Position,
    PositionStatus,
    Side,
    TradingMode,
    User,
)


@pytest.fixture()
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def user(db):
    row = db.query(User).filter(User.email == "recon@zumvia.com").first()
    if row is None:
        row = User(email="recon@zumvia.com",
                   password_hash=hash_password("recontest12345"),
                   vault_salt=new_salt())
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@pytest.fixture()
def bot(db, user):
    row = Bot(
        user_id=user.id, name="Mutabakat testi", market="demo", exchange="demo",
        symbol="DEMO/USDT", timeframe="1h", mode=TradingMode.PAPER,
        autonomy=Autonomy.FULL, decision_mode="algo_only", status=BotStatus.RUNNING,
        strategies_json=json.dumps(["trend_following"]),
        initial_balance=10_000.0, paper_balance=10_000.0,
        peak_equity=10_000.0, day_start_equity=10_000.0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    yield row

    db.query(Position).filter(Position.bot_id == row.id).delete()
    fresh = db.get(Bot, row.id)
    if fresh is not None:
        db.delete(fresh)
    db.commit()


def _position(db, bot, *, entry=100.0, stop=95.0, target=115.0,
              side=Side.LONG, hours_ago=6) -> Position:
    row = Position(
        bot_id=bot.id, symbol=bot.symbol, side=side,
        status=PositionStatus.OPEN, mode=bot.mode,
        qty=1.0, entry_price=entry, stop_loss=stop, take_profit=target,
        initial_stop=stop, risk_amount=abs(entry - stop),
        notional=entry, confidence=0.8, reasoning="mutabakat testi",
        opened_at=datetime.now(UTC) - timedelta(hours=hours_ago),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _bars(low: float, high: float, rows: int = 30) -> pd.DataFrame:
    """Verilen aralıkta gezinen sentetik barlar."""
    idx = pd.date_range(datetime.now(UTC) - timedelta(hours=rows),
                        periods=rows, freq="h", tz="UTC")
    mid = (low + high) / 2
    return pd.DataFrame({
        "open": np.full(rows, mid), "high": np.full(rows, high),
        "low": np.full(rows, low), "close": np.full(rows, mid),
        "volume": np.full(rows, 1000.0),
    }, index=idx)


# --------------------------------------------------------------------------- #
#  Sanal mod — platform kapalıyken geçilen seviyeler
# --------------------------------------------------------------------------- #

def test_stop_touched_while_offline_is_settled(db, bot, monkeypatch) -> None:
    """Fiyat stop seviyesine değmişse pozisyon açık kalamaz."""
    position = _position(db, bot, entry=100.0, stop=95.0, target=115.0)
    monkeypatch.setattr(reconcile, "fetch_ohlcv",
                        lambda *a, **k: _bars(low=90.0, high=101.0))

    report = reconcile.run(db)
    db.refresh(position)

    assert position.id in report.paper_settled
    assert position.status == PositionStatus.CLOSED
    assert position.exit_price == pytest.approx(95.0)
    assert position.pnl < 0


def test_target_touched_while_offline_is_settled(db, bot, monkeypatch) -> None:
    position = _position(db, bot, entry=100.0, stop=95.0, target=115.0)
    monkeypatch.setattr(reconcile, "fetch_ohlcv",
                        lambda *a, **k: _bars(low=99.0, high=120.0))

    reconcile.run(db)
    db.refresh(position)

    assert position.status == PositionStatus.CLOSED
    assert position.exit_price == pytest.approx(115.0)
    assert position.pnl > 0


def test_stop_wins_when_both_levels_touched(db, bot, monkeypatch) -> None:
    """
    Aynı barda hem stop hem hedef dokunulduysa muhafazakâr varsayım:
    kötü olan gerçekleşti. Aksi, geri testi olduğundan iyi gösterir.
    """
    position = _position(db, bot, entry=100.0, stop=95.0, target=115.0)
    monkeypatch.setattr(reconcile, "fetch_ohlcv",
                        lambda *a, **k: _bars(low=90.0, high=120.0))

    reconcile.run(db)
    db.refresh(position)
    assert position.exit_price == pytest.approx(95.0)


def test_untouched_position_stays_open(db, bot, monkeypatch) -> None:
    position = _position(db, bot, entry=100.0, stop=95.0, target=115.0)
    monkeypatch.setattr(reconcile, "fetch_ohlcv",
                        lambda *a, **k: _bars(low=98.0, high=104.0))

    reconcile.run(db)
    db.refresh(position)
    assert position.status == PositionStatus.OPEN


def test_short_position_stop_is_above_entry(db, bot, monkeypatch) -> None:
    position = _position(db, bot, entry=100.0, stop=105.0, target=90.0,
                         side=Side.SHORT)
    monkeypatch.setattr(reconcile, "fetch_ohlcv",
                        lambda *a, **k: _bars(low=99.0, high=110.0))

    reconcile.run(db)
    db.refresh(position)
    assert position.status == PositionStatus.CLOSED
    assert position.exit_price == pytest.approx(105.0)


def test_missing_price_data_changes_nothing(db, bot, monkeypatch) -> None:
    """Veri alınamazsa pozisyona dokunulmaz."""
    position = _position(db, bot)

    def boom(*args, **kwargs):
        raise RuntimeError("veri yok")

    monkeypatch.setattr(reconcile, "fetch_ohlcv", boom)
    reconcile.run(db)
    db.refresh(position)
    assert position.status == PositionStatus.OPEN


# --------------------------------------------------------------------------- #
#  Canlı mod — "şüphede hiçbir şey yapma"
# --------------------------------------------------------------------------- #

class _Broker:
    """Kontrollü sahte canlı broker."""

    def __init__(self, qty, has_stop=True) -> None:
        self._qty = qty
        self._has_stop = has_stop
        self.stop_calls = 0

    def open_position_qty(self, symbol):  # noqa: ARG002
        return self._qty

    def has_open_stop(self, symbol):  # noqa: ARG002
        return self._has_stop

    def place_protective_stop(self, symbol, side, qty, stop_price):  # noqa: ARG002
        self.stop_calls += 1
        from app.layers.l5_execution import OrderResult  # noqa: PLC0415
        return OrderResult(True, order_id="stop-1")


def _live(db, bot, broker, monkeypatch) -> None:
    """Botu canlıya alır ve sahte broker'ı bağlar."""
    bot.mode = TradingMode.LIVE
    db.commit()
    monkeypatch.setattr(reconcile, "LiveBroker", _Broker)
    import app.engine.orchestrator as orch  # noqa: PLC0415
    monkeypatch.setattr(orch, "_make_broker", lambda *a, **k: broker)


def test_unreadable_exchange_never_closes_a_position(db, bot, monkeypatch) -> None:
    """
    En kritik test: borsa okunamıyorsa (None) pozisyon KAPATILMAZ.
    'Bilinmiyor' ile 'yok' karıştırılırsa gerçek pozisyon kaybolur.
    """
    position = _position(db, bot)
    broker = _Broker(qty=None)
    _live(db, bot, broker, monkeypatch)

    report = reconcile.run(db)
    db.refresh(position)

    assert position.status == PositionStatus.OPEN
    assert position.id in report.unreadable
    assert not report.closed_by_exchange


def test_position_gone_at_exchange_is_closed_at_stop(db, bot, monkeypatch) -> None:
    """Borsada pozisyon yoksa koruyucu stop tetiklenmiş demektir."""
    position = _position(db, bot, entry=100.0, stop=95.0)
    broker = _Broker(qty=0.0)
    _live(db, bot, broker, monkeypatch)

    report = reconcile.run(db)
    db.refresh(position)

    assert position.id in report.closed_by_exchange
    assert position.status == PositionStatus.CLOSED
    assert position.exit_price == pytest.approx(95.0)
    assert "stop" in (position.close_reason or "").lower()


def test_missing_protective_stop_is_replaced(db, bot, monkeypatch) -> None:
    """Borsadaki koruyucu emir düşmüşse yeniden bırakılır."""
    position = _position(db, bot)
    broker = _Broker(qty=1.0, has_stop=False)
    _live(db, bot, broker, monkeypatch)

    report = reconcile.run(db)
    db.refresh(position)

    assert position.id in report.stops_replaced
    assert broker.stop_calls == 1
    assert position.status == PositionStatus.OPEN, "stop yenilendi, pozisyon kapanmadı"


def test_existing_stop_is_not_duplicated(db, bot, monkeypatch) -> None:
    position = _position(db, bot)
    broker = _Broker(qty=1.0, has_stop=True)
    _live(db, bot, broker, monkeypatch)

    reconcile.run(db)
    assert broker.stop_calls == 0, "zaten duran stop tekrar bırakılmamalı"
    db.refresh(position)
    assert position.status == PositionStatus.OPEN


def test_unknown_stop_state_does_not_place_duplicate(db, bot, monkeypatch) -> None:
    """Stop durumu öğrenilemiyorsa (None) ikinci bir stop bırakılmaz."""
    _position(db, bot)
    broker = _Broker(qty=1.0, has_stop=None)
    _live(db, bot, broker, monkeypatch)

    reconcile.run(db)
    assert broker.stop_calls == 0


def test_report_is_serializable(db, bot) -> None:
    report = reconcile.run(db)
    assert isinstance(report.to_dict(), dict)
    assert "checked" in report.to_dict()
