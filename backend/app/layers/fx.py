"""
ZUMVIA FINANCE — döviz çevrim katmanı
=====================================

Portföy tek para varsayamaz; bu YANLIŞTIR. Bu katman pozisyonları tek
para birimine çevirir. Kaynak yalnızca yfinance (`XXXYYY=X` pariteleri),
harici API anahtarı GEREKTİRMEZ.

Kurallar:

  * `rate()` 60 sn önbelleklidir (`finance_hub._CACHE`; yeni sınıf YOK).
  * Çevrilemeyen satır sessizce ATLANMAZ: `unconverted` listesinde
    gerekçesiyle raporlanır.
  * Kur alınamazsa istisna sızdırılmaz; çağıran katman gerekçeyi görür.
  * Tüm matematik Python'dadır (ADR-001).
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from ..core.logging import get_logger
from .finance_hub import _CACHE

log = get_logger("zumvia.fx")

# Kur hızlı eskir; 60 saniyelik pencere hem borsayı dövmez hem de portföy
# toplamını çürütmez.
FX_TTL = 60.0
SOURCE = "yfinance"


def _now_iso() -> str:
    """Kayıt zaman damgası: UTC ISO."""
    return datetime.now(UTC).isoformat()


def _norm(code: str) -> str:
    """Para kodu normalizasyonu: boşluk/küçük harf kabul edilir, sonuç büyük."""
    return (code or "").strip().upper()


def _fetch_rate_uncached(base: str, quote: str) -> float:
    """
    Tek kur ölçümü (önbelleksiz).

    Tek giriş noktası: testler ağı buradan kapatır. Önce doğrudan parite
    (`USDTRY=X`), olmazsa ters parite denenip TERSİ alınır (yfinance her
    yönü listelemez; örn. TRYUSD=X yoktur ama USDTRY=X vardır).
    """
    import yfinance as yf  # noqa: PLC0415

    base_n, quote_n = _norm(base), _norm(quote)
    if not base_n or not quote_n:
        raise ValueError("Para kodu boş.")
    if base_n == quote_n:
        return 1.0

    tried: list[str] = []
    direct = f"{base_n}{quote_n}=X"
    tried.append(direct)
    try:
        price = _ticker_close(yf.Ticker(direct))
        if price is not None:
            return price
    except Exception as exc:  # noqa: BLE001
        log.debug("doğrudan parite okunamadı (%s): %s", direct, exc)

    inverse = f"{quote_n}{base_n}=X"
    tried.append(inverse)
    try:
        price = _ticker_close(yf.Ticker(inverse))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Kur alınamadı ({'/'.join(tried)}): "
                           f"{str(exc)[:120]}") from exc
    if price is not None and price != 0:
        return 1.0 / price
    raise RuntimeError(f"Kur alınamadı ({'/'.join(tried)}): boş yanıt.")


def _ticker_close(ticker: Any) -> float | None:
    """
    Ticker'ın son kapanışını okur: önce hızlı bilgi, sonra kısa geçmiş.

    Hızlı bilgi yoksa 5 günlük geçmişe düşülür. İkisi de boşsa None —
    istisna DEĞİL (çağıran ters pariteyi deneyebilsin diye).
    """
    try:
        fast = getattr(ticker, "fast_info", None)
        if fast is not None:
            last = getattr(fast, "last_price", None)
            if last is not None and math.isfinite(float(last)) and float(last) > 0:
                return float(last)
    except Exception as exc:  # noqa: BLE001
        log.debug("hızlı fiyat okunamadı: %s", exc)
    try:
        frame = ticker.history(period="5d", interval="1d")
        if frame is None or getattr(frame, "empty", True):
            return None
        closes = frame.rename(columns=str.lower)["close"].dropna()
        if len(closes) == 0:
            return None
        value = float(closes.iloc[-1])
        return value if math.isfinite(value) and value > 0 else None
    except Exception as exc:  # noqa: BLE001
        log.debug("geçmiş kapanış okunamadı: %s", exc)
        return None


def rate(base: str, quote: str) -> float:
    """
    Anlık kur: 1 `base` kaç `quote` eder. 60 sn önbelleklidir.

    Aynı para için 1.0 döner (ağa ÇIKILMAZ). Kur alınamazsa ValueError
    yerine RuntimeError yükselir — çağıran bunu gerekçeye yazar.
    """
    base_n, quote_n = _norm(base), _norm(quote)
    if not base_n or not quote_n:
        raise ValueError("Para kodu boş.")
    if base_n == quote_n:
        return 1.0
    key = f"fx:{base_n}:{quote_n}"
    hit = _CACHE.get(key, FX_TTL)
    if hit is not None:
        return hit[0]
    fresh = _fetch_rate_uncached(base_n, quote_n)
    _CACHE.put(key, fresh)
    return fresh


def convert(amount: float, base: str, quote: str) -> float:
    """Tutarı hedef paraya çevirir. Sayı hesabı burada, modelde değil."""
    try:
        value = float(amount)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Tutar sayı değil: {amount!r}") from exc
    if not math.isfinite(value):
        raise ValueError("Tutar sonlu bir sayı olmalı.")
    return value * rate(base, quote)


def _row_amount(row: dict[str, Any]) -> tuple[float | None, str]:
    """
    Satırdan (tutar, para kodu) çıkarır.

    Satırlar farklı şekillerde gelebilir (portföy, pozisyon, elle liste);
    bilinen alan adlarının tamamı denenir. Bulunamazsa (None, sebep).
    """
    amount: float | None = None
    for key in ("amount", "value", "market_value", "notional", "total"):
        if row.get(key) is not None:
            try:
                number = float(row[key])
                if math.isfinite(number):
                    amount = number
                    break
            except (TypeError, ValueError):
                continue
    if amount is None:
        return None, "tutar alanı yok ya da sayı değil"
    currency = ""
    for key in ("currency", "ccy", "currency_code", "quote", "para_birimi"):
        if row.get(key):
            currency = _norm(str(row[key]))
            break
    if not currency:
        return None, "para kodu alanı yok"
    return amount, ""


def normalize_portfolio(rows: list[dict[str, Any]],
                        target_ccy: str) -> dict[str, Any]:
    """
    Pozisyon listesini tek para birimine çevirip toplam döner.

    Çevrilemeyen satır sessizce atlanmaz: `unconverted` listesinde
    gerekçesiyle raporlanır. Toplam SADECE çevrilebilenlerin toplamıdır
    ve bu açıkça yazılır — eksik satır varken toplamı "tam" gibi sunmak
    sessiz bir yalandır.
    """
    target = _norm(target_ccy)
    if not target:
        raise ValueError("Hedef para kodu boş.")
    if rows is None:
        rows = []

    converted: list[dict[str, Any]] = []
    unconverted: list[dict[str, Any]] = []
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            unconverted.append({"row": str(row)[:120],
                                "reason": "Satır sözlük değil."})
            continue
        amount, problem = _row_amount(row)
        if amount is None:
            unconverted.append({**row, "reason": problem})
            continue
        currency = ""
        for key in ("currency", "ccy", "currency_code", "quote", "para_birimi"):
            if row.get(key):
                currency = _norm(str(row[key]))
                break
        try:
            value = convert(amount, currency, target)
        except Exception as exc:  # noqa: BLE001 — satır raporlanır, tur ölmez
            unconverted.append({**row, "reason": f"Kur alınamadı: {str(exc)[:140]}"})
            continue
        total += value
        converted.append({**row, "target_ccy": target,
                          "converted_value": round(value, 2)})

    return {
        "target_ccy": target,
        "total": round(total, 2),
        "converted_count": len(converted),
        "unconverted_count": len(unconverted),
        "converted": converted,
        "unconverted": unconverted,
        "complete": not unconverted,
        "source": SOURCE,
        "fetched_at": _now_iso(),
        "not": ("Toplam yalnızca çevrilebilen satırları içerir. "
                "Çevrilemeyen varsa `complete` false olur."),
    }
