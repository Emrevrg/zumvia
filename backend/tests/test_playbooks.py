"""
Sistem botu (playbook) kütüphanesi testleri.

Buradaki asıl iddia şudur: **yapay zeka bot yazmaz.** Sistemler kodun içinde
sabittir, seçim skoru deterministiktir ve risk tavanları kütüphane tarafında
zorlanır. Aşağıdaki testler tam olarak bunu doğrular.
"""
from __future__ import annotations

import inspect
import re

import pytest

from app.core.config import settings
from app.layers.l2_indicators import classify_regime
from app.layers.playbooks import (
    PLAYBOOKS,
    REGIME_MAP,
    get_playbook,
    playbook_catalog,
    recommend_playbooks,
    score_playbook,
)
from app.layers.strategies import STRATEGIES

# --------------------------------------------------------------------------- #
#  Kütüphane bütünlüğü
# --------------------------------------------------------------------------- #

def test_every_playbook_uses_only_real_strategies() -> None:
    """Playbook, var olmayan bir stratejiye atıfta bulunamaz."""
    for pb in PLAYBOOKS.values():
        unknown = [s for s in pb.strategies if s not in STRATEGIES]
        assert not unknown, f"{pb.id} bilinmeyen strateji kullanıyor: {unknown}"


def test_min_agree_never_exceeds_strategy_count() -> None:
    """Ulaşılamayacak hemfikirlik eşiği koyan playbook sonsuza kadar bekler."""
    for pb in PLAYBOOKS.values():
        assert 1 <= pb.min_agree <= len(pb.strategies), pb.id


def test_risk_never_exceeds_hard_ceiling() -> None:
    """Kütüphane, sistem genelindeki risk tavanını aşamaz."""
    for pb in PLAYBOOKS.values():
        assert pb.risk_pct <= settings.hard_max_risk_pct, pb.id
        assert pb.risk_pct > 0, pb.id


def test_every_playbook_declares_its_weakness() -> None:
    """Zayıf yanını gizleyen sistem kullanıcıyı yanıltır — zorunlu alan."""
    for pb in PLAYBOOKS.values():
        assert len(pb.weakness) > 20, pb.id
        assert len(pb.avoid_when) > 10, pb.id
        assert len(pb.thesis) > 20, pb.id


def test_poll_interval_is_sane() -> None:
    for pb in PLAYBOOKS.values():
        assert pb.poll_seconds >= 60, pb.id


def test_catalog_is_serializable_and_complete() -> None:
    rows = playbook_catalog()
    assert len(rows) == len(PLAYBOOKS)
    for row in rows:
        assert set(row) >= {"id", "label", "thesis", "strategies", "weakness"}


# --------------------------------------------------------------------------- #
#  Rejim eşlemesi — sessizce bozulması en tehlikeli yer
# --------------------------------------------------------------------------- #

def test_every_regime_label_is_mapped() -> None:
    """
    classify_regime() yeni bir etiket üretmeye başlarsa eşleme kaçar ve
    öneriler sessizce anlamsızlaşır. Bu test o sessiz bozulmayı yakalar.
    """
    produced = set(re.findall(r'return "([^"]+)"', inspect.getsource(classify_regime)))
    unmapped = produced - set(REGIME_MAP)
    assert not unmapped, f"eşlenmemiş rejim etiketi: {unmapped}"


@pytest.mark.parametrize("regime,expected", [
    ("YATAY_RANGE", "range_harvester"),
    ("SIKIŞMA_DÜŞÜK_VOLATİLİTE", "breakout_hunter"),
])
def test_regime_picks_the_right_specialist(regime: str, expected: str) -> None:
    atr = 0.5 if "SIKIŞMA" in regime else 1.2
    picks = recommend_playbooks(regime=regime, atr_pct=atr, market="crypto", limit=1)
    assert picks[0]["id"] == expected


def test_trend_regimes_prefer_trend_systems() -> None:
    picks = recommend_playbooks(regime="GÜÇLÜ_YÜKSELİŞ_TRENDİ", atr_pct=2.0,
                                market="crypto", limit=3)
    trend_families = {"trend_rider", "pullback_sniper", "swing_core",
                      "trend_pyramid", "dual_momentum"}
    assert trend_families & {p["family"] for p in picks}
    assert "range_harvester" not in {p["family"] for p in picks}


# --------------------------------------------------------------------------- #
#  Güvenlik davranışı
# --------------------------------------------------------------------------- #

def test_recovery_phase_forces_capital_guard() -> None:
    """Zarardayken kütüphane, agresif sistem yerine sermaye korumayı önerir."""
    for phase in ("defensive", "preservation", "lockdown"):
        picks = recommend_playbooks(regime="GÜÇLÜ_YÜKSELİŞ_TRENDİ", atr_pct=2.0,
                                    market="crypto", phase=phase, limit=1)
        assert picks[0]["id"] == "capital_guard", phase


