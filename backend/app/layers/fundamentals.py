"""
ZUMVIA FINANCE — temel analiz katmanı
=====================================

Fiyatın NEDEN orada olduğunu soran katman: değerleme oranları, gelir
tablosu, bilanço, bilanço takvimi, temettü ve emsal karşılaştırma.

Kurallar (görev emrinden, kodun kendisi uygular):

  * Kaynak yalnızca `yfinance` — yeni ağır bağımlılık yok.
  * Kripto için temel analiz UYGULANAMAZ: sessizce boş değil, açıkça
    `available: False` + gerekçe döner.
  * Önbellek `finance_hub._CACHE` üzerinden çalışır; yeni önbellek sınıfı
    YOKTUR. Değerleme 15 dk, tablolar 12 saat yaşar.
  * Her kayıt `source` + `fetched_at` (UTC ISO) taşır — izlenebilirlik.
  * Ağ hatası veri yokluğu DEĞİLDİR: istisna sızdırılmaz, `available=False`
    + `reason` döner.
  * Veri eksikse alan `None` kalır; ASLA 0 yazılmaz. 0 gerçek bir değerdir
    ve karar katmanlarını yanıltır.
  * Tüm matematik burada (Python); LLM yalnızca yorumcudur (ADR-001).
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from ..core.logging import get_logger
from .finance_hub import _CACHE, Instrument

log = get_logger("zumvia.fundamentals")

# Önbellek pencereleri: değerleme hızlı eskir (fiyatla oynar), muhasebe
# tabloları çeyrekte bir değişir. İkisini aynı süreyle tutmak ya borsayı
# döver ya da eski fiyat gösterir — finance_hub'daki ayrımın aynısı.
VALUATION_TTL = 900.0
TABLES_TTL = 43200.0

SOURCE = "yfinance"


# --------------------------------------------------------------------------- #
#  Yardımcılar
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    """Kayıt zaman damgası: UTC ISO. İzlenebilirlik kuralı bunu ister."""
    return datetime.now(UTC).isoformat()


def _safe_float(value: Any) -> float | None:
    """
    Ölçülemeyen `None` kalır — 0 değil.

    yfinance boş hücreyi NaN/inf/"" olarak verebilir; hepsi "yok" demektir
    ve karar katmanına 0 diye YANSITILAMAZ.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _get_ticker(symbol: str):
    """
    Tek yfinance giriş noktası.

    Testler ağı KAPATMAK zorundadır; bu yüzden bütün yfinance erişimi
    buradan geçer ve testler bu fonksiyonu yamalar.
    """
    import yfinance as yf  # noqa: PLC0415

    return yf.Ticker(symbol)


def _get_info(ticker: Any) -> dict[str, Any]:
    """
    Ticker bilgi sözlüğünü okur.

    `.info` bazen boş dönerken `.get_info()` dolu döner (sürüm farkı).
    İkisi de denenir; ikisi de boşsa boş sözlük — istisna değil.
    """
    for attr in ("info",):
        try:
            info = getattr(ticker, attr, None)
            if callable(info):
                info = info()
            if isinstance(info, dict) and info:
                return dict(info)
        except Exception as exc:  # noqa: BLE001 — bilgi okunamadı, yok sayılır
            log.debug("ticker bilgisi okunamadı (%s): %s", attr, exc)
    try:
        getter = getattr(ticker, "get_info", None)
        if callable(getter):
            info = getter()
            if isinstance(info, dict) and info:
                return dict(info)
    except Exception as exc:  # noqa: BLE001
        log.debug("ticker get_info okunamadı: %s", exc)
    return {}


def _unavailable(inst: Instrument, reason: str) -> dict[str, Any]:
    """Ağ hatası ya da uygulanamazlık: istisna sızdırmadan raporlanır."""
    return {
        "symbol": inst.symbol,
        "available": False,
        "reason": reason,
        "source": SOURCE,
        "fetched_at": _now_iso(),
    }


def _is_crypto(inst: Instrument) -> bool:
    """Kriptonun bilançosu yoktur; sormak bile kategorik hatadır."""
    return (inst.market or "").lower() == "crypto"


