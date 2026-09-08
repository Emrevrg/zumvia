"""Katman 2 — Deterministik matematik motoru testleri."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.layers.l2_indicators import (
    atr,
    bollinger,
    build_snapshot,
    classify_regime,
    compute_all,
    ema,
    rsi,
    sma,
    supertrend,
    technical_bias,
)


@pytest.fixture()
def ohlcv() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, 400)))
    high = close * (1 + np.abs(rng.normal(0, 0.003, 400)))
    low = close * (1 - np.abs(rng.normal(0, 0.003, 400)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    volume = np.abs(rng.normal(1000, 200, 400))
    index = pd.date_range("2024-01-01", periods=400, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": np.maximum(high, np.maximum(open_, close)),
         "low": np.minimum(low, np.minimum(open_, close)),
         "close": close, "volume": volume}, index=index)


def test_sma_matches_manual_average(ohlcv: pd.DataFrame) -> None:
    value = sma(ohlcv["close"], 10).iloc[-1]
    assert value == pytest.approx(ohlcv["close"].iloc[-10:].mean())


def test_ema_first_value_is_sma_like(ohlcv: pd.DataFrame) -> None:
    series = ema(ohlcv["close"], 20)
    assert series.iloc[:19].isna().all()      # ısınma periyodu
    assert np.isfinite(series.iloc[-1])


def test_rsi_bounds_and_monotonic_series(ohlcv: pd.DataFrame) -> None:
    values = rsi(ohlcv["close"], 14).dropna()
    assert ((values >= 0) & (values <= 100)).all()

    # Kesintisiz yükselişte RSI 100'e doyar
    rising = pd.Series(np.arange(1, 60, dtype=float))
    assert rsi(rising, 14).iloc[-1] == pytest.approx(100.0)


def test_atr_is_positive(ohlcv: pd.DataFrame) -> None:
    values = atr(ohlcv["high"], ohlcv["low"], ohlcv["close"], 14).dropna()
    assert (values > 0).all()


def test_bollinger_band_ordering(ohlcv: pd.DataFrame) -> None:
    bands = bollinger(ohlcv["close"]).dropna()
    assert (bands["bb_upper"] >= bands["bb_mid"]).all()
    assert (bands["bb_mid"] >= bands["bb_lower"]).all()


def test_supertrend_direction_is_binary(ohlcv: pd.DataFrame) -> None:
    st = supertrend(ohlcv["high"], ohlcv["low"], ohlcv["close"]).dropna()
    assert set(np.unique(st["supertrend_dir"])) <= {-1.0, 1.0}


def test_compute_all_adds_every_column(ohlcv: pd.DataFrame) -> None:
    out = compute_all(ohlcv)
    for column in ("rsi_14", "ema_50", "ema_200", "macd", "bb_upper", "atr_14",
                   "adx", "stoch_k", "supertrend", "volume_z"):
        assert column in out.columns


def test_compute_all_rejects_missing_columns() -> None:
    with pytest.raises(ValueError, match="OHLCV"):
        compute_all(pd.DataFrame({"close": [1.0, 2.0]}))


def test_snapshot_contains_no_nan(ohlcv: pd.DataFrame) -> None:
    snap = build_snapshot(compute_all(ohlcv), "TEST/USDT", "1h")
    assert snap.price > 0
    for key, value in snap.indicators.items():
        assert np.isfinite(value), f"{key} sonlu değil"
    assert isinstance(snap.regime, str) and snap.regime


def test_technical_bias_range(ohlcv: pd.DataFrame) -> None:
    snap = build_snapshot(compute_all(ohlcv), "TEST/USDT", "1h")
    bias = technical_bias(snap)
    assert -100 <= bias["score"] <= 100
    assert bias["label"] in ("YUKARI", "ASAGI", "NOTR")


def test_regime_classifier_returns_known_label(ohlcv: pd.DataFrame) -> None:
    row = compute_all(ohlcv).iloc[-1]
    assert classify_regime(row) in {
        "GÜÇLÜ_YÜKSELİŞ_TRENDİ", "GÜÇLÜ_DÜŞÜŞ_TRENDİ",
        "SIKIŞMA_DÜŞÜK_VOLATİLİTE", "YATAY_RANGE", "ZAYIF_TREND",
    }
