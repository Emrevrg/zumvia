"""
ZUMVIA FINANCE — makro rejim katmanı
====================================

Piyasanın "rejimi": risk iştahı açık mı, kapalı mı, arada mı? Kaynak
yalnızca yfinance ticker'ları — harici API anahtarı GEREKTİRMEZ.

Kural: `regime()` saf fonksiyondur — emir yazmaz, DB yazmaz. Çıktısı
ileride bot kararlarını etkileyecek; o yüzden girdisi ölçülen veri,
hesabı deterministik Python'dur (ADR-001).

Eşikler modül tepesinde adlandırılmış sabittir; fonksiyon gövdesine
sihirli sayı GÖMÜLMEZ.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from ..core.logging import get_logger
from .finance_hub import _CACHE

log = get_logger("zumvia.macro")

# --------------------------------------------------------------------------- #
#  Göstergeler (yfinance sembolleri — anahtar gerektirmez)
# --------------------------------------------------------------------------- #

YIELD_SHORT_TICKER = "^IRX"   # politika faizi vekili (13 haftalık bono)
YIELD_MID_TICKER = "^FVX"     # orta vade (5 yıllık; 2y vekili olarak kullanılır)
YIELD_10Y_TICKER = "^TNX"     # 10 yıllık tahvil getirisi
YIELD_30Y_TICKER = "^TYX"     # 30 yıllık tahvil getirisi
DXY_TICKER = "DX-Y.NYB"       # dolar endeksi
VIX_TICKER = "^VIX"           # korku endeksi
GOLD_TICKER = "GC=F"          # altın (ons)
BRENT_TICKER = "BZ=F"         # brent petrol
COPPER_TICKER = "HG=F"        # bakır (büyüme metali)

MACRO_TICKERS: tuple[str, ...] = (
    YIELD_SHORT_TICKER, YIELD_MID_TICKER, YIELD_10Y_TICKER, YIELD_30Y_TICKER,
    DXY_TICKER, VIX_TICKER, GOLD_TICKER, BRENT_TICKER, COPPER_TICKER,
)

# --------------------------------------------------------------------------- #
#  Eşikler (adlandırılmış sabit — gövdeye sihirli sayı gömülmez)
# --------------------------------------------------------------------------- #

VIX_RISK_OFF = 25.0        # üstü: korku yüksek, riskten kaçış
VIX_RISK_ON = 18.0         # altı: korku düşük, risk iştahı
DXY_STRONG = 105.0         # üstü: güçlü dolar, riskli varlıklara ters rüzgâr
DXY_WEAK = 98.0            # altı: zayıf dolar, riskli varlıklara arka rüzgâr
CURVE_INVERSION = 0.0      # 10y-orta vade eğimi bunun altındaysa tersine dönmüş
POLICY_HIGH = 4.5          # politika vekili bunun üstündeyse para sıkı
BRENT_SHOCK_PCT = 3.0      # günlük brent sıçraması eşiği (petrol şoku)
COPPER_SLUMP_PCT = -2.0    # günlük bakır düşüşü eşiği (büyüme korkusu)
GOLD_FLIGHT_PCT = 2.0      # günlük altın sıçraması eşiği (güvenli liman akını)

MACRO_TTL = 300.0
SOURCE = "yfinance"


# --------------------------------------------------------------------------- #
#  Yardımcılar
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    """Kayıt zaman damgası: UTC ISO."""
    return datetime.now(UTC).isoformat()


def _safe_float(value: Any) -> float | None:
    """Ölçülemeyen `None` kalır — 0 gerçek bir değerdir, boşluk değil."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _fetch_prices() -> dict[str, dict[str, float | None]]:
    """
    Dokuz göstergeyi TEK yfinance çağrısında indirir.

    Tek giriş noktası: testler ağı buradan kapatır. Dönen sözlük
    `sembol -> {price, change_pct}` biçimindedir; ölçülemeyen None kalır.
    """
    import yfinance as yf  # noqa: PLC0415

    raw = yf.download(" ".join(MACRO_TICKERS), period="5d", interval="1d",
                      progress=False, auto_adjust=False, threads=False,
                      group_by="ticker")
    if raw is None or getattr(raw, "empty", True):
        raise RuntimeError("Makro veri indirilemedi (boş yanıt).")

    out: dict[str, dict[str, float | None]] = {}
    import pandas as pd  # noqa: PLC0415

    multi = isinstance(getattr(raw, "columns", None), pd.MultiIndex)
    for symbol in MACRO_TICKERS:
        try:
            frame = raw[symbol] if multi else raw
            closes = frame.rename(columns=str.lower)["close"].dropna()
            if len(closes) == 0:
                out[symbol] = {"price": None, "change_pct": None}
                continue
            last = float(closes.iloc[-1])
            change: float | None = None
            if len(closes) >= 2 and float(closes.iloc[-2]):
                prev = float(closes.iloc[-2])
                change = round((last - prev) / abs(prev) * 100.0, 2)
            out[symbol] = {"price": last, "change_pct": change}
        except Exception as exc:  # noqa: BLE001 — bir gösterge düşerse diğerleri yaşar
            log.debug("%s makro gösterge okunamadı: %s", symbol, exc)
            out[symbol] = {"price": None, "change_pct": None}
    return out


