"""
ÇALIŞTIRMA KİLİDİ, DURDURMA VE SAĞLAYICI DEVRİ

Üç davranış burada güvence altına alınır:

  1. Aynı görev iki kez birden çalışamaz — aksi hâlde iki tur aynı kararı
     verip AYNI İŞLEMİ İKİ KEZ açardı.
  2. "Durdur" gerçekten durdurur — yalnızca durum alanını değiştirmek
     yetmez, çalışan döngü bir sonraki adımda üzerine yazar.
  3. Bir sağlayıcı düştüğünde görev bitmez; kullanıcının başka anahtarına
     geçilir ve bu kullanıcıya açıkça bildirilir.
"""
from __future__ import annotations

import threading

import pytest

from app.agent import controller, run_lock
from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.layers.l3_llm_gateway import ChatTurn
from app.models import AgentMessage, AgentSession, User


@pytest.fixture(autouse=True)
def _clean_locks():
    run_lock.release_all()
    yield
    run_lock.release_all()


@pytest.fixture()
def session_id():
    db = SessionLocal()
    user = db.query(User).filter(User.email == "lock@zumvia.com").first()
    if user is None:
        user = User(email="lock@zumvia.com", password_hash=hash_password("locktest12345"),
                    vault_salt=new_salt())
        db.add(user)
        db.commit()
        db.refresh(user)
    row = AgentSession(user_id=user.id, title="Kilit testi", control_tool="api",
                       control_model="test-model", council_mode="solo", status="idle")
    db.add(row)
    db.commit()
    sid = row.id
    db.close()
    yield sid
    db = SessionLocal()
    db.query(AgentMessage).filter(AgentMessage.session_id == sid).delete(
        synchronize_session=False)
    db.query(AgentSession).filter(AgentSession.id == sid).delete(synchronize_session=False)
    db.commit()
    db.close()


# --------------------------------------------------------------------------- #
#  1. Kilit
# --------------------------------------------------------------------------- #

def test_lock_blocks_second_holder() -> None:
    with run_lock.hold(7):
        assert run_lock.is_running(7)
        with pytest.raises(run_lock.SessionBusy):
            with run_lock.hold(7):
                pytest.fail("ikinci tur başlamamalıydı")
    assert not run_lock.is_running(7)


def test_lock_is_per_session() -> None:
    """Farklı görevler birbirini bloklamaz — çoklu görev buna dayanır."""
    with run_lock.hold(1), run_lock.hold(2), run_lock.hold(3):
        assert run_lock.active_sessions() == [1, 2, 3]


def test_lock_released_after_exception() -> None:
    """Tur çökse bile kilit kalmaz; görev kalıcı olarak kilitlenmez."""
    with pytest.raises(ValueError):
        with run_lock.hold(9):
            raise ValueError("tur çöktü")
    assert not run_lock.is_running(9)


def test_second_run_of_same_session_is_refused(session_id, monkeypatch) -> None:
    """Kullanıcı mesajı çalışan bir turun üstüne binmez."""
    entered = threading.Event()
    release = threading.Event()

    class Blocking:
        def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
            entered.set()
            release.wait(timeout=10)
            return ChatTurn(True, text="bitti")

    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: Blocking())

    def first() -> None:
        db = SessionLocal()
        session = db.get(AgentSession, session_id)
        controller.run_agent(db, db.get(User, session.user_id), session, "birinci")
        db.close()

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(timeout=10), "ilk tur başlamadı"

    db = SessionLocal()
    session = db.get(AgentSession, session_id)
    result = controller.run_agent(db, db.get(User, session.user_id), session, "ikinci")
    db.close()

    release.set()
    thread.join(timeout=10)

    assert result["ok"] is False
    assert result.get("busy") is True
    assert "çalışıyor" in result["error"]


def test_heartbeat_skips_instead_of_stacking(session_id) -> None:
    """
    Otonom kalp atışı çalışan turun üstüne binmez, sessizce atlar.

    Aksi hâlde yavaş bir tur sırasında biriken tikler tur biter bitmez
    arka arkaya çalışır ve aynı işlemi tekrar tekrar açardı.
    """
    db = SessionLocal()
    session = db.get(AgentSession, session_id)
    user = db.get(User, session.user_id)
    with run_lock.hold(session_id):
        result = controller.run_agent(db, user, session, None, autonomous=True)
    db.close()

    assert result["ok"] is True
    assert result["skipped"] == "already_running"


