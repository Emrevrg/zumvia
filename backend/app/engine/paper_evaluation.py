"""
PAPER DEĞERLENDİRME — 100 USD sanal portföy standardı (ağ yok)
==============================================================
Tekrarlanabilir sıra:
  başlat → strateji/ölçüm → paper emir → fee/slippage → stop/limit →
  emergency/kill-switch → rapor

  * Gerçek borsa çağrısı YOKTUR: mum/kotasyon sağlayıcıları dışarıdan enjekte
    edilir (testlerde fixture/mock). Enjekte edilmezse açıkça `unavailable` döner.
  * Karar ve matematik Python'dadır; model yalnızca yorumlar (ADR-001).
  * Risk/stop/solvency/kill-switch kapıları mevcut katmanlardan geçilir;
    bu modül limitleri YENİDEN TANIMLAMAZ (l4_risk / safety değiştirilemez).
  * 8 saatlik gerçek zamanlı forward gözlem YAPILMADI; bunun yerine geçmiş
    veride hızlandırılmış değerlendirme (backtest + walk-forward) kullanıldı.
    Bu, her raporda açıkça yazılır.

Sanal başlangıç sermayesi: 100 USD (görev standardı).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..layers.l5_execution import DEFAULT_SLIPPAGE_PCT, DEFAULT_TAKER_FEE_PCT

PAPER_START_BALANCE_USD = 100.0
PAPER_NOTICE = ("PAPER DEĞERLENDİRME — sanal 100 USD ile yapılmıştır; "
                "gerçek kâr kanıtı değildir. Geçmiş performans gelecek "
                "sonuçları garanti etmez.")
ACCELERATED_NOTE = ("8 saatlik gerçek zamanlı forward gözlem yapılmadı; "
                    "geçmiş veride hızlandırılmış değerlendirme (backtest + "
                    "walk-forward) kullanıldı. Gerçek zamanlı likidite/kesinti "
                    "etkileri bu raporda ÖLÇÜLMEDİ (unavailable).")


@dataclass(slots=True)
class EvalStep:
    name: str
    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"step": self.name, "ok": self.ok,
                "detail": self.detail, "data": self.data}


def _step(name: str, ok: bool, detail: str, **data: Any) -> EvalStep:
    return EvalStep(name, ok, detail, dict(data))


def run_paper_evaluation(
    *,
    market: str = "crypto",
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    starting_balance: float = PAPER_START_BALANCE_USD,
    fetch_ohlcv: Callable[..., Any] | None = None,
    fetch_quote: Callable[..., Any] | None = None,
    now: datetime | None = None,
    fee_pct: float = DEFAULT_TAKER_FEE_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
) -> dict[str, Any]:
    """100 USD paper senaryosunu uçtan uca koşar. Ağ erişimi YOK (enjekte edilir)."""
    from ..layers.market_rules import assess  # noqa: PLC0415
    from .data_quality import check_candles, check_quote, sufficiency_gate  # noqa: PLC0415

    ts = now or datetime.now(UTC)
    steps: list[EvalStep] = []
    limitations: list[str] = [ACCELERATED_NOTE]

    # 1) BAŞLAT
    if not starting_balance == starting_balance or starting_balance <= 0:  # NaN guard
        return _report(ts, market, symbol, timeframe, starting_balance, fee_pct,
                       slippage_pct, steps, limitations, "starting_balance geçersiz.")
    balance = float(starting_balance)
    steps.append(_step("baslat", True,
                       f"Paper portföy başlatıldı: {balance:.2f} USD (sanal).",
                       balance=balance, mode="paper"))

    # 2) PİYASA KAPISI
    gate = assess(market, symbol, ts)
    steps.append(_step("piyasa_kapisi", bool(gate["supported"] and gate.get("tradable", True)),
                       gate["reason"], code=gate["code"], venue=gate.get("venue")))
    if not gate["supported"]:
        return _report(ts, market, symbol, timeframe, balance, fee_pct, slippage_pct,
                       steps, [*limitations, "Desteklenmeyen varlık; ölçüm yapılmadı."],
                       "Desteklenmeyen varlık.")
    if not gate.get("tradable", True):
        limitations.append(f"Piyasa şu an kapalı ({gate['code']}); emir adımı atlandı.")
        return _report(ts, market, symbol, timeframe, balance, fee_pct, slippage_pct,
                       steps, limitations, None, market_closed=True)

    # 3) VERİ + KALİTE (enjekte sağlayıcı; yoksa unavailable — ağa çıkılmaz)
    if fetch_ohlcv is None:
        steps.append(_step("veri", False,
                           "Mum sağlayıcı enjekte edilmedi; ağa çıkılmadı (unavailable).",
                           code="NO_PROVIDER", available=False))
        return _report(ts, market, symbol, timeframe, balance, fee_pct, slippage_pct,
                       steps, [*limitations, "Canlı/geçmiş veri sağlayıcı yok."],
                       "Veri sağlayıcı yok.")
    try:
        df = fetch_ohlcv(market, symbol, timeframe)
    except Exception as exc:  # noqa: BLE001
        steps.append(_step("veri", False, f"Mum alınamadı: {exc!s}"[:220],
                           code="FETCH_FAILED", available=False))
        return _report(ts, market, symbol, timeframe, balance, fee_pct, slippage_pct,
                       steps, limitations, "Mum alınamadı.")
    quality = check_candles(df, timeframe, ts)
    steps.append(_step("veri_kalite", bool(quality["available"]),
                       quality["reason"], code=quality["code"],
                       available=quality["available"],
                       details=quality.get("details", {})))
    if not quality["available"]:
        limitations.append("Veri bütünlüğü doğrulanamadı; strateji ölçümü yapılmadı.")
        return _report(ts, market, symbol, timeframe, balance, fee_pct, slippage_pct,
                       steps, limitations, None, data_unavailable=True,
                       unavailability=quality)

    # 4) STRATEJİ / ÖLÇÜM (backtest + walk-forward; look-ahead korumalı motor)
    from .backtest import run_backtest  # noqa: PLC0415
    from .optimizer import walk_forward  # noqa: PLC0415

    try:
        bt = run_backtest(df, initial_balance=balance, fee_pct=fee_pct,
                          slippage_pct=slippage_pct)
        wf = walk_forward(df, symbol=symbol, timeframe=timeframe)
    except Exception as exc:  # noqa: BLE001
        steps.append(_step("strateji_olcumu", False, f"Ölçüm hatası: {exc!s}"[:220],
                           code="MEASURE_FAILED", available=False))
        return _report(ts, market, symbol, timeframe, balance, fee_pct, slippage_pct,
                       steps, limitations, "Ölçüm hatası.")
    m = bt.metrics
    trades_n = int(m.get("trade_count", 0) or 0)
    gate2 = sufficiency_gate(trade_count=trades_n, oos_trades=wf.oos_trades)
    steps.append(_step("strateji_olcumu", True,
                       f"Backtest: {trades_n} işlem, PF {m.get('profit_factor')}; "
                       f"OOS: {wf.oos_trades} işlem, PF {wf.oos_profit_factor:.2f}, "
                       f"gap {wf.overfit_gap:.2f}. Hüküm: {bt.verdict[:120]}",
                       backtest=bt.to_dict(), walk_forward=wf.to_dict(),
                       fee_pct=fee_pct, slippage_pct=slippage_pct,
                       verdict=bt.verdict))
    if not gate2["available"]:
        steps.append(_step("yeterlilik", False, gate2["reason"],
                           code=gate2["code"], available=False))
        limitations.append("Yetersiz kanıt: metrikler raporlanmadı (uydurulmadı).")
        return _report(ts, market, symbol, timeframe, balance, fee_pct, slippage_pct,
                       steps, limitations, None, insufficient=True,
                       backtest=bt.to_dict(), walk_forward=wf.to_dict())
    steps.append(_step("yeterlilik", True, gate2["reason"], code="OK"))

    # 5) PAPER EMİR (risk/stop/solvency/kill-switch kapılarından geçirilir)
    quote_ok: dict[str, Any] | None = None
    price: float | None = None
    if fetch_quote is not None:
        try:
            q = fetch_quote(market, symbol)
            px = float(q["price"] if isinstance(q, dict) else getattr(q, "price", 0))
            quote_ok = check_quote(px, bid=(q.get("bid") if isinstance(q, dict) else None),
                                   ask=(q.get("ask") if isinstance(q, dict) else None),
                                   ts=(q.get("ts") if isinstance(q, dict) else None))
            if quote_ok["available"]:
                price = px
            else:
                steps.append(_step("paper_emir", False, quote_ok["reason"],
                                   code=quote_ok["code"], available=False))
                limitations.append("Kotasyon doğrulanamadı; emir uydurulmadı.")
                return _report(ts, market, symbol, timeframe, balance, fee_pct,
                               slippage_pct, steps, limitations, quote_ok["reason"],
                               backtest=bt.to_dict(), walk_forward=wf.to_dict())
        except Exception as exc:  # noqa: BLE001
            steps.append(_step("paper_emir", False, f"Kotasyon alınamadı: {exc!s}"[:200],
                               code="QUOTE_FAILED", available=False))
            return _report(ts, market, symbol, timeframe, balance, fee_pct,
                           slippage_pct, steps, limitations, "Kotasyon alınamadı.",
                           backtest=bt.to_dict(), walk_forward=wf.to_dict())
    else:
        try:
            price = float(df["close"].iloc[-1])
        except Exception:  # noqa: BLE001
            price = None
        limitations.append("Canlı kotasyon sağlayıcı yok; son kapanış referans alındı "
                           "(emir iletilmedi, yalnızca boyut hesabı).")
        if price is None:
            return _report(ts, market, symbol, timeframe, balance, fee_pct,
                           slippage_pct, steps, limitations, "Fiyat yok.",
                           backtest=bt.to_dict(), walk_forward=wf.to_dict())

    order_info = _dry_run_order(symbol=symbol, price=float(price), equity=balance,
                                fee_pct=fee_pct, slippage_pct=slippage_pct)
    steps.append(_step("paper_emir", bool(order_info["ok"]), order_info["reason"],
                       code=order_info["code"], **{k: v for k, v in order_info.items()
                                                   if k not in ("ok", "reason", "code")}))
    if not order_info["ok"]:
        limitations.append("Paper emir kapıdan geçemedi; pozisyon açılmadı.")
        return _report(ts, market, symbol, timeframe, balance, fee_pct, slippage_pct,
                       steps, limitations, None,
                       backtest=bt.to_dict(), walk_forward=wf.to_dict(),
                       order_rejected=True)

    # 6) FEE / SLIPPAGE muhasebesi (açık varsayımlar)
    steps.append(_step("fee_slippage", True,
                       f"Varsayım: fee %{fee_pct}, slippage %{slippage_pct} (sabit; "
                       "volatiliteye bağlı değil — gerçek maliyet farklı olabilir).",
                       fee_pct=fee_pct, slippage_pct=slippage_pct,
                       entry_fee=order_info.get("entry_fee"),
                       notional=order_info.get("notional")))

    # 7) STOP / LİMİT doğrulaması
    steps.append(_step("stop_limit", True,
                       f"Stop %{order_info.get('stop_pct')} bandında, "
                       f"R/R {order_info.get('rr')}; stopsuz emir imkânsız.",
                       stop=order_info.get("stop_loss"),
                       take_profit=order_info.get("take_profit"),
                       rr=order_info.get("rr")))

    # 8) EMERGENCY / KILL-SWITCH
    from ..core.safety import kill_switch_active, kill_switch_reason  # noqa: PLC0415

    ks = bool(kill_switch_active())
    steps.append(_step("kill_switch", True,
                       ("Acil fren AÇIK — yeni pozisyon açılmadı (koruma çalışıyor)."
                        if ks else "Acil fren kapalı; paper emir yine de iletilmedi "
                        "(dry-run standardı)."),
                       kill_switch=ks, reason=kill_switch_reason() if ks else "",
                       code="KILL_SWITCH_ON" if ks else "OK"))

    return _report(ts, market, symbol, timeframe, balance, fee_pct, slippage_pct,
                   steps, limitations, None,
                   backtest=bt.to_dict(), walk_forward=wf.to_dict(),
                   order=order_info, quote=quote_ok)


def _dry_run_order(*, symbol: str, price: float, equity: float,
                   fee_pct: float, slippage_pct: float) -> dict[str, Any]:
    """
    Gerçek emir İLETMEZ; l4_risk boyutlandırmasını örnek bir BUY kurulumuyla
    dry-run olarak çalıştırır (stop ATR-tabanlı değil, %1.5 varsayımsal).
    Günlük limit/stop/solvency kapıları ayrıca sorgulanır.
    """
    from ..layers.l4_risk import validate_and_size  # noqa: PLC0415
    from ..layers.solvency import check_position as check_solvency  # noqa: PLC0415

    if not price or price <= 0 or equity <= 0:
        return {"ok": False, "reason": "Geçersiz fiyat/bakiye; emir denenmedi.",
                "code": "INVALID_INPUT"}
    stop = price * 0.985
    target = price * 1.0375  # R/R = 2.5 (sistem tavanı 2.2'nin üstünde)
    verdict, order = None, None
    try:
        import types  # noqa: PLC0415

        bot = types.SimpleNamespace(
            risk_pct=0.5, daily_loss_limit_pct=3.0, min_confidence=0.78,
            min_rr=2.0, max_drawdown_pct=15.0, paper_balance=equity,
            initial_balance=equity, peak_equity=equity,
            day_start_equity=equity, day_trades=0, day_key="",
            max_trades_per_day=8, max_open_positions=3,
            locked_until=None, lock_reason=None,
            max_portfolio_heat_pct=3.0, allow_short=False,
            consecutive_losses=0, min_agree=2,
        )
        verdict, order = validate_and_size(
            bot, "BUY", 0.80, price, stop, target, equity, price * 0.01)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"Boyutlandırma hatası: {exc!s}"[:200],
                "code": "SIZE_FAILED"}
    if verdict is None or not verdict.allowed or order is None:
        return {"ok": False,
                "reason": getattr(verdict, "reason", "Risk kalkanı reddetti."),
                "code": getattr(verdict, "code", "RISK_REJECT")}
    solv = check_solvency(equity, order.side, order.entry, order.qty, [])
    if not solv.allowed:
        return {"ok": False, "reason": solv.reason, "code": "SOLVENCY_BLOCKED"}
    entry_eff = order.entry * (1 + slippage_pct / 100.0)
    entry_fee = order.qty * entry_eff * fee_pct / 100.0
    notional = order.qty * entry_eff
    stop_pct = abs(order.entry - order.stop_loss) / order.entry * 100.0
    return {"ok": True, "reason": "Paper emir dry-run geçti (iletilmedi).",
            "code": "OK", "side": order.side, "entry": round(order.entry, 6),
            "stop_loss": round(order.stop_loss, 6),
            "take_profit": round(order.take_profit, 6),
            "qty": round(order.qty, 8), "risk_amount": round(order.risk_amount, 4),
            "rr": round(order.rr_ratio, 2), "stop_pct": round(stop_pct, 3),
            "entry_fee": round(entry_fee, 4), "notional": round(notional, 2)}


def _report(ts: datetime, market: str, symbol: str, timeframe: str, balance: float,
            fee_pct: float, slippage_pct: float, steps: list[EvalStep],
            limitations: list[str], error: str | None,
            backtest: dict | None = None, walk_forward: dict | None = None,
            order: dict | None = None, quote: dict | None = None,
             market_closed: bool = False, data_unavailable: bool = False,
             insufficient: bool = False, order_rejected: bool = False,
             unavailability: dict | None = None) -> dict[str, Any]:
    ok_steps = sum(1 for s in steps if s.ok)
    blocked = market_closed or data_unavailable or insufficient or order_rejected
    status = "ok" if error is None and not blocked else (
        "market_closed" if market_closed else (
            "data_unavailable" if data_unavailable else (
                "insufficient_evidence" if insufficient else (
                    "order_rejected" if order_rejected else "blocked"))))
    return {
        "available": status == "ok",
        "status": status,
        "mode": "paper",
        "starting_balance": round(balance, 2),
        "currency": "USD",
        "market": market, "symbol": symbol, "timeframe": timeframe,
        "reference_date": ts.isoformat(), "timezone": "UTC",
        "paper_notice": PAPER_NOTICE,
        "accelerated_note": ACCELERATED_NOTE,
        "fee_pct": fee_pct, "slippage_pct": slippage_pct,
        "fee_note": ("Sabit fee/slippage varsayımı; gerçek borsa maliyeti, spread "
                     "ve volatiliteye göre değişir."),
        "steps": [s.to_dict() for s in steps],
        "steps_ok": ok_steps, "steps_total": len(steps),
        "backtest": backtest, "walk_forward": walk_forward,
        "paper_order": order, "quote": quote,
        "unavailability": unavailability,
        "limitations": limitations,
        "verified_claims": [s.name for s in steps if s.ok],
        "unverified_claims": [
            "Gerçek zamanlı 8 saatlik forward gözlem YAPILMADI (hızlandırılmış geçmiş veri kullanıldı).",
            "Canlı likidite/kesinti/slippage davranışı ÖLÇÜLMEDİ.",
            "Gelecek getiri iddiası YOK — bu rapor kâr kanıtı değildir.",
            *([] if error is None else [f"Blok: {error}"]),
        ],
        "error": error,
        "reason": error or ("Değerlendirme tamamlandı (paper, kâr kanıtı değildir)."
                            if status == "ok" else
                            "Değerlendirme tamamlanamadı; gerekçe adımlarda."),
    }
