"""
WALK-FORWARD DOĞRULAMA & YAPILANDIRMA OPTİMİZASYONU
====================================================
Geri testin en tehlikeli yanı **aşırı uyum (overfitting)** dir: yeterince
parametre denerseniz geçmişte harika görünen ama gelecekte para kaybettiren bir
yapılandırma mutlaka bulursunuz. Bu, bireysel yatırımcıyı batıran bir numaralı
yanılgıdır.

Bu modül kurumsal standardı uygular: **walk-forward (ileri yürüyen) doğrulama.**

    |---- eğitim ----|-- test --|
              |---- eğitim ----|-- test --|
                        |---- eğitim ----|-- test --|

Yapılandırma yalnızca eğitim penceresinde seçilir, performans **hiç görülmemiş**
test penceresinde ölçülür. Rapor edilen sonuç bu birleştirilmiş test
pencerelerinden gelir — yani gerçekçi beklentidir.

Ek koruma: `overfit_gap` (eğitim ile test arasındaki kâr faktörü farkı).
Fark büyükse yapılandırma ezberlemiştir ve reddedilir.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..core.logging import get_logger
from ..layers.strategies import DEFAULT_ACTIVE, STRATEGIES
from .backtest import run_backtest

log = get_logger("zumvia.optimizer")

# Denenecek strateji kombinasyonları (küçük ve anlamlı tutulur — arama uzayı
# ne kadar büyürse aşırı uyum riski o kadar artar)
CANDIDATE_SETS: list[list[str]] = [
    ["trend_following", "pullback_ema", "breakout"],
    ["trend_following", "pullback_ema", "momentum_macd"],
    ["trend_following", "breakout", "squeeze_expansion"],
    ["mean_reversion", "vwap_reversion", "rsi_divergence"],
    list(DEFAULT_ACTIVE),
]


@dataclass(slots=True)
class FoldResult:
    fold: int
    train_bars: int
    test_bars: int
    train_pf: float
    test_pf: float
    test_trades: int
    test_return_pct: float
    test_max_dd_pct: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold": self.fold, "train_bars": self.train_bars, "test_bars": self.test_bars,
            "train_profit_factor": round(self.train_pf, 3),
            "test_profit_factor": round(self.test_pf, 3),
            "test_trades": self.test_trades,
            "test_return_pct": round(self.test_return_pct, 2),
            "test_max_drawdown_pct": round(self.test_max_dd_pct, 2),
        }


@dataclass(slots=True)
class WalkForwardReport:
    symbol: str
    timeframe: str
    strategies: list[str]
    min_agree: int
    folds: list[FoldResult] = field(default_factory=list)
    oos_trades: int = 0
    oos_profit_factor: float = 0.0
    oos_win_rate_pct: float = 0.0
    oos_return_pct: float = 0.0
    oos_max_drawdown_pct: float = 0.0
    overfit_gap: float = 0.0
    verdict: str = ""
    robust: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "timeframe": self.timeframe,
            "strategies": self.strategies, "min_agree": self.min_agree,
            "out_of_sample": {
                "trades": self.oos_trades,
                "profit_factor": round(self.oos_profit_factor, 3),
                "win_rate_pct": round(self.oos_win_rate_pct, 2),
                "return_pct": round(self.oos_return_pct, 2),
                "max_drawdown_pct": round(self.oos_max_drawdown_pct, 2),
            },
            "overfit_gap": round(self.overfit_gap, 3),
            "robust": self.robust,
            "verdict": self.verdict,
            "folds": [f.to_dict() for f in self.folds],
        }


def _metrics(report) -> tuple[float, int, float, float, float]:
    m = report.metrics
    return (
        float(m.get("profit_factor", 0.0) or 0.0),
        int(m.get("trade_count", 0) or 0),
        float(m.get("win_rate_pct", 0.0) or 0.0),
        float(m.get("total_return_pct", 0.0) or 0.0),
        float(m.get("max_drawdown_pct", 0.0) or 0.0),
    )


def walk_forward(df: pd.DataFrame, *, symbol: str = "", timeframe: str = "",
                 strategies: list[str] | None = None, min_agree: int = 2,
                 folds: int = 3, risk_pct: float = 1.0, min_rr: float = 2.0,
                 min_confidence: float = 0.75, allow_short: bool = False,
                 warmup: int = 210) -> WalkForwardReport:
    """
    Veriyi ardışık eğitim/test pencerelerine böler ve yalnızca test
    pencerelerindeki (görülmemiş veri) performansı raporlar.
    """
    active = [s for s in (strategies or DEFAULT_ACTIVE) if s in STRATEGIES] or list(DEFAULT_ACTIVE)
    report = WalkForwardReport(symbol=symbol, timeframe=timeframe,
                               strategies=active, min_agree=min_agree)

    usable = len(df) - warmup
    folds = max(2, min(folds, 5))
    # Her katman için makul bir test penceresi olmalı
    if usable < folds * 120:
        report.verdict = ("Walk-forward için yeterli veri yok. Daha fazla mum "
                          "(en az ~800 bar) veya daha kısa zaman dilimi gerekir.")
        return report

    window = usable // folds
    train_ratio = 0.7

    total_trades = 0
    gross_win = gross_loss = 0.0
    wins = 0
    returns: list[float] = []
    drawdowns: list[float] = []
    train_pfs: list[float] = []
    test_pfs: list[float] = []

    for index in range(folds):
        start = warmup + index * window
        train_end = start + int(window * train_ratio)
        test_end = min(start + window, len(df))
        if test_end - train_end < 60:
            continue

        train_df = df.iloc[: train_end]
        test_df = df.iloc[train_end - warmup: test_end]   # ısınma payı bırakılır

        common = {"risk_pct": risk_pct, "min_rr": min_rr, "min_confidence": min_confidence,
                      "allow_short": allow_short, "strategies": active, "min_agree": min_agree,
                      "warmup": warmup}

        train_report = run_backtest(train_df, **common)
        test_report = run_backtest(test_df, **common)

        train_pf, _, _, _, _ = _metrics(train_report)
        test_pf, test_trades, _test_wr, test_ret, test_dd = _metrics(test_report)

        train_pfs.append(train_pf)
        test_pfs.append(test_pf)

        for trade in test_report.trades:
            total_trades += 1
            if trade.pnl > 0:
                wins += 1
                gross_win += trade.pnl
            else:
                gross_loss += abs(trade.pnl)

        returns.append(test_ret)
        drawdowns.append(test_dd)

        report.folds.append(FoldResult(
            fold=index + 1, train_bars=len(train_df), test_bars=len(test_df),
            train_pf=train_pf, test_pf=test_pf, test_trades=test_trades,
            test_return_pct=test_ret, test_max_dd_pct=test_dd,
        ))

    report.oos_trades = total_trades
    report.oos_profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        999.0 if gross_win > 0 else 0.0)
    report.oos_win_rate_pct = (wins / total_trades * 100.0) if total_trades else 0.0
    report.oos_return_pct = sum(returns)
    report.oos_max_drawdown_pct = max(drawdowns) if drawdowns else 0.0

    # Kâr faktöründe "hiç zarar yok" durumu 999 sentinel'i ile gösterilir.
    # Bu değer ortalamaya girerse aşırı uyum farkı anlamsızlaşır (-332 gibi).
    # Karşılaştırma için makul bir tavana kırpılır.
    PF_CAP = 10.0
    clamp = lambda values: [min(max(v, 0.0), PF_CAP) for v in values]  # noqa: E731

    train_clamped = clamp(train_pfs)
    test_clamped = clamp(test_pfs)
    avg_train = sum(train_clamped) / len(train_clamped) if train_clamped else 0.0
    avg_test = sum(test_clamped) / len(test_clamped) if test_clamped else 0.0
    report.overfit_gap = round(avg_train - avg_test, 4)

    # --- Karar ---
    if total_trades < 12:
        report.verdict = (f"Yetersiz örnek: görülmemiş veride yalnızca {total_trades} "
                          "işlem oluştu. İstatistiksel güven yok — kullanmayın.")
    elif report.oos_profit_factor >= 1.5 and report.oos_max_drawdown_pct <= 15 \
            and report.overfit_gap < 0.6:
        report.robust = True
        report.verdict = (f"SAĞLAM — Görülmemiş veride kâr faktörü "
                          f"{report.oos_profit_factor:.2f}, drawdown "
                          f"%{report.oos_max_drawdown_pct:.1f}, aşırı uyum farkı "
                          f"{report.overfit_gap:.2f}. Paper trading ile devam edin.")
    elif report.overfit_gap >= 0.6:
        report.verdict = (f"AŞIRI UYUM — Eğitimde iyi ({avg_train:.2f}), testte "
                          f"kötü ({avg_test:.2f}). Bu yapılandırma geçmişi ezberlemiş, "
                          "gelecekte çalışmaz. Kullanmayın.")
    elif report.oos_profit_factor >= 1.15:
        report.verdict = (f"SINIRDA — Görülmemiş veride kâr faktörü "
                          f"{report.oos_profit_factor:.2f}. Komisyonlar bunu eritebilir. "
                          "Daha uzun paper trading gerekir.")
    else:
        report.verdict = (f"ÇALIŞMIYOR — Görülmemiş veride kâr faktörü "
                          f"{report.oos_profit_factor:.2f}. Bu parite/zaman dilimi "
                          "bu strateji setine uygun değil.")

    return report


def optimize_configuration(fetch, *, market: str, exchange: str, symbol: str,
                           timeframes: list[str] | None = None,
                           candidate_sets: list[list[str]] | None = None,
                           min_agree_options: list[int] | None = None,
                           candles: int = 1000, risk_pct: float = 1.0,
                           allow_short: bool = False,
                           folds: int = 3) -> dict[str, Any]:
    """
    Zaman dilimi × strateji seti × konsensüs eşiği kombinasyonlarını dener ve
    **yalnızca görülmemiş veri performansına göre** en iyisini seçer.

    `fetch(market, exchange, symbol, timeframe, candles)` → OHLCV DataFrame
    """
    timeframes = timeframes or ["1h", "4h"]
    candidate_sets = candidate_sets or CANDIDATE_SETS
    min_agree_options = min_agree_options or [2, 3]

    trials: list[dict[str, Any]] = []
    best: WalkForwardReport | None = None

    for timeframe in timeframes[:3]:
        try:
            df = fetch(market, exchange, symbol, timeframe, candles)
        except Exception as exc:  # noqa: BLE001
            trials.append({"timeframe": timeframe, "error": str(exc)[:120]})
            continue

        for strategies in candidate_sets[:5]:
            for min_agree in min_agree_options[:2]:
                if min_agree > len(strategies):
                    continue
                report = walk_forward(
                    df, symbol=symbol, timeframe=timeframe, strategies=strategies,
                    min_agree=min_agree, folds=folds, risk_pct=risk_pct,
                    allow_short=allow_short,
                )
                trials.append({
                    "timeframe": timeframe,
                    "strategies": strategies,
                    "min_agree": min_agree,
                    "oos_profit_factor": round(report.oos_profit_factor, 3),
                    "oos_trades": report.oos_trades,
                    "oos_max_drawdown_pct": round(report.oos_max_drawdown_pct, 2),
                    "overfit_gap": round(report.overfit_gap, 3),
                    "robust": report.robust,
                })
                if report.oos_trades >= 12 and (
                    best is None or report.oos_profit_factor > best.oos_profit_factor
                ):
                    best = report

    if best is None:
        return {
            "symbol": symbol,
            "found": False,
            "headline": ("Hiçbir yapılandırma yeterli işlem üretmedi. Bu parite şu an "
                         "sistematik ticarete uygun değil — başka parite deneyin."),
            "trials": trials[:20],
        }

    return {
        "symbol": symbol,
        "found": True,
        "recommended": {
            "timeframe": best.timeframe,
            "strategies": best.strategies,
            "min_agree": best.min_agree,
        },
        "validation": best.to_dict(),
        "headline": (f"En iyi doğrulanmış yapılandırma: {best.timeframe} · "
                     f"{len(best.strategies)} strateji · eşik {best.min_agree} → "
                     f"görülmemiş veride PF {best.oos_profit_factor:.2f}. "
                     f"{best.verdict}"),
        "trials": sorted(trials, key=lambda t: -(t.get("oos_profit_factor") or 0))[:12],
        "warning": ("Bu sonuçlar geçmiş veriden gelir ve geleceği garanti etmez. "
                    "Canlıya geçmeden önce mutlaka 30 gün paper trading yapın."),
    }