def _cached_or_fetch(key: str, ttl: float, fetcher: Any) -> dict[str, Any]:
    """
    Önbellekli okuma: isabette ağa ÇIKILMAZ.

    Dönen sözlüğe `age_seconds` eklenir — finance_hub.tick deseninin aynısı:
    yanıt kaç saniyelik olduğunu kendi taşır.
    """
    hit = _CACHE.get(key, ttl)
    if hit is not None:
        cached, age = hit
        return {**cached, "age_seconds": round(age, 1)}
    fresh = fetcher()
    if isinstance(fresh, dict):
        _CACHE.put(key, fresh)
        return {**fresh, "age_seconds": 0.0}
    return fresh


# --------------------------------------------------------------------------- #
#  Değerleme fotoğrafı
# --------------------------------------------------------------------------- #

# yfinance anahtarları sürümler arasında oynar; her alan için bilinen
# adların TAMAMI denenir. İlk bulunan kazanır, hiçbiri yoksa None.
_INFO_KEYS: dict[str, tuple[str, ...]] = {
    "market_cap": ("marketCap",),
    "trailing_pe": ("trailingPE",),
    "forward_pe": ("forwardPE",),
    "price_to_book": ("priceToBook",),
    "ev_to_ebitda": ("enterpriseToEbitda",),
    "dividend_yield": ("dividendYield",),
    "eps_trailing": ("trailingEps",),
    "roe": ("returnOnEquity",),
    "debt_to_equity": ("debtToEquity",),
    "gross_margin": ("grossMargins",),
    "net_margin": ("profitMargins", "netMargins"),
    "payout_ratio": ("payoutRatio",),
    "sector": ("sector",),
    "long_name": ("longName", "shortName"),
}


def _pick(info: dict[str, Any], field: str) -> float | None:
    """Bir alanın bilinen adlarından ilk ölçüleni döndürür."""
    for key in _INFO_KEYS[field]:
        value = _safe_float(info.get(key))
        if value is not None:
            return value
    return None


def snapshot(inst: Instrument) -> dict[str, Any]:
    """
    Değerleme oranları fotoğrafı: piyasa değeri, F/K (trailing + forward),
    PD/DD, FD/FAVÖK, temettü verimi, HBK, ROE, borç/özsermaye, brüt ve net
    marj. Kripto için uygulanamaz döner.
    """
    if _is_crypto(inst):
        return _unavailable(inst, "Kripto varlıkların bilançosu yoktur; "
                                  "temel analiz hisse/ETF içindir.")

    def _fetch() -> dict[str, Any]:
        try:
            info = _get_info(_get_ticker(inst.symbol))
        except Exception as exc:  # noqa: BLE001 — ağ hatası veri yokluğu değil
            log.info("temel veri alınamadı (%s): %s", inst.symbol, str(exc)[:140])
            return _unavailable(inst, f"Veri alınamadı: {str(exc)[:160]}")
        if not info:
            return _unavailable(inst, "Bu sembol için temel veri bulunamadı.")
        payload: dict[str, Any] = {
            "symbol": inst.symbol,
            "available": True,
            "reason": "",
            "name": info.get("longName") or info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "currency": info.get("currency"),
            "market_cap": _pick(info, "market_cap"),
            "trailing_pe": _pick(info, "trailing_pe"),
            "forward_pe": _pick(info, "forward_pe"),
            "price_to_book": _pick(info, "price_to_book"),
            "ev_to_ebitda": _pick(info, "ev_to_ebitda"),
            "dividend_yield": _pick(info, "dividend_yield"),
            "eps_trailing": _pick(info, "eps_trailing"),
            "roe": _pick(info, "roe"),
            "debt_to_equity": _pick(info, "debt_to_equity"),
            "gross_margin": _pick(info, "gross_margin"),
            "net_margin": _pick(info, "net_margin"),
            "source": SOURCE,
            "fetched_at": _now_iso(),
        }
        return payload

    return _cached_or_fetch(f"fund:snapshot:{inst.symbol.upper()}", VALUATION_TTL, _fetch)


# --------------------------------------------------------------------------- #
#  Gelir tablosu ve bilanço (çeyreklik)
# --------------------------------------------------------------------------- #

# Satır adları yfinance sürümüne göre değişir; her kalem için bilinen
# adların tamamı denenir.
_INCOME_ROWS: dict[str, tuple[str, ...]] = {
    "revenue": ("Total Revenue", "TotalRevenue", "Revenue"),
    "gross_profit": ("Gross Profit", "GrossProfit"),
    "operating_income": ("Operating Income", "OperatingIncome"),
    "net_income": ("Net Income", "NetIncome"),
}

