"""
NULL TOLERANSI — eski satırlar paneli çökertemez
===============================================
Şema göçü yalnızca ekleyicidir (`ADD COLUMN`); eski satırlarda sonradan
eklenen kolonlar NULL kalabilir. Serileştirici `round(x)` / `x + y` yaparken
NULL 500 üretir ve arayüz çöker ("butona basınca çökme" sınıfı arıza).

Serileştiriciler ORM tipine değil, OKUDUKLARI ALANLARA bağlıdır; bu yüzden
test doğrudan None taşıyan kayıtlarla çalışır (DB kısıtını baypas eder —
sınanan şey sunum katmanıdır, şema değil).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import num
from app.api.routes_bots import serialize_bot, serialize_position
from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import create_access_token, hash_password
from app.engine.reporting import build_report
from app.layers.portfolio_risk import (
    check_portfolio_limits,
    open_risk_amount,
    portfolio_heat,
    portfolio_summary,
)
from app.layers.recovery import build_recovery_plan
from app.main import app
from app.models import Bot, Position, PositionStatus, Side, TradingMode, User


def _null_bot(**over) -> SimpleNamespace:
    base = {
        "id": 1, "name": "Eski", "market": "demo", "exchange": "demo",
        "symbol": "BTC/USDT", "timeframe": "1h",
        "mode": SimpleNamespace(value="paper"),
        "autonomy": SimpleNamespace(value="full"),
        "decision_mode": "algo_only", "status": SimpleNamespace(value="stopped"),
        "poll_seconds": None, "allow_short": None,
        "llm_credential_id": None, "llm_model": None,
        "exchange_credential_id": None, "telegram_credential_id": None,
        "strategies_json": None, "min_agree": None, "strategy_notes": None,
        "risk_pct": None, "daily_loss_limit_pct": None, "min_confidence": None,
        "min_rr": None, "max_open_positions": None, "max_trades_per_day": None,
        "max_drawdown_pct": None, "trailing_stop": None, "breakeven_at_r": None,
        "recovery_mode": None, "consecutive_losses": None,
        "locked_until": None, "lock_reason": None, "day_trades": None,
        "paper_balance": None, "initial_balance": None, "peak_equity": None,
        "day_start_equity": None, "council_mode": None,
        "last_run_at": None, "created_at": None, "user": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _null_pos(**over) -> SimpleNamespace:
    base = {
        "id": 7, "bot_id": 1, "symbol": "BTC/USDT",
        "side": SimpleNamespace(value="long"),
        "status": SimpleNamespace(value="closed"),
        "mode": SimpleNamespace(value="paper"),
        "qty": None, "entry_price": None, "stop_loss": None,
        "take_profit": None, "initial_stop": None, "risk_amount": None,
        "notional": None, "exit_price": None, "pnl": None, "pnl_pct": None,
        "r_multiple": None, "fees": None, "confidence": None,
        "reasoning": None, "close_reason": None,
        "opened_at": None, "closed_at": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_num_coerces_safely() -> None:
    assert num(None) == 0.0
    assert num("bozuk", 3.0) == 3.0
    assert num(float("nan")) == 0.0
    assert num(float("inf")) == 0.0
    assert num("12.5") == 12.5
    assert num(7) == 7.0


def test_serialize_position_never_crashes_on_none() -> None:
    out = serialize_position(_null_pos())  # type: ignore[arg-type]
    assert out["pnl"] == 0.0
    assert out["r_multiple"] == 0.0
    assert out["exit_price"] is None
    assert out["reasoning"] == ""


def test_open_risk_amount_falls_back() -> None:
    assert open_risk_amount(_null_pos()) == 0.0
    assert open_risk_amount(_null_pos(risk_amount=42.0)) == 42.0
    assert portfolio_heat([_null_pos()], 1000.0) == 0.0


def test_unmeasured_open_risk_fails_closed() -> None:
    position = _null_pos(status=SimpleNamespace(value="open"))
    verdict = check_portfolio_limits(
        equity=1000.0,
        new_risk_amount=10.0,
        open_positions=[position],
        new_symbol="ETH/USDT",
        new_side="long",
    )
    assert verdict.allowed is False
    assert verdict.code == "UNMEASURED_OPEN_RISK"
    summary = portfolio_summary([position], 1000.0)
    assert summary["measurement_complete"] is False
    assert summary["unmeasured_positions"] == 1


def test_recovery_plan_survives_none() -> None:
    plan = build_recovery_plan(_null_bot(), 0.0)
    assert plan.to_dict()["phase"] in (
        "normal", "savunma", "sermaye_koruma", "kilit")


def test_serialize_bot_never_crashes_on_none() -> None:
    db = SessionLocal()
    try:
        out = serialize_bot(db, _null_bot())  # type: ignore[arg-type]
    finally:
        db.close()
    assert out["capital"]["balance"] == 0.0
    assert isinstance(out["risk"]["effective_risk_pct"], (int, float))
    assert out["stats"]["total_trades"] == 0


def test_build_report_never_crashes_on_none_bot() -> None:
    db = SessionLocal()
    try:
        user = db.query(User).order_by(User.id).first()
        assert user is not None
        out = build_report(db, user)
    finally:
        db.close()
    assert "capital" in out and "performance" in out


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def nullable_case():
    """Gerçekten NULL olabilen kolon (exit_price) üzerinden uçtan uca kanıt."""
    db = SessionLocal()
    email = "nullable@zumvia.com"
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(email=email, password_hash=hash_password("nullable-12345"),
                    vault_salt=new_salt())
        db.add(user)
        db.commit()
        db.refresh(user)
    bot = Bot(user_id=user.id, name="Cikisiz", market="demo", exchange="demo",
              symbol="BTC/USDT", timeframe="1h")
    db.add(bot)
    db.commit()
    db.refresh(bot)
    pos = Position(bot_id=bot.id, symbol="BTC/USDT", side=Side.LONG,
                   status=PositionStatus.CLOSED, mode=TradingMode.PAPER,
                   qty=1.0, entry_price=100.0, stop_loss=98.0,
                   take_profit=104.0, pnl=5.0, r_multiple=0.5)
    db.add(pos)
    db.commit()
    db.execute(text("UPDATE positions SET exit_price = NULL WHERE id = :i"),
               {"i": pos.id})
    db.commit()
    data = {"token": create_access_token(user.id, email), "bot_id": bot.id,
            "user_id": user.id}
    db.close()
    yield data

    db = SessionLocal()
    db.query(Position).filter(Position.bot_id == data["bot_id"]).delete(
        synchronize_session=False)
    db.query(Bot).filter(Bot.id == data["bot_id"]).delete(
        synchronize_session=False)
    db.commit()
    db.close()


def test_nullable_exit_price_end_to_end(client, nullable_case) -> None:
    headers = {"Authorization": f"Bearer {nullable_case['token']}"}
    response = client.get(f"/api/bots/{nullable_case['bot_id']}/positions",
                          headers=headers)
    assert response.status_code == 200
    assert response.json()["closed"][0]["exit_price"] is None
