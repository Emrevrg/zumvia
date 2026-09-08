"""
SERMAYE MÜHENDİSLİĞİ + HIZ SINIRI TESTLERİ

İki iddia doğrulanır:

1. Sermaye büyüdükçe sistem **daha agresif değil, daha dikkatli** olur:
   emri böler, likidite yetmiyorsa küçültür, tek enstrümana yığmaz.
2. Sağlayıcı kuralları **aşılmaz**: dakikalık sınır dolduğunda istek
   kuyruğa girer, kuyruk açılmazsa sistem fail-safe'ine düşer — asla
   429 yiyerek turu kaybetmez.
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pandas as pd
import pytest

from app.layers import rate_limit
from app.layers.capital import (
    DEFAULT_PARTICIPATION,
    MAX_SLICES,
    allocate,
    impact_estimate,
    liquidity_profile,
    plan_execution,
)


def frame(bar_volume: float = 1000.0, price: float = 100.0, rows: int = 120) -> pd.DataFrame:
    """Sabit hacimli, hafif dalgalı sentetik seri."""
    rng = np.random.default_rng(7)
    closes = price * (1 + rng.normal(0, 0.004, rows)).cumprod()
    return pd.DataFrame({
        "open": closes, "high": closes * 1.002, "low": closes * 0.998,
        "close": closes, "volume": [bar_volume] * rows,
    })


# --------------------------------------------------------------------------- #
#  Likidite profili
# --------------------------------------------------------------------------- #

def test_thin_market_is_flagged() -> None:
    profile = liquidity_profile(frame(bar_volume=1.0, price=10.0), "THIN/USDT", "1h")
    assert profile.tier == "thin"
    # Not: Türkçe "İ" harfinin .lower() karşılığı "i̇" (birleşik nokta) olduğu için
    # aramada bu harfi içermeyen bir parça kullanılır.
    assert any("piyasa" in note and "emir" in note for note in profile.notes)


def test_deep_market_is_recognised() -> None:
    profile = liquidity_profile(frame(bar_volume=50_000, price=1000.0), "DEEP/USDT", "1h")
    assert profile.tier == "deep"


def test_missing_volume_is_not_treated_as_infinite_liquidity() -> None:
    """Hacim yoksa 'sınırsız' varsayılırsa devasa emir geçerdi."""
    df = frame()
    df["volume"] = 0.0
    profile = liquidity_profile(df, "X/USDT", "1h")
    assert profile.tier == "unknown"
    assert profile.bar_turnover == 0.0


def test_empty_data_never_crashes() -> None:
    profile = liquidity_profile(pd.DataFrame(), "X/USDT", "1h")
    assert profile.tier == "unknown"


def test_median_is_used_so_one_spike_cannot_inflate_liquidity() -> None:
    """Tek haber barındaki devasa hacim sistemi yanıltmamalı."""
    df = frame(bar_volume=100.0)
    df.loc[df.index[-1], "volume"] = 10_000_000.0     # tek dev bar
    profile = liquidity_profile(df, "X/USDT", "1h")
    assert profile.bar_volume == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
#  Etki modeli
# --------------------------------------------------------------------------- #

def test_impact_grows_with_size() -> None:
    profile = liquidity_profile(frame(bar_volume=1000, price=100), "X/USDT", "1h")
    small = impact_estimate(1_000, profile)["impact_pct"]
    large = impact_estimate(1_000_000, profile)["impact_pct"]
    assert large > small


def test_impact_grows_sublinearly() -> None:
    """Karekök yasası: 100 kat emir, 100 kat etki YAPMAZ."""
    profile = liquidity_profile(frame(bar_volume=1000, price=100), "X/USDT", "1h")
    base = impact_estimate(10_000, profile)["impact_pct"]
    hundred = impact_estimate(1_000_000, profile)["impact_pct"]
    assert hundred < base * 100


def test_unknown_liquidity_reports_unknown_not_zero() -> None:
    df = frame()
    df["volume"] = 0.0
    profile = liquidity_profile(df, "X/USDT", "1h")
    assert impact_estimate(10_000, profile)["verdict"] == "unknown"


# --------------------------------------------------------------------------- #
#  Yürütme planı
# --------------------------------------------------------------------------- #

def test_small_order_goes_in_one_piece() -> None:
    profile = liquidity_profile(frame(bar_volume=10_000, price=100), "X/USDT", "1h")
    plan = plan_execution(1_000, profile, timeframe="1h")
    assert plan.style == "immediate"
    assert plan.slices == 1


def test_large_order_is_sliced() -> None:
    profile = liquidity_profile(frame(bar_volume=1000, price=100), "X/USDT", "1h")
    plan = plan_execution(60_000, profile, timeframe="1h")
    assert plan.style == "sliced"
    assert plan.slices > 1
    assert plan.slice_quote < plan.total_quote


def test_each_slice_respects_participation_rate() -> None:
    """Her parça, bar hacminin hedeflenen payını aşmamalı."""
    profile = liquidity_profile(frame(bar_volume=1000, price=100), "X/USDT", "1h")
    plan = plan_execution(60_000, profile, timeframe="1h",
                          participation=DEFAULT_PARTICIPATION)
    ceiling = profile.bar_turnover * DEFAULT_PARTICIPATION
    assert plan.slice_quote <= ceiling * 1.05          # yuvarlama payı


def test_oversized_order_is_reduced_not_forced() -> None:
    """
    Likiditeye sığmayan emir zorlanmaz; küçültülür ve sebebi yazılır.
    Bu, büyük sermayede en pahalı hatanın önlenmesidir.
    """
    profile = liquidity_profile(frame(bar_volume=100, price=10), "THIN/USDT", "1h")
    plan = plan_execution(50_000_000, profile, timeframe="1h")

    assert plan.style == "reduced"
    assert plan.total_quote < 50_000_000
    assert plan.reduced_from == 50_000_000
    assert plan.slices <= MAX_SLICES
    assert "likidite" in plan.reason.lower()


def test_unknown_liquidity_is_handled_conservatively() -> None:
    df = frame()
    df["volume"] = 0.0
    profile = liquidity_profile(df, "X/USDT", "1h")
    plan = plan_execution(10_000, profile)
    assert plan.style == "immediate"
    assert "ölçülemedi" in plan.reason


def test_zero_order_is_rejected() -> None:
    profile = liquidity_profile(frame(), "X/USDT", "1h")
    assert plan_execution(0, profile).style == "rejected"


# --------------------------------------------------------------------------- #
#  Sermaye dağıtımı
# --------------------------------------------------------------------------- #

def _candidates(n: int = 4, bar_volume: float = 5_000) -> list[dict]:
    return [{"symbol": f"SYM{i}/USDT", "score": 0.9 - i * 0.1,
             "liquidity": liquidity_profile(frame(bar_volume=bar_volume), f"SYM{i}/USDT", "1h")}
            for i in range(n)]


def test_capital_is_spread_across_instruments() -> None:
    plan = allocate(100_000, _candidates(4))
    assert plan["instruments"] > 1


def test_no_single_instrument_exceeds_concentration_cap() -> None:
    plan = allocate(100_000, _candidates(4), max_per_instrument_pct=30.0)
    for row in plan["allocations"]:
        assert row["share_pct"] <= 30.5


def test_illiquid_candidates_receive_less() -> None:
    """Likidite düşükse pay da düşük olmalı — zorlamak giriş fiyatını bozar."""
    candidates = [
        {"symbol": "DEEP/USDT", "score": 0.8,
         "liquidity": liquidity_profile(frame(bar_volume=100_000), "DEEP/USDT", "1h")},
        {"symbol": "THIN/USDT", "score": 0.8,
         "liquidity": liquidity_profile(frame(bar_volume=5), "THIN/USDT", "1h")},
    ]
    plan = allocate(1_000_000, candidates)
    amounts = {row["symbol"]: row["amount"] for row in plan["allocations"]}
    assert amounts.get("DEEP/USDT", 0) > amounts.get("THIN/USDT", 0)


def test_unallocatable_capital_stays_in_cash_with_reason() -> None:
    """Sığmayan sermaye zorla piyasaya sokulmaz; nakitte kalır ve açıklanır."""
    plan = allocate(50_000_000, _candidates(2, bar_volume=10))
    assert plan["unallocated"] > 0
    assert "nakitte" in plan["note"].lower()


def test_no_candidates_means_no_allocation() -> None:
    plan = allocate(10_000, [])
    assert plan["allocations"] == []
    assert plan["unallocated"] == 10_000


# --------------------------------------------------------------------------- #
#  Hız sınırı
# --------------------------------------------------------------------------- #

def test_known_provider_limits_are_registered() -> None:
    """Kullanıcının adını verdiği NVIDIA NIM sınırı tabloda olmalı."""
    assert rate_limit.PROVIDER_LIMITS["nvidia"].rpm == 40


def test_requests_within_limit_do_not_wait() -> None:
    rate_limit.configure("t_fast", rpm=100)
    started = time.monotonic()
    for _ in range(10):
        with rate_limit.acquire("t_fast"):
            pass
    assert time.monotonic() - started < 0.5


def test_exceeding_limit_rejects_instead_of_flooding() -> None:
    """
    Sınır dolduğunda istek gönderilmez. `max_wait` kısa tutulursa çağıran
    fail-safe'ine düşer — sağlayıcıya 429 yedirmeyiz.
    """
    rate_limit.configure("t_slow", rpm=2, concurrent=0)
    passed = 0
    with pytest.raises(rate_limit.RateLimitTimeout):
        for _ in range(5):
            with rate_limit.acquire("t_slow", max_wait=0.4):
                passed += 1
    assert passed == 2


def test_429_penalty_blocks_further_requests() -> None:
    rate_limit.configure("t_429", rpm=100)
    with rate_limit.acquire("t_429") as slot:
        slot.penalize(retry_after=1.0)

    with pytest.raises(rate_limit.RateLimitTimeout):
        with rate_limit.acquire("t_429", max_wait=0.2):
            pass


def test_limits_are_shared_across_threads() -> None:
    """
    Konsey ve bot motoru aynı sağlayıcıya paralel gidebilir; sayaç tek olmalı.
    Aksi hâlde sınır sessizce ikiye katlanırdı.
    """
    rate_limit.configure("t_thread", rpm=4)
    passed: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            with rate_limit.acquire("t_thread", max_wait=0.3):
                with lock:
                    passed.append(1)
        except rate_limit.RateLimitTimeout:
            pass

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(passed) == 4, f"sınır aşıldı: {len(passed)} istek geçti"


def test_configure_can_raise_limit_for_paid_tier() -> None:
    rate_limit.configure("t_paid", rpm=10)
    rate_limit.configure("t_paid", rpm=500)
    assert rate_limit.PROVIDER_LIMITS["t_paid"].rpm == 500


def test_snapshot_reports_usage() -> None:
    rate_limit.configure("t_snap", rpm=50)
    with rate_limit.acquire("t_snap"):
        pass
    row = rate_limit.snapshot()["t_snap"]
    assert row["limit_rpm"] == 50
    assert row["used_last_minute"] >= 1
    assert row["served"] >= 1
