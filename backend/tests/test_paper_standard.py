"""
PAPER STANDARDI — 100 USD sanal portföy kabul testleri
=======================================================
Gerçek borsa çağrısı YASAK: tüm mum/kotasyon sağlayıcıları fixture/mock ile
enjekte edilir. Ağ erişimi ayrıca `test_no_real_network_usage` içinde
doğrudan kilitlenir (ccxt/yfinance ithal edilemez hâle getirilir).

Sıra: başlat → strateji/ölçüm → paper emir → fee/slippage → stop/limit →
emergency/kill-switch → rapor.
"""
from __future__ import annotations

import sys
import time
from datetime import UTC, datetime

import pandas as pd
import pytest

from app.engine.paper_evaluation import (
    PAPER_START_BALANCE_USD,
    run_paper_evaluation,
)
from app.layers.l1_market_data import _demo_ohlcv

EXPECTED_ORDER = ["baslat", "piyasa_kapisi", "veri_kalite", "strateji_olcumu",
                  "yeterlilik", "paper_emir", "fee_slippage", "stop_limit",
                  "kill_switch"]

NOW = datetime.now(UTC)


def _quote_for(df: pd.DataFrame) -> dict:
    price = float(df["close"].iloc[-1])
    return {"price": price, "bid": price * 0.9999, "ask": price * 1.0001,
            "ts": time.time()}


def _rich_df(symbol: str = "BTC/USDT") -> pd.DataFrame:
    # 5000 bar: backtest>=10 ve OOS>=12 işlem üretir (deneyle doğrulandı).
    return _demo_ohlcv(symbol, "1h", 5000)


# --------------------------------------------------------------------------- #
#  1) Tam sıra — 100 USD paper
# --------------------------------------------------------------------------- #

def test_full_sequence_order_and_balances() -> None:
    df = _rich_df()
    report = run_paper_evaluation(
        market="crypto", symbol="BTC/USDT", timeframe="1h",
        starting_balance=100.0,
        fetch_ohlcv=lambda m, s, tf: df,
        fetch_quote=lambda m, s: _quote_for(df),
        now=NOW,
    )
    assert report["starting_balance"] == pytest.approx(100.0)
    assert report["starting_balance"] == pytest.approx(PAPER_START_BALANCE_USD)
    assert report["mode"] == "paper"
    names = [s["step"] for s in report["steps"]]
    assert names == EXPECTED_ORDER, names
    assert all(s["ok"] for s in report["steps"])
    assert report["status"] == "ok"
    assert report["available"] is True

    # Bakiye/pozisyon/PnL tutarlılığı: backtest final bakiyesi sayı ve sonlu.
    bt = report["backtest"]
    assert bt["initial_balance"] == pytest.approx(100.0)
    assert bt["final_balance"] == bt["final_balance"]  # NaN değil
    assert abs(bt["final_balance"]) < 1e12

    # Fee/slippage açık varsayım ve muhasebede.
    assert report["fee_pct"] > 0 and report["slippage_pct"] >= 0
    order = report["paper_order"]
    assert order["ok"] is True
    assert order["entry_fee"] > 0
    assert order["notional"] > 0
    assert order["notional"] <= 100.0 * 1.0001  # kaldıraçsız notional tavanı

    # Stop/limit bandı.
    assert 0.10 <= order["stop_pct"] <= 12.0
    assert order["rr"] >= 2.0

    # Rapor dürüstlük alanları.
    assert "gerçek kâr" in report["paper_notice"]
    assert "8 saat" in report["accelerated_note"]
    assert any("8 saat" in c for c in report["unverified_claims"])
    assert report["reason"] is not None


def test_no_real_network_usage(monkeypatch) -> None:
    """ccxt/yfinance patlarsa bile değerlendirme fixture ile tamamlanır."""
    monkeypatch.setitem(sys.modules, "ccxt", None)
    monkeypatch.setitem(sys.modules, "yfinance", None)
    df = _rich_df()
    report = run_paper_evaluation(
        market="crypto", symbol="BTC/USDT", timeframe="1h",
        fetch_ohlcv=lambda m, s, tf: df,
        fetch_quote=lambda m, s: _quote_for(df),
        now=NOW,
    )
    assert report["status"] == "ok"


# --------------------------------------------------------------------------- #
#  2) Veri kusurları — işlem/PnL uydurulmaz
# --------------------------------------------------------------------------- #

def test_stale_data_blocks_with_reason() -> None:
    df = _rich_df().copy()
    df.index = df.index - pd.Timedelta(days=30)  # son bar 30 gün eski
    report = run_paper_evaluation(
        market="crypto", symbol="BTC/USDT", timeframe="1h",
        fetch_ohlcv=lambda m, s, tf: df, now=NOW)
    assert report["available"] is False
    assert report["status"] == "data_unavailable"
    assert report["paper_order"] is None
    assert report["unavailability"]["code"] == "STALE_DATA"


def test_empty_data_blocks_with_reason() -> None:
    df = _rich_df().iloc[:0]
    report = run_paper_evaluation(
        market="crypto", symbol="BTC/USDT", timeframe="1h",
        fetch_ohlcv=lambda m, s, tf: df, now=NOW)
    assert report["available"] is False
    assert report["status"] == "data_unavailable"


