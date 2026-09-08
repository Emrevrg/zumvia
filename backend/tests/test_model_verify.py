"""
MODEL DOĞRULAMA

Ölçülmüş gerçek: NVIDIA NIM 69 model listeliyor, kullanıcının hesabı bunların
yalnızca 14'ünü çağırabiliyor ve 12'si araç çağırabiliyor. Yani listeden
rastgele seçilen bir modelin görevi öldürme olasılığı %80.

Bu yüzden "listede var" ile "çalışıyor" ayrı tutulur ve fark, tahminle değil
gerçek bir çağrıyla öğrenilir.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.layers import model_verify
from app.models import Credential, CredentialKind, ModelCheck, User


@pytest.fixture()
def cred_id():
    db = SessionLocal()
    user = db.query(User).filter(User.email == "verify@zumvia.com").first()
    if user is None:
        user = User(email="verify@zumvia.com", password_hash=hash_password("verifytest12345"),
                    vault_salt=new_salt())
        db.add(user)
        db.commit()
        db.refresh(user)
    cred = Credential(user_id=user.id, kind=CredentialKind.LLM, provider="nvidia",
                      label="Doğrulama", payload_enc=b"", hint="",
                      extra_json=json.dumps({"model": ""}))
    db.add(cred)
    db.commit()
    cid = cred.id
    db.close()
    yield cid
    db = SessionLocal()
    db.query(ModelCheck).filter(ModelCheck.credential_id == cid).delete(
        synchronize_session=False)
    db.query(Credential).filter(Credential.id == cid).delete(synchronize_session=False)
    db.commit()
    db.close()


def _response(status: int, body: str = "{}") -> httpx.Response:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return httpx.Response(status, text=body, request=request)


class _FakeClient:
    """İstenen yanıtları sırayla döndüren sahte HTTP istemcisi."""

    def __init__(self, answers: dict[str, httpx.Response]) -> None:
        self.answers = answers
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):  # noqa: A002, ARG002
        self.calls.append(json or {})
        model = (json or {}).get("model", "")
        return self.answers.get(model, _response(404, '{"error":"unknown"}'))


# --------------------------------------------------------------------------- #
#  Tek model yoklaması
# --------------------------------------------------------------------------- #

def test_working_model_is_verified(monkeypatch) -> None:
    client = _FakeClient({"iyi/model": _response(200, '{"choices":[]}')})
    monkeypatch.setattr(model_verify.httpx, "Client", lambda **k: client)

    verdict = model_verify.probe("nvidia", "anahtar", "", "iyi/model")

    assert verdict.ok is True
    assert verdict.supports_tools is True
    assert client.calls[0]["max_tokens"] == model_verify.PROBE_TOKENS, \
        "yoklama kotadan gereksiz yiyor"


def test_retired_model_is_reported_as_retired(monkeypatch) -> None:
    """410 = emekliye ayrıldı. Kullanıcı bunu bilmeli, '404' görmemeli."""
    client = _FakeClient({"eski/model": _response(410, "reached its end of life")})
    monkeypatch.setattr(model_verify.httpx, "Client", lambda **k: client)

    verdict = model_verify.probe("nvidia", "anahtar", "", "eski/model")

    assert verdict.ok is False
    assert verdict.code == "RETIRED"


def test_model_without_tool_support_still_counts_as_working(monkeypatch) -> None:
    """
    Araç desteklemeyen model BOZUK değildir — yalnızca ajan için uygun değildir.

    İkisini karıştırmak, çalışan modelleri listeden atardı.
    """
    calls: list[dict] = []

    class Picky(_FakeClient):
        def post(self, url, headers=None, json=None):  # noqa: A002, ARG002
            calls.append(json or {})
            if "tools" in (json or {}):
                return _response(400, "tools not supported")
            return _response(200, '{"choices":[]}')

    monkeypatch.setattr(model_verify.httpx, "Client", lambda **k: Picky({}))

    verdict = model_verify.probe("nvidia", "anahtar", "", "sade/model")

    assert verdict.ok is True, "araç desteklemeyen model bozuk sayıldı"
    assert verdict.supports_tools is False
    assert len(calls) == 2, "araçsız tekrar denenmedi"


def test_network_error_is_not_blamed_on_the_model(monkeypatch) -> None:
    """Ağ arızası modelin suçu değildir; kalıcı 'bozuk' damgası vurulmamalı."""
    class Broken(_FakeClient):
        def post(self, *a, **k):
            raise httpx.ConnectError("bağlanamadı")

    monkeypatch.setattr(model_verify.httpx, "Client", lambda **k: Broken({}))

    verdict = model_verify.probe("nvidia", "anahtar", "", "her/model")

    assert verdict.ok is False
    assert verdict.code == "NETWORK"


# --------------------------------------------------------------------------- #
#  Kalıcı sicil
# --------------------------------------------------------------------------- #

def test_results_are_remembered(cred_id) -> None:
    db = SessionLocal()
    model_verify.remember(db, cred_id, model_verify.Verdict(
        "a/b", True, supports_tools=True, latency_ms=120))
    known = model_verify.cached(db, cred_id)
    db.close()

    assert known["a/b"]["ok"] is True
    assert known["a/b"]["supports_tools"] is True
    assert known["a/b"]["fresh"] is True


def test_stale_results_are_not_trusted(cred_id) -> None:
    """
    Eski sonuç "taze" sayılmaz.

    Sağlayıcılar model emekliye ayırır; dünkü doğrulama bugünü garanti etmez.
    """
    db = SessionLocal()
    model_verify.remember(db, cred_id, model_verify.Verdict("eski/model", True))
    row = (db.query(ModelCheck)
           .filter(ModelCheck.credential_id == cred_id).first())
    row.checked_at = datetime.now(UTC) - model_verify.FRESH_FOR - timedelta(minutes=1)
    db.commit()

    known = model_verify.cached(db, cred_id)
    db.close()
    assert known["eski/model"]["fresh"] is False


def test_verification_skips_fresh_results(cred_id, monkeypatch) -> None:
    """İkinci tarama, taze bilinenleri yeniden çağırmamalı — kota boşa gitmesin."""
    probes: list[str] = []

    def fake_probe(provider, key, base, model, check_tools=True):  # noqa: ARG001
        probes.append(model)
        return model_verify.Verdict(model, True, supports_tools=True)

    monkeypatch.setattr(model_verify, "probe", fake_probe)

    db = SessionLocal()
    model_verify.verify_all(db, cred_id, "nvidia", "k", "", ["x/1", "x/2"])
    assert probes == ["x/1", "x/2"]

    probes.clear()
    report = model_verify.verify_all(db, cred_id, "nvidia", "k", "", ["x/1", "x/2"])
    db.close()

    assert probes == [], "taze sonuçlar yeniden sorgulandı"
    assert report.skipped == 2
    assert report.working == ["x/1", "x/2"]


def test_force_rechecks_everything(cred_id, monkeypatch) -> None:
    probes: list[str] = []
    monkeypatch.setattr(model_verify, "probe",
                        lambda p, k, b, m, check_tools=True: (probes.append(m),
                                                              model_verify.Verdict(m, True))[1])
    db = SessionLocal()
    model_verify.verify_all(db, cred_id, "nvidia", "k", "", ["x/1"])
    probes.clear()
    model_verify.verify_all(db, cred_id, "nvidia", "k", "", ["x/1"], force=True)
    db.close()
    assert probes == ["x/1"]


def test_forget_clears_history(cred_id) -> None:
    db = SessionLocal()
    model_verify.remember(db, cred_id, model_verify.Verdict("a/b", True))
    assert model_verify.forget(db, cred_id) == 1
    assert model_verify.cached(db, cred_id) == {}
    db.close()


# --------------------------------------------------------------------------- #
#  Varsayılan seçim
# --------------------------------------------------------------------------- #

def test_best_model_prefers_tool_capable(cred_id) -> None:
    """
    Ajan araç çağıramayan bir modelle iş yapamaz.

    "Çalışıyor" yeterli değildir; varsayılan, araç çağırabilen olmalıdır.
    """
    db = SessionLocal()
    model_verify.remember(db, cred_id, model_verify.Verdict("sade/model", True,
                                                            supports_tools=False))
    model_verify.remember(db, cred_id, model_verify.Verdict("araclı/model", True,
                                                            supports_tools=True))
    best = model_verify.best_model(db, cred_id, ["sade/model", "araclı/model"])
    db.close()
    assert best == "araclı/model"


def test_best_model_never_picks_a_known_broken_one(cred_id) -> None:
    db = SessionLocal()
    model_verify.remember(db, cred_id, model_verify.Verdict("bozuk/model", False,
                                                            code="RETIRED"))
    model_verify.remember(db, cred_id, model_verify.Verdict("saglam/model", True,
                                                            supports_tools=True))
    best = model_verify.best_model(db, cred_id, ["bozuk/model", "saglam/model"])
    db.close()
    assert best == "saglam/model"


def test_unknown_model_is_preferred_over_known_broken(cred_id) -> None:
    """
    Hiç denenmemiş model, bozuk olduğu BİLİNEN modelden iyidir.

    Bilinmemek, bozuk olmakla aynı şey değildir.
    """
    db = SessionLocal()
    model_verify.remember(db, cred_id, model_verify.Verdict("bozuk/model", False,
                                                            code="NOT_FOUND"))
    best = model_verify.best_model(db, cred_id, ["bozuk/model", "yeni/model"])
    db.close()
    assert best == "yeni/model"


def test_catalog_defaults_are_tool_capable() -> None:
    """
    Koddaki NVIDIA varsayılanları gerçekten doğrulanmış olmalı.

    Bu liste 2026-08-26'da 69 modelin tamamı tek tek çağrılarak süzüldü;
    yeniden düzenlenirken yanlışlıkla çalışmayan bir model eklenmesin.
    """
    from app.layers.l3_llm_gateway import PROVIDERS

    verified_tool_capable = {
        "google/diffusiongemma-26b-a4b-it",
        "meta/llama-3.2-11b-vision-instruct", "meta/llama-3.2-90b-vision-instruct",
        "meta/muse-glimmer-30b", "minimaxai/minimax-m3", "moonshotai/kimi-k3",
        "nvidia/nemotron-3-nano-30b-a3b",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        "nvidia/nemotron-3-super-120b-a12b",
        "nvidia/nemotron-3.5-lightning-30b-a3b",
        "openai/gpt-oss-20b", "stepfun-ai/step-3.7-flash",
    }
    listed = set(PROVIDERS["nvidia"].models)
    assert listed <= verified_tool_capable, \
        f"doğrulanmamış model katalogda: {listed - verified_tool_capable}"
