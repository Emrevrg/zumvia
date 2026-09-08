"""
API SÖZLEŞMELERİ

Bu rotalar canlı koşularda çalıştı ama hiçbir testi yoktu: `routes_bots`
658 satır, `routes_safety` 409, `routes_agent` 398. Çalışıyor olmaları
yarın da çalışacakları anlamına gelmez — sözleşmeleri kilitli değilse bir
değişiklik onları sessizce bozabilir.

Burada tek tek uç noktalar değil, İHLAL EDİLMEMESİ GEREKEN KURALLAR
sınanır:

    * kimliksiz istek veri göremez,
    * bir kullanıcı başkasının verisine erişemez,
    * gerçek paraya geçiş tek başına ajanla olmaz,
    * acil fren herkese kapalı değil, sahibine açıktır,
    * anahtarlar hiçbir yanıtta düz metin görünmez.

Son madde en kritiğidir: bir sızıntı geri alınamaz.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import create_access_token, hash_password
from app.main import app
from app.models import Bot, Credential, User

API_KEY_PLAINTEXT = "sk-gizli-anahtar-1234567890-cok-gizli"


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def people():
    """İki ayrı kullanıcı: yalıtım ancak iki taraf varsa sınanabilir."""
    db = SessionLocal()
    made = {}
    for name in ("alice", "bob"):
        email = f"{name}@apitest.com"
        row = db.query(User).filter(User.email == email).first()
        if row is None:
            row = User(email=email, password_hash=hash_password(f"{name}-parola-12345"),
                       vault_salt=new_salt())
            db.add(row)
            db.commit()
            db.refresh(row)
        made[name] = {"id": row.id, "email": email,
                      "token": create_access_token(row.id, email)}
    db.close()
    yield made

    db = SessionLocal()
    ids = [p["id"] for p in made.values()]
    db.query(Credential).filter(Credential.user_id.in_(ids)).delete(
        synchronize_session=False)
    db.query(Bot).filter(Bot.user_id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    db.close()


def auth(person) -> dict[str, str]:
    return {"Authorization": f"Bearer {person['token']}"}


@pytest.mark.parametrize("path", ["/", "/ui", "/ui/"])
def test_every_ui_entrypoint_serves_the_application(client, path: str) -> None:
    """Yenileme veya panel geçişi hiçbir UI adresinde 404/beyaz ekran vermemeli."""
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert 'id="app-view"' in response.text
    assert response.headers.get("cache-control") == "no-store, must-revalidate"


# --------------------------------------------------------------------------- #
#  Kimlik
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", [
    "/api/portfolio/overview",
    "/api/bots",
    "/api/keys",
    "/api/agent/sessions",
    "/api/safety/status",
    "/api/research/reports",
])
def test_data_endpoints_require_identity(client, path: str) -> None:
    """Kimliksiz istek veri göremez."""
    response = client.get(path)
    assert response.status_code in (401, 403), \
        f"{path} kimliksiz {response.status_code} döndü"


def test_a_forged_token_is_rejected(client) -> None:
    response = client.get("/api/bots",
                          headers={"Authorization": "Bearer uydurma.jeton.abc"})
    assert response.status_code in (401, 403)


@pytest.mark.parametrize("path", ["/api/health", "/api/agent/catalog",
                                  "/api/research/roles"])
def test_public_endpoints_stay_public(client, path: str) -> None:
    """
    Herkese açık uçlar açık kalmalı.

    Karşılama ekranı ve katalog kimlik istemez; istemesi kullanıcıyı
    kaydolmadan hiçbir şey göremez hâle getirirdi.
    """
    assert client.get(path).status_code == 200


# --------------------------------------------------------------------------- #
#  Yalıtım
# --------------------------------------------------------------------------- #

def test_one_user_cannot_read_another_users_bot(client, people) -> None:
    """
    Kullanıcı yalıtımı, çok kullanıcılı bir üründe en temel sözleşmedir.

    Kimlik doğrulaması yapıp SAHİPLİK kontrolü unutmak, en sık görülen
    ve en pahalı hatadır.
    """
    db = SessionLocal()
    bot = Bot(user_id=people["alice"]["id"], name="Alice botu", market="crypto",
              exchange="binance", symbol="BTC/USDT", timeframe="1h",
              initial_balance=1000.0, paper_balance=1000.0)
    db.add(bot)
    db.commit()
    bot_id = bot.id
    db.close()

    mine = client.get(f"/api/bots/{bot_id}", headers=auth(people["alice"]))
    assert mine.status_code == 200

    theirs = client.get(f"/api/bots/{bot_id}", headers=auth(people["bob"]))
    assert theirs.status_code == 404, \
        "başka kullanıcının botu görülebildi"


def test_one_user_cannot_control_another_users_bot(client, people) -> None:
    """Okumak kadar KOMUT vermek de yalıtılmalı."""
    db = SessionLocal()
    bot = Bot(user_id=people["alice"]["id"], name="Alice botu 2", market="crypto",
              exchange="binance", symbol="ETH/USDT", timeframe="1h",
              initial_balance=1000.0, paper_balance=1000.0)
    db.add(bot)
    db.commit()
    bot_id = bot.id
    db.close()

    response = client.post(f"/api/bots/{bot_id}/start", headers=auth(people["bob"]))
    assert response.status_code in (403, 404)


def test_bot_listing_shows_only_your_own(client, people) -> None:
    alice = client.get("/api/bots", headers=auth(people["alice"])).json()
    bob = client.get("/api/bots", headers=auth(people["bob"])).json()

    alice_rows = alice if isinstance(alice, list) else alice.get("bots", [])
    bob_rows = bob if isinstance(bob, list) else bob.get("bots", [])
    assert {b["id"] for b in alice_rows} & {b["id"] for b in bob_rows} == set()


# --------------------------------------------------------------------------- #
#  Sır sızıntısı
# --------------------------------------------------------------------------- #

def test_an_api_key_never_appears_in_any_response(client, people) -> None:
    """
    Anahtarlar hiçbir yanıtta düz metin görünmez.

    Bu, geri alınamaz tek hatadır: sızmış bir anahtar geri çağrılamaz.
    Bu yüzden yalnızca kasa ucu değil, anahtarı görebilecek HER uç
    denetlenir.
    """
    created = client.post("/api/keys", headers=auth(people["alice"]), json={
        "kind": "llm", "provider": "nvidia", "label": "Test anahtarı",
        "api_key": API_KEY_PLAINTEXT,
    })
    assert created.status_code in (200, 201), created.text

    for path in ("/api/keys", "/api/portfolio/overview", "/api/safety/status"):
        body = client.get(path, headers=auth(people["alice"])).text
        assert API_KEY_PLAINTEXT not in body, f"{path} anahtarı sızdırdı"
        assert "sk-gizli" not in body


def test_stored_keys_are_encrypted_at_rest(people) -> None:
    """Veritabanı dosyası ele geçse bile anahtar okunamamalı."""
    db = SessionLocal()
    rows = (db.query(Credential)
            .filter(Credential.user_id == people["alice"]["id"]).all())
    db.close()

    assert rows, "test anahtarı kaydedilmemiş"
    for row in rows:
        # Şifreli gövde metin ya da ikili olarak saklanabilir; ikisinde de
        # düz anahtar bulunmamalı.
        payload = row.payload_enc or b""
        blob = payload.encode() if isinstance(payload, str) else bytes(payload)
        assert API_KEY_PLAINTEXT.encode() not in blob, "anahtar şifresiz saklanmış"


# --------------------------------------------------------------------------- #
#  Yetki sınırları
# --------------------------------------------------------------------------- #

def test_live_trading_cannot_be_enabled_for_someone_elses_bot(client, people) -> None:
    """
    Gerçek paraya geçiş, sistemdeki en ağır yetkidir.

    Sahiplik kontrolü burada unutulursa başkasının parası riske girer.
    """
    db = SessionLocal()
    bot = Bot(user_id=people["alice"]["id"], name="Alice canlı", market="crypto",
              exchange="binance", symbol="BTC/USDT", timeframe="1h",
              initial_balance=1000.0, paper_balance=1000.0)
    db.add(bot)
    db.commit()
    bot_id = bot.id
    db.close()

    response = client.post("/api/safety/live-trading",
                           headers=auth(people["bob"]),
                           json={"bot_id": bot_id, "enabled": True,
                                 "capital": 100, "hours": 1})
    assert response.status_code in (400, 403, 404, 422), \
        "başkasının botu canlı moda alınabildi"


def test_kill_switch_is_readable_by_its_owner(client, people) -> None:
    """
    Acil fren durumu sahibine AÇIK olmalı.

    Kapalı bir acil fren, olmayan bir acil frendir: kullanıcı durumu
    göremiyorsa ona güvenemez.
    """
    response = client.get("/api/safety/status", headers=auth(people["alice"]))
    assert response.status_code == 200
    body = response.json()
    assert "kill_switch" in body


# --------------------------------------------------------------------------- #
#  Araştırma uçları
# --------------------------------------------------------------------------- #

def test_research_reports_are_scoped_to_the_owner(client, people) -> None:
    """Rapor dosyaları kullanıcı kimliğiyle adlanır; başkası okuyamaz."""
    response = client.get(
        f"/api/research/reports/{people['alice']['id']}-uydurma-rapor",
        headers=auth(people["bob"]))
    assert response.status_code == 404


def test_a_path_traversal_report_id_is_refused(client, people) -> None:
    """
    `../` içeren bir kimlik rapor klasörünün dışına çıkmamalı.

    Dosya adı kontrolü tek başına yetmez; çözümlenmiş yolun da klasör
    içinde kaldığı doğrulanır.
    """
    user_id = people["alice"]["id"]
    response = client.get(f"/api/research/reports/{user_id}-..%2f..%2fsecrets",
                          headers=auth(people["alice"]))
    assert response.status_code in (400, 404)


def test_role_catalog_describes_every_expert(client) -> None:
    body = client.get("/api/research/roles").json()
    ids = {role["id"] for role in body["roles"]}
    assert {"market", "news", "evidence", "risk", "skeptic", "synthesis"} <= ids
    for role in body["roles"]:
        assert role["mission"], f"{role['id']} görev tanımı boş"


# --------------------------------------------------------------------------- #
#  Sürüm el sıkışması
# --------------------------------------------------------------------------- #

def test_health_reports_the_routes_it_actually_has(client) -> None:
    """
    Sağlık ucu, süreçte GERÇEKTEN kayıtlı olan uç gruplarını bildirir.

    ÖLÇÜLDÜ: uvicorn statik dosyaları her istekte diskten okur ama Python
    rotalarını yalnızca açılışta kaydeder. Yeni bir ekran eklenip süreç
    yeniden başlatılmazsa `finance.js` yüklenir, çağırdığı uç 404 döner ve
    kullanıcı "not found" görür — kodda hata yokken bozuk bir ürün.

    Arayüz bu listeyi beklediğiyle karşılaştırıp "sunucunuz eski" diyebilsin
    diye liste ELLE YAZILMAZ, rota tablosundan çıkarılır. Elle yazılan bir
    liste eskir ve tam da önlemeye çalıştığımız yalanı üretir.
    """
    data = client.get("/api/health").json()

    assert isinstance(data, dict), "sağlık ucu sözlük yerine başka bir şey döndü"
    assert data["ok"] is True
    assert isinstance(data.get("capabilities"), list)

    for group in ("agent", "auth", "bots", "finance", "keys",
                  "market", "portfolio", "safety", "skills"):
        assert group in data["capabilities"], f"{group} bildirilmiyor"


def test_reported_capabilities_match_the_real_route_table() -> None:
    """
    Bildirilen liste, uygulamanın kendi rota tablosuyla BİREBİR aynı olmalı.

    Aksi hâlde "bu özellik var" diyen ama olmayan bir sağlık ucu elde
    ederiz — ki bu, hiç bildirmemekten daha kötüdür.
    """
    from app.main import _capabilities, _route_paths, app

    reported = set(_capabilities())
    real = {
        path.split("/")[2]
        for path in _route_paths(list(app.routes))
        if path.startswith("/api/") and len(path.split("/")) > 2 and path.split("/")[2]
    }
    assert reported == real, f"fark: {reported ^ real}"


def test_the_health_route_is_still_bound_to_the_health_handler(client) -> None:
    """
    Dekoratörün doğru fonksiyona bağlı olduğunu doğrular.

    Bu test bir kez gerçek bir hatayı yakaladı: `_capabilities` yardımcısı
    `@app.get("/api/health")` ile `health()` ARASINA eklenmişti ve dekoratör
    yardımcıya bağlanmıştı — sağlık ucu bir liste döndürüyordu.
    """
    data = client.get("/api/health").json()
    for key in ("ok", "app", "version", "disk", "hard_limits", "capabilities"):
        assert key in data, f"sağlık yanıtında {key} yok"
