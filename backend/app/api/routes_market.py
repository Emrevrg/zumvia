"""
Piyasa Verisi, Analiz ve Geri Test Uçları
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..engine.backtest import run_backtest
from ..layers.l1_market_data import (
    POPULAR_CRYPTO,
    POPULAR_STOCKS,
    TIMEFRAMES,
    MarketDataError,
    fetch_ohlcv,
    fetch_order_book_depth,
    fetch_quote,
    list_exchanges,
    search_symbols,
)
from ..layers.l2_indicators import build_snapshot, compute_all, technical_bias
from ..layers.strategies import StrategyEngine
from ..models import User
from ..schemas import BacktestIn
from .deps import current_user

router = APIRouter(prefix="/api/market", tags=["Piyasa"])


@router.get("/meta")
def meta() -> dict:
    """Borsalar, popüler semboller ve zaman dilimleri."""
    return {
        "exchanges": list_exchanges()[:80],
        "timeframes": TIMEFRAMES,
        "popular": {"crypto": POPULAR_CRYPTO, "stock": POPULAR_STOCKS},
    }


@router.get("/symbols")
def symbols(market: str = "crypto", exchange: str = "binance",
            q: str = "", _: User = Depends(current_user)) -> list[str]:
    return search_symbols(market, exchange, q)


@router.get("/candles")
def candles(market: str = "crypto", exchange: str = "binance",
            symbol: str = "BTC/USDT", timeframe: str = "1h",
            limit: int = Query(default=300, ge=60, le=1000),
            _: User = Depends(current_user)) -> dict:
    """Grafik için OHLCV + temel gösterge serileri."""
    try:
        df = compute_all(fetch_ohlcv(market, exchange, symbol, timeframe, limit))
    except MarketDataError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    def series(column: str) -> list[dict[str, Any]]:
        sub = df[column].dropna()
        return [{"time": int(ts.timestamp()), "value": round(float(v), 8)}
                for ts, v in sub.items()]

    ohlc = [
        {"time": int(ts.timestamp()), "open": float(r["open"]), "high": float(r["high"]),
         "low": float(r["low"]), "close": float(r["close"])}
        for ts, r in df.iterrows()
    ]
    volume = [
        {"time": int(ts.timestamp()), "value": float(r["volume"]),
         "color": "rgba(0,230,118,0.45)" if r["close"] >= r["open"]
         else "rgba(239,83,80,0.45)"}
        for ts, r in df.iterrows()
    ]
    return {
        "symbol": symbol, "timeframe": timeframe,
        "candles": ohlc, "volume": volume,
        "ema_50": series("ema_50"), "ema_200": series("ema_200"),
        "bb_upper": series("bb_upper"), "bb_lower": series("bb_lower"),
        "supertrend": series("supertrend"),
        "rsi": series("rsi_14"),
    }


@router.get("/quote")
def quote(market: str = "crypto", exchange: str = "binance", symbol: str = "BTC/USDT",
          _: User = Depends(current_user)) -> dict:
    try:
        q = fetch_quote(market, exchange, symbol)
    except MarketDataError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {"symbol": q.symbol, "price": q.price, "bid": q.bid, "ask": q.ask,
            "spread_pct": q.spread_pct}


@router.get("/analyze")
def analyze(market: str = "crypto", exchange: str = "binance",
            symbol: str = "BTC/USDT", timeframe: str = "1h",
            _: User = Depends(current_user)) -> dict:
    """
    Katman 2 + Katman 2.5 çıktısı: göstergeler, rejim, teknik skor ve
    strateji konsensüsü. Yapay zeka çağrılmaz — anında ve ücretsizdir.
    """
    try:
        df = compute_all(fetch_ohlcv(market, exchange, symbol, timeframe, 320))
    except MarketDataError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    snap = build_snapshot(df, symbol, timeframe)
    consensus = StrategyEngine().run(df)
    depth = fetch_order_book_depth(market, exchange, symbol)
    return {
        "snapshot": snap.to_dict(),
        "technical_bias": technical_bias(snap),
        "consensus": consensus.to_dict(),
        "order_book": depth,
    }


@router.post("/backtest")
def backtest(payload: BacktestIn, _: User = Depends(current_user)) -> dict:
    """Strateji setini geçmiş veride test eder ve canlıya hazırlık kararı verir."""
    try:
        df = fetch_ohlcv(payload.market, payload.exchange, payload.symbol,
                         payload.timeframe, payload.candles)
    except MarketDataError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    report = run_backtest(
        df,
        initial_balance=payload.initial_balance,
        risk_pct=payload.risk_pct,
        min_rr=payload.min_rr,
        min_confidence=payload.min_confidence,
        allow_short=payload.allow_short,
        strategies=payload.strategies or None,
        min_agree=payload.min_agree,
    )
    return {"symbol": payload.symbol, "timeframe": payload.timeframe,
            **report.to_dict()}