_BALANCE_ROWS: dict[str, tuple[str, ...]] = {
    "assets": ("Total Assets", "TotalAssets"),
    "liabilities": ("Total Liab", "TotalLiabilities", "Total Liab.", "Total Liabilities"),
    "equity": ("Total Stockholder Equity", "TotalStockholderEquity",
               "Stockholders Equity", "Total Equity"),
    "cash": ("Cash And Cash Equivalents", "CashAndCashEquivalents", "Cash"),
    "total_debt": ("Total Debt", "TotalDebt"),
}


def _frame_to_periods(frame: Any, row_map: dict[str, tuple[str, ...]],
                      periods: int) -> list[dict[str, Any]]:
    """
    Çeyreklik DataFrame'i dönem listesine çevirir.

    Kolonlar dönemdir (tarih), satırlar kalemdir. Eksik hücre None kalır;
    0 ile doldurmak muhasebe sahteciliğidir.
    """
    if frame is None or getattr(frame, "empty", True):
        return []
    try:
        columns = list(frame.columns)
    except Exception:  # noqa: BLE001
        return []
    # En güncel dönem önce: kolonları tarihe göre azalan sırala.
    try:
        columns = sorted(columns, key=str, reverse=True)[:max(1, periods)]
    except Exception:  # noqa: BLE001
        columns = columns[:max(1, periods)]

    out: list[dict[str, Any]] = []
    for col in columns:
        row: dict[str, Any] = {"period": str(col)[:10]}
        for field, names in row_map.items():
            value: float | None = None
            for name in names:
                try:
                    cell = frame.loc[name, col] if name in frame.index else None
                except Exception:  # noqa: BLE001
                    cell = None
                value = _safe_float(cell)
                if value is not None:
                    break
            row[field] = value
        out.append(row)
    return out


def _growth(current: float | None, previous: float | None) -> float | None:
    """
    Büyüme yüzdesi — tamamı Python'da, modele hesaplattırılmaz (ADR-001).

    Baz sıfırsa oran tanımsızdır: None döner, sonsuz YAZILMAZ.
    """
    if current is None or previous is None or previous == 0:
        return None
    try:
        return round((current - previous) / abs(previous) * 100.0, 2)
    except Exception:  # noqa: BLE001
        return None


def income_statement(inst: Instrument, periods: int = 4) -> dict[str, Any]:
    """
    Çeyreklik gelir tablosu: gelir, brüt kâr, faaliyet kârı, net kâr +
    çeyrek-üstü-çeyrek ve yıl-üstü-yıl büyüme yüzdeleri.
    """
    if _is_crypto(inst):
        return _unavailable(inst, "Kripto varlıkların gelir tablosu yoktur; "
                                  "temel analiz hisse/ETF içindir.")
    periods = max(1, min(int(periods or 4), 12))

    def _fetch() -> dict[str, Any]:
        try:
            ticker = _get_ticker(inst.symbol)
            frame = getattr(ticker, "quarterly_income_stmt", None)
            if callable(frame):
                frame = frame()
        except Exception as exc:  # noqa: BLE001
            log.info("gelir tablosu alınamadı (%s): %s", inst.symbol, str(exc)[:140])
            return _unavailable(inst, f"Veri alınamadı: {str(exc)[:160]}")
        rows = _frame_to_periods(frame, _INCOME_ROWS, periods)
        if not rows:
            return _unavailable(inst, "Bu sembol için gelir tablosu bulunamadı.")
        # Büyüme: QoQ bir önceki döneme, YoY dört dönem önceye göre.
        ordered = list(reversed(rows))  # eskiden yeniye
        by_period = {r["period"]: r for r in ordered}
        ordered_periods = [r["period"] for r in ordered]
        for row in rows:
            idx = ordered_periods.index(row["period"])
            prev = by_period.get(ordered_periods[idx - 1]) if idx > 0 else None
            yoy = by_period.get(ordered_periods[idx - 4]) if idx >= 4 else None
            row["revenue_qoq_pct"] = _growth(row.get("revenue"),
                                             prev.get("revenue") if prev else None)
            row["revenue_yoy_pct"] = _growth(row.get("revenue"),
                                             yoy.get("revenue") if yoy else None)
            row["net_qoq_pct"] = _growth(row.get("net_income"),
                                         prev.get("net_income") if prev else None)
            row["net_yoy_pct"] = _growth(row.get("net_income"),
                                         yoy.get("net_income") if yoy else None)
        return {
            "symbol": inst.symbol,
            "available": True,
            "reason": "",
            "periods": rows,
            "source": SOURCE,
            "fetched_at": _now_iso(),
        }

    return _cached_or_fetch(f"fund:income:{inst.symbol.upper()}:{periods}",
                            TABLES_TTL, _fetch)


