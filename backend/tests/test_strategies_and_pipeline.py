"""Katman 2.5 + uçtan uca boru hattı testleri."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.engine.backtest import run_backtest
from app.layers.l1_market_data import fetch_ohlcv
from app.layers.strategies import STRATEGIES, StrategyEngine, strategy_catalog
from app.main import app


@pytest.fixture(scope="module")
def demo_df() -> pd.DataFrame:
    return fetch_ohlcv("demo", "demo", "BTC/USDT", "1h", 600)


@pytest.fixture()
def uptrend_df() -> pd.DataFrame:
    """Belirgin yükseliş trendi — trend stratejileri BUY üretmeli."""
    n = 400
    close = 100 * np.exp(np.cumsum(np.full(n, 0.004)))
    noise = np.abs(np.sin(np.arange(n) / 7)) * 0.2
    index = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "open": close - noise, "high": close + noise + 0.4,
        "low": close - noise - 0.3, "close": close,
        "volume": np.full(n, 1000.0) + noise * 500,
    }, index=index)


def test_every_strategy_returns_valid_signal(demo_df: pd.DataFrame) -> None:
    from app.layers.l2_indicators import compute_all

    enriched = compute_all(demo_df)
    for name, fn in STRATEGIES.items():
        signal = fn(enriched)
        assert signal.action in ("BUY", "SELL", "WAIT"), name
        assert 0.0 <= signal.confidence <= 1.0, name
        if signal.action in ("BUY", "SELL"):
            assert signal.stop_loss > 0 and signal.take_profit > 0, name


def test_strategy_levels_respect_direction(uptrend_df: pd.DataFrame) -> None:
    from app.layers.l2_indicators import compute_all

    enriched = compute_all(uptrend_df)
    for name, fn in STRATEGIES.items():
        s = fn(enriched)
        if s.action == "BUY":
            assert s.stop_loss < enriched["close"].iloc[-1] < s.take_profit, name
        elif s.action == "SELL":
            assert s.take_profit < enriched["close"].iloc[-1] < s.stop_loss, name


def test_consensus_detects_uptrend(uptrend_df: pd.DataFrame) -> None:
    result = StrategyEngine(min_agree=1).run(uptrend_df)
    assert result.score > 0
    assert result.action in ("BUY", "WAIT")


def test_consensus_requires_min_agreement(uptrend_df: pd.DataFrame) -> None:
    strict = StrategyEngine(min_agree=8).run(uptrend_df)
    assert strict.action == "WAIT"      # 8 stratejinin tümü asla aynı anda tetiklenmez


def test_engine_survives_broken_strategy(monkeypatch, demo_df: pd.DataFrame) -> None:
    """Tek bir strateji patlarsa motor durmaz."""
    def boom(_df):
        raise RuntimeError("patladım")

    monkeypatch.setitem(STRATEGIES, "trend_following", boom)
    result = StrategyEngine(["trend_following", "breakout"], min_agree=1).run(demo_df)
    assert result.total == 2


def test_catalog_lists_all_strategies() -> None:
    assert len(strategy_catalog()) == len(STRATEGIES)


def test_backtest_has_no_lookahead(demo_df: pd.DataFrame) -> None:
    """Girişler bir sonraki barın açılışında olmalı — çıkış zamanı girişten sonra."""
    report = run_backtest(demo_df, allow_short=True, min_agree=1, min_confidence=0.5)
    for trade in report.trades:
        assert trade.exit_time >= trade.entry_time


def test_backtest_respects_risk_per_trade(demo_df: pd.DataFrame) -> None:
    report = run_backtest(demo_df, initial_balance=1000.0, risk_pct=1.0,
                          allow_short=True, min_agree=1, min_confidence=0.5)
    for trade in report.trades:
        if trade.reason == "STOP_LOSS":
            # Zarar, kasa riskinin makul katını aşmamalı (slipaj + komisyon payı)
            assert trade.pnl > -30.0, trade.to_dict()


# --------------------------------------------------------------------------- #
#  Uçtan uca API
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth(client: TestClient) -> dict:
    payload = {"email": "pytest@zumvia.com", "password": "pytest12345"}
    r = client.post("/api/auth/register", json=payload)
    if r.status_code == 409:
        r = client.post("/api/auth/login", json=payload)
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_health_endpoint(client: TestClient) -> None:
    data = client.get("/api/health").json()
    assert data["ok"] and data["hard_limits"]["max_risk_pct"] <= 1.5


def test_auth_required(client: TestClient) -> None:
    assert client.get("/api/bots").status_code == 401


def test_full_bot_lifecycle(client: TestClient, auth: dict) -> None:
    created = client.post("/api/bots", headers=auth, json={
        "name": "Pytest Bot", "market": "demo", "exchange": "demo",
        "symbol": "ETH/USDT", "timeframe": "1h", "decision_mode": "algo_only",
        "initial_balance": 1000, "min_agree": 1,
    })
    assert created.status_code == 201
    bot_id = created.json()["id"]

    cycle = client.post(f"/api/bots/{bot_id}/run-once", headers=auth).json()
    assert cycle["ok"] is True

    events = client.get(f"/api/bots/{bot_id}/events", headers=auth).json()
    assert any(e["category"] == "data" for e in events)
    assert any(e["category"] == "math" for e in events)

    positions = client.get(f"/api/bots/{bot_id}/positions", headers=auth)
    assert positions.status_code == 200

    assert client.delete(f"/api/bots/{bot_id}", headers=auth).status_code == 200


def test_risk_caps_enforced_by_api(client: TestClient, auth: dict) -> None:
    """Kullanıcı %10 risk istese bile API tavanı uygular."""
    bot = client.post("/api/bots", headers=auth, json={
        "name": "Aç Gözlü Bot", "market": "demo", "exchange": "demo",
        "decision_mode": "algo_only", "risk_pct": 10.0,
        "daily_loss_limit_pct": 50.0, "min_confidence": 0.1, "min_rr": 0.5,
    }).json()
    assert bot["risk"]["risk_pct"] <= 1.5
    assert bot["risk"]["daily_loss_limit_pct"] <= 3.0
    assert bot["risk"]["min_confidence"] >= 0.75
    assert bot["risk"]["min_rr"] >= 2.0
    client.delete(f"/api/bots/{bot['id']}", headers=auth)


def test_ai_mode_requires_credential(client: TestClient, auth: dict) -> None:
    bot = client.post("/api/bots", headers=auth, json={
        "name": "AI Bot", "market": "demo", "exchange": "demo",
        "decision_mode": "hybrid",
    }).json()
    r = client.post(f"/api/bots/{bot['id']}/start", headers=auth)
    assert r.status_code == 400
    client.delete(f"/api/bots/{bot['id']}", headers=auth)


def test_market_analyze_endpoint(client: TestClient, auth: dict) -> None:
    data = client.get(
        "/api/market/analyze?market=demo&exchange=demo&symbol=BTC/USDT&timeframe=1h",
        headers=auth,
    ).json()
    assert "snapshot" in data and "consensus" in data
    assert data["snapshot"]["price"] > 0


def test_catalog_endpoint(client: TestClient) -> None:
    data = client.get("/api/bots/catalog").json()
    assert data["hard_limits"]["min_rr"] == 2.2
    assert data["hard_limits"]["max_risk_pct"] == 1.0
    assert len(data["decision_modes"]) == 4
