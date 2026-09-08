"""
FIRSAT TARAYICI (Market Scanner)
=================================
Tek bir pariteye bakıp fırsat beklemek amatör yaklaşımdır: piyasanın %90'ı
çoğu zaman yatay seyreder. Profesyonel masalar **onlarca enstrümanı aynı anda
tarar** ve yalnızca en net kurulumu alır.

Bu modül bir sembol evrenini paralel tarar ve her biri için deterministik bir
fırsat skoru üretir:

    skor = trend kalitesi (ADX + EMA dizilimi)
         + strateji konsensüs gücü
         + volatilite uygunluğu (çok ölü / çok çılgın olmamalı)
         + hacim teyidi
         − likidite cezası (geniş spread)

Hiçbir puan dil modeli tarafından üretilmez. Ajan bu sıralamayı **girdi** olarak
okur, kararı yine kendi verir ve risk kalkanı yine son sözü söyler.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..core.logging import get_logger
from ..layers.l1_market_data import (
    POPULAR_CRYPTO,
    POPULAR_STOCKS,
    MarketDataError,
    fetch_ohlcv,
    fetch_quote,
)
from ..layers.l2_indicators import build_snapshot, compute_all, technical_bias
from ..layers.strategies import StrategyEngine

log = get_logger("zumvia.scanner")

# Volatilite penceresi: bu aralık dışı pariteler cezalandırılır
MIN_HEALTHY_ATR_PCT = 0.35    # altı = ölü piyasa, stop mesafesi anlamsızlaşır
MAX_HEALTHY_ATR_PCT = 6.00    # üstü = kumarhane, stop sürekli avlanır


@dataclass(slots=True)
class ScanResult:
    symbol: str
    score: float
    action: str                 # BUY | SELL | WAIT
    confidence: float
    price: float
    regime: str
    adx: float
    atr_pct: float
    rsi: float
    technical_score: int
    agree: int
    total: int
    stop_loss: float = 0.0
    take_profit: float = 0.0
    rr: float = 0.0
    spread_pct: float | None = None
    notes: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "score": round(self.score, 2),
            "action": self.action,
            "confidence": round(self.confidence, 3),
            "price": self.price,
            "regime": self.regime,
            "adx": round(self.adx, 1),
            "atr_pct": round(self.atr_pct, 3),
            "rsi": round(self.rsi, 1),
            "technical_score": self.technical_score,
            "agreement": f"{self.agree}/{self.total}",
            "stop_loss": round(self.stop_loss, 8) if self.stop_loss else 0.0,
            "take_profit": round(self.take_profit, 8) if self.take_profit else 0.0,
            "rr": round(self.rr, 2),
            "spread_pct": self.spread_pct,
            "notes": self.notes,
            **({"error": self.error} if self.error else {}),
        }


def _score_one(market: str, exchange: str, symbol: str, timeframe: str,
               strategies: list[str] | None, min_agree: int,
               check_spread: bool) -> ScanResult:
    try:
        raw = fetch_ohlcv(market, exchange, symbol, timeframe, 300)
        if len(raw) < 210:
            return ScanResult(symbol, -999, "WAIT", 0, 0, "", 0, 0, 0, 0, 0, 0,
                              error="Yetersiz geçmiş veri")

        df = compute_all(raw)
        snap = build_snapshot(df, symbol, timeframe)
        bias = technical_bias(snap)
        consensus = StrategyEngine(strategies, min_agree).run(df)

        indicators = snap.indicators
        adx = float(indicators.get("adx_14", 0.0))
        atr_pct = float(indicators.get("atr_percent", 0.0))
        rsi = float(indicators.get("rsi_14", 50.0))
        volume_z = float(indicators.get("volume_z_score", 0.0))

        notes: list[str] = []
        score = 0.0

        # 1) Konsensüs gücü — en ağır bileşen
        if consensus.action in ("BUY", "SELL"):
            score += consensus.confidence * 45.0
            score += consensus.agree * 4.0
            notes.append(f"{consensus.agree}/{consensus.total} strateji {consensus.action}")
        else:
            score -= 12.0
            notes.append("Strateji konsensüsü yok")

        # 2) Trend kalitesi
        if adx >= 30:
            score += 18.0
            notes.append(f"Güçlü trend (ADX {adx:.0f})")
        elif adx >= 22:
            score += 10.0
        elif adx < 15:
            score -= 8.0
            notes.append(f"Yönsüz piyasa (ADX {adx:.0f})")

        # 3) Teknik skorun konsensüsle uyumu
        if consensus.action == "BUY" and bias["score"] > 0:
            score += min(12.0, bias["score"] / 6.0)
        elif consensus.action == "SELL" and bias["score"] < 0:
            score += min(12.0, abs(bias["score"]) / 6.0)
        elif consensus.action != "WAIT":
            score -= 10.0
            notes.append("Teknik skor konsensüsle çelişiyor")

        # 4) Volatilite sağlığı
        if atr_pct < MIN_HEALTHY_ATR_PCT:
            score -= 15.0
            notes.append(f"Volatilite çok düşük (ATR %{atr_pct:.2f})")
        elif atr_pct > MAX_HEALTHY_ATR_PCT:
            score -= 18.0
            notes.append(f"Volatilite aşırı (ATR %{atr_pct:.2f}) — stop avlanır")
        else:
            score += 8.0

        # 5) Hacim teyidi
        if volume_z >= 1.5:
            score += 7.0
            notes.append(f"Hacim teyidi güçlü (z={volume_z:.1f})")
        elif volume_z <= -1.0:
            score -= 5.0
            notes.append("Hacim zayıf")

        # 6) Likidite / spread
        spread = None
        if check_spread:
            try:
                spread = fetch_quote(market, exchange, symbol).spread_pct
            except Exception:  # noqa: BLE001 — spread alınamazsa taramayı durdurma
                spread = None
            if spread is not None:
                if spread > 0.25:
                    score -= 20.0
                    notes.append(f"Spread geniş (%{spread:.3f}) — maliyet yüksek")
                elif spread < 0.05:
                    score += 4.0

        rr = 0.0
        if consensus.action in ("BUY", "SELL") and consensus.stop_loss:
            risk = abs(snap.price - consensus.stop_loss)
            reward = abs(consensus.take_profit - snap.price)
            rr = reward / risk if risk > 0 else 0.0

        return ScanResult(
            symbol=symbol, score=score, action=consensus.action,
            confidence=consensus.confidence, price=snap.price, regime=snap.regime,
            adx=adx, atr_pct=atr_pct, rsi=rsi, technical_score=bias["score"],
            agree=consensus.agree, total=consensus.total,
            stop_loss=consensus.stop_loss, take_profit=consensus.take_profit,
            rr=rr, spread_pct=spread, notes=notes,
        )

    except MarketDataError as exc:
        return ScanResult(symbol, -999, "WAIT", 0, 0, "", 0, 0, 0, 0, 0, 0,
                          error=str(exc)[:140])
    except Exception as exc:  # noqa: BLE001
        log.warning("Tarama hatası %s: %s", symbol, exc)
        return ScanResult(symbol, -999, "WAIT", 0, 0, "", 0, 0, 0, 0, 0, 0,
                          error=f"{type(exc).__name__}: {exc}"[:140])


def default_universe(market: str) -> list[str]:
    if market == "stock":
        return list(POPULAR_STOCKS)
    return list(POPULAR_CRYPTO)


def scan_markets(*, market: str = "crypto", exchange: str = "binance",
                 symbols: list[str] | None = None, timeframe: str = "1h",
                 strategies: list[str] | None = None, min_agree: int = 2,
                 top: int = 6, only_actionable: bool = True,
                 check_spread: bool = True, workers: int = 5) -> dict[str, Any]:
    """
    Sembol evrenini paralel tarar ve fırsatları skora göre sıralar.

    `only_actionable=True` iken yalnızca BUY/SELL sinyali olanlar döner —
    ajan boş sonuçları okuyup token harcamasın diye.
    """
    universe = [s.strip().upper() for s in (symbols or default_universe(market)) if s.strip()]
    universe = universe[:24]                  # taramayı makul tut

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        results = list(pool.map(
            lambda symbol: _score_one(market, exchange, symbol, timeframe,
                                      strategies, min_agree, check_spread),
            universe,
        ))

    healthy = [r for r in results if not r.error]
    failed = [{"symbol": r.symbol, "error": r.error} for r in results if r.error]

    actionable = [r for r in healthy if r.action in ("BUY", "SELL")]
    pool_to_rank = actionable if (only_actionable and actionable) else healthy
    ranked = sorted(pool_to_rank, key=lambda r: -r.score)[:max(1, top)]

    best = ranked[0] if ranked else None
    if best is None:
        headline = "Taranan hiçbir pariteda işlem açmaya değer kurulum yok."
    elif best.action == "WAIT":
        headline = (f"{len(healthy)} parite tarandı, hiçbirinde net sinyal yok. "
                    "Beklemek doğru karar.")
    else:
        headline = (f"{len(actionable)} pariteda sinyal var. En güçlüsü: "
                    f"{best.symbol} {best.action} (skor {best.score:.0f}, "
                    f"{best.agree}/{best.total} strateji).")

    return {
        "market": market,
        "exchange": exchange,
        "timeframe": timeframe,
        "scanned": len(universe),
        "with_signal": len(actionable),
        "headline": headline,
        "candidates": [r.to_dict() for r in ranked],
        "failed": failed[:5],
        "note": ("Skorlar deterministik kurallarla hesaplanır, tahmin değildir. "
                 "Yüksek skor 'kesin kazanç' anlamına gelmez; yalnızca kurulumun "
                 "teknik olarak daha net olduğunu gösterir."),
    }
