"""
KATMAN 2 — DETERMİNİSTİK MATEMATİK MOTORU  (%0 Halüsinasyon)
============================================================
Bu modüldeki HİÇBİR sayı bir dil modeli tarafından üretilmez.
Tüm göstergeler saf `numpy` / `pandas` ile, kapalı formüllerle hesaplanır.
Yapay zekaya yalnızca burada üretilmiş kesin sayılar gönderilir.

Uygulanan göstergeler:
  - EMA / SMA
  - RSI (Wilder smoothing, 14)
  - MACD (12, 26, 9)
  - Bollinger Bands (20, 2σ)
  - ATR (Wilder, 14)
  - Supertrend (10, 3.0)
  - ADX / +DI / -DI (14)
  - Stochastic (14, 3)
  - OBV, hacim z-skoru
  - Swing tabanlı destek/direnç
  - Rejim sınıflandırıcı (trend / range / volatilite)

Not: pandas-ta kurulu ise sonuçlar aynıdır; bağımlılık zinciri kırılmasın diye
     formüller burada birebir uygulanmıştır (Wilder RMA dahil).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
#  Temel yardımcılar
# --------------------------------------------------------------------------- #


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length, min_periods=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def rma(series: pd.Series, length: int) -> pd.Series:
    """Wilder's smoothing (RSI/ATR/ADX için kullanılan üstel ortalama)."""
    return series.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


# --------------------------------------------------------------------------- #
#  Göstergeler
# --------------------------------------------------------------------------- #


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = rma(gain, length)
    avg_loss = rma(loss, length)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss == 0 ve kazanç varsa RSI 100 (kesintisiz yükseliş)
    saturated = pd.Series(
        np.where(avg_loss.eq(0.0) & avg_gain.gt(0.0), 100.0, np.nan), index=close.index
    )
    return out.fillna(saturated)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {"macd": macd_line, "macd_signal": signal_line, "macd_hist": macd_line - signal_line}
    )


def bollinger(close: pd.Series, length: int = 20, std: float = 2.0) -> pd.DataFrame:
    mid = sma(close, length)
    dev = close.rolling(length, min_periods=length).std(ddof=0)
    upper, lower = mid + std * dev, mid - std * dev
    width = (upper - lower) / mid.replace(0.0, np.nan) * 100.0
    pctb = (close - lower) / (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame(
        {"bb_upper": upper, "bb_mid": mid, "bb_lower": lower, "bb_width": width, "bb_pctb": pctb}
    )


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    return rma(true_range(high, low, close), length)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.DataFrame:
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    tr_n = rma(true_range(high, low, close), length)
    plus_di = 100.0 * rma(plus_dm, length) / tr_n.replace(0.0, np.nan)
    minus_di = 100.0 * rma(minus_dm, length) / tr_n.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return pd.DataFrame({"adx": rma(dx, length), "plus_di": plus_di, "minus_di": minus_di})


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series,
               k: int = 14, d: int = 3) -> pd.DataFrame:
    lowest = low.rolling(k, min_periods=k).min()
    highest = high.rolling(k, min_periods=k).max()
    stoch_k = 100.0 * (close - lowest) / (highest - lowest).replace(0.0, np.nan)
    return pd.DataFrame({"stoch_k": stoch_k, "stoch_d": stoch_k.rolling(d, min_periods=d).mean()})


def supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
               length: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """Supertrend — klasik ATR bantlı trend takip göstergesi."""
    atr_v = atr(high, low, close, length)
    hl2 = (high + low) / 2.0
    upper_basic = hl2 + multiplier * atr_v
    lower_basic = hl2 - multiplier * atr_v

    n = len(close)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    trend = np.full(n, np.nan)
    line = np.full(n, np.nan)

    c = close.to_numpy(dtype=float)
    ub = upper_basic.to_numpy(dtype=float)
    lb = lower_basic.to_numpy(dtype=float)

    start = int(np.argmax(~np.isnan(ub))) if np.any(~np.isnan(ub)) else n
    for i in range(start, n):
        if i == start or np.isnan(upper[i - 1]):
            upper[i], lower[i], trend[i] = ub[i], lb[i], 1.0
        else:
            upper[i] = ub[i] if (ub[i] < upper[i - 1] or c[i - 1] > upper[i - 1]) else upper[i - 1]
            lower[i] = lb[i] if (lb[i] > lower[i - 1] or c[i - 1] < lower[i - 1]) else lower[i - 1]
            if trend[i - 1] == 1.0:
                trend[i] = -1.0 if c[i] < lower[i] else 1.0
            else:
                trend[i] = 1.0 if c[i] > upper[i] else -1.0
        line[i] = lower[i] if trend[i] == 1.0 else upper[i]

    return pd.DataFrame(
        {"supertrend": line, "supertrend_dir": trend}, index=close.index
    )


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume).cumsum()


