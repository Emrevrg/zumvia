"""
GERİ TEST (BACKTEST) MOTORU
============================
Strateji konsensüsünü + risk kalkanını geçmiş veri üzerinde bar-bar çalıştırır.
Canlıya geçmeden önce "bu strateji bu pariteye uyuyor mu?" sorusunu ölçümle
yanıtlar. Yapay zeka çağrılmaz (maliyetsiz ve tekrarlanabilir olsun diye);
hibrit modun alt sınırını gösterir.

Gerçekçilik önlemleri:
  * Sinyal bar KAPANIŞINDA üretilir, giriş BİR SONRAKİ barın açılışında olur
    (look-ahead bias yok).
  * Komisyon ve slipaj uygulanır.
  * Aynı barda SL ve TP birlikte tetiklenirse STOP öncelikli sayılır.
  * Pozisyon boyutu canlıdaki formülün aynısıyla hesaplanır.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..layers.l2_indicators import compute_all
from ..layers.l5_execution import DEFAULT_SLIPPAGE_PCT, DEFAULT_TAKER_FEE_PCT
from ..layers.strategies import StrategyEngine


@dataclass(slots=True)
class BacktestTrade:
    entry_time: str
    exit_time: str
    side: str
    entry: float
    exit: float
    qty: float
    pnl: float
    r_multiple: float
    reason: str
    strategy: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_time": self.entry_time, "exit_time": self.exit_time,
            "side": self.side, "entry": round(self.entry, 8), "exit": round(self.exit, 8),
            "qty": round(self.qty, 8), "pnl": round(self.pnl, 4),
            "r_multiple": round(self.r_multiple, 3), "reason": self.reason,
            "strategy": self.strategy,
        }


@dataclass(slots=True)
class BacktestReport:
    initial_balance: float
    final_balance: float
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    verdict: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_balance": round(self.initial_balance, 2),
            "final_balance": round(self.final_balance, 2),
            "metrics": self.metrics,
            "verdict": self.verdict,
            "trades": [t.to_dict() for t in self.trades],
            "equity_curve": self.equity_curve,
        }


def _metrics(trades: list[BacktestTrade], curve: list[float],
             initial: float, final: float, bars: int) -> dict[str, Any]:
    if not trades:
        return {"trade_count": 0, "note": "Bu dönemde kurulum oluşmadı."}

    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    equity = np.array(curve, dtype=float)

    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / np.where(peak == 0, 1, peak) * 100.0
    max_dd = float(dd.max()) if len(dd) else 0.0

    rets = np.diff(equity) / np.where(equity[:-1] == 0, 1, equity[:-1])
    sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(252)) if len(rets) > 2 and np.std(rets) > 0 else 0.0

    gross_win = float(wins.sum())
    gross_loss = float(abs(losses.sum()))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    r_values = np.array([t.r_multiple for t in trades], dtype=float)
    return {
        "trade_count": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 2),
        "profit_factor": round(profit_factor, 3) if np.isfinite(profit_factor) else 999.0,
        "total_return_pct": round(float((final - initial) / initial * 100.0), 2) if initial else 0.0,
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 2),
        "expectancy_R": round(float(r_values.mean()), 3),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "best_trade": round(float(pnls.max()), 2),
        "worst_trade": round(float(pnls.min()), 2),
        "max_consecutive_losses": int(_max_streak(pnls <= 0)),
        "bars_tested": bars,
    }


def _max_streak(mask: np.ndarray) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def _verdict(m: dict[str, Any]) -> str:
    """Plandaki canlıya geçiş metrikleriyle karşılaştırır."""
    if m.get("trade_count", 0) < 10:
        return ("YETERSİZ ÖRNEK — En az 10 işlem gerekir. Daha uzun dönem veya "
                "daha kısa zaman dilimi deneyin.")
    wr = m.get("win_rate_pct", 0)
    pf = m.get("profit_factor", 0)
    dd = m.get("max_drawdown_pct", 100)
    if wr >= 55 and pf >= 1.8 and dd <= 15:
        return "HAZIR — Hedef metrikler karşılandı (kazanma ≥%55, kâr faktörü ≥1.8, drawdown ≤%15)."
    if pf >= 1.3 and dd <= 20:
        return ("KABUL EDİLEBİLİR — Sanal modda 30 gün daha test edin. "
                "Hedef: PF≥1.8, WR≥55%.")
    return ("UYGUN DEĞİL — Bu parite/zaman dilimi bu strateji setine uymuyor. "
            "Farklı zaman dilimi veya strateji kombinasyonu deneyin.")


def run_backtest(df: pd.DataFrame, *, initial_balance: float = 1000.0,
                 risk_pct: float = 1.0, min_rr: float = 2.0,
                 min_confidence: float = 0.75, allow_short: bool = False,
                 strategies: list[str] | None = None, min_agree: int = 2,
                 fee_pct: float = DEFAULT_TAKER_FEE_PCT,
                 slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
                 warmup: int = 210) -> BacktestReport:
    """Bar-bar simülasyon. `df` ham OHLCV olmalıdır."""
    data = compute_all(df).dropna(subset=["ema_200", "atr_14", "rsi_14"])
    if len(data) < warmup + 30:
        return BacktestReport(initial_balance, initial_balance, [], [],
                              {"trade_count": 0, "note": "Yetersiz geçmiş veri."},
                              "Veri yetersiz — daha uzun dönem seçin.")

    engine = StrategyEngine(strategies, min_agree)
    balance = initial_balance
    curve: list[float] = []
    curve_points: list[dict[str, Any]] = []
    trades: list[BacktestTrade] = []
    position: dict[str, Any] | None = None

    highs = data["high"].to_numpy(dtype=float)
    lows = data["low"].to_numpy(dtype=float)
    opens = data["open"].to_numpy(dtype=float)
    closes = data["close"].to_numpy(dtype=float)
    atrs = data["atr_14"].to_numpy(dtype=float)
    index = data.index

    for i in range(warmup, len(data) - 1):
        # --- Açık pozisyonun bu bardaki akıbeti --- #
        if position is not None:
            high, low = highs[i], lows[i]
            exit_price = reason = None
            is_long = position["side"] == "long"

            if is_long and low <= position["sl"]:
                exit_price, reason = position["sl"], "STOP_LOSS"
            elif is_long and high >= position["tp"]:
                exit_price, reason = position["tp"], "TAKE_PROFIT"
            elif (not is_long) and high >= position["sl"]:
                exit_price, reason = position["sl"], "STOP_LOSS"
            elif (not is_long) and low <= position["tp"]:
                exit_price, reason = position["tp"], "TAKE_PROFIT"

            if exit_price is not None:
                fill = exit_price * (1 - slippage_pct / 100.0 if is_long
                                     else 1 + slippage_pct / 100.0)
                gross = ((fill - position["entry"]) if is_long
                         else (position["entry"] - fill)) * position["qty"]
                fee = (fill * position["qty"]) * fee_pct / 100.0
                pnl = gross - fee - position["entry_fee"]
                balance += pnl
                trades.append(BacktestTrade(
                    entry_time=str(position["time"]), exit_time=str(index[i]),
                    side=position["side"], entry=position["entry"], exit=fill,
                    qty=position["qty"], pnl=pnl,
                    r_multiple=pnl / position["risk"] if position["risk"] else 0.0,
                    reason=reason, strategy=position["strategy"],
                ))
                position = None
            else:
                # İz süren stop (canlı motorla aynı mantık)
                risk_dist = abs(position["entry"] - position["initial_sl"])
                price_now = closes[i]
                r_now = ((price_now - position["entry"]) if is_long
                         else (position["entry"] - price_now)) / risk_dist if risk_dist else 0
                if r_now >= 1.0:
                    trail = (price_now - 2.0 * atrs[i]) if is_long else (price_now + 2.0 * atrs[i])
                    be = position["entry"] * (1.0005 if is_long else 0.9995)
                    candidate = max(trail, be) if is_long else min(trail, be)
                    if (is_long and candidate > position["sl"]) or \
                       ((not is_long) and candidate < position["sl"]):
                        position["sl"] = candidate

        equity = balance
        if position is not None:
            equity += (((closes[i] - position["entry"]) if position["side"] == "long"
                        else (position["entry"] - closes[i])) * position["qty"])
        curve.append(equity)
        if i % 5 == 0:
            curve_points.append({"t": str(index[i]), "equity": round(equity, 4)})

        # --- Yeni giriş sinyali (bir sonraki barın açılışında uygulanır) --- #
        if position is not None:
            continue

        window = data.iloc[: i + 1]
        result = engine.run(window)
        if result.action not in ("BUY", "SELL"):
            continue
        if result.action == "SELL" and not allow_short:
            continue
        if result.confidence < min_confidence:
            continue

        entry_raw = opens[i + 1]
        is_long = result.action == "BUY"
        entry = entry_raw * (1 + slippage_pct / 100.0 if is_long else 1 - slippage_pct / 100.0)
        sl, tp = result.stop_loss, result.take_profit

        # Sinyal seviyeleri bir sonraki barın açılışına göre yeniden doğrulanır
        if is_long and not (sl < entry < tp):
            continue
        if (not is_long) and not (tp < entry < sl):
            continue

        stop_dist = abs(entry - sl)
        if stop_dist <= 0 or abs(tp - entry) / stop_dist < min_rr:
            continue
        stop_pct = stop_dist / entry * 100.0
        if stop_pct < 0.10 or stop_pct > 12.0:
            continue

        risk_amount = equity * risk_pct / 100.0
        qty = risk_amount / stop_dist
        if qty * entry > equity:            # kaldıraçsız notional tavanı
            qty = equity / entry
            risk_amount = qty * stop_dist
        entry_fee = qty * entry * fee_pct / 100.0

        position = {
            "side": "long" if is_long else "short", "entry": entry, "sl": sl,
            "initial_sl": sl, "tp": tp, "qty": qty, "risk": risk_amount,
            "time": index[i + 1], "entry_fee": entry_fee,
            "strategy": "+".join(s.name for s in result.signals if s.action == result.action),
        }

    final = curve[-1] if curve else initial_balance
    metrics = _metrics(trades, curve or [initial_balance], initial_balance, final, len(data))
    return BacktestReport(initial_balance, final, trades, curve_points, metrics,
                          _verdict(metrics))
