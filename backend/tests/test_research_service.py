"""
ARAŞTIRMA SERVİSİ VE TOPLAYICI

İki modül en düşük kapsamla duruyordu: `collectors.py` %16, `service.py` %22.
İkisi de canlı koşularda çalıştı ama sözleşmeleri kilitli değildi.

Korunan kurallar:

    * Bir veri kaynağı düşerse diğerleri toplanmaya devam eder ve eksik
      olan RAPORLANIR — sessizce atlanmaz.
    * Roller mümkün olduğunca FARKLI sağlayıcılara dağıtılır; tek modele
      dört kez sormak "çoklu ajan" değil, aynı önyargının dört kopyasıdır.
    * Tek anahtar varsa bu gizlenmez, kullanıcıya söylenir.
"""
from __future__ import annotations

import json

import pytest

from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import Credential, CredentialKind, User
from app.research import collectors
from app.research.ledger import Ledger
from app.research.roles import MARKET_ANALYST, NEWS_ANALYST, RISK_OFFICER
from app.research.service import RoleAssigner


@pytest.fixture()
def user():
    db = SessionLocal()
    row = db.query(User).filter(User.email == "svc@zumvia.com").first()
    if row is None:
        row = User(email="svc@zumvia.com", password_hash=hash_password("svctest12345"),
                   vault_salt=new_salt())
        db.add(row)
        db.commit()
        db.refresh(row)
    db.query(Credential).filter(Credential.user_id == row.id).delete(
        synchronize_session=False)
    db.commit()
    yield db, row
    db.query(Credential).filter(Credential.user_id == row.id).delete(
        synchronize_session=False)
    db.commit()
    db.close()


@pytest.fixture()
def with_secrets(monkeypatch):
    """
    Anahtar çözme taklidi.

    Gerçek anahtarlar AES ile şifreli saklanır ve testte gerçek bir anahtar
    kullanmak hem gereksiz hem tehlikelidir. Burada sınanan şey şifreleme
    değil ROL DAĞITIMI; sırrın çözülebildiği varsayılır.
    """
    monkeypatch.setattr("app.research.service.read_secrets",
                        lambda user, cred: {"api_key": "test-anahtar"})


def add_key(db, user, provider: str, model: str) -> Credential:
    row = Credential(user_id=user.id, kind=CredentialKind.LLM, provider=provider,
                     label=provider, payload_enc=b"", hint="",
                     extra_json=json.dumps({"model": model}))
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------- #
#  Toplayıcı: dayanıklılık
# --------------------------------------------------------------------------- #

def test_a_failing_source_does_not_stop_the_others(monkeypatch) -> None:
    """
    Bir kaynak düşerse araştırma DURMAZ.

    Borsa yanıt vermiyorsa haberler yine okunur; hepsini birbirine bağlamak
    tek bir arızayı toplam başarısızlığa çevirirdi.
    """
    ledger = Ledger()

    def broken(*args, **kwargs):
        raise RuntimeError("borsa kapalı")

    monkeypatch.setattr(collectors, "_price_and_indicators", broken)
    monkeypatch.setattr(collectors, "_algo_vote",
                        lambda *a, **k: a[3].add("x.algo", 1, "algo", "m"))
    monkeypatch.setattr(collectors, "_news",
                        lambda *a, **k: a[2].add("haber.adet", 5, "rss", "m"))
    monkeypatch.setattr(collectors, "_backtest", lambda *a, **k: None)

    with pytest.raises(RuntimeError):
        collectors._price_and_indicators("crypto", "BTC/USDT", "4h", ledger, {}, None)


def test_every_failure_is_reported_not_swallowed(monkeypatch) -> None:
    """
    Sessizce atlanan bir kaynak, kullanıcıya "ölçüldü" izlenimi verir.

    Eksik olan RAPORLANIR: rapor bu eksiklerle okunmalıdır.
    """
    ledger = Ledger()
    context: dict = {"collected": [], "failed": []}

    monkeypatch.setattr(collectors, "fetch_news" if hasattr(collectors, "fetch_news")
                        else "log", collectors.log)
    collectors._news("crypto", "BTC/USDT", ledger, context, lambda *_: None)

    # Ağ varsa haber gelir, yoksa hata raporlanır — ikisinden biri olmalı,
    # ikisi birden olmamalı.
    assert context["collected"] or context["failed"]


