"""
Profesyonel motor testleri: portföy riski, toparlanma planı, kısmi kâr,
walk-forward doğrulama, tarayıcı, konsey ve canlı yetki kapısı.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.agent.council import (
    CouncilMember,
    MemberOpinion,
    ModelCouncil,
    member_weight,
)
from app.core.config import settings
from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.safety import (
    can_enable_live,
    grant_live_authorization,
    kill_switch_active,
    live_authorization_status,
    revoke_live_authorization,
    set_kill_switch,
)
from app.core.security import hash_password
from app.engine.optimizer import walk_forward
from app.engine.scanner import scan_markets
from app.layers.l1_market_data import fetch_ohlcv
from app.layers.l4_risk import check_partial_take_profit
from app.layers.portfolio_risk import (
    check_portfolio_limits,
    cluster_of,
    correlated_exposure,
    open_risk_amount,
    portfolio_heat,
    returns_correlation,
)
from app.layers.recovery import build_recovery_plan, phase_for
from app.main import app
from app.models import ModelScore, User


def make_position(**overrides) -> SimpleNamespace:
    base = {"side": "long", "entry_price": 100.0, "stop_loss": 98.0, "take_profit": 106.0,
                "initial_stop": 98.0, "qty": 5.0, "risk_amount": 10.0, "symbol": "BTC/USDT",
                "partial_taken": False}
    base.update(overrides)
    return SimpleNamespace(**base)


def make_bot(**overrides) -> SimpleNamespace:
    base = {"risk_pct": 1.0, "peak_equity": 1000.0, "paper_balance": 1000.0,
                "consecutive_losses": 0, "partial_tp_enabled": True, "partial_tp_at_r": 1.5,
                "partial_tp_fraction": 0.5, "max_portfolio_heat_pct": 3.0,
                "max_spread_pct": 0.15, "exchange_credential_id": 1}
    base.update(overrides)
    return SimpleNamespace(**base)


# =========================================================================== #
#  1. Portföy riski
# =========================================================================== #

def test_open_risk_is_zero_when_stop_in_profit() -> None:
    """Stop kâra çekilmişse o pozisyon artık portföy ısısına yük bindirmez."""
    risky = make_position(stop_loss=98.0)
    riskfree = make_position(stop_loss=101.0)
    assert open_risk_amount(risky) == pytest.approx(10.0)
    assert open_risk_amount(riskfree) == 0.0


def test_portfolio_heat_sums_open_risk() -> None:
    positions = [make_position(), make_position(qty=2.5)]
    assert portfolio_heat(positions, 1000.0) == pytest.approx(1.5)


def test_portfolio_blocks_when_heat_exceeded() -> None:
    positions = [make_position(qty=10.0)]      # %2 risk
    verdict = check_portfolio_limits(
        equity=1000.0, new_risk_amount=15.0, open_positions=positions,
        new_symbol="ETH/USDT", new_side="long", max_heat_pct=3.0,
    )
    assert not verdict.allowed and verdict.code == "PORTFOLIO_HEAT"


def test_portfolio_allows_within_heat() -> None:
    verdict = check_portfolio_limits(
        equity=1000.0, new_risk_amount=10.0, open_positions=[],
        new_symbol="BTC/USDT", new_side="long", max_heat_pct=3.0,
    )
    assert verdict.allowed


def test_correlated_cluster_is_blocked() -> None:
    """BTC long açıkken ETH long ayrı işlem değil, aynı bahsin büyütülmesidir."""
    positions = [make_position(symbol="BTC/USDT", side="long")]
    verdict = check_portfolio_limits(
        equity=10000.0, new_risk_amount=10.0, open_positions=positions,
        new_symbol="ETH/USDT", new_side="long", max_heat_pct=5.0,
    )
    assert not verdict.allowed and verdict.code == "CORRELATED_CLUSTER"


def test_opposite_side_is_not_cluster_risk() -> None:
    positions = [make_position(symbol="BTC/USDT", side="long")]
    exposure = correlated_exposure("ETH/USDT", "short", positions)
    assert exposure["count"] == 0


def test_wide_spread_is_rejected() -> None:
    verdict = check_portfolio_limits(
        equity=1000.0, new_risk_amount=5.0, open_positions=[],
        new_symbol="XYZ/USDT", new_side="long",
        spread_pct=0.9, max_spread_pct=0.15,
    )
    assert not verdict.allowed and verdict.code == "WIDE_SPREAD"


def test_real_correlation_overrides_static_cluster() -> None:
    """Gerçek getiri korelasyonu varsa statik küme haritası yerine o kullanılır."""
    index = pd.date_range("2024-01-01", periods=200, freq="1h", tz="UTC")
    rng = np.random.default_rng(7)
    a = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 200))), index=index)
    b = pd.Series(50 * np.exp(np.cumsum(rng.normal(0, 0.01, 200))), index=index)

    correlation = returns_correlation(a, b)
    assert correlation is not None and -1.0 <= correlation <= 1.0

    positions = [make_position(symbol="BTC/USDT", side="long")]
    exposure = correlated_exposure("ETH/USDT", "long", positions,
                                   {"BTC/USDT": a, "ETH/USDT": b})
    # Bağımsız seriler → küme riski yok (statik harita devre dışı kalır)
    assert exposure["count"] == 0


def test_cluster_mapping() -> None:
    assert cluster_of("BTC/USDT") == cluster_of("ETH/USDT")
    assert cluster_of("AAPL") != cluster_of("BTC/USDT")


# =========================================================================== #
#  2. Toparlanma planı
# =========================================================================== #

def test_phase_escalates_with_drawdown() -> None:
    assert phase_for(0.0).id == "normal"
    assert phase_for(6.0).id == "defensive"
    assert phase_for(12.0).id == "preservation"
    assert phase_for(25.0).id == "lockdown"


def test_consecutive_losses_tighten_phase_even_without_drawdown() -> None:
    assert phase_for(1.0, consecutive_losses=4).id in ("preservation", "lockdown")


def test_recovery_plan_reports_required_gain() -> None:
    """%20 kayıp → başabaş için %25 kazanç gerekir (asimetri)."""
    plan = build_recovery_plan(make_bot(peak_equity=1000.0), 800.0)
    assert plan.drawdown_pct == pytest.approx(20.0)
    assert plan.required_gain_pct == pytest.approx(25.0)
    assert plan.estimated_trades > 0
    assert "martingale" in " ".join(plan.rules).lower()


def test_recovery_plan_reduces_risk() -> None:
    normal = build_recovery_plan(make_bot(peak_equity=1000.0), 1000.0)
    stressed = build_recovery_plan(make_bot(peak_equity=1000.0), 850.0)
    assert stressed.effective_risk_pct < normal.effective_risk_pct


def test_recovery_plan_headline_is_human_readable() -> None:
    plan = build_recovery_plan(make_bot(peak_equity=1000.0), 880.0)
    assert "başabaş" in plan.headline.lower()
    assert plan.phase.id != "normal"


# =========================================================================== #
#  3. Kısmi kâr alma
# =========================================================================== #

def test_partial_tp_triggers_at_target_r() -> None:
    bot = make_bot(partial_tp_at_r=1.5, partial_tp_fraction=0.5)
    position = make_position(entry_price=100.0, initial_stop=98.0)
    assert check_partial_take_profit(bot, position, 102.0) is None       # 1.0R
    result = check_partial_take_profit(bot, position, 103.5)             # 1.75R
    assert result is not None and result[0] == pytest.approx(0.5)


def test_partial_tp_only_once() -> None:
    bot = make_bot()
    position = make_position(partial_taken=True)
    assert check_partial_take_profit(bot, position, 110.0) is None


def test_partial_tp_disabled_returns_none() -> None:
    bot = make_bot(partial_tp_enabled=False)
    assert check_partial_take_profit(bot, make_position(), 110.0) is None


def test_partial_tp_works_for_short() -> None:
    bot = make_bot(partial_tp_at_r=1.0)
    position = make_position(side="short", entry_price=100.0,
                             initial_stop=102.0, stop_loss=102.0)
    assert check_partial_take_profit(bot, position, 97.0) is not None


# =========================================================================== #
#  4. Walk-forward doğrulama ve tarayıcı
# =========================================================================== #

@pytest.fixture(scope="module")
def demo_df() -> pd.DataFrame:
    return fetch_ohlcv("demo", "demo", "BTC/USDT", "1h", 1000)


def test_walk_forward_reports_out_of_sample(demo_df: pd.DataFrame) -> None:
    report = walk_forward(demo_df, symbol="BTC/USDT", timeframe="1h",
                          min_agree=1, allow_short=True, folds=3)
    data = report.to_dict()
    assert "out_of_sample" in data
    assert data["verdict"]
    assert len(report.folds) >= 1


def test_walk_forward_detects_insufficient_data() -> None:
    small = fetch_ohlcv("demo", "demo", "ETH/USDT", "1h", 300)
    report = walk_forward(small, min_agree=1)
    assert "yeterli veri yok" in report.verdict.lower() or report.oos_trades == 0


def test_walk_forward_never_claims_robust_without_trades(demo_df: pd.DataFrame) -> None:
    report = walk_forward(demo_df, min_agree=8, folds=3)   # imkânsız eşik
    assert report.robust is False


def test_scanner_ranks_candidates() -> None:
    result = scan_markets(market="demo", exchange="demo",
                          symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
                          timeframe="1h", min_agree=1, top=3,
                          only_actionable=False, check_spread=False)
    assert result["scanned"] == 3
    assert result["headline"]
    scores = [c["score"] for c in result["candidates"]]
    assert scores == sorted(scores, reverse=True)


def test_scanner_handles_bad_symbol_gracefully() -> None:
    result = scan_markets(market="crypto", exchange="binance",
                          symbols=["KESINLIKLE/YOK"], timeframe="1h",
                          only_actionable=False, check_spread=False)
    assert result["scanned"] == 1
    assert result["failed"] or not result["candidates"]


# =========================================================================== #
#  5. Konsey
# =========================================================================== #

@pytest.fixture(scope="module")
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture(scope="module")
def user(db):
    existing = db.query(User).filter(User.email == "council@zumvia.com").first()
    if existing:
        return existing
    created = User(email="council@zumvia.com",
                   password_hash=hash_password("council12345"),
                   vault_salt=new_salt())
    db.add(created)
    db.commit()
    db.refresh(created)
    return created


def _member(model: str, role: str = "analyst", weight: float = 1.0) -> CouncilMember:
    return CouncilMember(label=model, credential_id=0, provider="test",
                         model=model, role=role, weight=weight)


def _opinion(member: CouncilMember, action: str, confidence: float,
             stop: float, target: float) -> MemberOpinion:
    return MemberOpinion(member=member, ok=True, action=action, confidence=confidence,
                         stop_loss=stop, take_profit=target, reasoning=f"{model_reason(action)}")


def model_reason(action: str) -> str:
    return {"BUY": "trend yukarı", "SELL": "trend aşağı", "WAIT": "belirsiz"}[action]


class StubCouncil(ModelCouncil):
    """Gerçek model çağrısı yapmadan konsey mantığını test eder."""

    def __init__(self, db, user, members, opinions, critique=None, arbitration=None):
        super().__init__(db, user, members)
        self._opinions = opinions
        self._critique = critique
        self._arbitration = arbitration

    def _ask_analyst(self, member, snapshot, context, recovery, extra_rules):  # noqa: ARG002
        return next(o for o in self._opinions if o.member.model == member.model)

    def _ask_critic(self, proposal, snapshot, context, opinions):  # noqa: ARG002
        return self._critique

    def _ask_arbiter(self, snapshot, context, opinions):  # noqa: ARG002
        return self._arbitration


def test_council_agrees_and_uses_median_levels(db, user) -> None:
    members = [_member("m1"), _member("m2"), _member("m3")]
    opinions = [
        _opinion(members[0], "BUY", 0.85, 98.0, 110.0),
        _opinion(members[1], "BUY", 0.80, 99.0, 112.0),
        _opinion(members[2], "BUY", 0.90, 98.5, 111.0),
    ]
    council = StubCouncil(db, user, members, opinions)
    verdict = council.deliberate({"price": 100}, {})

    assert verdict.action == "BUY"
    assert verdict.stop_loss == pytest.approx(98.5)     # medyan — modelden değil koddan
    assert verdict.take_profit == pytest.approx(111.0)
    assert verdict.agreement_pct > 90


def test_council_waits_when_split(db, user) -> None:
    """Bölünmüş konsey işlem açmaz."""
    members = [_member("m1"), _member("m2")]
    opinions = [
        _opinion(members[0], "BUY", 0.85, 98.0, 110.0),
        _opinion(members[1], "SELL", 0.85, 102.0, 90.0),
    ]
    council = StubCouncil(db, user, members, opinions,
                          arbitration={"action": "WAIT", "reasoning": "kararsız",
                                       "model": "arb"})
    verdict = council.deliberate({"price": 100}, {})
    assert verdict.action == "WAIT"


def test_council_respects_risk_critic_veto(db, user) -> None:
    members = [_member("m1"), _member("m2")]
    opinions = [
        _opinion(members[0], "BUY", 0.9, 98.0, 110.0),
        _opinion(members[1], "BUY", 0.9, 98.2, 111.0),
    ]
    council = StubCouncil(db, user, members, opinions,
                          critique={"verdict": "VETO", "reason": "trend tersine",
                                    "model": "critic"})
    verdict = council.deliberate({"price": 100}, {})
    assert verdict.action == "WAIT" and verdict.veto is True
    assert "trend tersine" in verdict.veto_reason


def test_council_blocks_on_level_dispersion(db, user) -> None:
    """Yön aynı ama seviyeler çok ayrışıksa kanaat zayıftır → işlem yok."""
    members = [_member("m1"), _member("m2")]
    opinions = [
        _opinion(members[0], "BUY", 0.9, 99.0, 110.0),
        _opinion(members[1], "BUY", 0.9, 60.0, 180.0),
    ]
    council = StubCouncil(db, user, members, opinions)
    verdict = council.deliberate({"price": 100}, {})
    assert verdict.action == "WAIT"


def test_council_blocks_when_algo_disagrees(db, user) -> None:
    members = [_member("m1"), _member("m2")]
    opinions = [
        _opinion(members[0], "BUY", 0.9, 98.0, 110.0),
        _opinion(members[1], "BUY", 0.9, 98.5, 111.0),
    ]
    council = StubCouncil(db, user, members, opinions)
    verdict = council.deliberate({"price": 100}, {}, algo_action="SELL")
    assert verdict.action == "WAIT"


def test_council_fails_safe_when_no_model_responds(db, user) -> None:
    members = [_member("m1")]
    opinions = [MemberOpinion(member=members[0], ok=False, error="timeout")]
    council = StubCouncil(db, user, members, opinions)
    verdict = council.deliberate({"price": 100}, {})
    assert verdict.action == "WAIT" and verdict.responded == 0


def test_model_weight_defaults_to_one(db, user) -> None:
    assert member_weight(db, user.id, "test", "brand-new-model") == 1.0


def test_model_weight_follows_track_record(db, user) -> None:
    db.query(ModelScore).filter(ModelScore.user_id == user.id,
                                ModelScore.model == "scored").delete()
    db.add(ModelScore(user_id=user.id, provider="test", model="scored",
                      decisions=30, trades=20, wins=14, losses=6, total_r=8.0))
    db.commit()
    weight = member_weight(db, user.id, "test", "scored")
    assert weight > 1.0          # iyi sicil → daha çok söz hakkı

    db.query(ModelScore).filter(ModelScore.user_id == user.id,
                                ModelScore.model == "scored").delete()
    db.add(ModelScore(user_id=user.id, provider="test", model="scored",
                      decisions=30, trades=20, wins=4, losses=16, total_r=-9.0))
    db.commit()
    assert member_weight(db, user.id, "test", "scored") < 1.0


# =========================================================================== #
#  6. Canlı yetki ve kill switch
# =========================================================================== #

@pytest.fixture()
def live_user(db):
    existing = db.query(User).filter(User.email == "live@zumvia.com").first()
    if existing is None:
        existing = User(email="live@zumvia.com",
                        password_hash=hash_password("live12345"),
                        vault_salt=new_salt())
        db.add(existing)
        db.commit()
        db.refresh(existing)
    yield existing
    revoke_live_authorization(db, existing)
    db.commit()


def test_live_requires_explicit_authorization(db, live_user) -> None:
    status = live_authorization_status(live_user)
    assert status["authorized"] is False

    gate = can_enable_live(db, live_user, make_bot(), 100.0)
    assert not gate["allowed"] and gate["code"] == "NO_AUTHORIZATION"


def test_live_authorization_has_capital_ceiling(db, live_user) -> None:
    grant_live_authorization(db, live_user, max_capital=200.0, hours=24)
    db.commit()

    within = can_enable_live(db, live_user, make_bot(), 150.0)
    assert within["allowed"] is True

    over = can_enable_live(db, live_user, make_bot(), 500.0)
    assert not over["allowed"] and over["code"] == "OVER_CAPITAL_LIMIT"


def test_live_authorization_expires(db, live_user) -> None:
    grant_live_authorization(db, live_user, max_capital=100.0, hours=1)
    live_user.live_authorized_until = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()

    status = live_authorization_status(live_user)
    assert status["authorized"] is False and status.get("expired") is True


def test_revoke_is_immediate(db, live_user) -> None:
    grant_live_authorization(db, live_user, max_capital=100.0, hours=24)
    db.commit()
    assert live_authorization_status(live_user)["authorized"] is True

    revoke_live_authorization(db, live_user)
    db.commit()
    assert live_authorization_status(live_user)["authorized"] is False


def test_live_requires_exchange_key(db, live_user) -> None:
    grant_live_authorization(db, live_user, max_capital=500.0, hours=24)
    db.commit()
    gate = can_enable_live(db, live_user, make_bot(exchange_credential_id=None), 100.0)
    assert not gate["allowed"] and gate["code"] == "NO_EXCHANGE_KEY"


def test_kill_switch_blocks_live_enable(db, live_user) -> None:
    grant_live_authorization(db, live_user, max_capital=500.0, hours=24)
    set_kill_switch(db, live_user, True, "test")
    db.commit()
    try:
        assert kill_switch_active() is True
        gate = can_enable_live(db, live_user, make_bot(), 100.0)
        assert not gate["allowed"] and gate["code"] == "KILL_SWITCH"
    finally:
        set_kill_switch(db, live_user, False, "test bitti")
        db.commit()


# =========================================================================== #
#  7. API kapıları
# =========================================================================== #

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth(client):
    payload = {"email": "proapi@zumvia.com", "password": "proapi12345"}
    r = client.post("/api/auth/register", json=payload)
    if r.status_code == 409:
        r = client.post("/api/auth/login", json=payload)
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_live_authorization_rejects_wrong_confirmation(client, auth) -> None:
    r = client.post("/api/safety/live-authorization", headers=auth,
                    json={"max_capital": 100, "confirmation": "tamam"})
    assert r.status_code == 400


def test_agent_cannot_grant_itself_live_authorization(client, auth) -> None:
    """Ajanın araç setinde yetki VERME aracı yoktur — yalnızca kullanma vardır."""
    from app.agent.tools import REGISTRY

    assert "grant_live_authorization" not in REGISTRY
    assert "enable_live_trading" in REGISTRY
    assert "disable_live_trading" in REGISTRY


def test_enable_live_blocked_without_authorization(client, auth) -> None:
    bot = client.post("/api/bots", headers=auth, json={
        "name": "Canlı Deneme", "market": "demo", "exchange": "demo",
        "decision_mode": "algo_only",
    }).json()

    result = client.post("/api/agent/tool", headers=auth, json={
        "name": "enable_live_trading",
        "arguments": {"bot_id": bot["id"], "capital": 100, "reason": "test"},
    }).json()["result"]

    assert result["enabled"] is False
    assert result["code"] in ("NO_AUTHORIZATION", "KILL_SWITCH", "NO_EXCHANGE_KEY")
    client.delete(f"/api/bots/{bot['id']}", headers=auth)


def test_safety_status_endpoint(client, auth) -> None:
    data = client.get("/api/safety/status", headers=auth).json()
    assert "kill_switch" in data and "live_authorization" in data
    assert data["hard_limits"]["max_risk_pct"] <= settings.hard_max_risk_pct


def test_report_generation(client, auth) -> None:
    result = client.post("/api/safety/reports", headers=auth).json()
    assert result["ok"] is True
    assert "ZUMVIA" in result["markdown"]
    assert "tavsiye değildir" in result["markdown"]
    assert result["report"]["meta"]["run_id"]


def test_new_pro_tools_are_registered(client, auth) -> None:
    tools = client.get("/api/agent/catalog").json()["tools"]
    names = {t["name"] for t in tools}
    for expected in ("scan_markets", "validate_strategy", "optimize_setup",
                     "get_recovery_plan", "get_portfolio_risk", "get_safety_status",
                     "activate_kill_switch", "get_model_scoreboard"):
        assert expected in names, expected