def balance_sheet(inst: Instrument, periods: int = 4) -> dict[str, Any]:
    """Çeyreklik bilanço: varlık, borç, özsermaye, nakit, net borç."""
    if _is_crypto(inst):
        return _unavailable(inst, "Kripto varlıkların bilançosu yoktur; "
                                  "temel analiz hisse/ETF içindir.")
    periods = max(1, min(int(periods or 4), 12))

    def _fetch() -> dict[str, Any]:
        try:
            ticker = _get_ticker(inst.symbol)
            frame = getattr(ticker, "quarterly_balance_sheet", None)
            if callable(frame):
                frame = frame()
        except Exception as exc:  # noqa: BLE001
            log.info("bilanço alınamadı (%s): %s", inst.symbol, str(exc)[:140])
            return _unavailable(inst, f"Veri alınamadı: {str(exc)[:160]}")
        rows = _frame_to_periods(frame, _BALANCE_ROWS, periods)
        if not rows:
            return _unavailable(inst, "Bu sembol için bilanço bulunamadı.")
        for row in rows:
            debt, cash = row.get("total_debt"), row.get("cash")
            row["net_debt"] = (debt - cash) if (debt is not None
                                                and cash is not None) else None
            row.pop("total_debt", None)
        return {
            "symbol": inst.symbol,
            "available": True,
            "reason": "",
            "periods": rows,
            "source": SOURCE,
            "fetched_at": _now_iso(),
        }

    return _cached_or_fetch(f"fund:balance:{inst.symbol.upper()}:{periods}",
                            TABLES_TTL, _fetch)


# --------------------------------------------------------------------------- #
#  Bilanço takvimi
# --------------------------------------------------------------------------- #

def _surprise_pct(reported: float | None, expected: float | None) -> float | None:
    """EPS sürprizi: gerçekleşen vs beklenen. Baz sıfırsa tanımsızdır."""
    if reported is None or expected is None or expected == 0:
        return None
    try:
        return round((reported - expected) / abs(expected) * 100.0, 2)
    except Exception:  # noqa: BLE001
        return None


def earnings_calendar(inst: Instrument) -> dict[str, Any]:
    """
    Bir sonraki bilanço tarihi, beklenen EPS ve son 4 çeyreğin sürpriz
    yüzdesi (gerçekleşen vs beklenen).
    """
    if _is_crypto(inst):
        return _unavailable(inst, "Kripto varlıkların bilanço tarihi yoktur; "
                                  "temel analiz hisse/ETF içindir.")

    def _fetch() -> dict[str, Any]:
        try:
            ticker = _get_ticker(inst.symbol)
            calendar = getattr(ticker, "calendar", None)
            if callable(calendar):
                calendar = calendar()
            dates = getattr(ticker, "earnings_dates", None)
            if callable(dates):
                dates = dates()
        except Exception as exc:  # noqa: BLE001
            log.info("bilanço takvimi alınamadı (%s): %s", inst.symbol, str(exc)[:140])
            return _unavailable(inst, f"Veri alınamadı: {str(exc)[:160]}")

        next_date: str | None = None
        expected_eps: float | None = None
        # Takvim dict de DataFrame de olabilir; ikisi de okunur.
        try:
            if isinstance(calendar, dict):
                raw_date = calendar.get("Earnings Date") or calendar.get("EarningsDate")
                if isinstance(raw_date, (list, tuple)) and raw_date:
                    next_date = str(raw_date[0])[:10]
                elif raw_date is not None:
                    next_date = str(raw_date)[:10]
                expected_eps = _safe_float(calendar.get("EPS Estimate")
                                           or calendar.get("EpsEstimate"))
            elif calendar is not None and getattr(calendar, "empty", True) is False:
                first_col = calendar.columns[0] if len(calendar.columns) else None
                if first_col is not None and "Earnings Date" in calendar.index:
                    next_date = str(calendar.loc["Earnings Date", first_col])[:10]
                if "EPS Estimate" in getattr(calendar, "index", []):
                    expected_eps = _safe_float(calendar.loc["EPS Estimate", first_col])
        except Exception as exc:  # noqa: BLE001 — takvim bozuksa sürprizler yine okunur
            log.debug("takvim ayrıştırılamadı (%s): %s", inst.symbol, exc)

        surprises: list[dict[str, Any]] = []
        try:
            if dates is not None and getattr(dates, "empty", True) is False:
                # Kolon adları sürüme göre değişir; bilinen adlar aranır.
                cols = {str(c).lower(): c for c in dates.columns}
                rep_col = next((cols[k] for k in cols
                                if "report" in k), None)
                est_col = next((cols[k] for k in cols
                                if "estimat" in k), None)
                recent = dates.head(4)
                for idx, row in recent.iterrows():
                    reported = _safe_float(row[rep_col]) if rep_col is not None else None
                    expected = _safe_float(row[est_col]) if est_col is not None else None
                    surprises.append({
                        "period": str(idx)[:10],
                        "reported_eps": reported,
                        "expected_eps": expected,
                        "surprise_pct": _surprise_pct(reported, expected),
                    })
        except Exception as exc:  # noqa: BLE001
            log.debug("sürprizler ayrıştırılamadı (%s): %s", inst.symbol, exc)

        return {
            "symbol": inst.symbol,
            "available": True,
            "reason": "",
            "next_earnings_date": next_date,
            "expected_eps": expected_eps,
            "last_surprises": surprises,
            "source": SOURCE,
            "fetched_at": _now_iso(),
        }

    return _cached_or_fetch(f"fund:earnings:{inst.symbol.upper()}",
                            VALUATION_TTL, _fetch)


