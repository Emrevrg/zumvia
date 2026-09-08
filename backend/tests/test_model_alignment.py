"""
MODEL–SAĞLAYICI UYUMU

Yaşanmış arıza: kullanıcı oturumun sağlayıcısını NVIDIA'dan OpenRouter'a
çevirdi; model alanında NVIDIA modeli (`moonshotai/kimi-k3`) kaldı. İki alan
birbirinden bağımsız yazıldığı için oturum, o sağlayıcıda var olmayan bir
modeli çağırdı ve her turda hata verdi.

Kural: anahtar değişirse model de o anahtarın sağlayıcısına çekilir — ama
kullanıcı modeli aynı istekte açıkça yazdıysa ona dokunulmaz (elle model
girişi desteklenen bir özelliktir).
"""
from __future__ import annotations

import json

import pytest

from app.agent.controller import align_model_to_credential
from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import AgentSession, Credential, CredentialKind, User


@pytest.fixture()
def fixture_ids():
    db = SessionLocal()
    user = db.query(User).filter(User.email == "align@zumvia.com").first()
    if user is None:
        user = User(email="align@zumvia.com", password_hash=hash_password("aligntest12345"),
                    vault_salt=new_salt())
        db.add(user)
        db.commit()
        db.refresh(user)

    nvidia = Credential(user_id=user.id, kind=CredentialKind.LLM, provider="nvidia",
                        label="NVIDIA", payload_enc=b"", hint="",
                        extra_json=json.dumps({"model": "moonshotai/kimi-k3"}))
    openrouter = Credential(user_id=user.id, kind=CredentialKind.LLM, provider="openrouter",
                            label="OpenRouter", payload_enc=b"", hint="",
                            extra_json=json.dumps({"model": "poolside/laguna-s-2.1:free"}))
    db.add_all([nvidia, openrouter])
    db.commit()

    session = AgentSession(user_id=user.id, title="Hizalama", control_tool="api",
                           control_credential_id=nvidia.id,
                           control_model="moonshotai/kimi-k3", status="idle")
    db.add(session)
    db.commit()
    ids = (user.id, session.id, nvidia.id, openrouter.id)
    db.close()
    yield ids

    db = SessionLocal()
    db.query(AgentSession).filter(AgentSession.id == ids[1]).delete(synchronize_session=False)
    db.query(Credential).filter(Credential.id.in_(ids[2:])).delete(synchronize_session=False)
    db.commit()
    db.close()


def test_model_follows_the_credential(fixture_ids) -> None:
    """Sağlayıcı değişince model de o sağlayıcının modeline çekilir."""
    user_id, session_id, _, openrouter_id = fixture_ids
    db = SessionLocal()
    user = db.get(User, user_id)
    session = db.get(AgentSession, session_id)

    session.control_credential_id = openrouter_id       # kullanıcı sağlayıcıyı değiştirdi
    applied = align_model_to_credential(db, user, session, model_given=False)
    db.commit()
    db.close()

    assert applied == "poolside/laguna-s-2.1:free"
    probe = SessionLocal()
    assert probe.get(AgentSession, session_id).control_model == "poolside/laguna-s-2.1:free"
    probe.close()


def test_explicit_model_is_never_overwritten(fixture_ids) -> None:
    """
    Kullanıcı modeli elle yazdıysa sistem üstüne yazmaz.

    Elle model girişi bilinçli bir özellik: sağlayıcının listelemediği ama
    çalışan modeller böyle kullanılır.
    """
    user_id, session_id, _, openrouter_id = fixture_ids
    db = SessionLocal()
    user = db.get(User, user_id)
    session = db.get(AgentSession, session_id)

    session.control_credential_id = openrouter_id
    session.control_model = "elle/yazdigim-model"
    applied = align_model_to_credential(db, user, session, model_given=True)
    db.commit()
    db.close()

    assert applied == ""
    probe = SessionLocal()
    assert probe.get(AgentSession, session_id).control_model == "elle/yazdigim-model"
    probe.close()


def test_alignment_is_a_no_op_when_already_correct(fixture_ids) -> None:
    """Gereksiz yazma yapılmaz — model zaten doğruysa dokunulmaz."""
    user_id, session_id, nvidia_id, _ = fixture_ids
    db = SessionLocal()
    user = db.get(User, user_id)
    session = db.get(AgentSession, session_id)
    session.control_credential_id = nvidia_id
    assert align_model_to_credential(db, user, session, model_given=False) == ""
    db.close()


def test_other_users_credential_is_ignored(fixture_ids) -> None:
    """Başka kullanıcının anahtarı üzerinden model hizalanamaz."""
    _, session_id, _, openrouter_id = fixture_ids
    db = SessionLocal()
    stranger = db.query(User).filter(User.email == "stranger@zumvia.com").first()
    if stranger is None:
        stranger = User(email="stranger@zumvia.com",
                        password_hash=hash_password("strangertest12345"),
                        vault_salt=new_salt())
        db.add(stranger)
        db.commit()
        db.refresh(stranger)
    session = db.get(AgentSession, session_id)
    session.control_credential_id = openrouter_id
    assert align_model_to_credential(db, stranger, session, model_given=False) == ""
    db.close()