def _price(quotes: dict[str, dict[str, float | None]], symbol: str) -> float | None:
    """Gösterge fiyatını okur; yoksa None."""
    row = quotes.get(symbol) or {}
    value = row.get("price")
    return float(value) if isinstance(value, (int, float)) else None


def _change(quotes: dict[str, dict[str, float | None]], symbol: str) -> float | None:
    """Günlük değişim yüzdesini okur; yoksa None."""
    row = quotes.get(symbol) or {}
    value = row.get("change_pct")
    return float(value) if isinstance(value, (int, float)) else None


# --------------------------------------------------------------------------- #
#  Saf sınıflandırma (ağ YOK — testler eğriyi buradan sürer)
# --------------------------------------------------------------------------- #

def classify(quotes: dict[str, dict[str, float | None]]) -> dict[str, Any]:
    """
    Ölçülmüş göstergelerden tek kelimelik rejim üretir.

    Saf fonksiyondur: ağa çıkmaz, DB yazmaz, emir vermez. Aynı girdi her
    zaman aynı rejimi verir — bot kararları buna dayanacaksa şart budur.
    """
    reasons: list[str] = []
    votes_off = 0
    votes_on = 0

    short = _price(quotes, YIELD_SHORT_TICKER)
    mid = _price(quotes, YIELD_MID_TICKER)
    y10 = _price(quotes, YIELD_10Y_TICKER)
    y30 = _price(quotes, YIELD_30Y_TICKER)
    dxy = _price(quotes, DXY_TICKER)
    vix = _price(quotes, VIX_TICKER)
    brent_chg = _change(quotes, BRENT_TICKER)
    copper_chg = _change(quotes, COPPER_TICKER)
    gold_chg = _change(quotes, GOLD_TICKER)

    slope: float | None = None
    inverted = False
    if y10 is not None and mid is not None:
        slope = round(y10 - mid, 3)
        if slope < CURVE_INVERSION:
            inverted = True
            votes_off += 1
            reasons.append(f"Getiri eğrisi tersine döndü (10y-orta vade {slope:+.2f}).")
        else:
            reasons.append(f"Getiri eğrisi normal (10y-orta vade {slope:+.2f}).")

    if vix is not None:
        if vix >= VIX_RISK_OFF:
            votes_off += 1
            reasons.append(f"VIX {vix:.1f} ile korku eşiğinin ({VIX_RISK_OFF}) üstünde.")
        elif vix <= VIX_RISK_ON:
            votes_on += 1
            reasons.append(f"VIX {vix:.1f} ile sakin aralıkta ({VIX_RISK_ON} altı).")

    if dxy is not None:
        if dxy >= DXY_STRONG:
            votes_off += 1
            reasons.append(f"Dolar endeksi {dxy:.1f} ile güçlü ({DXY_STRONG} üstü).")
        elif dxy <= DXY_WEAK:
            votes_on += 1
            reasons.append(f"Dolar endeksi {dxy:.1f} ile zayıf ({DXY_WEAK} altı).")

    if short is not None and short >= POLICY_HIGH:
        votes_off += 1
        reasons.append(f"Politika faizi vekili %{short:.2f} ile sıkı ({POLICY_HIGH} üstü).")

    if brent_chg is not None and brent_chg >= BRENT_SHOCK_PCT:
        votes_off += 1
        reasons.append(f"Brent günlük %{brent_chg:+.1f} sıçradı (petrol şoku).")

    if copper_chg is not None and copper_chg <= COPPER_SLUMP_PCT:
        votes_off += 1
        reasons.append(f"Bakır günlük %{copper_chg:+.1f} düştü (büyüme korkusu).")

    if gold_chg is not None and gold_chg >= GOLD_FLIGHT_PCT:
        votes_off += 1
        reasons.append(f"Altın günlük %{gold_chg:+.1f} yükseldi (güvenli liman akını).")

    if votes_off > votes_on:
        regime = "risk_off"
    elif votes_on > votes_off:
        regime = "risk_on"
    else:
        regime = "belirsiz"
        reasons.append("Göstergeler karışık; tek yöne oy çıkmadı.")

    long_slope: float | None = None
    if y30 is not None and y10 is not None:
        long_slope = round(y30 - y10, 3)

    return {
        "regime": regime,
        "reasons": reasons,
        "votes": {"risk_off": votes_off, "risk_on": votes_on},
        "indicators": {
            "policy_proxy": short,
            "mid_yield": mid,
            "yield_10y": y10,
            "yield_30y": y30,
            "curve_slope_10y_mid": slope,
            "curve_inverted": inverted,
            "curve_slope_30y_10y": long_slope,
            "dxy": dxy,
            "vix": vix,
            "gold_chg_pct": gold_chg,
            "brent_chg_pct": brent_chg,
            "copper_chg_pct": copper_chg,
        },
    }


