"""
UÇUŞ ÖNCESİ KONTROL TESTLERİ

Canlı emirlerin çoğu piyasa yanlış gittiği için değil, borsanın kurallarına
uymadığı için reddedilir. Bu testler o kuralların emir gönderilmeden ÖNCE
yakalandığını doğrular — ilk canlı emir aynı zamanda ilk test olmasın diye.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.layers.preflight import check, dry_run


class FakeExchange:
    """ccxt benzeri kontrollü sahte borsa."""

    def __init__(self, market: dict[str, Any] | None = None,
                 step: float = 0.001, balance: float | None = None) -> None:
        self.markets = {"BTC/USDT": market} if market is not None else {}
        self.step = step
        self._balance = balance
        self.load_calls = 0

    def load_markets(self) -> None:
        self.load_calls += 1

    def amount_to_precision(self, symbol: str, qty: float) -> str:  # noqa: ARG002
        # Kayan nokta hatasını önlemek için tam sayı adımıyla yuvarla
        steps = round(qty / self.step + 1e-9)
        if qty < self.step:
            steps = int(qty / self.step)
        return f"{steps * self.step:.8f}"

    def fetch_balance(self) -> dict[str, Any]:
        if self._balance is None:
            raise RuntimeError("bakiye yok")
        return {"free": {"USDT": self._balance, "BTC": self._balance}}


def market(*, min_amount=0.0001, min_cost=10.0, max_amount=None,
           active=True) -> dict[str, Any]:
    return {
        "symbol": "BTC/USDT", "active": active,
        "limits": {"amount": {"min": min_amount, "max": max_amount},
                   "cost": {"min": min_cost}},
        "precision": {"amount": 3},
    }


# --------------------------------------------------------------------------- #
#  Geçerli emir
# --------------------------------------------------------------------------- #

def test_valid_order_passes() -> None:
    result = check(FakeExchange(market()), "BTC/USDT", "buy", 0.5, 50_000.0)
    assert result.ok is True
    assert result.code == "OK"
    assert result.qty == pytest.approx(0.5, rel=1e-6)


def test_quantity_is_rounded_to_exchange_precision() -> None:
    """Borsanın istemediği ondalık, emri reddettirir."""
    result = check(FakeExchange(market(), step=0.001),
                   "BTC/USDT", "buy", 0.123456789, 50_000.0)
    assert result.ok is True
    assert result.qty == pytest.approx(0.123, abs=1e-6)


# --------------------------------------------------------------------------- #
#  Reddedilen emirler — her biri gerçek bir borsa hatası
# --------------------------------------------------------------------------- #

def test_below_minimum_lot_is_rejected() -> None:
    result = check(FakeExchange(market(min_amount=1.0)),
                   "BTC/USDT", "buy", 0.5, 50_000.0)
    assert result.ok is False
    assert result.code == "BELOW_MIN_AMOUNT"
    assert "minimum lot" in result.reason


def test_below_minimum_notional_is_rejected() -> None:
    """Binance'te 10 USDT altı emirler reddedilir — önceden bilinmeli."""
    result = check(FakeExchange(market(min_amount=0.00001, min_cost=10.0),
                                step=0.00001),
                   "BTC/USDT", "buy", 0.0001, 50.0)     # 0.005 USDT
    assert result.ok is False
    assert result.code == "BELOW_MIN_NOTIONAL"


def test_quantity_rounding_to_zero_is_rejected() -> None:
    result = check(FakeExchange(market(), step=1.0),
                   "BTC/USDT", "buy", 0.4, 50_000.0)
    assert result.ok is False
    assert result.code == "QTY_ROUNDS_TO_ZERO"


def test_inactive_market_is_rejected() -> None:
    result = check(FakeExchange(market(active=False)),
                   "BTC/USDT", "buy", 1.0, 50_000.0)
    assert result.ok is False
    assert result.code == "MARKET_INACTIVE"


def test_insufficient_balance_is_rejected() -> None:
    result = check(FakeExchange(market()), "BTC/USDT", "buy", 1.0, 50_000.0,
                   free_balance=100.0)
    assert result.ok is False
    assert result.code == "INSUFFICIENT_BALANCE"
    assert "yetersiz" in result.reason


def test_invalid_input_is_rejected() -> None:
    assert check(FakeExchange(market()), "BTC/USDT", "buy", 0, 100).ok is False
    assert check(FakeExchange(market()), "BTC/USDT", "buy", 1, 0).ok is False


# --------------------------------------------------------------------------- #
#  Sınır davranışları
# --------------------------------------------------------------------------- #

def test_order_above_max_is_trimmed_not_rejected() -> None:
    """Tek emir tavanı aşılıyorsa emir küçültülür, iptal edilmez."""
    result = check(FakeExchange(market(max_amount=2.0)),
                   "BTC/USDT", "buy", 5.0, 50_000.0)
    assert result.ok is True
    assert result.qty == pytest.approx(2.0)
    assert any("tavan" in w for w in result.warnings)


def test_unknown_market_does_not_block_trading() -> None:
    """
    Borsa meta verisi vermiyorsa ticaret durmaz; yalnızca 'doğrulanamadı'
    uyarısı eklenir. Uydurma bir limit koymak, gerçek emirleri engellerdi.
    """
    result = check(FakeExchange(), "BTC/USDT", "buy", 1.0, 50_000.0)
    assert result.ok is True
    assert result.code == "UNVERIFIED"
    assert result.warnings


def test_market_load_is_attempted_once() -> None:
    exchange = FakeExchange()
    check(exchange, "BTC/USDT", "buy", 1.0, 50_000.0)
    assert exchange.load_calls == 1


def test_sell_side_checks_base_currency_balance() -> None:
    """Satışta gereken şey nakit değil, elde bulunan varlıktır."""
    result = check(FakeExchange(market()), "BTC/USDT", "sell", 1.0, 50_000.0,
                   free_balance=0.5)
    assert result.ok is False
    assert result.code == "INSUFFICIENT_BALANCE"


# --------------------------------------------------------------------------- #
#  Prova (dry run)
# --------------------------------------------------------------------------- #

def test_dry_run_sends_nothing_and_reports() -> None:
    report = dry_run(FakeExchange(market(), balance=1_000_000.0),
                     "BTC/USDT", "buy", 0.5, 50_000.0)
    assert report["gonderilmedi"] is True
    assert report["ok"] is True
    assert report["checks"]["notional"] == pytest.approx(25_000.0)


def test_dry_run_explains_rejection() -> None:
    report = dry_run(FakeExchange(market(min_cost=1_000_000.0), balance=10.0),
                     "BTC/USDT", "buy", 0.0001, 50.0)
    assert report["ok"] is False
    assert report["reason"]
    assert report["gonderilmedi"] is True


def test_dry_run_survives_balance_failure() -> None:
    """Bakiye okunamasa bile prova rapor üretmeli."""
    report = dry_run(FakeExchange(market()), "BTC/USDT", "buy", 0.5, 50_000.0)
    assert "ok" in report
