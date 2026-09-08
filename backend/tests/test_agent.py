"""
Komuta Ajanı testleri — araç döngüsü, risk sınırları ve fail-safe davranışı.
Gerçek bir LLM çağrılmaz; model yanıtları senaryo olarak enjekte edilir.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.agent import controller
from app.agent.tools import REGISTRY, ToolContext, execute_tool, tool_manifest, tool_schemas
from app.core.config import settings
from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.layers.l3_llm_gateway import ChatTurn
from app.main import app
from app.models import AgentMessage, AgentSession, Bot, ControlTool, TradingMode, User

# --------------------------------------------------------------------------- #
#  Sabitler
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture(scope="module")
def user(db):
    existing = db.query(User).filter(User.email == "agent@zumvia.com").first()
    if existing:
        return existing
    created = User(email="agent@zumvia.com",
                   password_hash=hash_password("agenttest123"),
                   vault_salt=new_salt())
    db.add(created)
    db.commit()
    db.refresh(created)
    return created


@pytest.fixture()
def session(db, user):
    row = AgentSession(user_id=user.id, title="Test Oturumu",
                       control_tool=ControlTool.API, control_model="fake-model")
    db.add(row)
    db.commit()
    db.refresh(row)
    yield row
    db.delete(row)
    db.commit()


@pytest.fixture()
def ctx(db, user):
    return ToolContext(db=db, user=user)


# --------------------------------------------------------------------------- #
#  Araç kayıt defteri
# --------------------------------------------------------------------------- #

def test_registry_is_populated() -> None:
    for expected in ("get_market_snapshot", "run_strategy_engine", "get_news",
                     "get_portfolio", "create_bot", "update_bot", "control_bot",
                     "open_position", "close_position", "system_health"):
        assert expected in REGISTRY, expected


def test_every_tool_has_valid_schema() -> None:
    for schema in tool_schemas():
        fn = schema["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"
        for field in fn["parameters"].get("required", []):
            assert field in fn["parameters"]["properties"], f"{fn['name']}.{field}"


def test_manifest_marks_mutating_tools() -> None:
    manifest = {t["name"]: t["mutating"] for t in tool_manifest()}
    assert manifest["open_position"] is True
    assert manifest["get_market_snapshot"] is False


def test_unknown_tool_returns_error_not_exception(ctx) -> None:
    result = execute_tool(ctx, "kesinlikle_yok", {})
    assert "error" in result and "available" in result


def test_tool_errors_are_returned_not_raised(ctx) -> None:
    result = execute_tool(ctx, "get_bot_detail", {"bot_id": 999999})
    assert "error" in result


# --------------------------------------------------------------------------- #
#  Araç davranışı
# --------------------------------------------------------------------------- #

def test_market_snapshot_returns_real_numbers(ctx) -> None:
    result = execute_tool(ctx, "get_market_snapshot",
                          {"market": "demo", "symbol": "BTC/USDT", "timeframe": "1h"})
    assert result["snapshot"]["price"] > 0
    assert "rsi_14" in result["snapshot"]["indicators"]
    assert -100 <= result["technical_bias"]["score"] <= 100


def test_strategy_engine_tool_returns_consensus(ctx) -> None:
    result = execute_tool(ctx, "run_strategy_engine",
                          {"market": "demo", "symbol": "ETH/USDT", "timeframe": "1h"})
    assert result["action"] in ("BUY", "SELL", "WAIT")
    assert len(result["signals"]) >= 6


def test_agent_cannot_exceed_risk_cap(ctx) -> None:
    """Ajan %9 risk isterse bile sistem tavanı uygulanır."""
    created = execute_tool(ctx, "create_bot", {
        "name": "Aç Gözlü Ajan Botu", "market": "demo", "symbol": "BTC/USDT",
        "timeframe": "1h", "decision_mode": "algo_only", "risk_pct": 9.0,
    })
    bot = ctx.db.get(Bot, created["bot_id"])
    assert bot.risk_pct <= settings.hard_max_risk_pct

    updated = execute_tool(ctx, "update_bot", {
        "bot_id": bot.id, "risk_pct": 25.0, "min_rr": 0.5,
        "min_confidence": 0.1, "reason": "test",
    })
    ctx.db.refresh(bot)
    assert bot.risk_pct <= settings.hard_max_risk_pct
    assert bot.min_rr >= settings.hard_min_rr_ratio
    assert bot.min_confidence >= settings.hard_min_confidence
    # Değerler zaten tavanda olduğu için değişiklik yok — kritik olan tavanın tutması.
    assert "error" not in updated
    for change in updated["changes"].values():
        assert change["yeni"] <= settings.hard_max_risk_pct or change["yeni"] >= 2.0


def test_agent_cannot_switch_bot_to_live(ctx) -> None:
    """Gerçek para moduna geçiş ajanın yetkisinde DEĞİLDİR."""
    created = execute_tool(ctx, "create_bot", {
        "name": "Mod Testi", "market": "demo", "symbol": "BTC/USDT",
        "timeframe": "1h", "decision_mode": "algo_only",
    })
    bot = ctx.db.get(Bot, created["bot_id"])
    assert bot.mode == TradingMode.PAPER

    execute_tool(ctx, "update_bot", {"bot_id": bot.id, "mode": "live", "reason": "test"})
    ctx.db.refresh(bot)
    assert bot.mode == TradingMode.PAPER          # değişmedi

    schema = REGISTRY["update_bot"].parameters["properties"]
    assert "mode" not in schema                    # şemada bile yok


def test_open_position_requires_valid_stop(ctx) -> None:
    """Risk kalkanı ajanın mantıksız emrini reddeder."""
    created = execute_tool(ctx, "create_bot", {
        "name": "Risk Testi", "market": "demo", "symbol": "BTC/USDT",
        "timeframe": "1h", "decision_mode": "algo_only",
    })
    bot_id = created["bot_id"]

    result = execute_tool(ctx, "open_position", {
        "bot_id": bot_id, "action": "BUY",
        "stop_loss": 999999.0, "take_profit": 1000000.0,
        "confidence": 0.99, "reasoning": "mantıksız stop testi",
    })
    assert result["opened"] is False
    assert result["blocked_by"] in ("risk_shield", "risk_preconditions")


def test_open_position_rejects_low_confidence(ctx) -> None:
    created = execute_tool(ctx, "create_bot", {
        "name": "Güven Testi", "market": "demo", "symbol": "BTC/USDT",
        "timeframe": "1h", "decision_mode": "algo_only",
    })
    quote = execute_tool(ctx, "get_quote", {"market": "demo", "symbol": "BTC/USDT"})
    price = quote["price"]
    result = execute_tool(ctx, "open_position", {
        "bot_id": created["bot_id"], "action": "BUY",
        "stop_loss": price * 0.98, "take_profit": price * 1.06,
        "confidence": 0.30, "reasoning": "düşük güven testi",
    })
    assert result["opened"] is False
    assert result["code"] == "LOW_CONFIDENCE"


def test_open_position_succeeds_with_sound_setup(ctx) -> None:
    created = execute_tool(ctx, "create_bot", {
        "name": "Geçerli Kurulum", "market": "demo", "symbol": "BTC/USDT",
        "timeframe": "1h", "decision_mode": "algo_only", "initial_balance": 2000,
    })
    quote = execute_tool(ctx, "get_quote", {"market": "demo", "symbol": "BTC/USDT"})
    price = quote["price"]
    result = execute_tool(ctx, "open_position", {
        "bot_id": created["bot_id"], "action": "BUY",
        "stop_loss": price * 0.98, "take_profit": price * 1.06,   # R/R 1:3
        "confidence": 0.88, "reasoning": "sağlam kurulum testi",
    })
    assert result["opened"] is True
    assert result["rr"] >= settings.hard_min_rr_ratio
    assert result["risk_amount"] <= 2000 * settings.hard_max_risk_pct / 100 + 0.01

    closed = execute_tool(ctx, "close_position", {
        "bot_id": created["bot_id"], "position_id": result["position_id"],
        "reason": "test kapanışı",
    })
    assert closed["closed"] is True


def test_credentials_never_leak_secrets(ctx) -> None:
    result = execute_tool(ctx, "list_credentials", {})
    dumped = json.dumps(result)
    assert "api_key" not in dumped or "payload_enc" not in dumped
    for cred in result["credentials"]:
        assert "payload_enc" not in cred


# --------------------------------------------------------------------------- #
#  Ajan döngüsü (senaryolu sahte model)
# --------------------------------------------------------------------------- #

class ScriptedGateway:
    """Sırayla verilen turları döndüren sahte model."""

    def __init__(self, turns: list[ChatTurn]) -> None:
        self.turns = turns
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
        self.calls.append(messages)
        return self.turns.pop(0) if self.turns else ChatTurn(True, "bitti.")


def test_agent_loop_executes_tools_and_replies(db, user, session, monkeypatch) -> None:
    gateway = ScriptedGateway([
        ChatTurn(True, "Portföye bakıyorum.",
                 [{"id": "1", "name": "get_portfolio", "arguments": {}}]),
        ChatTurn(True, "Şimdi piyasayı okuyorum.",
                 [{"id": "2", "name": "get_market_snapshot",
                   "arguments": {"market": "demo", "symbol": "BTC/USDT", "timeframe": "1h"}}]),
        ChatTurn(True, "Durum net: şu an kurulum yok, bekliyorum."),
    ])
    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: gateway)

    result = controller.run_agent(db, user, session, "durumu özetle")
    db.commit()

    assert result["ok"] is True
    assert result["steps"] == 3

    messages = (db.query(AgentMessage)
                .filter(AgentMessage.session_id == session.id)
                .order_by(AgentMessage.id).all())
    roles = [m.role for m in messages]
    assert roles[0] == "user"
    assert "tool" in roles
    tool_names = [m.tool_name for m in messages if m.role == "tool"]
    assert tool_names == ["get_portfolio", "get_market_snapshot"]
    assert session.status == "idle"


def test_agent_completion_gate_executes_and_verifies_before_finishing(
        db, user, session, monkeypatch) -> None:
    """
    Uygula modu yalnızca "hazırlıyorum" diyerek kapanamaz; değişiklikten
    sonra da bir okuma/doğrulama aracı görmeden nihai cevabı kabul etmez.
    """
    session.work_mode = "agent"
    db.commit()
    gateway = ScriptedGateway([
        ChatTurn(True, "Hazırlıyorum."),
        ChatTurn(True, "Kuruyorum.", [{
            "id": "m1", "name": "create_bot", "arguments": {"name": "Kapı testi"},
        }]),
        ChatTurn(True, "Bitti."),
        ChatTurn(True, "Doğruluyorum.", [{
            "id": "r1", "name": "get_bot_detail", "arguments": {"bot_id": 91},
        }]),
        ChatTurn(True, "Kapı testi botu kuruldu ve son durum doğrulandı."),
    ])
    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: gateway)

    def fake_execute(_ctx, name, _args):
        if name == "create_bot":
            return {"ok": True, "bot_id": 91, "created": True}
        return {"ok": True, "bot_id": 91, "status": "stopped"}

    monkeypatch.setattr(controller, "execute_tool", fake_execute)
    result = controller.run_agent(db, user, session, "Botu kur ve tamamla")
    db.commit()

    assert result["ok"] is True
    assert result["steps"] == 5
    assert "doğrulandı" in result["reply"]
    assert len(gateway.calls) == 5

    rows = (db.query(AgentMessage)
            .filter(AgentMessage.session_id == session.id)
            .order_by(AgentMessage.id).all())
    assert [m.tool_name for m in rows if m.role == "tool"] == [
        "create_bot", "get_bot_detail"]
    # İki erken kapanış sohbeti şişirmez; yalnızca gerçek ara adımlar görünür.
    assistant_texts = [m.content for m in rows if m.role == "assistant"]
    assert "Hazırlıyorum." not in assistant_texts
    assert "Bitti." not in assistant_texts


def test_agent_completion_gate_accepts_a_measured_wait_decision(
        db, user, session, monkeypatch) -> None:
    """Uygula, zorla işlem aç demek değildir; güvenli bekleme terminaldir."""
    session.work_mode = "agent"
    db.commit()
    gateway = ScriptedGateway([
        ChatTurn(True, "Net kurulum yok; işlem açmıyorum ve bekliyorum."),
    ])
    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: gateway)

    result = controller.run_agent(db, user, session, "Paramı yönet")
    db.commit()

    assert result["ok"] is True
    assert result["steps"] == 1
    assert len(gateway.calls) == 1


def test_agent_loop_survives_model_failure(db, user, session, monkeypatch) -> None:
    """
    Model sürekli hız sınırı döndürürse tur temiz biter.

    Sözleşme iki katmanlıdır: sistem önce BEKLEYİP TEKRAR DENER (429 "şu an
    değil" demektir), pes ettiğinde de kullanıcıya ham HTTP kodu değil ne
    yapacağını söyleyen bir açıklama bırakır.
    """
    monkeypatch.setattr(controller, "RATE_BACKOFF", (0.01, 0.01, 0.01, 0.01))

    class AlwaysFailing:
        """Hiç düzelmeyen sağlayıcı — geçici arıza DEĞİL."""

        def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
            return ChatTurn(False, error="429 kota doldu")

    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: AlwaysFailing())

    result = controller.run_agent(db, user, session, "bir şey yap")
    db.commit()

    assert result["ok"] is False
    assert session.status == "error"

    messages = (db.query(AgentMessage).filter(AgentMessage.session_id == session.id)
                .order_by(AgentMessage.id).all())

    # Pes etmeden önce gerçekten beklenmiş olmalı — mesaj söylediğini yapmalı.
    assert any("tekrarlıyorum" in (m.content or "") for m in messages),         "sistem beklemeden pes etti"

    final = next(m for m in reversed(messages) if m.role == "assistant")
    assert final.ok is False
    assert "429" not in final.content, "ham hata kodu kullanıcıya gösterilmemeli"
    assert "sınır" in final.content.lower() or "limit" in final.content.lower()



def test_agent_recovers_from_a_transient_failure(db, user, session, monkeypatch) -> None:
    """
    Geçici bir sağlayıcı arızası görevi öldürmemeli.

    Sağlayıcılar sürekli anlık 429 döndürür. Bunların her biri bir görevi
    bitirseydi, sistem gerçek parayla güvenilmez olurdu.
    """
    monkeypatch.setattr(controller, "RATE_BACKOFF", (0.01, 0.01, 0.01, 0.01))
    gateway = ScriptedGateway([ChatTurn(False, error="429 kota doldu")])
    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: gateway)

    result = controller.run_agent(db, user, session, "bir şey yap")
    db.commit()

    assert result["ok"] is True, "geçici arıza görevi öldürdü"
    assert session.status == "idle"


def test_agent_loop_blocks_repeated_identical_calls(db, user, session, monkeypatch) -> None:
    """Aynı aracı aynı argümanlarla tekrar çağırmak döngüye girmemeli."""
    call = {"id": "x", "name": "get_portfolio", "arguments": {}}
    gateway = ScriptedGateway([
        ChatTurn(True, "1", [call]),
        ChatTurn(True, "2", [call]),
        ChatTurn(True, "tamam."),
    ])
    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: gateway)

    controller.run_agent(db, user, session, "tekrar testi")
    db.commit()

    tool_messages = [m for m in db.query(AgentMessage)
                     .filter(AgentMessage.session_id == session.id).all()
                     if m.role == "tool"]
    assert len(tool_messages) == 2
    assert "zaten çağırdın" in tool_messages[1].tool_result_json


def test_agent_without_model_reports_clearly(db, user, session, monkeypatch) -> None:
    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: None)
    result = controller.run_agent(db, user, session, "başla")
    db.commit()
    assert result["ok"] is False
    assert "yapay zeka anahtarı yok" in result["error"]


def test_autonomous_tick_uses_audit_prompt(db, user, session, monkeypatch) -> None:
    gateway = ScriptedGateway([ChatTurn(True, "Her şey yolunda.")])
    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: gateway)

    controller.run_agent(db, user, session, None, autonomous=True)
    db.commit()

    sent = gateway.calls[0][-1]["content"]
    assert "OTONOM DENETİM TURU" in sent


def test_system_prompt_declares_hard_limits(session) -> None:
    prompt = controller._system_prompt(session, {"budget": 1000})
    assert "stop-loss" in prompt.lower()
    assert str(settings.hard_max_risk_pct) in prompt
    assert "Kullanıcı panelden açık" in prompt      # canlı yetki yalnızca kullanıcıdan
    assert "budget" in prompt                       # yetki çerçevesi eklendi


# --------------------------------------------------------------------------- #
#  API uçları
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth(client):
    payload = {"email": "agentapi@zumvia.com", "password": "agentapi123"}
    r = client.post("/api/auth/register", json=payload)
    if r.status_code == 409:
        r = client.post("/api/auth/login", json=payload)
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_catalog_lists_control_tools(client) -> None:
    data = client.get("/api/agent/catalog").json()
    ids = [t["id"] for t in data["control_tools"]]
    assert "api" in ids and "claude_code" in ids and "codex" in ids
    assert len(data["quick_missions"]) >= 4
    assert len(data["tools"]) >= 15


def test_session_crud(client, auth) -> None:
    created = client.post("/api/agent/sessions", headers=auth,
                          json={"title": "API Test", "control_tool": "api"})
    assert created.status_code == 201
    session_id = created.json()["id"]

    assert client.get(f"/api/agent/sessions/{session_id}", headers=auth).status_code == 200

    # Yeni oturum en güvenli modda doğar: okur, anlatır, hiçbir şeyi değiştirmez.
    assert created.json()["work_mode"] == "ask"
    assert created.json()["autonomous"] is False

    patched = client.patch(f"/api/agent/sessions/{session_id}", headers=auth,
                           json={"work_mode": "agent", "heartbeat_seconds": 300}).json()
    # Otonomluk moddan TÜRER; ayrı bir düğme yoktur. "Uygula" modundaki bir
    # ajan kurduğunu izlemek zorundadır, yoksa işi yarım bırakmış olur.
    assert patched["work_mode"] == "agent"
    assert patched["autonomous"] is True
    assert patched["heartbeat_seconds"] == 300

    back = client.patch(f"/api/agent/sessions/{session_id}", headers=auth,
                        json={"work_mode": "ask"}).json()
    assert back["autonomous"] is False, "Sor moduna dönünce otonomluk kapanmadı"

    assert client.delete(f"/api/agent/sessions/{session_id}", headers=auth).status_code == 200


def test_tool_endpoint_requires_auth(client) -> None:
    assert client.post("/api/agent/tool", json={"name": "get_portfolio"}).status_code == 401


def test_tool_endpoint_executes(client, auth) -> None:
    result = client.post("/api/agent/tool", headers=auth,
                         json={"name": "system_health", "arguments": {}}).json()
    assert result["tool"] == "system_health"
    assert "hard_limits" in result["result"]
    assert result["result"]["hard_limits"]["max_risk_pct"] <= 1.5


def test_sessions_are_user_scoped(client, auth) -> None:
    other = client.post("/api/auth/register", headers={},
                        json={"email": "other@zumvia.com", "password": "other12345"})
    if other.status_code == 409:
        other = client.post("/api/auth/login",
                            json={"email": "other@zumvia.com", "password": "other12345"})
    other_auth = {"Authorization": f"Bearer {other.json()['access_token']}"}

    mine = client.post("/api/agent/sessions", headers=auth, json={"title": "Gizli"}).json()
    assert client.get(f"/api/agent/sessions/{mine['id']}", headers=other_auth).status_code == 404
    client.delete(f"/api/agent/sessions/{mine['id']}", headers=auth)
