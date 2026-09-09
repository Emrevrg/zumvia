"""
PİYASA KURALLARI — piyasaya özgü kısıtlar (saf Python, ağ yok)
==============================================================
Amaç: desteklenmeyen varlığı sessizce "işlem yapılabilir" göstermemek.

  * Hisse: işlem saati dışında `tradable=False` (kod: MARKET_CLOSED).
    Tatil takvimi yerleşik değildir — bu dürüstçe `details` içinde yazılır.
  * Kripto: 7/24 `tradable` (saat kısıtı yok).
  * FX: parite formatı doğrulanır (ISO-4217 benzeri `AAA/BBB` veya
    yfinance `AAABBB=X`); bozuk format `UNSUPPORTED_SYMBOL` olur.
  * Bilinmeyen market/sembol: `supported=False`, gerekçeli.

Model/MCP/skill katmanları bu kapıyı atlayamaz: karar yine risk kalkanındadır;
bu modül yalnızca "bu piyasada şu an emir denemek anlamlı mı?" sorusunu
yanıtlar. Matematik ve veto Python'dadır (ADR-001).
"""
from __future__ import annotations

import re
from datetime import UTC, datetime

SUPPORTED_MARKETS = ("crypto", "stock", "forex", "demo")

_CRYPTO_RE = re.compile(r"^[A-Z0-9]{2,12}/[A-Z0-9]{2,12}$")
_FX_SLASH_RE = re.compile(r"^[A-Z]{3}/[A-Z]{3}$")
_FX_YF_RE = re.compile(r"^[A-Z]{6}=X$")
_STOCK_RE = re.compile(r"^[A-Z0-9.\-^]{1,16}$")

# Basitleştirilmiş seans saatleri (UTC). Gerçek tatil takvimi YOKTUR —
# bu, sonuçta açıkça belirtilir (dürüstlük: tatil günü "açık" görünebilir,
# bu yüzden kritik kararlar için borsa takvimine bakılmalıdır).
_NYSE_OPEN_H, NYSE_CLOSE_H = 14, 21   # ~09:30-16:00 ET (kış saati yaklaşıklığı)
_BIST_OPEN_H, BIST_CLOSE_H = 7, 15    # ~10:00-18:00 TSİ yaklaşıklığı


def _norm_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


def validate_symbol(market: str, symbol: str) -> dict:
    """Sembol formatını piyasaya göre doğrular. Ağ yok."""
    m = (market or "").strip().lower()
    s = _norm_symbol(symbol)
    if m not in SUPPORTED_MARKETS:
        return {"supported": False, "tradable": False,
                "reason": f"Desteklenmeyen piyasa: {market!r}.",
                "code": "UNSUPPORTED_MARKET"}
    if not s:
        return {"supported": False, "tradable": False,
                "reason": "Sembol boş.", "code": "UNSUPPORTED_SYMBOL"}
    if m == "demo":
        return {"supported": True, "tradable": True,
                "reason": "Demo sembol (test amaçlı).", "code": "OK"}
    if m == "crypto":
        if _CRYPTO_RE.match(s):
            return {"supported": True, "tradable": True,
                    "reason": "Kripto sembol formatı geçerli; piyasa 7/24 açıktır.",
                    "code": "OK"}
        return {"supported": False, "tradable": False,
                "reason": f"Kripto sembol formatı geçersiz: {symbol!r} (beklenen: BASE/QUOTE).",
                "code": "UNSUPPORTED_SYMBOL"}
    if m == "forex":
        if _FX_SLASH_RE.match(s) or _FX_YF_RE.match(s):
            base = s[:3] if s.endswith("=X") else s.split("/")[0]
            quote = s[3:6] if s.endswith("=X") else s.split("/")[1]
            if base == quote:
                return {"supported": False, "tradable": False,
                        "reason": "Paritenin iki ayağı aynı para olamaz.",
                        "code": "UNSUPPORTED_SYMBOL"}
            return {"supported": True, "tradable": True,
                    "reason": "FX parite formatı geçerli.", "code": "OK"}
        return {"supported": False, "tradable": False,
                "reason": f"FX parite formatı geçersiz: {symbol!r} (beklenen: AAA/BBB veya AAABBB=X).",
                "code": "UNSUPPORTED_SYMBOL"}
    # stock
    if "/" in s:
        return {"supported": False, "tradable": False,
                "reason": f"Hisse sembolünde '/' olamaz: {symbol!r}.",
                "code": "UNSUPPORTED_SYMBOL"}
    if _STOCK_RE.match(s):
        return {"supported": True, "tradable": True,
                "reason": "Hisse sembol formatı geçerli (seans kontrolü ayrıca yapılır).",
                "code": "OK_FORMAT"}
    return {"supported": False, "tradable": False,
            "reason": f"Hisse sembol formatı geçersiz: {symbol!r}.",
            "code": "UNSUPPORTED_SYMBOL"}


