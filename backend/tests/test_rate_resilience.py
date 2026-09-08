"""
HIZ SINIRINA DAYANIKLILIK

Yaşanmış arıza: üç görev aynı anda çalıştı, NVIDIA 429 döndü ve iki görev
öldü. Üstelik kullanıcıya gösterilen mesaj "sistem otomatik bekleyip yeniden
deniyor" diyordu — sistem denemiyordu. Mesajın yalan söylemesi, hatanın
kendisinden daha kötüdür.

Kural: hız sınırı "şu an değil" demektir, "asla" değil. Beklenir, tekrar
denenir; sabır biterse başka sağlayıcıya geçilir. Görev ancak gidilecek
hiçbir yer kalmadığında biter.
"""
from __future__ import annotations

import time

import pytest

from app.agent import controller, run_lock
from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.layers import rate_limit
from app.layers.l3_llm_gateway import ChatTurn
from app.models import AgentMessage, AgentSession, User


@pytest.fixture(autouse=True)
def _clean():
    run_lock.release_all()
    yield
    run_lock.release_all()


@pytest.fixture()
def session_id():
    db = SessionLocal()
    user = db.query(User).filter(User.email == "rate@zumvia.com").first()
    if user is None:
        user = User(email="rate@zumvia.com", password_hash=hash_password("ratetest12345"),
                    vault_salt=new_salt())
        db.add(user)
        db.commit()
        db.refresh(user)
    row = AgentSession(user_id=user.id, title="Hız testi", control_tool="api",
                       control_model="test-model", status="idle")
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
#  Sınıflandırma
# --------------------------------------------------------------------------- #

def test_rate_limit_is_recognised() -> None:
    assert controller._is_rate_limited("Client error '429 Too Many Requests'")
    assert controller._is_rate_limited("HTTP 429")
    assert not controller._is_rate_limited("404 Not Found")
    assert not controller._is_rate_limited("401 Unauthorized")


def test_backoff_grows_and_is_bounded() -> None:
    """Bekleme süresi artmalı ama sınırsız uzamamalı."""
    waits = [controller._rate_backoff(i) for i in range(1, controller.MAX_RATE_WAITS + 1)]
    assert waits == sorted(waits), "bekleme süresi artmıyor"
    assert max(waits) <= 120, "tek bekleme çok uzun"
    assert sum(waits) <= 180, "toplam bekleme kullanıcıyı çok bekletir"


# --------------------------------------------------------------------------- #
#  Döngü davranışı
# --------------------------------------------------------------------------- #

def test_agent_waits_and_retries_instead_of_dying(session_id, monkeypatch) -> None:
    """
    İlk çağrı 429 alır, ikincisi başarılı olur → görev TAMAMLANMALI.

    Eski davranışta ilk 429 turu bitiriyordu.
    """
    monkeypatch.setattr(controller, "RATE_BACKOFF", (0.05, 0.05, 0.05, 0.05))
    calls = {"n": 0}

    class Flaky:
        def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
            calls["n"] += 1
            if calls["n"] == 1:
                return ChatTurn(False, error="Client error '429 Too Many Requests'")
            return ChatTurn(True, text="BTC yükseliş eğiliminde.")

    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: Flaky())

    db = SessionLocal()
    session = db.get(AgentSession, session_id)
    result = controller.run_agent(db, db.get(User, session.user_id), session, "BTC?")
    db.close()

    assert result["ok"] is True, f"429 sonrası görev öldü: {result}"
    assert calls["n"] == 2, "tekrar denenmedi"

    probe = SessionLocal()
    msgs = probe.query(AgentMessage).filter(AgentMessage.session_id == session_id).all()
    probe.close()
    assert any("hız sınırına takıldı" in (m.content or "") for m in msgs), \
        "kullanıcıya beklendiği söylenmedi"
    assert any("BTC yükseliş" in (m.content or "") for m in msgs), "yanıt üretilmedi"


def test_waiting_does_not_consume_the_step_budget(session_id, monkeypatch) -> None:
    """
    Bekleme, modelin düşünme adımlarından düşülmemeli.

    Aksi hâlde birkaç 429, ajanın işi bitirmeden adım bütçesini tüketmesine
    yol açar — sınır sağlayıcıdan geldiği hâlde ceza kullanıcıya kesilirdi.
    """
    monkeypatch.setattr(controller, "RATE_BACKOFF", (0.02, 0.02, 0.02, 0.02))
    calls = {"n": 0}

    class TwoStrikes:
        def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
            calls["n"] += 1
            if calls["n"] <= 2:
                return ChatTurn(False, error="HTTP 429")
            return ChatTurn(True, text="bitti")

    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: TwoStrikes())

    db = SessionLocal()
    session = db.get(AgentSession, session_id)
    result = controller.run_api_agent(db, db.get(User, session.user_id), session,
                                      "soru", max_steps=2)
    db.close()

    assert result["ok"] is True, "iki bekleme adım bütçesini tüketti"