def test_recovery_penalizes_high_risk_systems() -> None:
    aggressive = PLAYBOOKS["trend_rider"]
    normal, _ = score_playbook(aggressive, regime="GÜÇLÜ_YÜKSELİŞ_TRENDİ",
                               atr_pct=2.0, market="crypto")
    drawdown, why = score_playbook(aggressive, regime="GÜÇLÜ_YÜKSELİŞ_TRENDİ",
                                   atr_pct=2.0, market="crypto", phase="preservation")
    assert drawdown < normal
    assert any("kurtarma" in w for w in why)


def test_without_ai_only_deterministic_systems_are_offered() -> None:
    """Model erişimi yokken sistem durmaz; deterministik motora geçer."""
    picks = recommend_playbooks(regime="YATAY_RANGE", market="crypto",
                                ai_available=False, limit=5)
    assert picks, "yapay zeka yokken hiç öneri üretilmedi"
    for pick in picks:
        assert PLAYBOOKS[pick["id"]].decision_mode == "algo_only"


def test_scoring_is_deterministic() -> None:
    """Aynı girdi her zaman aynı skoru vermeli (yeniden üretilebilirlik)."""
    args = {"regime": "YATAY_RANGE", "atr_pct": 1.1, "market": "crypto"}
    first = recommend_playbooks(**args, limit=5)
    for _ in range(5):
        assert recommend_playbooks(**args, limit=5) == first


def test_unknown_playbook_returns_none() -> None:
    assert get_playbook("boyle_bir_sistem_yok") is None


# --------------------------------------------------------------------------- #
#  Önlemler — "sistemin zayıf yanı açıkta kalmasın"
# --------------------------------------------------------------------------- #

def test_every_playbook_has_guards() -> None:
    """Önlemsiz sistem, bilinen zaafıyla sahaya çıkmış demektir."""
    naked = [pb.id for pb in PLAYBOOKS.values() if not pb.guards]
    assert not naked, f"önlemsiz sistem: {naked[:5]}"


def test_guards_are_known_filters() -> None:
    """Motorun tanımadığı bir önlem sessizce yok sayılırdı."""
    from app.layers.guards import GUARD_LABELS  # noqa: PLC0415

    for pb in PLAYBOOKS.values():
        unknown = set(pb.guards) - set(GUARD_LABELS)
        assert not unknown, f"{pb.id} bilinmeyen önlem: {unknown}"


def test_trend_systems_refuse_flat_markets() -> None:
    """Trend sistemi yatay piyasada testere yememeli: ADX tabanı zorunlu."""
    for pid in ("trend_rider", "swing_core", "trend_pyramid", "crash_defense"):
        assert PLAYBOOKS[pid].guards.get("adx_min"), pid


def test_range_systems_step_aside_in_trends() -> None:
    """Bant sistemi trend başlayınca en büyük kaybı verir: ADX tavanı zorunlu."""
    for pid in ("range_harvester", "gap_reversion"):
        assert PLAYBOOKS[pid].guards.get("adx_max"), pid


def test_breakout_systems_require_volume() -> None:
    """Hacimsiz kırılım tuzaktır."""
    for pid in ("breakout_hunter", "volatility_breakout"):
        assert PLAYBOOKS[pid].guards.get("volume_z_min"), pid


def test_library_is_large_and_diverse() -> None:
    """Kütüphane hem geniş hem de gerçekten çeşitli olmalı."""
    assert len(PLAYBOOKS) >= 100
    families = {pb.family or pb.id for pb in PLAYBOOKS.values()}
    assert len(families) >= 12
    timeframes = {pb.timeframe for pb in PLAYBOOKS.values()}
    assert len(timeframes) >= 4


def test_variants_never_exceed_family_risk_ceiling() -> None:
    """Sürümler ailenin riskini AŞAMAZ — yalnızca eşitler veya küçültür."""
    families = {pb.id: pb for pb in PLAYBOOKS.values() if not pb.family or pb.family == pb.id}
    for pb in PLAYBOOKS.values():
        parent = families.get(pb.family)
        if parent is None or parent.id == pb.id:
            continue
        assert pb.risk_pct <= parent.risk_pct + 1e-9, pb.id


def test_recommendation_shows_different_approaches() -> None:
    """Öneri listesi aynı ailenin sürümleriyle dolmamalı."""
    picks = recommend_playbooks(regime="GÜÇLÜ_YÜKSELİŞ_TRENDİ", atr_pct=2.0,
                                market="crypto", limit=4)
    families = [p["family"] for p in picks]
    assert len(families) == len(set(families))
