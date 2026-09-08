"""
KATMAN 2.5 — KLASİK ALGORİTMİK STRATEJİ MOTORU  (AI'sız da çalışır)
====================================================================
Bu motor tamamen deterministiktir: internet kesilse, API kotası bitse veya
yapay zeka yanıt vermese bile bot burada tanımlı kurallarla çalışmaya devam eder.

Her strateji `StrategySignal` üretir; `StrategyEngine` bunları ağırlıklı oyla
birleştirip tek bir konsensüs sinyali çıkarır. Yapay zeka (Katman 3) bu sinyali
girdi olarak okur; onaylar, reddeder veya seviyeleri iyileştirir.

Strateji seti (kurumsal literatürde en çok kullanılan, kanıtlanmış çekirdek):
  1. trend_following   — EMA50/EMA200 + Supertrend + ADX filtresi
  2. mean_reversion    — Bollinger alt/üst bant + RSI aşırılık (range rejimi)
  3. breakout          — Donchian(20) kırılımı + hacim teyidi
  4. momentum_macd     — MACD kesişimi + EMA200 yön filtresi
  5. vwap_reversion    — Seansiçi VWAP sapması (düşük zaman dilimleri)
  6. squeeze_expansion — Bollinger sıkışması sonrası patlama yönü
  7. pullback_ema      — Trendde EMA20/50 geri çekilme alımı (en yüksek isabet)
  8. rsi_divergence    — Fiyat/RSI uyumsuzluğu (dönüş erken uyarısı)

Her strateji stop-loss ve take-profit seviyesini ATR tabanlı üretir; böylece
Katman 4 (Risk Kalkanı) her zaman geçerli bir stop mesafesi bulur.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .l2_indicators import atr, compute_all

# --------------------------------------------------------------------------- #
#  Sinyal tipi
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class StrategySignal:
    name: str
    action: str                      # BUY | SELL | WAIT
    confidence: float                # 0.0 - 1.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    reason: str = ""
    weight: float = 1.0              # konsensüsteki ağırlık
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "action": self.action,
            "confidence": round(self.confidence, 3),
            "stop_loss": round(self.stop_loss, 8) if self.stop_loss else 0.0,
            "take_profit": round(self.take_profit, 8) if self.take_profit else 0.0,
            "reason": self.reason, "weight": self.weight,
        }


WAIT = "WAIT"
BUY = "BUY"
SELL = "SELL"


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if np.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _levels(price: float, atr_v: float, direction: str,
            sl_mult: float = 1.8, tp_mult: float = 4.0) -> tuple[float, float]:
    """ATR tabanlı stop/hedef. tp_mult/sl_mult >= 2 → R/R şartı doğal olarak sağlanır."""
    if atr_v <= 0:
        atr_v = price * 0.01
    if direction == BUY:
        return price - sl_mult * atr_v, price + tp_mult * atr_v
    return price + sl_mult * atr_v, price - tp_mult * atr_v


# --------------------------------------------------------------------------- #
#  Stratejiler
# --------------------------------------------------------------------------- #


def strat_trend_following(df: pd.DataFrame) -> StrategySignal:
    """Trend takibi: EMA dizilimi + Supertrend yönü + ADX gücü."""
    r, p = df.iloc[-1], df.iloc[-2]
    price, atr_v = _f(r["close"]), _f(r.get("atr_14"))
    ema50, ema200 = _f(r.get("ema_50")), _f(r.get("ema_200"))
    st_dir, adx_v = _f(r.get("supertrend_dir")), _f(r.get("adx"))
    prev_dir = _f(p.get("supertrend_dir"))

    if not (ema50 and ema200 and atr_v):
        return StrategySignal("trend_following", WAIT, 0.0, reason="Yetersiz veri")

    up = ema50 > ema200 and st_dir > 0 and price > ema200
    down = ema50 < ema200 and st_dir < 0 and price < ema200
    fresh = st_dir != prev_dir  # yeni dönüş barı

    if up and adx_v >= 20:
        conf = min(0.92, 0.60 + adx_v / 100 + (0.10 if fresh else 0.0))
        sl, tp = _levels(price, atr_v, BUY, 2.0, 4.5)
        return StrategySignal("trend_following", BUY, conf, sl, tp,
                              f"Yükseliş trendi: EMA50>EMA200, Supertrend yukarı, ADX {adx_v:.0f}",
                              weight=1.4)
    if down and adx_v >= 20:
        conf = min(0.92, 0.60 + adx_v / 100 + (0.10 if fresh else 0.0))
        sl, tp = _levels(price, atr_v, SELL, 2.0, 4.5)
        return StrategySignal("trend_following", SELL, conf, sl, tp,
                              f"Düşüş trendi: EMA50<EMA200, Supertrend aşağı, ADX {adx_v:.0f}",
                              weight=1.4)
    return StrategySignal("trend_following", WAIT, 0.0,
                          reason=f"Trend teyidi yok (ADX {adx_v:.0f})", weight=1.4)


def strat_mean_reversion(df: pd.DataFrame) -> StrategySignal:
    """Ortalamaya dönüş: yalnızca ADX düşükken (range rejimi) devreye girer."""
    r = df.iloc[-1]
    price, atr_v = _f(r["close"]), _f(r.get("atr_14"))
    rsi_v, adx_v = _f(r.get("rsi_14"), 50), _f(r.get("adx"))
    lower, upper, mid = _f(r.get("bb_lower")), _f(r.get("bb_upper")), _f(r.get("bb_mid"))

    if adx_v >= 22 or not (lower and upper):
        return StrategySignal("mean_reversion", WAIT, 0.0,
                              reason="Trendli piyasa — ortalamaya dönüş kapalı", weight=0.9)

    if price <= lower and rsi_v <= 32:
        conf = min(0.85, 0.55 + (32 - rsi_v) / 100)
        sl = price - 1.5 * (atr_v or price * 0.01)
        tp = mid if mid > price else price + 3 * (atr_v or price * 0.01)
        if (tp - price) < 2 * (price - sl):
            tp = price + 2.2 * (price - sl)
        return StrategySignal("mean_reversion", BUY, conf, sl, tp,
                              f"Alt bantta aşırı satım (RSI {rsi_v:.0f}), range rejimi", weight=0.9)

    if price >= upper and rsi_v >= 68:
        conf = min(0.85, 0.55 + (rsi_v - 68) / 100)
        sl = price + 1.5 * (atr_v or price * 0.01)
        tp = mid if mid < price else price - 3 * (atr_v or price * 0.01)
        if (price - tp) < 2 * (sl - price):
            tp = price - 2.2 * (sl - price)
        return StrategySignal("mean_reversion", SELL, conf, sl, tp,
                              f"Üst bantta aşırı alım (RSI {rsi_v:.0f}), range rejimi", weight=0.9)

    return StrategySignal("mean_reversion", WAIT, 0.0, reason="Bant içi", weight=0.9)


def strat_breakout(df: pd.DataFrame, lookback: int = 20) -> StrategySignal:
    """Donchian kırılımı + hacim teyidi."""
    if len(df) < lookback + 2:
        return StrategySignal("breakout", WAIT, 0.0, reason="Yetersiz veri", weight=1.2)

    r = df.iloc[-1]
    window = df.iloc[-(lookback + 1): -1]
    hh, ll = _f(window["high"].max()), _f(window["low"].min())
    price, atr_v = _f(r["close"]), _f(r.get("atr_14"))
    vz = _f(r.get("volume_z"))

    if price > hh and vz >= 0.8:
        conf = min(0.90, 0.62 + vz / 12)
        sl, _ = _levels(price, atr_v, BUY, 1.8, 4.0)
        sl = max(sl, hh * 0.998)  # kırılan direnç artık destek
        return StrategySignal("breakout", BUY, conf, sl, price + 2.5 * (price - sl),
                              f"{lookback} bar zirvesi kırıldı, hacim z={vz:.1f}", weight=1.2)

    if price < ll and vz >= 0.8:
        conf = min(0.90, 0.62 + vz / 12)
        sl, _tp = _levels(price, atr_v, SELL, 1.8, 4.0)
        sl = min(sl, ll * 1.002)
        return StrategySignal("breakout", SELL, conf, sl, price - 2.5 * (sl - price),
                              f"{lookback} bar dibi kırıldı, hacim z={vz:.1f}", weight=1.2)

    return StrategySignal("breakout", WAIT, 0.0, reason="Kırılım yok", weight=1.2)


def strat_momentum_macd(df: pd.DataFrame) -> StrategySignal:
    """MACD histogram kesişimi, EMA200 yön filtresiyle."""
    r, p = df.iloc[-1], df.iloc[-2]
    price, atr_v = _f(r["close"]), _f(r.get("atr_14"))
    h, hp = _f(r.get("macd_hist")), _f(p.get("macd_hist"))
    ema200 = _f(r.get("ema_200"))

    if not ema200:
        return StrategySignal("momentum_macd", WAIT, 0.0, reason="Yetersiz veri")

    if h > 0 >= hp and price > ema200:
        sl, tp = _levels(price, atr_v, BUY, 1.8, 4.0)
        return StrategySignal("momentum_macd", BUY, 0.72, sl, tp,
                              "MACD yukarı kesti ve fiyat EMA200 üzerinde")
    if h < 0 <= hp and price < ema200:
        sl, tp = _levels(price, atr_v, SELL, 1.8, 4.0)
        return StrategySignal("momentum_macd", SELL, 0.72, sl, tp,
                              "MACD aşağı kesti ve fiyat EMA200 altında")
    return StrategySignal("momentum_macd", WAIT, 0.0, reason="Kesişim yok")


def strat_pullback_ema(df: pd.DataFrame) -> StrategySignal:
    """Trend yönünde EMA20/EMA50 bölgesine geri çekilme — en yüksek isabetli kurulum."""
    r = df.iloc[-1]
    price, atr_v = _f(r["close"]), _f(r.get("atr_14"))
    ema20, ema50, ema200 = _f(r.get("ema_20")), _f(r.get("ema_50")), _f(r.get("ema_200"))
    rsi_v, adx_v = _f(r.get("rsi_14"), 50), _f(r.get("adx"))

    if not (ema20 and ema50 and ema200 and atr_v) or adx_v < 20:
        return StrategySignal("pullback_ema", WAIT, 0.0, reason="Trend zayıf", weight=1.3)

    near_zone = abs(price - ema20) <= 0.8 * atr_v or abs(price - ema50) <= 0.8 * atr_v

    if ema50 > ema200 and price > ema200 and near_zone and 40 <= rsi_v <= 60:
        sl = min(ema50, price) - 1.6 * atr_v
        return StrategySignal("pullback_ema", BUY, 0.80, sl, price + 2.4 * (price - sl),
                              "Yükseliş trendinde EMA geri çekilmesi (nötr RSI)", weight=1.3)

    if ema50 < ema200 and price < ema200 and near_zone and 40 <= rsi_v <= 60:
        sl = max(ema50, price) + 1.6 * atr_v
        return StrategySignal("pullback_ema", SELL, 0.80, sl, price - 2.4 * (sl - price),
                              "Düşüş trendinde EMA geri tepmesi (nötr RSI)", weight=1.3)

    return StrategySignal("pullback_ema", WAIT, 0.0, reason="Geri çekilme bölgesinde değil",
                          weight=1.3)


def strat_squeeze_expansion(df: pd.DataFrame) -> StrategySignal:
    """Bollinger sıkışması sonrası genişleme — patlama yönünde pozisyon."""
    if len(df) < 30:
        return StrategySignal("squeeze_expansion", WAIT, 0.0, reason="Yetersiz veri", weight=1.1)

    r = df.iloc[-1]
    width = df["bb_width"].tail(30)
    price, atr_v = _f(r["close"]), _f(r.get("atr_14"))
    now_w, min_w = _f(r.get("bb_width")), _f(width.min())
    upper, lower = _f(r.get("bb_upper")), _f(r.get("bb_lower"))
    was_squeezed = bool((width.iloc[:-1] <= min_w * 1.25).tail(6).any())

    if not (upper and lower and was_squeezed and now_w > min_w * 1.4):
        return StrategySignal("squeeze_expansion", WAIT, 0.0, reason="Sıkışma/patlama yok",
                              weight=1.1)

    if price > upper:
        sl, tp = _levels(price, atr_v, BUY, 1.6, 4.0)
        return StrategySignal("squeeze_expansion", BUY, 0.76, sl, tp,
                              "Sıkışma sonrası yukarı patlama", weight=1.1)
    if price < lower:
        sl, tp = _levels(price, atr_v, SELL, 1.6, 4.0)
        return StrategySignal("squeeze_expansion", SELL, 0.76, sl, tp,
                              "Sıkışma sonrası aşağı patlama", weight=1.1)
    return StrategySignal("squeeze_expansion", WAIT, 0.0, reason="Yön netleşmedi", weight=1.1)


def strat_vwap_reversion(df: pd.DataFrame) -> StrategySignal:
    """Seansiçi VWAP sapması — kısa zaman dilimlerinde etkili."""
    tail = df.tail(96)
    if len(tail) < 20:
        return StrategySignal("vwap_reversion", WAIT, 0.0, reason="Yetersiz veri", weight=0.8)

    tp_series = (tail["high"] + tail["low"] + tail["close"]) / 3
    vol = tail["volume"].replace(0, np.nan)
    vwap = float((tp_series * vol).sum() / vol.sum()) if vol.sum() else 0.0
    r = df.iloc[-1]
    price, atr_v = _f(r["close"]), _f(r.get("atr_14"))
    adx_v = _f(r.get("adx"))

    if not vwap or atr_v <= 0 or adx_v >= 25:
        return StrategySignal("vwap_reversion", WAIT, 0.0, reason="Uygun rejim değil", weight=0.8)

    dev = (price - vwap) / atr_v
    if dev <= -2.0:
        sl = price - 1.5 * atr_v
        return StrategySignal("vwap_reversion", BUY, 0.68, sl, vwap,
                              f"Fiyat VWAP altında {abs(dev):.1f} ATR", weight=0.8)
    if dev >= 2.0:
        sl = price + 1.5 * atr_v
        return StrategySignal("vwap_reversion", SELL, 0.68, sl, vwap,
                              f"Fiyat VWAP üstünde {dev:.1f} ATR", weight=0.8)
    return StrategySignal("vwap_reversion", WAIT, 0.0, reason="VWAP yakınında", weight=0.8)


def strat_rsi_divergence(df: pd.DataFrame, lookback: int = 40) -> StrategySignal:
    """Fiyat/RSI uyumsuzluğu — dönüş erken uyarısı."""
    if len(df) < lookback + 5:
        return StrategySignal("rsi_divergence", WAIT, 0.0, reason="Yetersiz veri", weight=0.9)

    seg = df.tail(lookback)
    price, atr_v = _f(df["close"].iloc[-1]), _f(df["atr_14"].iloc[-1])
    lows, highs, rsi_s = seg["low"], seg["high"], seg["rsi_14"]

    lo_idx = int(np.argmin(lows.to_numpy()))
    hi_idx = int(np.argmax(highs.to_numpy()))
    n = len(seg)

    # Boğa uyumsuzluğu: fiyat daha düşük dip, RSI daha yüksek dip
    if n - lo_idx <= 5 and lo_idx > 5:
        prior_low = int(np.argmin(lows.to_numpy()[: lo_idx - 2])) if lo_idx > 7 else None
        if prior_low is not None and lows.iloc[lo_idx] < lows.iloc[prior_low] \
                and _f(rsi_s.iloc[lo_idx]) > _f(rsi_s.iloc[prior_low]):
            sl = float(lows.iloc[lo_idx]) - 0.8 * (atr_v or price * 0.01)
            return StrategySignal("rsi_divergence", BUY, 0.70, sl, price + 2.3 * (price - sl),
                                  "Boğa uyumsuzluğu: dip düşerken RSI yükseliyor", weight=0.9)

    # Ayı uyumsuzluğu
    if n - hi_idx <= 5 and hi_idx > 5:
        prior_high = int(np.argmax(highs.to_numpy()[: hi_idx - 2])) if hi_idx > 7 else None
        if prior_high is not None and highs.iloc[hi_idx] > highs.iloc[prior_high] \
                and _f(rsi_s.iloc[hi_idx]) < _f(rsi_s.iloc[prior_high]):
            sl = float(highs.iloc[hi_idx]) + 0.8 * (atr_v or price * 0.01)
            return StrategySignal("rsi_divergence", SELL, 0.70, sl, price - 2.3 * (sl - price),
                                  "Ayı uyumsuzluğu: zirve yükselirken RSI düşüyor", weight=0.9)

    return StrategySignal("rsi_divergence", WAIT, 0.0, reason="Uyumsuzluk yok", weight=0.9)


# --------------------------------------------------------------------------- #
#  Kayıt defteri ve konsensüs motoru
# --------------------------------------------------------------------------- #

def strat_turtle_55(df: pd.DataFrame) -> StrategySignal:
    """
    Turtle (Dennis/Eckhardt) 55 barlık kırılım — piyasadaki en uzun ömürlü
    trend sistemi. 20 barlık kırılımdan daha seyrek ama daha güvenilir sinyal
    üretir; stop klasikteki gibi 2N (2 x ATR).
    """
    if len(df) < 60:
        return StrategySignal("turtle_55", WAIT, 0.0, reason="Yetersiz geçmiş", weight=1.3)

    r = df.iloc[-1]
    price, atr_v = _f(r["close"]), _f(r.get("atr_14"))
    window = df.iloc[-56:-1]
    hi_55, lo_55 = _f(window["high"].max()), _f(window["low"].min())
    ema200 = _f(r.get("ema_200"))
    vol_z = _f(r.get("volume_z"))

    if not (hi_55 and lo_55 and atr_v):
        return StrategySignal("turtle_55", WAIT, 0.0, reason="Yetersiz veri", weight=1.3)

    # Klasik Turtle stopu: 2N. Hedef 5N -> R/R ~ 1:2.5
    if price > hi_55:
        conf = min(0.90, 0.62 + min(vol_z, 2.5) * 0.05 + (0.06 if price > ema200 else 0.0))
        sl, tp = price - 2.0 * atr_v, price + 5.0 * atr_v
        return StrategySignal("turtle_55", BUY, conf, sl, tp,
                              f"55 bar zirvesi kırıldı ({hi_55:.4f}), 2N stop",
                              weight=1.3)
    if price < lo_55:
        conf = min(0.90, 0.62 + min(vol_z, 2.5) * 0.05 + (0.06 if price < ema200 else 0.0))
        sl, tp = price + 2.0 * atr_v, price - 5.0 * atr_v
        return StrategySignal("turtle_55", SELL, conf, sl, tp,
                              f"55 bar dibi kırıldı ({lo_55:.4f}), 2N stop",
                              weight=1.3)
    return StrategySignal("turtle_55", WAIT, 0.0, reason="55 bar kanalı içinde", weight=1.3)


def strat_chandelier_trend(df: pd.DataFrame) -> StrategySignal:
    """
    Chandelier Exit trend devamı (Chuck LeBeau). Trend yönünde kalındığı
    sürece pozisyonu taşır; stop, zirveden 3 x ATR aşağıdadır. Kırılımı
    kaçıranın trene sonradan bindiği, düşük riskli devam sistemidir.
    """
    if len(df) < 30:
        return StrategySignal("chandelier_trend", WAIT, 0.0, reason="Yetersiz geçmiş", weight=1.2)

    r = df.iloc[-1]
    price, atr_v = _f(r["close"]), _f(r.get("atr_14"))
    ema50, ema200 = _f(r.get("ema_50")), _f(r.get("ema_200"))
    adx_v = _f(r.get("adx"))
    hi_22 = _f(df["high"].iloc[-22:].max())
    lo_22 = _f(df["low"].iloc[-22:].min())

    if not (atr_v and ema50 and ema200 and hi_22 and lo_22):
        return StrategySignal("chandelier_trend", WAIT, 0.0, reason="Yetersiz veri", weight=1.2)

    long_stop = hi_22 - 3.0 * atr_v
    short_stop = lo_22 + 3.0 * atr_v

    if ema50 > ema200 and price > long_stop and adx_v >= 18:
        conf = min(0.88, 0.58 + adx_v / 130 + (0.05 if price > ema50 else 0.0))
        tp = price + max(2.05 * (price - long_stop), 3.5 * atr_v)
        return StrategySignal("chandelier_trend", BUY, conf, long_stop, tp,
                              f"Yükseliş trendi sürüyor, Chandelier stop {long_stop:.4f}",
                              weight=1.2)
    if ema50 < ema200 and price < short_stop and adx_v >= 18:
        conf = min(0.88, 0.58 + adx_v / 130 + (0.05 if price < ema50 else 0.0))
        tp = price - max(2.05 * (short_stop - price), 3.5 * atr_v)
        return StrategySignal("chandelier_trend", SELL, conf, short_stop, tp,
                              f"Düşüş trendi sürüyor, Chandelier stop {short_stop:.4f}",
                              weight=1.2)
    return StrategySignal("chandelier_trend", WAIT, 0.0,
                          reason="Trend teyidi yok veya stop kırıldı", weight=1.2)


def strat_stoch_pullback(df: pd.DataFrame) -> StrategySignal:
    """
    Trend yönünde stokastik geri çekilme (Lane). Ana trend yukarıyken
    stokastik aşırı satımdan yukarı keserse alır — "ucuzdan al, trendle git".
    Karşı trend işlemi ASLA açmaz.
    """
    if len(df) < 3:
        return StrategySignal("stoch_pullback", WAIT, 0.0, reason="Yetersiz geçmiş", weight=1.1)

    r, p = df.iloc[-1], df.iloc[-2]
    price, atr_v = _f(r["close"]), _f(r.get("atr_14"))
    ema50, ema200 = _f(r.get("ema_50")), _f(r.get("ema_200"))
    k, d = _f(r.get("stoch_k"), 50), _f(r.get("stoch_d"), 50)
    k_prev, d_prev = _f(p.get("stoch_k"), 50), _f(p.get("stoch_d"), 50)

    if not (atr_v and ema50 and ema200):
        return StrategySignal("stoch_pullback", WAIT, 0.0, reason="Yetersiz veri", weight=1.1)

    cross_up = k_prev <= d_prev and k > d and k_prev < 30
    cross_down = k_prev >= d_prev and k < d and k_prev > 70

    if ema50 > ema200 and price > ema200 and cross_up:
        conf = min(0.86, 0.58 + (30 - min(k_prev, 30)) / 120)
        sl, tp = _levels(price, atr_v, BUY, 1.6, 3.6)
        return StrategySignal("stoch_pullback", BUY, conf, sl, tp,
                              f"Yükseliş trendinde stokastik dönüşü (K {k:.0f})", weight=1.1)
    if ema50 < ema200 and price < ema200 and cross_down:
        conf = min(0.86, 0.58 + (max(k_prev, 70) - 70) / 120)
        sl, tp = _levels(price, atr_v, SELL, 1.6, 3.6)
        return StrategySignal("stoch_pullback", SELL, conf, sl, tp,
                              f"Düşüş trendinde stokastik dönüşü (K {k:.0f})", weight=1.1)
    return StrategySignal("stoch_pullback", WAIT, 0.0,
                          reason="Trend yönünde geri çekilme sinyali yok", weight=1.1)


def strat_obv_thrust(df: pd.DataFrame) -> StrategySignal:
    """
    Hacim öncülüğü (Granville OBV). Fiyat yatayken OBV yeni zirve yapıyorsa
    büyük alıcı gizlice topluyor demektir; fiyat genelde arkadan gelir.
    Hacim teyidi olmayan kırılımların tuzak olduğunu bilen sistemdir.
    """
    if len(df) < 40:
        return StrategySignal("obv_thrust", WAIT, 0.0, reason="Yetersiz geçmiş", weight=1.0)

    r = df.iloc[-1]
    price, atr_v = _f(r["close"]), _f(r.get("atr_14"))
    obv_now = _f(r.get("obv"))
    obv_win = df["obv"].iloc[-30:-1]
    price_win = df["close"].iloc[-30:-1]
    ema50 = _f(r.get("ema_50"))
    vol_z = _f(r.get("volume_z"))

    if not (atr_v and ema50) or obv_win.isna().all():
        return StrategySignal("obv_thrust", WAIT, 0.0, reason="Yetersiz veri", weight=1.0)

    obv_high, obv_low = _f(obv_win.max()), _f(obv_win.min())
    px_high, px_low = _f(price_win.max()), _f(price_win.min())

    # OBV yeni zirvede ama fiyat henüz zirveyi geçmemiş -> gizli birikim
    if obv_now > obv_high and price <= px_high and price > ema50 and vol_z > 0.3:
        conf = min(0.82, 0.56 + min(vol_z, 2.0) * 0.06)
        sl, tp = _levels(price, atr_v, BUY, 1.7, 3.8)
        return StrategySignal("obv_thrust", BUY, conf, sl, tp,
                              "OBV yeni zirvede, fiyat henüz teyit etmedi (birikim)", weight=1.0)
    if obv_now < obv_low and price >= px_low and price < ema50 and vol_z > 0.3:
        conf = min(0.82, 0.56 + min(vol_z, 2.0) * 0.06)
        sl, tp = _levels(price, atr_v, SELL, 1.7, 3.8)
        return StrategySignal("obv_thrust", SELL, conf, sl, tp,
                              "OBV yeni dipte, fiyat henüz teyit etmedi (dağıtım)", weight=1.0)
    return StrategySignal("obv_thrust", WAIT, 0.0, reason="Hacim öncülüğü yok", weight=1.0)


STRATEGIES: dict[str, Callable[[pd.DataFrame], StrategySignal]] = {
    "trend_following": strat_trend_following,
    "pullback_ema": strat_pullback_ema,
    "breakout": strat_breakout,
    "momentum_macd": strat_momentum_macd,
    "squeeze_expansion": strat_squeeze_expansion,
    "mean_reversion": strat_mean_reversion,
    "vwap_reversion": strat_vwap_reversion,
    "rsi_divergence": strat_rsi_divergence,
    "turtle_55": strat_turtle_55,
    "chandelier_trend": strat_chandelier_trend,
    "stoch_pullback": strat_stoch_pullback,
    "obv_thrust": strat_obv_thrust,
}

STRATEGY_LABELS = {
    "trend_following": "Trend Takibi (EMA + Supertrend + ADX)",
    "pullback_ema": "Trendde Geri Çekilme Alımı",
    "breakout": "Donchian Kırılımı + Hacim",
    "momentum_macd": "MACD Momentum Kesişimi",
    "squeeze_expansion": "Bollinger Sıkışma Patlaması",
    "mean_reversion": "Ortalamaya Dönüş (Range)",
    "vwap_reversion": "VWAP Sapması",
    "rsi_divergence": "RSI Uyumsuzluğu",
    "turtle_55": "Turtle 55 Bar Kırılımı",
    "chandelier_trend": "Chandelier Trend Devamı",
    "stoch_pullback": "Stokastik Geri Çekilme (trend yönü)",
    "obv_thrust": "OBV Hacim Öncülüğü",
}

DEFAULT_ACTIVE = ["trend_following", "pullback_ema", "breakout",
                  "momentum_macd", "squeeze_expansion", "mean_reversion",
                  "turtle_55", "chandelier_trend"]


@dataclass(slots=True)
class ConsensusResult:
    action: str
    confidence: float
    stop_loss: float
    take_profit: float
    score: float                     # -1.0 (güçlü satış) .. +1.0 (güçlü alış)
    agree: int
    total: int
    signals: list[StrategySignal] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "confidence": round(self.confidence, 3),
            "stop_loss": round(self.stop_loss, 8),
            "take_profit": round(self.take_profit, 8),
            "score": round(self.score, 3),
            "agree": self.agree,
            "total": self.total,
            "summary": self.summary,
            "signals": [s.to_dict() for s in self.signals],
        }


class StrategyEngine:
    """
    Seçili stratejileri çalıştırır ve ağırlıklı oyla konsensüs üretir.

    `min_agree`: bir yönde işlem açmak için gereken minimum onaylayan strateji
    sayısı. Tek bir stratejinin yanılması sistemi yanıltmasın diye vardır.
    """

    def __init__(self, active: list[str] | None = None, min_agree: int = 2) -> None:
        self.active = [s for s in (active or DEFAULT_ACTIVE) if s in STRATEGIES] or DEFAULT_ACTIVE
        self.min_agree = max(1, min_agree)

    def run(self, df: pd.DataFrame) -> ConsensusResult:
        enriched = df if "rsi_14" in df.columns else compute_all(df)
        if "atr_14" not in enriched.columns:
            enriched["atr_14"] = atr(enriched["high"], enriched["low"], enriched["close"])

        signals: list[StrategySignal] = []
        for name in self.active:
            try:
                signals.append(STRATEGIES[name](enriched))
            except Exception as exc:  # noqa: BLE001  — tek strateji hatası motoru durdurmaz
                signals.append(StrategySignal(name, WAIT, 0.0, reason=f"Hata: {exc}"))

        buy = [s for s in signals if s.action == BUY]
        sell = [s for s in signals if s.action == SELL]
        w_buy = sum(s.confidence * s.weight for s in buy)
        w_sell = sum(s.confidence * s.weight for s in sell)
        w_total = sum(s.weight for s in signals) or 1.0
        score = (w_buy - w_sell) / w_total

        price = float(enriched["close"].iloc[-1])
        atr_v = _f(enriched["atr_14"].iloc[-1], price * 0.01)

        def _confidence(winners: list[StrategySignal], w_win: float, w_lose: float) -> float:
            """
            Güven = onaylayanların ağırlıklı ortalama güveni
                    × (karşıt sinyal cezası)  +  mutabakat bonusu
            Sessiz kalan (WAIT) stratejiler cezalandırılmaz; yalnızca AKTİF
            karşıt sinyal güveni düşürür.
            """
            weight_sum = sum(s.weight for s in winners) or 1.0
            mean_conf = w_win / weight_sum
            opposition = w_lose / (w_win + w_lose) if (w_win + w_lose) > 0 else 0.0
            bonus = 0.04 * (len(winners) - 1)
            return float(max(0.0, min(0.97, mean_conf * (1.0 - 0.6 * opposition) + bonus)))

        if len(buy) >= self.min_agree and w_buy > w_sell:
            winners = sorted(buy, key=lambda s: -s.confidence * s.weight)
            sl = float(np.median([s.stop_loss for s in winners if s.stop_loss > 0] or
                                 [price - 1.8 * atr_v]))
            tp = float(np.median([s.take_profit for s in winners if s.take_profit > 0] or
                                 [price + 4.0 * atr_v]))
            if (tp - price) < 2 * (price - sl):
                tp = price + 2.05 * (price - sl)
            conf = _confidence(winners, w_buy, w_sell)
            action, agree = BUY, len(buy)
        elif len(sell) >= self.min_agree and w_sell > w_buy:
            winners = sorted(sell, key=lambda s: -s.confidence * s.weight)
            sl = float(np.median([s.stop_loss for s in winners if s.stop_loss > 0] or
                                 [price + 1.8 * atr_v]))
            tp = float(np.median([s.take_profit for s in winners if s.take_profit > 0] or
                                 [price - 4.0 * atr_v]))
            if (price - tp) < 2 * (sl - price):
                tp = price - 2.05 * (sl - price)
            conf = _confidence(winners, w_sell, w_buy)
            action, agree = SELL, len(sell)
        else:
            action, conf, sl, tp = WAIT, 0.0, 0.0, 0.0
            agree = max(len(buy), len(sell))

        summary = (
            f"{agree}/{len(signals)} strateji {action} yönünde "
            f"(skor {score:+.2f})" if action != WAIT
            else f"Konsensüs yok — {len(buy)} alış / {len(sell)} satış sinyali"
        )
        return ConsensusResult(action, conf, sl, tp, score, agree, len(signals), signals, summary)


def strategy_catalog() -> list[dict[str, str]]:
    """Arayüzdeki strateji seçici için katalog."""
    return [
        {"id": key, "label": STRATEGY_LABELS[key],
         "default": key in DEFAULT_ACTIVE}
        for key in STRATEGIES
    ]