# --------------------------------------------------------------------------- #
#  Rejim (önbellekli)
# --------------------------------------------------------------------------- #

def regime() -> dict[str, Any]:
    """
    Piyasa rejimi: risk_on / risk_off / belirsiz + gerekçe listesi.

    Ağ hatası veri yokluğu DEĞİLDİR: istisna sızdırılmaz, `available=False`
    + `reason` döner.
    """
    hit = _CACHE.get("macro:regime", MACRO_TTL)
    if hit is not None:
        cached, age = hit
        return {**cached, "age_seconds": round(age, 1)}

    try:
        quotes = _fetch_prices()
    except Exception as exc:  # noqa: BLE001
        log.info("makro veri alınamadı: %s", str(exc)[:140])
        return {
            "available": False,
            "reason": f"Veri alınamadı: {str(exc)[:160]}",
            "source": SOURCE,
            "fetched_at": _now_iso(),
        }

    verdict = classify(quotes)
    payload = {
        "available": True,
        "reason": "",
        **verdict,
        "quotes": {symbol: quotes.get(symbol, {"price": None, "change_pct": None})
                   for symbol in MACRO_TICKERS},
        "source": SOURCE,
        "fetched_at": _now_iso(),
        "UYARI": ("Rejim etiketi bir GÖZLEMDİR, emir talimatı değildir. "
                  "Yatırım tavsiyesi değildir."),
    }
    _CACHE.put("macro:regime", payload)
    return {**payload, "age_seconds": 0.0}