def test_abnormal_ohlc_blocks_with_reason() -> None:
    df = _rich_df().copy()
    df.iloc[100, df.columns.get_loc("high")] = 1.0
    df.iloc[100, df.columns.get_loc("low")] = 999999.0  # high < low
    report = run_paper_evaluation(
        market="crypto", symbol="BTC/USDT", timeframe="1h",
        fetch_ohlcv=lambda m, s, tf: df, now=NOW)
    assert report["available"] is False
    assert report["unavailability"]["code"] == "OHLC_INCONSISTENT"


def test_data_gap_blocks_with_reason() -> None:
    df = _rich_df().iloc[::5]  # %80 bar kaybı
    report = run_paper_evaluation(
        market="crypto", symbol="BTC/USDT", timeframe="1h",
        fetch_ohlcv=lambda m, s, tf: df, now=NOW)
    assert report["available"] is False
    assert report["unavailability"]["code"] == "DATA_GAP"


def test_timezone_naive_rejected() -> None:
    from app.engine.data_quality import check_candles

    df = _rich_df().copy()
    df.index = df.index.tz_localize(None)
    result = check_candles(df, "1h", NOW)
    assert result["available"] is False
    assert result["code"] == "TZ_NAIVE"


def test_bad_quote_blocks_order() -> None:
    df = _rich_df()
    report = run_paper_evaluation(
        market="crypto", symbol="BTC/USDT", timeframe="1h",
        fetch_ohlcv=lambda m, s, tf: df,
        fetch_quote=lambda m, s: {"price": 0.0, "bid": None, "ask": None,
                                  "ts": time.time()},
        now=NOW)
    assert report["available"] is False
    step = next(s for s in report["steps"] if s["step"] == "paper_emir")
    assert step["ok"] is False
    assert step["data"].get("available") is False


# --------------------------------------------------------------------------- #
#  3) Yetersiz kanıt dürüstlüğü
# --------------------------------------------------------------------------- #

def test_insufficient_trades_are_not_fabricated() -> None:
    df = _demo_ohlcv("BTC/USDT", "1h", 300)  # walk-forward için çok kısa
    report = run_paper_evaluation(
        market="crypto", symbol="BTC/USDT", timeframe="1h",
        fetch_ohlcv=lambda m, s, tf: df, now=NOW)
    assert report["available"] is False
    assert report["status"] == "insufficient_evidence"
    assert report["paper_order"] is None  # emir uydurulmadı
    assert any("Yetersiz" in (s["detail"] or "") or s["data"].get("available") is False
               for s in report["steps"])


def test_sufficiency_gate_codes() -> None:
    from app.engine.data_quality import sufficiency_gate

    assert sufficiency_gate(trade_count=3)["code"] == "INSUFFICIENT_TRADES"
    assert sufficiency_gate(trade_count=50, oos_trades=2)["code"] == "INSUFFICIENT_OOS"
    assert sufficiency_gate(trade_count=50, avg_spread_pct=2.0)["code"] == "ILLIQUID"
    assert sufficiency_gate(trade_count=50, oos_trades=20)["available"] is True


# --------------------------------------------------------------------------- #
#  4) Piyasaya özgü kısıtlar
# --------------------------------------------------------------------------- #

def test_stock_closed_market_blocks() -> None:
    saturday = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)  # Cumartesi
    report = run_paper_evaluation(
        market="stock", symbol="AAPL", timeframe="1h",
        fetch_ohlcv=lambda m, s, tf: _rich_df(), now=saturday)
    assert report["status"] == "market_closed"
    assert report["available"] is False
    gate = next(s for s in report["steps"] if s["step"] == "piyasa_kapisi")
    assert gate["data"]["code"] == "MARKET_CLOSED"


def test_unsupported_assets_are_not_tradable() -> None:
    from app.layers.market_rules import assess

    assert assess("crypto", "BTCUSTI")["supported"] is False
    assert assess("crypto", "BTCUSTI")["tradable"] is False
    assert assess("forex", "BTC/USDT")["supported"] is False
    assert assess("forex", "US DOLLAR")["code"] == "UNSUPPORTED_SYMBOL"
    assert assess("forex", "USD/TRY")["supported"] is True
    assert assess("forex", "USDTRY=X")["supported"] is True
    assert assess("stock", "BTC/USDT")["supported"] is False
    assert assess("uzay", "BTC/USDT")["code"] == "UNSUPPORTED_MARKET"
    assert assess("crypto", "BTC/USDT")["code"] == "OK"


# --------------------------------------------------------------------------- #
#  5) Fee/slippage gerçekten uygulanıyor
# --------------------------------------------------------------------------- #

def test_fee_reduces_backtest_result() -> None:
    from app.engine.backtest import run_backtest

    df = _rich_df()
    free = run_backtest(df, initial_balance=100.0, fee_pct=0.0, slippage_pct=0.0)
    paid = run_backtest(df, initial_balance=100.0, fee_pct=1.0, slippage_pct=0.5)
    assert free.metrics.get("trade_count", 0) > 0
    assert paid.final_balance <= free.final_balance


def test_kill_switch_step_reported() -> None:
    from app.core import safety

    df = _rich_df()
    report = run_paper_evaluation(
        market="crypto", symbol="BTC/USDT", timeframe="1h",
        fetch_ohlcv=lambda m, s, tf: df,
        fetch_quote=lambda m, s: _quote_for(df),
        now=NOW)
    ks = next(s for s in report["steps"] if s["step"] == "kill_switch")
    assert ks["ok"] is True
    assert ks["data"]["kill_switch"] is bool(safety.kill_switch_active())
