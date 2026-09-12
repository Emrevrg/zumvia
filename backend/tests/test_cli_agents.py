"""
CLI AJANLARI — Claude Code / Codex / Gemini CLI adaptör testleri
================================================================
Bu yolun hiç testi yoktu. En kritik eksiği: CLI süreci sıfır-dışı kodla
bitse bile tur "idle + ok" kapanıyordu — kullanıcı işin bittiğini sanıyor,
görev yarıda kalıyordu. Bu dosya o davranışı kilitler.

Gerçek CLI çalıştırılmaz; `shutil.which` ve `subprocess.run` taklit edilir.
"""
from __future__ import annotations

import subprocess

import pytest

from app.agent import controller
from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import AgentMessage, AgentSession, ControlTool, User


@pytest.fixture(scope="module")
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture(scope="module")
def user(db):
    email = "cli@zumvia.com"
    row = db.query(User).filter(User.email == email).first()
    if row is None:
        row = User(email=email, password_hash=hash_password("cliparola123"),
                   vault_salt=new_salt())
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@pytest.fixture()
def session(db, user):
    row = AgentSession(user_id=user.id, title="CLI Denetim",
                       control_tool=ControlTool.CODEX, control_model="")
    db.add(row)
    db.commit()
    db.refresh(row)
    yield row
    db.query(AgentMessage).filter(AgentMessage.session_id == row.id).delete(
        synchronize_session=False)
    db.delete(row)
    db.commit()


def _messages(db, session):
    return (db.query(AgentMessage)
            .filter(AgentMessage.session_id == session.id)
            .order_by(AgentMessage.id).all())


def test_kurulu_olmayan_cli_acik_hata_verir(db, user, session, monkeypatch) -> None:
    monkeypatch.setattr(controller.shutil, "which", lambda *a, **k: None)
    result = controller.run_cli_agent(db, user, session, "portföye bak")
    assert result["ok"] is False
    assert "kurulu değil" in result["error"]
    db.refresh(session)
    assert session.status == "error"
    texts = " ".join(m.content for m in _messages(db, session))
    assert "kurulu değil" in texts


def test_hata_koduyla_bitme_basari_sayilmaz(db, user, session, monkeypatch) -> None:
    """Sıfır-dışı çıkış = tamamlanmamış görev (kilitlenen davranış)."""
    monkeypatch.setattr(controller.shutil, "which", lambda *a, **k: "C:/fake/codex")
    failed = subprocess.CompletedProcess(
        args=["codex"], returncode=1,
        stdout="kısmi çıktı", stderr="API anahtarı geçersiz")
    monkeypatch.setattr(controller.subprocess, "run", lambda *a, **k: failed)
    result = controller.run_cli_agent(db, user, session, "portföye bak")
    assert result["ok"] is False
    assert "TAMAMLANMADI" in result["error"]
    db.refresh(session)
    assert session.status == "error"
    assert "TAMAMLANMADI" in session.last_error


def test_temiz_bitis_kaydedilir(db, user, session, monkeypatch) -> None:
    monkeypatch.setattr(controller.shutil, "which", lambda *a, **k: "C:/fake/codex")
    done = subprocess.CompletedProcess(
        args=["codex"], returncode=0,
        stdout="Portföy okundu. 2 bot izleniyor.", stderr="")
    monkeypatch.setattr(controller.subprocess, "run", lambda *a, **k: done)
    result = controller.run_cli_agent(db, user, session, "portföye bak")
    assert result["ok"] is True
    assert "Codex" in result["tool"]
    db.refresh(session)
    assert session.status == "idle"
    texts = " ".join(m.content for m in _messages(db, session))
    assert "Portföy okundu" in texts


def test_zaman_asimi_hata_sayar(db, user, session, monkeypatch) -> None:
    monkeypatch.setattr(controller.shutil, "which", lambda *a, **k: "C:/fake/codex")

    def yavaş(*a, **k):
        raise subprocess.TimeoutExpired(cmd="codex", timeout=900)

    monkeypatch.setattr(controller.subprocess, "run", yavaş)
    result = controller.run_cli_agent(db, user, session, "portföye bak",
                                      timeout=900)
    assert result["ok"] is False
    db.refresh(session)
    assert session.status == "error"


def test_gorev_dosyasi_sirlari_sizdirmaz() -> None:
    metin = controller._briefing(
        session=None,  # type: ignore[arg-type]
        token="kisa-omurlu-token",
        mandate={"max_risk_pct": 0.5},
        task="portföye bak",
    )
    assert "127.0.0.1" in metin
    assert "Bearer kisa-omurlu-token" in metin
    assert "stop-loss zorunlu" in metin
    assert "sk-" not in metin
    assert "VQ_MASTER_KEY" not in metin


def test_kullanim_listesi_tutarli() -> None:
    rows = controller.cli_availability()
    ids = {r["id"] for r in rows}
    assert ids == {"claude_code", "codex", "gemini_cli"}
    for r in rows:
        assert r["label"] and r["install"]
        assert isinstance(r["available"], bool)


def test_sevk_codex_yoluna_gider(db, user, session, monkeypatch) -> None:
    """run_agent, codex oturumunu CLI adaptörüne yönlendirir."""
    calls: list[str] = []

    def sahte(db_, user_, session_, *a, **k):
        calls.append(session_.control_tool.value)
        return {"ok": True}

    monkeypatch.setattr(controller, "run_cli_agent", sahte)
    out = controller.run_agent(db, user, session, "bak")
    assert out == {"ok": True}
    assert calls == ["codex"]