def swing_levels(high: pd.Series, low: pd.Series, left: int = 3, right: int = 3,
                 lookback: int = 120) -> tuple[list[float], list[float]]:
    """Fraktal swing high/low ile destek ve direnç seviyeleri (yakın → uzak)."""
    h = high.tail(lookback).to_numpy(dtype=float)
    l = low.tail(lookback).to_numpy(dtype=float)
    highs: list[float] = []
    lows: list[float] = []
    for i in range(left, len(h) - right):
        window_h = h[i - left: i + right + 1]
        window_l = l[i - left: i + right + 1]
        if h[i] == window_h.max() and (window_h == h[i]).sum() == 1:
            highs.append(float(h[i]))
        if l[i] == window_l.min() and (window_l == l[i]).sum() == 1:
            lows.append(float(l[i]))
    return lows[::-1], highs[::-1]


# --------------------------------------------------------------------------- #
#  Toplu hesaplama
# --------------------------------------------------------------------------- #

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV DataFrame üzerine tüm göstergeleri ekler (kopya döner)."""
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"OHLCV kolonları eksik: {missing}")

    out = df.copy()
    close, high, low, vol = out["close"], out["high"], out["low"], out["volume"]

    out["ema_20"] = ema(close, 20)
    out["ema_50"] = ema(close, 50)
    out["ema_200"] = ema(close, 200)
    out["sma_20"] = sma(close, 20)
    out["rsi_14"] = rsi(close, 14)
    out = out.join(macd(close))
    out = out.join(bollinger(close))
    out["atr_14"] = atr(high, low, close, 14)
    out["atr_pct"] = out["atr_14"] / close * 100.0
    out = out.join(adx(high, low, close))
    out = out.join(stochastic(high, low, close))
    out = out.join(supertrend(high, low, close))
    out["obv"] = obv(close, vol)
    vol_mean = vol.rolling(20, min_periods=5).mean()
    vol_std = vol.rolling(20, min_periods=5).std(ddof=0)
    out["volume_z"] = (vol - vol_mean) / vol_std.replace(0.0, np.nan)
    out["ret_1"] = close.pct_change() * 100.0
    out["ret_24"] = close.pct_change(24) * 100.0
    return out


# --------------------------------------------------------------------------- #
#  Yapay zekaya gönderilecek özet paket
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class MarketSnapshot:
    """Katman 3'e (LLM) verilecek KESİN sayısal fotoğraf."""

    symbol: str
    timeframe: str
    price: float
    indicators: dict[str, float]
    structure: dict[str, Any]
    regime: str
    support: list[float] = field(default_factory=list)
    resistance: list[float] = field(default_factory=list)
    recent_candles: list[dict[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "price": self.price,
            "regime": self.regime,
            "indicators": self.indicators,
            "structure": self.structure,
            "support": self.support,
            "resistance": self.resistance,
            "recent_candles": self.recent_candles,
        }


def _f(value: Any, digits: int = 6) -> float | None:
    """NaN/inf güvenli yuvarlama."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    return round(v, digits)


def classify_regime(row: pd.Series) -> str:
    """ADX + EMA dizilimi ile rejim sınıflandırması (deterministik)."""
    adx_v = _f(row.get("adx")) or 0.0
    ema50, ema200 = _f(row.get("ema_50")), _f(row.get("ema_200"))
    bbw = _f(row.get("bb_width")) or 0.0

    if adx_v >= 25 and ema50 and ema200:
        return "GÜÇLÜ_YÜKSELİŞ_TRENDİ" if ema50 > ema200 else "GÜÇLÜ_DÜŞÜŞ_TRENDİ"
    if adx_v < 18 and bbw < 4:
        return "SIKIŞMA_DÜŞÜK_VOLATİLİTE"
    if adx_v < 20:
        return "YATAY_RANGE"
    return "ZAYIF_TREND"


def build_snapshot(df: pd.DataFrame, symbol: str, timeframe: str,
                   candles: int = 12) -> MarketSnapshot:
    """Göstergeleri hesaplanmış DataFrame'den LLM paketini üretir."""
    enriched = compute_all(df) if "rsi_14" not in df.columns else df
    row = enriched.iloc[-1]
    prev = enriched.iloc[-2] if len(enriched) > 1 else row
    price = float(row["close"])

    indicators = {
        "rsi_14": _f(row.get("rsi_14"), 2),
        "ema_20": _f(row.get("ema_20"), 6),
        "ema_50": _f(row.get("ema_50"), 6),
        "ema_200": _f(row.get("ema_200"), 6),
        "macd": _f(row.get("macd"), 6),
        "macd_signal": _f(row.get("macd_signal"), 6),
        "macd_hist": _f(row.get("macd_hist"), 6),
        "bb_upper": _f(row.get("bb_upper"), 6),
        "bb_mid": _f(row.get("bb_mid"), 6),
        "bb_lower": _f(row.get("bb_lower"), 6),
        "bb_width_pct": _f(row.get("bb_width"), 2),
        "bb_percent_b": _f(row.get("bb_pctb"), 3),
        "atr_14": _f(row.get("atr_14"), 6),
        "atr_percent": _f(row.get("atr_pct"), 3),
        "adx_14": _f(row.get("adx"), 2),
        "plus_di": _f(row.get("plus_di"), 2),
        "minus_di": _f(row.get("minus_di"), 2),
        "stoch_k": _f(row.get("stoch_k"), 2),
        "stoch_d": _f(row.get("stoch_d"), 2),
        "supertrend": _f(row.get("supertrend"), 6),
        "volume_z_score": _f(row.get("volume_z"), 2),
        "change_1_candle_pct": _f(row.get("ret_1"), 3),
        "change_24_candle_pct": _f(row.get("ret_24"), 3),
    }

    st_dir = _f(row.get("supertrend_dir")) or 0.0
    ema50, ema200 = indicators["ema_50"], indicators["ema_200"]
    structure = {
        "supertrend_yon": "YUKARI" if st_dir > 0 else ("ASAGI" if st_dir < 0 else "BELIRSIZ"),
        "fiyat_ema200_ustunde": bool(ema200 and price > ema200),
        "golden_cross": bool(ema50 and ema200 and ema50 > ema200),
        "macd_kesisim": (
            "YUKARI_KESTI"
            if (_f(row.get("macd_hist")) or 0) > 0 >= (_f(prev.get("macd_hist")) or 0)
            else "ASAGI_KESTI"
            if (_f(row.get("macd_hist")) or 0) < 0 <= (_f(prev.get("macd_hist")) or 0)
            else "YOK"
        ),
        "rsi_bolge": (
            "ASIRI_ALIM" if (indicators["rsi_14"] or 50) >= 70
            else "ASIRI_SATIM" if (indicators["rsi_14"] or 50) <= 30
            else "NOTR"
        ),
        "hacim_anomalisi": bool((indicators["volume_z_score"] or 0) >= 2.0),
    }

    sup, res = swing_levels(enriched["high"], enriched["low"])
    support = [round(s, 6) for s in sup if s < price][:3]
    resistance = [round(r, 6) for r in res if r > price][:3]

    tail = enriched.tail(candles)
    recent = [
        {
            "t": str(idx),
            "o": _f(r["open"]), "h": _f(r["high"]),
            "l": _f(r["low"]), "c": _f(r["close"]), "v": _f(r["volume"], 2),
        }
        for idx, r in tail.iterrows()
    ]

    return MarketSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        price=round(price, 8),
        indicators={k: v for k, v in indicators.items() if v is not None},
        structure=structure,
        regime=classify_regime(row),
        support=support,
        resistance=resistance,
        recent_candles=recent,
    )