def stock_session(now: datetime | None = None) -> dict:
    """Basitleştirilmiş NYSE/BIST seans durumu. Ağ yok, tatil takvimi yok."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    weekday = now.weekday()  # 0=Pzt ... 6=Paz
    hour = now.hour + now.minute / 60.0
    weekend = weekday >= 5
    nyse_open = (not weekend) and (_NYSE_OPEN_H <= hour < NYSE_CLOSE_H)
    bist_open = (not weekend) and (_BIST_OPEN_H <= hour < BIST_CLOSE_H)
    return {
        "now_utc": now.isoformat(),
        "weekday": weekday,
        "nyse_open": nyse_open,
        "bist_open": bist_open,
        "holiday_calendar": "unavailable",
        "holiday_note": ("Resmi tatil takvimi yerleşik değildir; tatil günleri "
                         "'açık' görünebilir. Kritik karar öncesi borsa takvimine bakın."),
    }


def assess(market: str, symbol: str, now: datetime | None = None) -> dict:
    """
    Birleşik piyasa kapısı: format + seans. Dönen sözlükte her zaman
    `supported`, `tradable`, `reason`, `code` vardır.
    """
    fmt = validate_symbol(market, symbol)
    if not fmt["supported"]:
        return {**fmt, "market": (market or '').lower(), "symbol": _norm_symbol(symbol)}
    m = (market or "").lower()
    if m == "stock":
        sess = stock_session(now)
        is_thy = _norm_symbol(symbol).endswith(".IS")
        open_now = sess["bist_open"] if is_thy else sess["nyse_open"]
        venue = "BIST" if is_thy else "NYSE"
        if not open_now:
            return {"supported": True, "tradable": False, "market": m,
                    "symbol": _norm_symbol(symbol), "venue": venue,
                    "reason": (f"{venue} seansı kapalı (basitleştirilmiş UTC saati; "
                               "tatil takvimi yok). Piyasa kapalıyken paper emir "
                               "kapanış fiyatından varsayım üretmez."),
                    "code": "MARKET_CLOSED", "session": sess}
        return {"supported": True, "tradable": True, "market": m,
                "symbol": _norm_symbol(symbol), "venue": venue,
                "reason": f"{venue} seansı açık (basitleştirilmiş saat).",
                "code": "OK", "session": sess}
    if m == "crypto":
        return {"supported": True, "tradable": True, "market": m,
                "symbol": _norm_symbol(symbol), "venue": "spot",
                "reason": "Kripto 7/24 işlem görür; saat kısıtı yok.",
                "code": "OK"}
    if m == "forex":
        return {"supported": True, "tradable": True, "market": m,
                "symbol": _norm_symbol(symbol), "venue": "OTC",
                "reason": "FX formatı geçerli (hafta sonu likidite uyarısı ayrıca değerlendirilir).",
                "code": "OK"}
    return {"supported": True, "tradable": True, "market": m,
            "symbol": _norm_symbol(symbol),
            "reason": fmt["reason"], "code": "OK"}
