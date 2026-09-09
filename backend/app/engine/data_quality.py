"""
VERİ KALİTESİ — mum ve kotasyon bütünlük kontrolleri (saf Python, ağ yok)
=========================================================================
İlke: gecikmiş, boş, anormal veya doğrulanamayan fiyatla işlem/PnL UYDURULMAZ;
açık durum + gerekçe dönülür (`available` / `reason` / `code`).

Kontroller:
  * Mum: boşluk, eksik kolon, tz farkındalığı, sıralılık, tekrarlı zaman,
    OHLC tutarlılığı (high>=..., fiyat>0), boşluk/gap oranı, bayatlık
    (son bar yaşı eşiği).
  * Kotasyon: sonlu/pozitif fiyat, bayatlık, çapraz spread (bid>ask).

Eksik metrik UYDURULMAZ: yetersiz kanıt `available=False` ile döner.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pandas as pd

_TIMEFRAME_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720,
    "1d": 1440, "1w": 10080,
}

REQUIRED_COLS = ("open", "high", "low", "close", "volume")


def _now(now: datetime | None) -> datetime:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now


def check_candles(df: Any, timeframe: str = "1h",
                  now: datetime | None = None,
                  max_gap_pct: float = 5.0,
                  max_spike_pct: float = 25.0) -> dict[str, Any]:
    """OHLCV bütünlüğünü denetler. Asla istisna fırlatmaz; sözlük döner."""
    ts = _now(now)
    if df is None or (hasattr(df, "empty") and df.empty) or len(df) == 0:
        return {"available": False, "ok": False,
                "reason": "Mum verisi boş; değerlendirme yapılamaz.",
                "code": "EMPTY_DATA",
                "details": {"bars": 0, "timeframe": timeframe}}
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        return {"available": False, "ok": False,
                "reason": f"Eksik kolonlar: {', '.join(missing)}.",
                "code": "MISSING_COLUMNS",
                "details": {"bars": len(df), "missing": missing}}

    idx = df.index
    try:
        tz_aware = idx.tz is not None
    except Exception:  # noqa: BLE001
        tz_aware = False
    if not tz_aware:
        return {"available": False, "ok": False,
                "reason": "Zaman dilimi bilgisi yok (tz-naive indeks); saat dilimi bütünlüğü doğrulanamadı.",
                "code": "TZ_NAIVE",
                "details": {"bars": len(df), "timeframe": timeframe}}

    details: dict[str, Any] = {"bars": len(df), "timeframe": timeframe}
    try:
        times = idx.tz_convert("UTC")
    except Exception:  # noqa: BLE001
        return {"available": False, "ok": False,
                "reason": "Zaman damgaları UTC'ye çevrilemedi.",
                "code": "TZ_UNCONVERTIBLE", "details": details}

    if bool(times.duplicated().any()):
        details["duplicates"] = int(times.duplicated().sum())
        return {"available": False, "ok": False,
                "reason": f"Tekrarlı zaman damgası ({details['duplicates']} adet); mum bütünlüğü bozuk.",
                "code": "DUPLICATE_TS", "details": details}
    if not bool(times.is_monotonic_increasing):
        return {"available": False, "ok": False,
                "reason": "Mumlar kronolojik sıralı değil.",
                "code": "UNSORTED", "details": details}

    # OHLC tutarlılığı
    try:
        o = df["open"].to_numpy(dtype=float)
        h = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        c = df["close"].to_numpy(dtype=float)
    except Exception:  # noqa: BLE001
        return {"available": False, "ok": False,
                "reason": "OHLC kolonları sayıya çevrilemedi.",
                "code": "NON_NUMERIC", "details": details}
    if not all(bool(math.isfinite(float(v))) for v in (*o, *h, *low, *c)):
        return {"available": False, "ok": False,
                "reason": "OHLC içinde sonlu olmayan değer var.",
                "code": "NON_FINITE", "details": details}
    if bool((h <= 0).any() or (low <= 0).any() or (o <= 0).any() or (c <= 0).any()):
        return {"available": False, "ok": False,
                "reason": "Sıfır veya negatif fiyat var; anormal veri.",
                "code": "NON_POSITIVE_PRICE", "details": details}
    bad = int(((h < o) | (h < c) | (low > o) | (low > c) | (h < low)).sum())
    if bad:
        details["inconsistent_bars"] = bad
        return {"available": False, "ok": False,
                "reason": f"{bad} barda OHLC tutarsız (high<low vb.); anormal veri.",
                "code": "OHLC_INCONSISTENT", "details": details}

    # Ani sıçrama (spike) — tek bar kapanış değişimi eşiği
    step_min = _TIMEFRAME_MINUTES.get((timeframe or "1h").lower(), 60)
    if len(c) >= 2:
        prev = c[:-1]
        with pd.option_context("mode.chained_assignment", None):
            pass
        import numpy as np  # noqa: PLC0415

        denom = np.where(prev == 0, np.nan, prev)
        jumps = np.abs(np.diff(c) / denom) * 100.0
        jumps = jumps[np.isfinite(jumps)]
        if len(jumps):
            details["max_bar_jump_pct"] = round(float(np.max(jumps)), 2)
            if float(np.max(jumps)) > max_spike_pct:
                return {"available": False, "ok": False,
                        "reason": (f"Anormal fiyat sıçraması (%{details['max_bar_jump_pct']} "
                                   f"> %{max_spike_pct} eşiği); doğrulanamayan fiyatla işlem yok."),
                        "code": "PRICE_SPIKE", "details": details}

    # Boşluk (gap) oranı
    diffs_min = (times[1:] - times[:-1]).total_seconds() / 60.0
    expected = float(step_min)
    gaps = int((diffs_min > expected * 1.5).sum())
    details["expected_step_min"] = expected
    details["gap_bars"] = gaps
    details["gap_pct"] = round(gaps / max(len(df) - 1, 1) * 100.0, 2)
    if details["gap_pct"] > max_gap_pct:
        return {"available": False, "ok": False,
                "reason": (f"Veri boşluğu yüksek (%{details['gap_pct']} > %{max_gap_pct}); "
                           "survivorship/data-gap riski — değerlendirme yapılamaz."),
                "code": "DATA_GAP", "details": details}

    # Bayatlık (son bar yaşı)
    last = times[-1].to_pydatetime()
    age_min = (ts - last).total_seconds() / 60.0
    details["last_bar_utc"] = last.isoformat()
    details["last_bar_age_min"] = round(age_min, 1)
    if age_min > expected * 3 + 5:
        return {"available": False, "ok": False,
                "reason": (f"Veri bayat: son bar {details['last_bar_age_min']} dk eski "
                           f"(eşik ~{expected * 3 + 5:.0f} dk). Gecikmiş veriyle işlem yok."),
                "code": "STALE_DATA", "details": details}

    details["tz"] = "UTC"
    details["sorted"] = True
    return {"available": True, "ok": True,
            "reason": "Mum bütünlüğü doğrulandı (UTC, sıralı, tutarlı, boşluksuz).",
            "code": "OK", "details": details}


def check_quote(price: Any, *, bid: Any = None, ask: Any = None,
                ts: float | None = None, now_ts: float | None = None,
                max_age_s: float = 120.0) -> dict[str, Any]:
    """Anlık kotasyonu denetler; bozuk/bayat fiyatla PnL uydurulmaz."""
    import time as _time  # noqa: PLC0415

    now_v = now_ts if now_ts is not None else _time.time()
    try:
        p = float(price)
    except (TypeError, ValueError):
        return {"available": False, "ok": False,
                "reason": "Fiyat sayı değil; kotasyon doğrulanamadı.",
                "code": "QUOTE_NON_NUMERIC"}
    if not math.isfinite(p) or p <= 0:
        return {"available": False, "ok": False,
                "reason": f"Geçersiz fiyat ({price!r}); sıfır/negatif fiyatla işlem yok.",
                "code": "QUOTE_INVALID"}
    if ts is not None:
        try:
            age = float(now_v) - float(ts)
        except (TypeError, ValueError):
            age = None
        if age is not None and age > max_age_s:
            return {"available": False, "ok": False,
                    "reason": f"Kotasyon bayat ({age:.0f} sn > {max_age_s:.0f} sn eşiği).",
                    "code": "QUOTE_STALE", "details": {"age_s": round(age, 1)}}
    spread_pct = None
    try:
        if bid is not None and ask is not None:
            b, a = float(bid), float(ask)
            if math.isfinite(b) and math.isfinite(a) and b > 0 and a > 0:
                if b > a:
                    return {"available": False, "ok": False,
                            "reason": "Çapraz spread (bid>ask); emir defteri anormal.",
                            "code": "CROSSED_SPREAD"}
                spread_pct = round((a - b) / p * 100.0, 4)
    except (TypeError, ValueError):
        spread_pct = None
    return {"available": True, "ok": True, "reason": "Kotasyon geçerli.",
            "code": "OK", "details": {"price": p, "spread_pct": spread_pct}}


def sufficiency_gate(*, trade_count: int, oos_trades: int | None = None,
                     avg_spread_pct: float | None = None,
                     max_spread_pct: float = 0.5) -> dict[str, Any]:
    """
    Az işlemli / düşük likiditeli varlıklar dürüstçe "yetersiz kanıt" sayılır.
    Metrik yoksa uydurulmaz; `available=False` + gerekçe dönülür.
    """
    if (trade_count or 0) < 10:
        return {"available": False, "reason": ("Yetersiz örnek: yalnızca "
                f"{trade_count} işlem. İstatistiksel güven yok — kanıt YOK."),
                "code": "INSUFFICIENT_TRADES"}
    if oos_trades is not None and oos_trades < 12:
        return {"available": False, "reason": ("Görülmemiş veride yetersiz örnek "
                f"({oos_trades} işlem). Forward-kanıt YOK."),
                "code": "INSUFFICIENT_OOS"}
    if avg_spread_pct is not None and avg_spread_pct > max_spread_pct:
        return {"available": False, "reason": (f"Düşük likidite: ortalama spread "
                f"%{avg_spread_pct} > %{max_spread_pct} eşiği. Maliyet beklentiyi "
                "eritir — kanıt YOK."),
                "code": "ILLIQUID"}
    return {"available": True, "reason": "Örnek ve likidite yeterli.",
            "code": "OK"}