# --------------------------------------------------------------------------- #
#  Sembolik ön-filtre (LLM çağrılmadan önce çalışır — token tasarrufu + disiplin)
# --------------------------------------------------------------------------- #


def technical_bias(snapshot: MarketSnapshot) -> dict[str, Any]:
    """
    Saf kural tabanlı teknik skor (-100 .. +100).
    LLM'in kararı bu skorla çelişirse Katman 4 ek onay ister.
    """
    ind, st = snapshot.indicators, snapshot.structure
    score = 0
    reasons: list[str] = []

    if st.get("fiyat_ema200_ustunde"):
        score += 20; reasons.append("Fiyat EMA200 üzerinde (+20)")
    else:
        score -= 20; reasons.append("Fiyat EMA200 altında (-20)")

    if st.get("golden_cross"):
        score += 15; reasons.append("EMA50 > EMA200 (+15)")
    else:
        score -= 15; reasons.append("EMA50 < EMA200 (-15)")

    if st.get("supertrend_yon") == "YUKARI":
        score += 20; reasons.append("Supertrend yukarı (+20)")
    elif st.get("supertrend_yon") == "ASAGI":
        score -= 20; reasons.append("Supertrend aşağı (-20)")

    hist = ind.get("macd_hist", 0.0)
    if hist > 0:
        score += 10; reasons.append("MACD histogram pozitif (+10)")
    elif hist < 0:
        score -= 10; reasons.append("MACD histogram negatif (-10)")

    rsi_v = ind.get("rsi_14", 50.0)
    if rsi_v >= 70:
        score -= 10; reasons.append("RSI aşırı alım (-10)")
    elif rsi_v <= 30:
        score += 10; reasons.append("RSI aşırı satım (+10)")

    adx_v = ind.get("adx_14", 0.0)
    if adx_v >= 25:
        score += 15 if score > 0 else -15
        reasons.append(f"ADX {adx_v:.1f} trendi güçlendiriyor")

    score = int(max(-100, min(100, score)))
    label = "YUKARI" if score >= 25 else "ASAGI" if score <= -25 else "NOTR"
    return {"score": score, "label": label, "reasons": reasons}