# --------------------------------------------------------------------------- #
#  2. Durdurma
# --------------------------------------------------------------------------- #

def test_cancel_only_works_on_running_session() -> None:
    assert run_lock.cancel(42) is False
    with run_lock.hold(42):
        assert run_lock.cancel(42) is True
        assert run_lock.cancelled(42) is True
    assert run_lock.cancelled(42) is False, "kilit bırakılınca istek de temizlenmeli"


def test_stop_actually_ends_the_loop(session_id, monkeypatch) -> None:
    """
    Durdurma isteği geldiğinde döngü DEVAM ETMEMELİ.

    Eski davranışta yalnızca veritabanı durumu değişiyordu; döngü çalışmaya
    devam edip bir sonraki adımda üzerine yazıyordu — düğme yalan söylüyordu.
    """
    calls = {"n": 0}

    class Chatty:
        """Durdurulmazsa sonsuza dek araç çağırmak isteyen model."""

        def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
            calls["n"] += 1
            if calls["n"] == 1:
                # İlk yanıttan sonra kullanıcı Durdur'a basar.
                run_lock.cancel(session_id)
            return ChatTurn(True, text=f"adım {calls['n']}",
                            tool_calls=[{"name": "get_portfolio", "arguments": {}}])

    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: Chatty())

    db = SessionLocal()
    session = db.get(AgentSession, session_id)
    user = db.get(User, session.user_id)
    result = controller.run_agent(db, user, session, "uzun görev")
    db.close()

    assert result.get("stopped") is True
    assert calls["n"] <= 2, f"durdurma sonrası döngü devam etti ({calls['n']} çağrı)"

    probe = SessionLocal()
    assert probe.get(AgentSession, session_id).status == "idle"
    texts = [m.content for m in probe.query(AgentMessage)
             .filter(AgentMessage.session_id == session_id).all()]
    probe.close()
    assert any("durduruldu" in (t or "") for t in texts), \
        "kullanıcıya turun durduğu bildirilmedi"


def test_stop_does_not_affect_other_sessions() -> None:
    with run_lock.hold(100), run_lock.hold(200):
        run_lock.cancel(100)
        assert run_lock.cancelled(100) is True
        assert run_lock.cancelled(200) is False


# --------------------------------------------------------------------------- #
#  3. Sağlayıcı devri
# --------------------------------------------------------------------------- #

def test_provider_faults_are_classified() -> None:
    """
    Hangi hatanın model, hangisinin sağlayıcı arızası olduğu doğru ayrılmalı.

    Yanlış sınıflandırma ya gereksiz sağlayıcı değişimine ya da düşmüş bir
    sağlayıcıda ısrar etmeye yol açar.
    """
    from app.layers.model_errors import explain

    assert explain("404 Not Found").code in controller._MODEL_FAULTS
    assert explain("410 Gone").code in controller._MODEL_FAULTS
    assert explain("401 Unauthorized").code in controller._PROVIDER_FAULTS
    assert explain("503 Service Unavailable").code in controller._PROVIDER_FAULTS
    assert explain("connection timed out").code in controller._PROVIDER_FAULTS


def test_failover_does_not_retry_a_dead_credential(session_id, monkeypatch) -> None:
    """
    Aynı bozuk anahtara sonsuz dönülmemeli.

    `tried` kümesi olmasaydı iki bozuk anahtar arasında sonsuza dek gidip
    gelinir, tur hiç bitmezdi.
    """
    seen: list[int] = []

    def fake_gateway(db, user, cred_id, model):  # noqa: ARG001
        seen.append(cred_id)
        return None                       # hiçbir anahtar kurulamıyor

    monkeypatch.setattr(controller, "_gateway_from", fake_gateway)

    db = SessionLocal()
    session = db.get(AgentSession, session_id)
    user = db.get(User, session.user_id)
    tried: set[int] = set()
    first = controller._switch_credential(db, user, session, _FakeProblem(), tried)
    second = controller._switch_credential(db, user, session, _FakeProblem(), tried)
    db.close()

    assert first is None and second is None
    assert len(seen) == len(set(seen)), f"aynı anahtar tekrar denendi: {seen}"


class _FakeProblem:
    code = "PROVIDER_DOWN"