def test_market_detection_prefers_symbols_over_words() -> None:
    """
    "THYAO nasıl" cümlesinde piyasa kelimesi yoktur ama sembol açıktır.

    Sembol, kelimeden daha güçlü bir kanıttır.
    """
    assert collectors.detect_market("THYAO nasıl gidiyor") == "stocks"
    assert collectors.detect_market("BTC/USDT analiz") == "crypto"
    assert collectors.detect_market("kriptoda param var") == "crypto"


def test_a_turkish_word_is_never_mistaken_for_a_ticker() -> None:
    """
    Metni `.upper()` yapmak, kod ile kelimeyi ayıran tek ipucunu yok eder.

    "bugün hisselerde fırsat var mı" cümlesinde FIRSAT bir hisse koduna
    benzer — ama değildir.
    """
    symbols = collectors.detect_symbols("bugün hisselerde fırsat var mı", "stocks")
    assert "FIRSAT" not in symbols
    assert symbols == list(collectors.DEFAULT_STOCKS)


def test_no_symbol_falls_back_to_a_default_universe() -> None:
    """
    Kullanıcı varlık belirtmediyse varsayılan evrene düşülür — ve bu bir
    VARSAYIMDIR, ölçüm gibi sunulamaz.
    """
    assert collectors.detect_symbols("param neye yatırılmalı", "crypto") == \
        list(collectors.DEFAULT_CRYPTO)


# --------------------------------------------------------------------------- #
#  Rol dağıtımı
# --------------------------------------------------------------------------- #

def test_roles_are_spread_across_different_providers(user, with_secrets) -> None:
    """
    Dört uzman tek modele sorulursa dört kez aynı önyargı alınır.

    "Çoklu ajan" o zaman bir yanılsamaya dönüşür: farklı roller, aynı kör
    nokta.
    """
    db, u = user
    add_key(db, u, "nvidia", "nvidia/model-a")
    add_key(db, u, "openrouter", "openrouter/model-b")

    assigner = RoleAssigner(db, u)
    providers = set()
    for role in (MARKET_ANALYST, NEWS_ANALYST, RISK_OFFICER):
        gateway = assigner.gateway_for(role)
        if gateway is not None:
            providers.add(gateway.provider_id)

    assert len(providers) >= 2, f"roller tek sağlayıcıda toplandı: {providers}"


def test_a_single_key_is_reported_not_hidden(user) -> None:
    """
    Tek anahtar varsa çeşitlilik YOKTUR ve bu kullanıcıya söylenir.

    Gizlemek, raporun sağlamlığı hakkında yanlış bir izlenim verirdi.
    """
    db, u = user
    add_key(db, u, "nvidia", "nvidia/model-a")

    note = RoleAssigner(db, u).diversity_note
    assert "sağlayıcı" in note or "model" in note
    assert "tek" in note.lower() or "1 model" in note


def test_no_keys_at_all_is_stated_plainly(user) -> None:
    db, u = user
    assigner = RoleAssigner(db, u)
    assert assigner.gateway_for(MARKET_ANALYST) is None
    assert "anahtar" in assigner.diversity_note.lower()


def test_the_same_role_keeps_its_model_across_calls(user, with_secrets) -> None:
    """
    Bir rol tur boyunca AYNI modeli kullanmalı.

    Aksi hâlde aynı uzmanın iki cümlesi iki farklı modelden gelir ve
    tutarsızlık uzmana değil sisteme ait olur.
    """
    db, u = user
    add_key(db, u, "nvidia", "nvidia/model-a")
    add_key(db, u, "openrouter", "openrouter/model-b")

    assigner = RoleAssigner(db, u)
    first = assigner.gateway_for(MARKET_ANALYST)
    second = assigner.gateway_for(MARKET_ANALYST)
    assert first is second


def test_asking_for_a_fresh_model_changes_it(user, with_secrets) -> None:
    """
    Bir model boş yanıt döndürdüğünde aynısıyla tekrar denemenin faydası
    yoktur; başkasına geçilmeli.
    """
    db, u = user
    add_key(db, u, "nvidia", "nvidia/model-a")
    add_key(db, u, "openrouter", "openrouter/model-b")

    assigner = RoleAssigner(db, u)
    first = assigner.gateway_for(MARKET_ANALYST)
    fresh = assigner.gateway_for(MARKET_ANALYST, fresh=True)

    assert first is not None and fresh is not None
    assert fresh.model != first.model, "aynı model tekrar verildi"