def test_stop_wins_over_waiting(session_id, monkeypatch) -> None:
    """Kullanıcı beklerken Durdur'a basarsa hemen durulmalı."""
    monkeypatch.setattr(controller, "RATE_BACKOFF", (30.0, 30.0, 30.0, 30.0))

    class AlwaysLimited:
        def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
            run_lock.cancel(session_id)         # kullanıcı Durdur'a bastı
            return ChatTurn(False, error="HTTP 429")

    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: AlwaysLimited())

    db = SessionLocal()
    session = db.get(AgentSession, session_id)
    started = time.time()
    result = controller.run_agent(db, db.get(User, session.user_id), session, "soru")
    elapsed = time.time() - started
    db.close()

    assert result.get("stopped") is True
    assert elapsed < 5, f"Durdur, beklemeyi kesmedi ({elapsed:.1f} sn)"


def test_gives_up_cleanly_when_nowhere_to_go(session_id, monkeypatch) -> None:
    """
    Sürekli 429 ve geçilecek başka sağlayıcı yoksa görev temiz biter.

    Sonsuz beklemek de kabul edilemez: kullanıcı ne olduğunu öğrenmeli.
    """
    monkeypatch.setattr(controller, "RATE_BACKOFF", (0.01, 0.01, 0.01, 0.01))

    class AlwaysLimited:
        def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
            return ChatTurn(False, error="HTTP 429")

    monkeypatch.setattr(controller, "_gateway_from", lambda *a, **k: AlwaysLimited())

    db = SessionLocal()
    session = db.get(AgentSession, session_id)
    result = controller.run_agent(db, db.get(User, session.user_id), session, "soru")
    db.close()

    assert result["ok"] is False
    assert "429" not in result["error"], "kullanıcıya ham HTTP kodu gösterildi"
    assert "sınır" in result["error"].lower()


# --------------------------------------------------------------------------- #
#  Uyarlanabilir sınır: sağlayıcıdan ÖĞRENME
# --------------------------------------------------------------------------- #

def test_limiter_tightens_after_throttling() -> None:
    """
    429 geldikçe tavan daralmalı.

    Yayınlanmış limit her hesap için doğru değildir; sistem sağlayıcının
    gerçekte kabul ettiğini gözlemleyerek öğrenmelidir.
    """
    provider = "test_tighten"
    rate_limit.configure(provider, rpm=40, concurrent=4)
    bucket = rate_limit._bucket(provider)

    with rate_limit.acquire(provider) as slot:
        slot.penalize(retry_after=0.01)

    assert bucket.learned_rpm > 0, "tavan öğrenilmedi"
    assert bucket.learned_rpm < 40, "tavan daralmadı"
    assert rate_limit._effective_concurrent(bucket) < 4, "eş zamanlılık daralmadı"


def test_learned_limit_never_reaches_zero() -> None:
    """Daralma sistemi tümden durdurmamalı."""
    provider = "test_floor"
    rate_limit.configure(provider, rpm=40, concurrent=4)
    bucket = rate_limit._bucket(provider)

    # Burada ÖĞRENME KURALI sınanıyor, kuyruk değil: her turda pencere ve
    # ceza sıfırlanır, yoksa daralan tavan kendi testini bloklardı.
    for _ in range(12):
        with rate_limit.acquire(provider) as slot:
            slot.penalize(retry_after=0.01)
        with bucket.lock:
            bucket.requests.clear()
            bucket.blocked_until = 0.0

    assert bucket.learned_rpm >= rate_limit.MIN_LEARNED_RPM
    assert rate_limit._effective_concurrent(bucket) >= 1


def test_limiter_relaxes_after_sustained_success() -> None:
    """
    Tek bir geçici red sistemi kalıcı olarak yavaşlatmamalı.

    Kesintisiz başarıdan sonra tavan kademe kademe geri açılır.
    """
    provider = "test_relax"
    rate_limit.configure(provider, rpm=60, concurrent=6)
    bucket = rate_limit._bucket(provider)

    with rate_limit.acquire(provider) as slot:
        slot.penalize(retry_after=0.01)
    tightened = bucket.learned_rpm
    assert tightened > 0

    time.sleep(0.05)                     # ceza süresi dolsun
    for _ in range(rate_limit.CLEAN_STREAK_TO_RELAX):
        with rate_limit.acquire(provider) as slot:
            slot.succeed()

    relaxed = bucket.learned_rpm
    assert relaxed == 0 or relaxed > tightened, \
        f"tavan gevşemedi ({tightened} -> {relaxed})"


def test_snapshot_reports_the_real_ceiling() -> None:
    """Arayüz, belgedeki değil GERÇEKTEN uygulanan sınırı göstermeli."""
    provider = "test_snapshot"
    rate_limit.configure(provider, rpm=40, concurrent=4)
    with rate_limit.acquire(provider) as slot:
        slot.penalize(retry_after=0.01)

    row = rate_limit.snapshot()[provider]
    assert row["limit_rpm"] == 40
    assert row["effective_rpm"] < 40
    assert row["learned"] is True
