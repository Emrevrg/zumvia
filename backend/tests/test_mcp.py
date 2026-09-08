"""
MCP sunucusu testleri — HTTP taşıması, erişim anahtarları ve güvenlik sınırları.
"""
from __future__ import annotations

import json
from datetime import UTC

import pytest
from fastapi.testclient import TestClient

from app.agent.tools import REGISTRY
from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.mcp_auth import PREFIX, create_token, list_tokens, resolve_token, revoke_token
from app.core.security import hash_password
from app.main import app
from app.models import User


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth(client):
    payload = {"email": "mcptest@zumvia.com", "password": "mcptest12345"}
    r = client.post("/api/auth/register", json=payload)
    if r.status_code == 409:
        r = client.post("/api/auth/login", json=payload)
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def token_user(db):
    """Her testte temiz sayfa: önceki anahtarlar silinir (limit dolmasın)."""
    from app.models import McpToken  # noqa: PLC0415

    user = db.query(User).filter(User.email == "tokenuser@zumvia.com").first()
    if user is None:
        user = User(email="tokenuser@zumvia.com",
                    password_hash=hash_password("tokenuser12345"),
                    vault_salt=new_salt())
        db.add(user)
        db.commit()
        db.refresh(user)

    db.query(McpToken).filter(McpToken.user_id == user.id).delete()
    db.commit()
    return user


# --------------------------------------------------------------------------- #
#  Erişim anahtarları
# --------------------------------------------------------------------------- #

def test_token_is_returned_once_and_stored_hashed(db, token_user) -> None:
    record, plain = create_token(db, token_user, "Test istemcisi")
    db.commit()

    assert plain.startswith(PREFIX)
    assert len(plain) > 30
    # Düz metin ASLA saklanmaz
    assert record.token_hash != plain
    assert plain not in record.token_hash
    assert record.prefix in plain


def test_token_resolves_to_owner(db, token_user) -> None:
    _, plain = create_token(db, token_user, "Çözümleme testi")
    db.commit()

    resolved = resolve_token(db, plain)
    db.commit()
    assert resolved is not None and resolved.id == token_user.id


def test_invalid_token_is_rejected(db, token_user) -> None:
    assert resolve_token(db, "vq_kesinlikle_gecersiz") is None
    assert resolve_token(db, "") is None
    assert resolve_token(db, "Bearer bir-jwt-degil") is None


def test_revoked_token_stops_working(db, token_user) -> None:
    record, plain = create_token(db, token_user, "İptal testi")
    db.commit()
    assert resolve_token(db, plain) is not None

    assert revoke_token(db, token_user, record.id) is True
    db.commit()
    assert resolve_token(db, plain) is None


def test_expired_token_stops_working(db, token_user) -> None:
    from datetime import datetime, timedelta  # noqa: PLC0415

    record, plain = create_token(db, token_user, "Süre testi", days=1)
    record.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    assert resolve_token(db, plain) is None


def test_usage_counter_increases(db, token_user) -> None:
    record, plain = create_token(db, token_user, "Sayaç testi")
    db.commit()

    for _ in range(3):
        resolve_token(db, plain)
    db.commit()
    db.refresh(record)

    assert record.calls == 3
    assert record.last_used_at is not None


def test_token_list_never_exposes_secret(db, token_user) -> None:
    _, plain = create_token(db, token_user, "Gizlilik testi")
    db.commit()

    dumped = json.dumps(list_tokens(db, token_user), default=str)
    assert plain not in dumped
    assert "token_hash" not in dumped


def test_cannot_revoke_someone_elses_token(db, token_user) -> None:
    other = db.query(User).filter(User.email == "mcptest@zumvia.com").first()
    record, _ = create_token(db, token_user, "Sahiplik testi")
    db.commit()
    if other is not None:
        assert revoke_token(db, other, record.id) is False


# --------------------------------------------------------------------------- #
#  HTTP taşıması
# --------------------------------------------------------------------------- #

def test_mcp_info_endpoint(client) -> None:
    data = client.get("/mcp").json()
    assert data["server"]["name"] == "zumvia"
    assert data["tools"] == len(REGISTRY)
    assert "claude mcp add" in data["nasil_baglanir"]["claude_code_cli"]
    assert data["nasil_baglanir"]["stdio_alternatifi"].endswith('mcp_server.py"')


