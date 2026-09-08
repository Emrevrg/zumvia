"""Katman 4 — Risk kalkanı testleri. Bu testler platformun can damarıdır."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.layers.l4_risk import (
    check_exit,
    check_preconditions,
    effective_min_confidence,
    effective_risk_pct,
    manage_open_position,
    should_trip_circuit_breaker,
    today_key,
    update_recovery_state,
    validate_and_size,
)


def make_bot(**overrides) -> SimpleNamespace:
    base = {
        "risk_pct": 1.0, "daily_loss_limit_pct": 3.0, "min_confidence": 0.75, "min_rr": 2.0,
        "max_open_positions": 1, "max_trades_per_day": 8, "max_drawdown_pct": 15.0,
        "trailing_stop": True, "breakeven_at_r": 1.0, "allow_short": True,
        "recovery_mode": False, "consecutive_losses": 0, "peak_equity": 1000.0,
        "day_key": today_key(), "day_start_equity": 1000.0, "day_trades": 0,
        "locked_until": None, "lock_reason": "", "paper_balance": 1000.0,
        "partial_tp_enabled": True, "partial_tp_at_r": 1.5, "partial_tp_fraction": 0.5,
        "max_portfolio_heat_pct": 3.0, "max_spread_pct": 0.15,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def make_position(**overrides) -> SimpleNamespace:
    base = {"side": "long", "entry_price": 100.0, "stop_loss": 98.0, "take_profit": 106.0,
                "initial_stop": 98.0, "qty": 5.0, "risk_amount": 10.0}
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
#  Zorunlu stop-loss
# --------------------------------------------------------------------------- #


def test_rejects_entry_without_stop_loss() -> None:
    verdict, order = validate_and_size(make_bot(), "BUY", 0.9, 100.0, 0.0, 110.0, 1000.0)
    assert not verdict.allowed and verdict.code == "NO_STOP"
    assert order is None


def test_derives_stop_from_atr_when_missing() -> None:
    verdict, order = validate_and_size(make_bot(), "BUY", 0.9, 100.0, 0.0, 0.0,
                                       1000.0, atr_value=1.0)
    assert verdict.allowed and order is not None
    assert order.stop_loss < 100.0


def test_rejects_illogical_stop_on_buy() -> None:
    verdict, _ = validate_and_size(make_bot(), "BUY", 0.9, 100.0, 101.0, 120.0, 1000.0)
    assert not verdict.allowed and verdict.code == "ILLOGICAL_STOP"


def test_rejects_illogical_stop_on_sell() -> None:
    verdict, _ = validate_and_size(make_bot(), "SELL", 0.9, 100.0, 99.0, 80.0, 1000.0)
    assert not verdict.allowed and verdict.code == "ILLOGICAL_STOP"


def test_rejects_stop_too_tight_and_too_wide() -> None:
    tight, _ = validate_and_size(make_bot(), "BUY", 0.9, 100.0, 99.999, 120.0, 1000.0)
    assert tight.code == "STOP_TOO_TIGHT"
    wide, _ = validate_and_size(make_bot(), "BUY", 0.9, 100.0, 80.0, 200.0, 1000.0)
    assert wide.code == "STOP_TOO_WIDE"


# --------------------------------------------------------------------------- #
#  Güven ve Risk/Ödül eşikleri
# --------------------------------------------------------------------------- #


def test_rejects_low_confidence() -> None:
    verdict, _ = validate_and_size(make_bot(), "BUY", 0.60, 100.0, 98.0, 110.0, 1000.0)
    assert not verdict.allowed and verdict.code == "LOW_CONFIDENCE"


def test_rejects_low_risk_reward() -> None:
    # Risk 2.0, ödül 2.0 → R/R 1.0 < 2.0
    verdict, _ = validate_and_size(make_bot(), "BUY", 0.9, 100.0, 98.0, 102.0, 1000.0)
    assert not verdict.allowed and verdict.code == "LOW_RR"


def test_short_blocked_when_disabled() -> None:
    verdict, _ = validate_and_size(make_bot(allow_short=False), "SELL", 0.9,
                                   100.0, 102.0, 94.0, 1000.0)
    assert verdict.code == "SHORT_DISABLED"


# --------------------------------------------------------------------------- #
#  Dinamik lot hesabı
# --------------------------------------------------------------------------- #


def test_position_size_formula() -> None:
    """Pozisyon = (Kasa × Risk%) / |Giriş − Stop|"""
    verdict, order = validate_and_size(make_bot(risk_pct=1.0), "BUY", 0.9,
                                       100.0, 98.0, 106.0, 1000.0)
    assert verdict.allowed and order is not None
    assert order.risk_amount == pytest.approx(10.0)      # 1000 × %1
    assert order.qty == pytest.approx(5.0)               # 10 / 2
    assert order.rr_ratio == pytest.approx(3.0)


def test_notional_never_exceeds_equity() -> None:
    """Kasanın tamamı tek pozisyona giremez."""
    _, order = validate_and_size(make_bot(risk_pct=1.5), "BUY", 0.9,
                                 100.0, 99.9, 100.5, 1000.0)
    if order is not None:
        assert order.notional <= 1000.0 + 1e-6


def test_risk_halves_in_recovery_phase() -> None:
    """Zirveden %5+ düşüşte risk otomatik yarıya iner (savunma aşaması)."""
    normal = effective_risk_pct(make_bot(risk_pct=1.0), equity=1000.0)
    defensive = effective_risk_pct(make_bot(risk_pct=1.0, peak_equity=1000.0), equity=930.0)
    assert defensive == pytest.approx(normal * 0.5)


def test_risk_drops_further_in_deep_drawdown() -> None:
    """Kayıp derinleştikçe risk KÜÇÜLMEYE devam eder — asla büyümez."""
    defensive = effective_risk_pct(make_bot(risk_pct=1.5, peak_equity=1000.0), equity=930.0)
    preservation = effective_risk_pct(make_bot(risk_pct=1.5, peak_equity=1000.0), equity=880.0)
    lockdown = effective_risk_pct(make_bot(risk_pct=1.5, peak_equity=1000.0), equity=800.0)
    assert defensive > preservation > lockdown


def test_confidence_threshold_rises_in_recovery_phase() -> None:
    """Zararda sistem daha SEÇİCİ olur: güven eşiği yükselir."""
    normal = effective_min_confidence(make_bot(peak_equity=1000.0), equity=1000.0)
    stressed = effective_min_confidence(make_bot(peak_equity=1000.0), equity=880.0)
    assert stressed > normal


def test_hard_cap_applies_even_if_user_sets_more() -> None:
    """Kullanıcı %10 risk yazsa bile sistem tavanı (%1.5) uygulanır."""
    assert effective_risk_pct(make_bot(risk_pct=10.0), equity=1000.0) <= 1.5


# --------------------------------------------------------------------------- #
#  Ön koşullar ve devre kesici
# --------------------------------------------------------------------------- #


def test_blocks_when_locked() -> None:
    bot = make_bot(locked_until=datetime.now(UTC) + timedelta(hours=5),
                   lock_reason="devre kesici")
    verdict = check_preconditions(bot, 1000.0, 0)
    assert not verdict.allowed and verdict.code == "LOCKED"


def test_blocks_when_max_positions_reached() -> None:
    verdict = check_preconditions(make_bot(max_open_positions=1), 1000.0, 1)
    assert verdict.code == "MAX_POSITIONS"


def test_blocks_when_daily_trade_cap_reached() -> None:
    verdict = check_preconditions(make_bot(day_trades=8, max_trades_per_day=8), 1000.0, 0)
    assert verdict.code == "MAX_TRADES"


def test_blocks_after_daily_loss_limit() -> None:
    verdict = check_preconditions(make_bot(day_start_equity=1000.0), 965.0, 0)
    assert verdict.code == "DAILY_LOSS"


def test_circuit_breaker_trips_at_three_percent() -> None:
    trip, message = should_trip_circuit_breaker(make_bot(day_start_equity=1000.0), 969.0)
    assert trip and "DEVRE KESİCİ" in message


def test_circuit_breaker_silent_within_limit() -> None:
    trip, _ = should_trip_circuit_breaker(make_bot(day_start_equity=1000.0), 985.0)
    assert not trip


def test_max_drawdown_stops_bot() -> None:
    trip, message = should_trip_circuit_breaker(
        make_bot(peak_equity=1000.0, day_start_equity=850.0), 840.0)
    assert trip and "DRAWDOWN" in message


# --------------------------------------------------------------------------- #
#  Toparlanma motoru
# --------------------------------------------------------------------------- #


def test_recovery_activates_on_drawdown() -> None:
    bot = make_bot(peak_equity=1000.0)
    active, message = update_recovery_state(bot, 940.0)
    assert active and "TOPARLANMA" in message


def test_recovery_closes_when_back_near_peak() -> None:
    bot = make_bot(peak_equity=1000.0, recovery_mode=True, consecutive_losses=0)
    active, message = update_recovery_state(bot, 995.0)
    assert not active and "TAMAMLANDI" in message


def test_recovery_never_increases_risk() -> None:
    """Martingale koruması: zarar arttıkça risk ASLA büyümez."""
    normal = effective_risk_pct(make_bot(risk_pct=1.0), equity=1000.0)
    after_losses = effective_risk_pct(
        make_bot(risk_pct=1.0, consecutive_losses=5, peak_equity=1000.0), equity=950.0)
    assert after_losses < normal


# --------------------------------------------------------------------------- #
#  Pozisyon yönetimi
# --------------------------------------------------------------------------- #


def test_breakeven_moves_stop_to_entry() -> None:
    bot = make_bot(breakeven_at_r=1.0, trailing_stop=False)
    new_stop, note = manage_open_position(bot, make_position(), 102.5, atr_value=1.0)
    assert new_stop is not None and new_stop >= 100.0
    assert "başabaş" in note.lower()


def test_trailing_stop_never_moves_backwards() -> None:
    bot = make_bot(trailing_stop=True, breakeven_at_r=1.0)
    position = make_position(stop_loss=104.0)
    new_stop, _ = manage_open_position(bot, position, 103.0, atr_value=1.0)
    assert new_stop is None or new_stop >= 104.0


def test_stop_loss_priority_when_both_hit_in_same_candle() -> None:
    """Aynı mumda SL ve TP birlikte tetiklenirse muhafazakâr davranılır."""
    result = check_exit(make_position(), high=107.0, low=97.0, price=100.0)
    assert result is not None and result[0] == "STOP_LOSS"


def test_take_profit_detected() -> None:
    result = check_exit(make_position(), high=107.0, low=101.0, price=106.5)
    assert result is not None and result[0] == "TAKE_PROFIT"


def test_short_position_exits_mirror_long() -> None:
    short = make_position(side="short", entry_price=100.0, stop_loss=102.0,
                          take_profit=94.0)
    assert check_exit(short, high=103.0, low=99.0, price=102.0)[0] == "STOP_LOSS"
    assert check_exit(short, high=101.0, low=93.0, price=94.0)[0] == "TAKE_PROFIT"
