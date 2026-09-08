"""
KATMAN 1 — PİYASA VERİSİ
========================
Canlı OHLCV mum verisi, anlık fiyat ve emir defteri derinliği.

Kaynaklar:
  * crypto → `ccxt` (100+ borsa: binance, bybit, okx, kucoin, mexc, ...)
  * stock  → `yfinance` (hisse, endeks, emtia, döviz)
  * demo   → İnternet/borsa erişimi olmadan test için deterministik simülasyon

Tasarım notları:
  - Genel (public) veri için API anahtarı GEREKMEZ. Anahtar yalnızca emir
    iletiminde (Katman 5) kullanılır.
  - Borsa nesneleri süreç içinde önbelleklenir (rate-limit dostu).
  - Tüm çağrılar senkron; FastAPI tarafında thread-pool içinde çalışır.
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from ..core.logging import get_logger

log = get_logger("zumvia.layer1")

_exchange_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()

# Ortak zaman dilimleri (arayüzde gösterilir)
TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

# yfinance eşlemesi
_YF_INTERVAL = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "60m", "4h": "1h", "1d": "1d",
}
_YF_PERIOD = {
    "1m": "7d", "5m": "60d", "15m": "60d", "30m": "60d",
    "1h": "730d", "4h": "730d", "1d": "5y",
}

# Kısa istekler (kotasyon, mini grafik) için yeterli en küçük pencere.
_SHORT_LIMIT = 120
_YF_SHORT_PERIOD = {
    "1m": "2d", "5m": "5d", "15m": "10d", "30m": "20d",
    "1h": "30d", "4h": "60d", "1d": "6mo",
}

POPULAR_CRYPTO = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USDT", "AVAX/USDT", "DOGE/USDT", "LINK/USDT", "TON/USDT",
]
POPULAR_STOCKS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
    "SPY", "QQQ", "GC=F", "THYAO.IS", "ASELS.IS",
]


class MarketDataError(RuntimeError):
    """Veri çekme hatası."""


@dataclass(slots=True)
class Quote:
    symbol: str
    price: float
    bid: float | None = None
    ask: float | None = None
    spread_pct: float | None = None
    ts: float = 0.0


# --------------------------------------------------------------------------- #
#  Borsa fabrikası
# --------------------------------------------------------------------------- #


def get_exchange(exchange_id: str, api_key: str | None = None,
                 secret: str | None = None, password: str | None = None,
                 sandbox: bool = False):
    """
    ccxt borsa nesnesi döner. Anahtarsız çağrılarda önbellek kullanılır;
    anahtarlı (özel) çağrılarda her seferinde yeni nesne üretilir.
    """
    try:
        import ccxt  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise MarketDataError(
            "ccxt kurulu değil. Kurulum:  pip install ccxt"
        ) from exc

    if not hasattr(ccxt, exchange_id):
        raise MarketDataError(f"Desteklenmeyen borsa: {exchange_id}")

    if api_key:
        klass = getattr(ccxt, exchange_id)
        ex = klass({
            "apiKey": api_key,
            "secret": secret or "",
            "password": password or "",
            "enableRateLimit": True,
            "timeout": 20000,
        })
        if sandbox:
            try:
                ex.set_sandbox_mode(True)
            except Exception:  # noqa: BLE001
                log.warning("%s testnet modunu desteklemiyor.", exchange_id)
        return ex

    with _cache_lock:
        if exchange_id not in _exchange_cache:
            _exchange_cache[exchange_id] = getattr(ccxt, exchange_id)(
                {"enableRateLimit": True, "timeout": 20000}
            )
        return _exchange_cache[exchange_id]


def list_exchanges() -> list[str]:
    """Kurulu ccxt sürümünün desteklediği borsalar."""
    try:
        import ccxt  # noqa: PLC0415
        return sorted(ccxt.exchanges)
    except ImportError:  # pragma: no cover
        return ["binance", "bybit", "okx", "kucoin", "mexc", "demo"]


# --------------------------------------------------------------------------- #
#  Demo (offline) veri üreteci
# --------------------------------------------------------------------------- #


def _demo_ohlcv(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    """
    Sembole göre tohumlanmış (deterministik) jump-diffusion fiyat serisi.
    Amaç: internet/borsa olmadan tüm boru hattını uçtan uca test edebilmek.
    """
    seed = int(hashlib.sha256(f"{symbol}{timeframe}".encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    step = minutes.get(timeframe, 60)

    base = 100.0 + (seed % 50_000) / 500.0
    drift, vol = 0.0002, 0.012
    shocks = rng.normal(drift, vol, limit)
    shocks += rng.choice([0.0, 0.0, 0.0, 0.05, -0.05], limit) * rng.random(limit) * 0.4
    closes = base * np.exp(np.cumsum(shocks))

    highs = closes * (1 + np.abs(rng.normal(0, 0.004, limit)))
    lows = closes * (1 - np.abs(rng.normal(0, 0.004, limit)))
    opens = np.concatenate([[closes[0]], closes[:-1]])
    volumes = np.abs(rng.normal(1_000, 260, limit)) * (1 + np.abs(shocks) * 25)

    end = pd.Timestamp.now(tz="UTC").floor("min")
    index = pd.date_range(end=end, periods=limit, freq=f"{step}min", tz="UTC")
    return pd.DataFrame(
        {"open": opens, "high": np.maximum(highs, np.maximum(opens, closes)),
         "low": np.minimum(lows, np.minimum(opens, closes)),
         "close": closes, "volume": volumes},
        index=index,
    )


# --------------------------------------------------------------------------- #
#  Genel API
# --------------------------------------------------------------------------- #


def fetch_ohlcv(market: str, exchange: str, symbol: str, timeframe: str = "1h",
                limit: int = 300) -> pd.DataFrame:
    """
    Zaman damgası indeksli OHLCV DataFrame döner.
    Kolonlar: open, high, low, close, volume
    """
    # Üst sınır neden 1000 değil? Borsalar TEK İSTEKTE en fazla ~1000 bar
    # verir; ama walk-forward doğrulaması için birkaç bin bar gerekir
    # (1000 bar → test penceresi ~79 bar → istatistiksel güven yok).
    # Bu yüzden `_fetch_crypto` sayfalama yapar.
    limit = max(60, min(limit, 20_000))

    if exchange == "demo" or market == "demo":
        return _demo_ohlcv(symbol, timeframe, limit)

    if market == "stock":
        return _fetch_stock(symbol, timeframe, limit)

    return _fetch_crypto(exchange, symbol, timeframe, limit)


PAGE_SIZE = 1000
MAX_PAGES = 20


def _timeframe_ms(timeframe: str) -> int:
    minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
               "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720,
               "1d": 1440, "1w": 10080}.get((timeframe or "1h").lower(), 60)
    return minutes * 60_000


def _fetch_crypto(exchange: str, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    """
    Mum verisi çeker; 1000'den fazlası isteniyorsa GERİYE DOĞRU sayfalar.

    Borsalar tek istekte ~1000 bar verir. Walk-forward doğrulaması bu kadarla
    anlamlı sonuç üretemez (test pencereleri çok kısa kalır), bu yüzden
    gereken kadar sayfa geriye gidilir. Sayfalama borsa hız sınırlarına
    saygılıdır: ccxt'nin kendi `rateLimit` beklemesi kullanılır.
    """
    ex = get_exchange(exchange)
    step = _timeframe_ms(timeframe)
    collected: list[list] = []
    since: int | None = None

    # Geriye doğru sayfalama: en yeni sayfadan başlayıp geçmişe gidilir.
    try:
        newest = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=min(limit, PAGE_SIZE))
    except Exception as exc:  # noqa: BLE001
        raise MarketDataError(f"{exchange} {symbol} verisi alınamadı: {exc}") from exc
    if not newest:
        raise MarketDataError(f"{exchange} {symbol} için mum verisi boş döndü.")

    collected = list(newest)
    pages = 1

    while len(collected) < limit and pages < MAX_PAGES:
        oldest_ts = collected[0][0]
        want = min(PAGE_SIZE, limit - len(collected))
        since = oldest_ts - want * step
        try:
            page = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=want)
        except Exception as exc:  # noqa: BLE001 — sayfalama hatası veriyi düşürmez
            log.info("%s %s sayfalama durdu: %s", exchange, symbol, exc)
            break
        page = [row for row in page if row[0] < oldest_ts]
        if not page:
            break                                  # borsada daha eski veri yok
        collected = page + collected
        pages += 1

    df = pd.DataFrame(collected, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="ts").set_index("ts").astype(float).sort_index()
    return df.tail(limit)


def _fetch_stock(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    try:
        import yfinance as yf  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise MarketDataError("yfinance kurulu değil. Kurulum: pip install yfinance") from exc

    interval = _YF_INTERVAL.get(timeframe, "60m")
    period = _YF_PERIOD.get(timeframe, "730d")

    # İhtiyaçtan fazlasını indirmek, ekranı bekletir.
    #
    # Ölçüldü: 40 günlük kotasyon için 5 YILLIK günlük veri iniyordu ve tek
    # hisse ~2 saniye sürüyordu. On dört hisselik bir tahta bu yüzden 40
    # saniyede açılıyordu. Geri testler hâlâ uzun geçmişi alır — yalnızca
    # KISA istekler kısa pencereye düşer.
    if limit <= _SHORT_LIMIT:
        period = _YF_SHORT_PERIOD.get(timeframe, period)
    try:
        raw = yf.download(
            symbol, period=period, interval=interval,
            progress=False, auto_adjust=False, threads=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise MarketDataError(f"yfinance {symbol} verisi alınamadı: {exc}") from exc

    if raw is None or raw.empty:
        raise MarketDataError(f"{symbol} için veri bulunamadı (sembolü kontrol edin).")

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
    if timeframe == "4h":  # yfinance 4h vermez → 1h barlardan yeniden örnekle
        df = df.resample("4h").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.tail(limit).astype(float)


def fetch_quote(market: str, exchange: str, symbol: str) -> Quote:
    """Anlık fiyat + spread (emir defteri en iyi seviyeleri)."""
    if exchange == "demo" or market == "demo":
        df = _demo_ohlcv(symbol, "1m", 60)
        price = float(df["close"].iloc[-1])
        return Quote(symbol, price, price * 0.9995, price * 1.0005, 0.1, time.time())

    if market == "stock":
        df = _fetch_stock(symbol, "1h", 5)
        price = float(df["close"].iloc[-1])
        return Quote(symbol, price, None, None, None, time.time())

    ex = get_exchange(exchange)
    try:
        t = ex.fetch_ticker(symbol)
    except Exception as exc:  # noqa: BLE001
        raise MarketDataError(f"{symbol} fiyatı alınamadı: {exc}") from exc

    price = float(t.get("last") or t.get("close") or 0.0)
    bid, ask = t.get("bid"), t.get("ask")
    spread = None
    if bid and ask and price:
        spread = round((float(ask) - float(bid)) / price * 100.0, 4)
    return Quote(symbol, price, bid, ask, spread, time.time())


def fetch_order_book_depth(market: str, exchange: str, symbol: str,
                           limit: int = 20) -> dict[str, Any]:
    """
    Emir defteri derinliği özeti — alış/satış baskısı ölçümü.
    Yapay zekaya likidite bağlamı olarak verilir.
    """
    if market != "crypto" or exchange == "demo":
        return {"available": False, "reason": "Bu piyasa için emir defteri yok."}

    try:
        ob = get_exchange(exchange).fetch_order_book(symbol, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("Emir defteri alınamadı (%s): %s", symbol, exc)
        return {"available": False, "reason": str(exc)[:120]}

    bids = ob.get("bids") or []
    asks = ob.get("asks") or []
    if not bids or not asks:
        return {"available": False, "reason": "Emir defteri boş."}

    bid_vol = float(sum(b[1] for b in bids[:limit]))
    ask_vol = float(sum(a[1] for a in asks[:limit]))
    total = bid_vol + ask_vol
    return {
        "available": True,
        "best_bid": float(bids[0][0]),
        "best_ask": float(asks[0][0]),
        "bid_volume": round(bid_vol, 4),
        "ask_volume": round(ask_vol, 4),
        "imbalance_pct": round((bid_vol - ask_vol) / total * 100.0, 2) if total else 0.0,
        "pressure": "ALIS_BASKISI" if bid_vol > ask_vol * 1.2
        else "SATIS_BASKISI" if ask_vol > bid_vol * 1.2 else "DENGELI",
    }


def search_symbols(market: str, exchange: str, query: str = "", limit: int = 40) -> list[str]:
    """Sembol arama — arayüzdeki otomatik tamamlama için."""
    query = (query or "").upper().strip()
    if market == "stock":
        pool = POPULAR_STOCKS
        return [s for s in pool if query in s][:limit] or ([query] if query else pool[:limit])

    if exchange == "demo":
        return [s for s in POPULAR_CRYPTO if query in s][:limit] or POPULAR_CRYPTO[:limit]

    try:
        ex = get_exchange(exchange)
        markets = ex.load_markets()
        symbols = [s for s in markets if markets[s].get("active", True)]
    except Exception as exc:  # noqa: BLE001
        log.warning("Market listesi alınamadı: %s", exc)
        symbols = POPULAR_CRYPTO

    matched = [s for s in symbols if query in s.upper()] if query else symbols
    matched.sort(key=lambda s: (not s.endswith("/USDT"), len(s)))
    return matched[:limit]