def test_initialize_handshake(client) -> None:
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                  "method": "initialize", "params": {}}).json()
    assert r["result"]["protocolVersion"]
    assert r["result"]["capabilities"]["tools"] is not None
    assert "risk" in r["result"]["instructions"].lower()


def test_tools_list_exposes_annotations(client) -> None:
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
    tools = r["result"]["tools"]
    assert len(tools) == len(REGISTRY)

    by_name = {t["name"]: t for t in tools}
    assert by_name["get_portfolio"]["annotations"]["readOnlyHint"] is True
    assert by_name["open_position"]["annotations"]["readOnlyHint"] is False
    assert by_name["activate_kill_switch"]["annotations"]["destructiveHint"] is True
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"


def test_notifications_return_no_body(client) -> None:
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202


def test_unknown_method_returns_error(client) -> None:
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "yok/boyle"}).json()
    assert r["error"]["code"] == -32601


def test_malformed_json_is_handled(client) -> None:
    r = client.post("/mcp", content=b"{bozuk",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700


def test_batch_requests(client) -> None:
    r = client.post("/mcp", json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]).json()
    assert len(r) == 2


def test_tool_call_requires_authentication(client) -> None:
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "get_portfolio", "arguments": {}},
    }).json()
    assert r["result"]["isError"] is True
    assert "Kimlik" in r["result"]["content"][0]["text"]


def test_tool_call_with_mcp_token(client, auth) -> None:
    created = client.post("/api/safety/mcp-tokens", headers=auth,
                          json={"name": "HTTP testi"}).json()
    token = created["token"]

    r = client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "get_safety_status", "arguments": {}},
    }).json()
    payload = json.loads(r["result"]["content"][0]["text"])
    assert "kill_switch" in payload
    assert payload["hard_limits"]["max_risk_pct"] <= 1.5

    # İptal edilince erişim anında biter
    client.delete(f"/api/safety/mcp-tokens/{created['id']}", headers=auth)
    blocked = client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "get_portfolio", "arguments": {}},
    }).json()
    assert blocked["result"]["isError"] is True


def test_query_parameter_authentication(client, auth) -> None:
    created = client.post("/api/safety/mcp-tokens", headers=auth,
                          json={"name": "Sorgu testi"}).json()
    r = client.post(f"/mcp?token={created['token']}", json={
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "get_safety_status", "arguments": {}},
    }).json()
    assert r["result"].get("isError") is not True
    client.delete(f"/api/safety/mcp-tokens/{created['id']}", headers=auth)


def test_risk_shield_applies_over_mcp(client, auth) -> None:
    """MCP üzerinden gelen mantıksız emir de reddedilir."""
    created = client.post("/api/safety/mcp-tokens", headers=auth,
                          json={"name": "Risk testi"}).json()
    headers = {"Authorization": f"Bearer {created['token']}"}

    bot = client.post("/api/bots", headers=auth, json={
        "name": "MCP Risk Botu", "market": "demo", "exchange": "demo",
        "decision_mode": "algo_only",
    }).json()

    r = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 8, "method": "tools/call",
        "params": {"name": "open_position", "arguments": {
            "bot_id": bot["id"], "action": "BUY", "stop_loss": 999999,
            "take_profit": 1000000, "confidence": 0.99, "reasoning": "mcp risk testi",
        }},
    }).json()

    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["opened"] is False
    assert payload["blocked_by"] in ("risk_shield", "risk_preconditions")

    client.delete(f"/api/bots/{bot['id']}", headers=auth)
    client.delete(f"/api/safety/mcp-tokens/{created['id']}", headers=auth)


def test_live_authorization_cannot_be_granted_over_mcp(client, auth) -> None:
    """Gerçek para yetkisi MCP araç setinde bulunmaz."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"}).json()
    names = {t["name"] for t in r["result"]["tools"]}
    assert "grant_live_authorization" not in names
    assert "enable_live_trading" in names       # yalnızca verilmiş yetkiyi kullanır


def test_token_limit_is_enforced(client, auth) -> None:
    created_ids = []
    last_error = None
    for index in range(12):
        response = client.post("/api/safety/mcp-tokens", headers=auth,
                               json={"name": f"Limit {index}"})
        if response.status_code == 201:
            created_ids.append(response.json()["id"])
        else:
            last_error = response
            break

    assert last_error is not None and last_error.status_code == 400
    for token_id in created_ids:
        client.delete(f"/api/safety/mcp-tokens/{token_id}", headers=auth)