# --------------------------------------------------------------------------- #
#  Temettü
# --------------------------------------------------------------------------- #

def dividends(inst: Instrument) -> dict[str, Any]:
    """Son 8 ödeme, verim, ödeme oranı ve kesintisiz artış serisi."""
    if _is_crypto(inst):
        return _unavailable(inst, "Kripto varlıkların temettüsü yoktur; "
                                  "temel analiz hisse/ETF içindir.")

    def _fetch() -> dict[str, Any]:
        try:
            ticker = _get_ticker(inst.symbol)
            series = getattr(ticker, "dividends", None)
            if callable(series):
                series = series()
            info = _get_info(ticker)
        except Exception as exc:  # noqa: BLE001
            log.info("temettü alınamadı (%s): %s", inst.symbol, str(exc)[:140])
            return _unavailable(inst, f"Veri alınamadı: {str(exc)[:160]}")
        payments: list[dict[str, Any]] = []
        try:
            if series is not None and len(series):
                tail = series.tail(8)
                # items() yerine iterrows/zip: Series de DataFrame de olabilir.
                items = tail.items() if hasattr(tail, "items") else []
                for date, amount in items:
                    value = _safe_float(amount)
                    if value is None:
                        continue
                    payments.append({"date": str(date)[:10], "amount": value})
        except Exception as exc:  # noqa: BLE001
            log.debug("temettü serisi ayrıştırılamadı (%s): %s", inst.symbol, exc)

        # Kesintisiz artış serisi: yıllık toplamlar üzerinden geriye doğru
        # kaç yıl üst üste artış var. Yıl hesabı Python'da yapılır.
        streak = 0
        try:
            if series is not None and len(series):
                yearly = series.resample("YE").sum().dropna()
                totals = [float(v) for v in yearly.tolist()]
                for current, previous in zip(reversed(totals),
                                             reversed(totals[:-1]),
                                             strict=False):
                    if current > previous:
                        streak += 1
                    else:
                        break
        except Exception as exc:  # noqa: BLE001
            log.debug("artış serisi hesaplanamadı (%s): %s", inst.symbol, exc)

        return {
            "symbol": inst.symbol,
            "available": True,
            "reason": "",
            "last_payments": payments,
            "yield": _pick(info, "dividend_yield"),
            "payout_ratio": _pick(info, "payout_ratio"),
            "increase_streak_years": streak,
            "source": SOURCE,
            "fetched_at": _now_iso(),
        }

    return _cached_or_fetch(f"fund:dividends:{inst.symbol.upper()}",
                            TABLES_TTL, _fetch)


# --------------------------------------------------------------------------- #
#  Emsaller
# --------------------------------------------------------------------------- #

