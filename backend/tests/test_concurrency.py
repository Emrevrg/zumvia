"""
EŞZAMANLILIK TESTLERİ — birden çok görev, birden çok sağlayıcı

Bu dosyanın koruduğu hata gerçekti ve şöyleydi:

    Ajan turu boyunca (dakikalarca sürebilir) tek bir SQLite YAZMA işlemi
    açık kalıyordu. Bu sırada başka hiçbir oturum veya bot yazamıyor,
    `busy_timeout` dolduğunda "database is locked" hatası alıyordu.
    Sonuç: birden çok görevi aynı anda çalıştırmak imkânsızdı.

Çözüm: her adım (record/set_status) hemen commit edilir — yazma işlemi
asla bir ağ çağrısını kapsamaz.
"""
from __future__ import annotations

import threading
import time
from collections import Counter

import pytest

from app.agent import controller
from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.layers.l3_llm_gateway import ChatTurn
from app.models import AgentMessage, AgentSession, User


class SlowGateway:
    """
    Yavaş bir model sağlayıcısını taklit eder.

    Gerçek hatayı ortaya çıkaran şey buydu: model çağrısı uzun sürerken
    veritabanı işleminin açık kalması.
    """

    def __init__(self, delay: float = 0.35, text: str = "tamam") -> None:
        self.delay = delay
        self.text = text
        self.calls = 0

    def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
        self.calls += 1
        time.sleep(self.delay)              # ağ gecikmesi
        return ChatTurn(True, text=self.text)


@pytest.fixture()
def users():
    """Birden çok kullanıcı ve oturum (birden çok sağlayıcıyı temsil eder)."""
    db = SessionLocal()
    created = []
    for index in range(4):
        email = f"conc{index}@zumvia.com"
        row = db.query(User).filter(User.email == email).first()
        if row is None:
            row = User(email=email, password_hash=hash_password("conctest12345"),
                       vault_salt=new_salt())
            db.add(row)
            db.commit()
            db.refresh(row)
        created.append(row.id)
    db.close()
    yield created


def _session_for(user_id: int, provider_label: str) -> int:
    db = SessionLocal()
    row = AgentSession(
        user_id=user_id, title=f"Eşzamanlı {provider_label}",
        control_tool="api", control_model=f"{provider_label}-model",
        council_mode="solo", status="idle",
    )
    db.add(row)
    db.commit()
    session_id = row.id
    db.close()
    return session_id


def _cleanup(session_ids: list[int]) -> None:
    db = SessionLocal()
    db.query(AgentMessage).filter(AgentMessage.session_id.in_(session_ids)).delete(
        synchronize_session=False)
    db.query(AgentSession).filter(AgentSession.id.in_(session_ids)).delete(
        synchronize_session=False)
    db.commit()
    db.close()


# --------------------------------------------------------------------------- #
#  Asıl regresyon testi
# --------------------------------------------------------------------------- #

def test_many_sessions_run_at_once_without_database_locks(users, monkeypatch) -> None:
    """
    Dört oturum, dört farklı sağlayıcı, hepsi aynı anda.

    Hiçbiri "database is locked" almamalı ve hepsi mesajını yazabilmeli.
    """
    providers = ["nvidia", "gemini", "openrouter", "groq"]
    sessions = [_session_for(uid, providers[i]) for i, uid in enumerate(users)]

    gateway = SlowGateway(delay=0.4)
    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: gateway)

    results: Counter = Counter()
    errors: list[str] = []
    lock = threading.Lock()

    def worker(session_id: int, label: str) -> None:
        db = SessionLocal()
        try:
            session = db.get(AgentSession, session_id)
            user = db.get(User, session.user_id)
            outcome = controller.run_agent(db, user, session, f"{label} için durum ver")
            with lock:
                results["ok" if outcome.get("ok") else "fail"] += 1
                if not outcome.get("ok"):
                    errors.append(str(outcome.get("error"))[:200])
        except Exception as exc:  # noqa: BLE001
            with lock:
                results[f"EXC:{type(exc).__name__}"] += 1
                errors.append(f"{type(exc).__name__}: {exc}"[:200])
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(sid, providers[i]))
               for i, sid in enumerate(sessions)]
    started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    elapsed = time.time() - started

    _cleanup(sessions)

    assert not any("locked" in e.lower() for e in errors), \
        f"veritabanı kilidi hâlâ var: {errors}"
    assert results["ok"] == len(sessions), f"başarısız turlar: {errors}"

    # Gerçekten PARALEL çalışmalı: seri olsaydı 4 × gecikme kadar sürerdi
    assert elapsed < gateway.delay * len(sessions), \
        f"turlar sıraya girdi ({elapsed:.2f} sn) — paralellik yok"


def test_steps_are_persisted_immediately(users, monkeypatch) -> None:
    """
    Tur bitmeden de adımlar veritabanında olmalı.

    Aksi hâlde tur yarıda kesildiğinde (çökme, durdurma) o ana kadarki
    denetim kaydı kaybolurdu.
    """
    session_id = _session_for(users[0], "nvidia")
    seen: list[int] = []

    class Watcher(SlowGateway):
        def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
            # Model çağrısı sırasında BAŞKA bir bağlantıdan okuma yap
            probe = SessionLocal()
            seen.append(probe.query(AgentMessage)
                        .filter(AgentMessage.session_id == session_id).count())
            probe.close()
            return ChatTurn(True, text="bitti")

    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: Watcher())

    db = SessionLocal()
    session = db.get(AgentSession, session_id)
    user = db.get(User, session.user_id)
    controller.run_agent(db, user, session, "merhaba")
    db.close()

    _cleanup([session_id])
    assert seen and seen[0] >= 1, \
        "kullanıcı mesajı, model çağrısı sırasında başka bağlantıdan görünmüyor"


def test_stopping_one_session_does_not_touch_others(users, monkeypatch) -> None:
    """Bir görevi durdurmak diğerlerini etkilememeli."""
    a = _session_for(users[0], "nvidia")
    b = _session_for(users[1], "gemini")

    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: SlowGateway(0.1))

    db = SessionLocal()
    session_a = db.get(AgentSession, a)
    session_b = db.get(AgentSession, b)
    controller.set_status(db, session_a, "idle")
    controller.set_status(db, session_b, "running")
    db.close()

    probe = SessionLocal()
    assert probe.get(AgentSession, a).status == "idle"
    assert probe.get(AgentSession, b).status == "running"
    probe.close()

    _cleanup([a, b])