# Aynı sektörden karşılaştırılabilir şirketler. yfinance emsal listesi
# vermez; bu yüzden sektör başına likit ve bilinen şirketlerden sabit bir
# evren tutulur ve DEĞERLERİ yfinance'tan canlı okunur. Liste sabittir ama
# sayılar her zaman ölçülür — sabit olan evren, değişken olan veridir.
_SECTOR_PEERS: dict[str, tuple[str, ...]] = {
    "Technology": ("MSFT", "AAPL", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "ORCL"),
    "Financial Services": ("JPM", "BAC", "WFC", "GS", "MS", "C", "AXP", "BLK"),
    "Healthcare": ("JNJ", "LLY", "UNH", "PFE", "MRK", "ABBV", "TMO", "AMGN"),
    "Consumer Cyclical": ("TSLA", "AMZN", "HD", "MCD", "NKE", "SBUX", "LOW", "TJX"),
    "Energy": ("XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO"),
    "Industrials": ("CAT", "HON", "UNP", "BA", "GE", "MMM", "LMT", "DE"),
}
_DEFAULT_PEERS: tuple[str, ...] = ("AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
                                   "META", "TSLA", "AVGO")


def _peer_info(symbol: str) -> dict[str, Any]:
    """
    Tek emsalin bilgi sözlüğü. Ayrı fonksiyon ki testler ağı tek noktadan
    kapatabilsin (`_get_ticker` üzerinden de kapatılabilir).
    """
    try:
        return _get_info(_get_ticker(symbol))
    except Exception as exc:  # noqa: BLE001
        log.debug("emsal bilgi alınamadı (%s): %s", symbol, exc)
        return {}


def peers(inst: Instrument, limit: int = 8) -> dict[str, Any]:
    """
    Aynı sektörden karşılaştırılabilir şirketler ve onların F/K + büyüme
    değerleri (göreli ucuzluk/pahalılık için).
    """
    if _is_crypto(inst):
        return _unavailable(inst, "Kripto varlıkların sektör emsali yoktur; "
                                  "temel analiz hisse/ETF içindir.")
    limit = max(1, min(int(limit or 8), 12))

    def _fetch() -> dict[str, Any]:
        try:
            info = _get_info(_get_ticker(inst.symbol))
        except Exception as exc:  # noqa: BLE001
            log.info("emsal alınamadı (%s): %s", inst.symbol, str(exc)[:140])
            return _unavailable(inst, f"Veri alınamadı: {str(exc)[:160]}")
        sector = info.get("sector") if info else None
        universe = _SECTOR_PEERS.get(sector or "", _DEFAULT_PEERS)
        # Kendisi listeden çıkarılır; yoksa liste aynen kalır.
        candidates = [s for s in universe if s.upper() != inst.symbol.upper()]
        rows: list[dict[str, Any]] = []
        for symbol in candidates[:limit]:
            peer = _peer_info(symbol)
            if not peer:
                rows.append({"symbol": symbol, "trailing_pe": None,
                             "revenue_growth": None, "earnings_growth": None,
                             "reason": "Veri alınamadı."})
                continue
            rows.append({
                "symbol": symbol,
                "name": peer.get("longName") or peer.get("shortName"),
                "trailing_pe": _safe_float(peer.get("trailingPE")),
                "revenue_growth": _safe_float(peer.get("revenueGrowth")),
                "earnings_growth": _safe_float(peer.get("earningsGrowth")),
            })
        own_pe = _safe_float(info.get("trailingPE")) if info else None
        measured = [r["trailing_pe"] for r in rows if r.get("trailing_pe") is not None]
        median_pe: float | None = None
        if measured:
            ordered = sorted(measured)
            median_pe = ordered[len(ordered) // 2]
        cheaper: bool | None = None
        if own_pe is not None and median_pe is not None and median_pe != 0:
            cheaper = bool(own_pe < median_pe)
        return {
            "symbol": inst.symbol,
            "available": True,
            "reason": "",
            "sector": sector,
            "own_trailing_pe": own_pe,
            "median_peer_pe": median_pe,
            "cheaper_than_median": cheaper,
            "peers": rows,
            "source": SOURCE,
            "fetched_at": _now_iso(),
        }

    return _cached_or_fetch(f"fund:peers:{inst.symbol.upper()}:{limit}",
                            VALUATION_TTL, _fetch)
