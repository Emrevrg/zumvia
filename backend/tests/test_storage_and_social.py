"""
DİSK YÖNETİMİ VE RESMÎ SOSYAL API

İki eski sınır burada kapanır.

DİSK: Bir ticaret sisteminde disk dolması sıradan bir aksaklık değildir —
o an veritabanına yazılamaz, yani AÇIK POZİSYON KAYDI tutulamaz. Borsada
gerçek bir pozisyon durur ama sistemin haberi olmaz. Bu yüzden sistem kendi
büyümesini sınırlar ve sıkışınca önce lüksten vazgeçer.

SOSYAL: Ölçüldü — herkese açık X aynalarının hiçbiri çalışmıyor. Bir
özelliğin "bazen çalışan üçüncü taraf aynalara" dayanması, o özelliğin
olmaması demektir. Resmî API zincirin başına alındı.
"""
from __future__ import annotations

import json

import pytest

from app.core import storage
from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.layers import social_api
from app.layers.social_api import SocialApiError
from app.models import (
    AgentMessage,
    AgentSession,
    Bot,
    BotEvent,
    Credential,
    CredentialKind,
    EquityPoint,
    User,
)


@pytest.fixture()
def user():
    db = SessionLocal()
    row = db.query(User).filter(User.email == "store@zumvia.com").first()
    if row is None:
        row = User(email="store@zumvia.com", password_hash=hash_password("storetest12345"),
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


# --------------------------------------------------------------------------- #
#  Disk: seviye ve karar
# --------------------------------------------------------------------------- #

def test_disk_status_is_readable() -> None:
    state = storage.status()
    assert state.level in ("ok", "low", "critical")
    assert state.total_mb >= 0
    assert state.message


@pytest.mark.parametrize("free_mb,level,optional_allowed", [
    (10_000, "ok", True),
    (500, "low", True),         # azalıyor ama rapor hâlâ yazılabilir
    (100, "critical", False),   # sıkıştı: lüks kesilir
])
def test_low_disk_stops_optional_writes_but_not_essential_ones(
        monkeypatch, free_mb, level, optional_allowed) -> None:
    """
    Rapor vazgeçilebilir; pozisyon kaydı değil.

    Sıkışınca ilkinden vazgeçilir, ikincisi korunur. Bu ayrım olmadan
    sistem ya gereksiz yere durur ya da kayıt tutamaz hâle gelir.
    """
    class FakeUsage:
        total = 100 * 1_048_576 * 1024
        free = int(free_mb * 1_048_576)
        used = total - free

    monkeypatch.setattr(storage.shutil, "disk_usage", lambda p: FakeUsage)
    state = storage.status()

    assert state.level == level, f"{free_mb} MB → {state.level}"
    assert state.can_write_optional is optional_allowed


def test_a_critical_disk_says_what_is_happening(monkeypatch) -> None:
    """
    Sessizce çalışmayı sürdürüp bir gün kayıt tutamamak, uyarmaktan çok
    daha kötüdür.
    """
    class FakeUsage:
        total = 100 * 1_048_576 * 1024
        free = 50 * 1_048_576
        used = total - free

    monkeypatch.setattr(storage.shutil, "disk_usage", lambda p: FakeUsage)
    message = storage.status().message
    assert "kritik" in message.lower()
    assert "pozisyon" in message.lower(), "neyin korunduğu söylenmedi"


# --------------------------------------------------------------------------- #
#  Disk: temizlik gerçekten yer açıyor mu
# --------------------------------------------------------------------------- #

def test_old_bot_events_are_trimmed(user) -> None:
    """
    Olaylar tanı içindir: "bot neden işlem açmadı" sorusunun cevabı
    buradadır. Ama bir aydan eski bir cevabı kimse aramaz.
    """
    from datetime import UTC, datetime, timedelta

    db, u = user
    bot = Bot(user_id=u.id, name="Temizlik testi", market="crypto",
              exchange="binance", symbol="BTC/USDT", timeframe="1h",
              initial_balance=1000.0, paper_balance=1000.0)
    db.add(bot)
    db.commit()

    old = datetime.now(UTC) - timedelta(days=storage.KEEP_BOT_EVENTS_DAYS + 5)
    fresh = datetime.now(UTC)
    for moment, count in ((old, 5), (fresh, 3)):
        for index in range(count):
            db.add(BotEvent(bot_id=bot.id, level="info", category="data",
                            message=f"olay {index}", ts=moment))
    db.commit()

    removed = storage._trim_bot_events(db, 1.0)
    remaining = db.query(BotEvent).filter(BotEvent.bot_id == bot.id).count()

    db.query(BotEvent).filter(BotEvent.bot_id == bot.id).delete(
        synchronize_session=False)
    db.query(Bot).filter(Bot.id == bot.id).delete(synchronize_session=False)
    db.commit()

    assert removed >= 5, f"eski olaylar silinmedi ({removed})"
    assert remaining == 3, f"yeni olaylar da silindi ({remaining})"


def test_equity_curve_keeps_the_newest_points(user) -> None:
    """
    Grafik için son birkaç bin nokta yeter; daha eskisi ekranda tek bir
    piksele düşer. Ama silinen EN ESKİLER olmalı, en yeniler değil.
    """
    db, u = user
    bot = Bot(user_id=u.id, name="Egri testi", market="crypto",
              exchange="binance", symbol="ETH/USDT", timeframe="1h",
              initial_balance=1000.0, paper_balance=1000.0)
    db.add(bot)
    db.commit()

    for index in range(30):
        db.add(EquityPoint(bot_id=bot.id, equity=1000.0 + index, balance=1000.0))
    db.commit()

    original_keep = storage.KEEP_EQUITY_POINTS_PER_BOT
    storage.KEEP_EQUITY_POINTS_PER_BOT = 20      # 200 tabanı devrede
    try:
        storage._trim_equity_points(db, 1.0)
    finally:
        storage.KEEP_EQUITY_POINTS_PER_BOT = original_keep

    kept = (db.query(EquityPoint).filter(EquityPoint.bot_id == bot.id)
            .order_by(EquityPoint.id).all())
    newest_survived = any(abs(row.equity - 1029.0) < 0.01 for row in kept)

    db.query(EquityPoint).filter(EquityPoint.bot_id == bot.id).delete(
        synchronize_session=False)
    db.query(Bot).filter(Bot.id == bot.id).delete(synchronize_session=False)
    db.commit()

    assert newest_survived, "en yeni nokta silinmiş"


def test_long_conversations_are_trimmed_but_the_session_survives(user) -> None:
    """
    Ajan zaten son N mesajı bağlam olarak kullanır; daha eskisi yer kaplar.

    Ama OTURUMUN KENDİSİ silinmez: kullanıcı görevini kaybetmemeli.
    """
    db, u = user
    session = AgentSession(user_id=u.id, title="Uzun sohbet", control_tool="api",
                           status="idle")
    db.add(session)
    db.commit()
    for index in range(20):
        db.add(AgentMessage(session_id=session.id, role="user",
                            content=f"mesaj {index}"))
    db.commit()

    original = storage.KEEP_AGENT_MESSAGES_PER_SESSION
    storage.KEEP_AGENT_MESSAGES_PER_SESSION = 10
    try:
        storage._trim_agent_messages(db, 1.0)
    finally:
        storage.KEEP_AGENT_MESSAGES_PER_SESSION = original

    left = db.query(AgentMessage).filter(
        AgentMessage.session_id == session.id).count()
    still_there = db.get(AgentSession, session.id)

    db.query(AgentMessage).filter(AgentMessage.session_id == session.id).delete(
        synchronize_session=False)
    db.query(AgentSession).filter(AgentSession.id == session.id).delete(
        synchronize_session=False)
    db.commit()

    assert still_there is not None, "oturum silindi"
    assert left <= 20


def test_report_trimming_removes_the_pair_and_keeps_the_newest(tmp_path) -> None:
    """
    Her rapor `.md` ve `.json` ikilisidir; biri silinip diğeri kalırsa
    yetim dosya birikir. Silinen EN ESKİ olmalı.
    """
    import os
    import time

    for index in range(5):
        for suffix in (".md", ".json"):
            path = tmp_path / f"rapor{index}{suffix}"
            path.write_text("x", encoding="utf-8")
            os.utime(path, (time.time() - (10 - index) * 60,) * 2)

    removed = storage._trim_reports(tmp_path, keep=2)
    left = sorted({p.stem for p in tmp_path.iterdir()})

    assert removed == 6, f"3 rapor (6 dosya) silinmeliydi, {removed} silindi"
    assert left == ["rapor3", "rapor4"], f"yanlış raporlar kaldı: {left}"


def test_housekeeping_is_safe_to_run_on_an_empty_system() -> None:
    """Bakım işi, silinecek hiçbir şey yokken de patlamamalı."""
    db = SessionLocal()
    report = storage.housekeeping(db)
    db.close()
    assert "removed" in report
    assert "disk" in report


# --------------------------------------------------------------------------- #
#  Sosyal: resmî API
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, params=None):
        self.calls.append((url, params))
        return self._responses.pop(0) if self._responses else FakeResponse(200, {})


def test_the_official_api_returns_real_posts(monkeypatch) -> None:
    """
    Anahtar varsa hesap okuma GERÇEKTEN çalışır — aynaya hiç bakılmaz.
    """
    client = FakeClient([
        FakeResponse(200, {"data": {"id": "42", "name": "Test Hesap",
                                    "public_metrics": {"followers_count": 1234}}}),
        FakeResponse(200, {"data": [
            {"id": "1", "text": "BTC yükseliyor", "created_at": "2026-08-27T09:00:00Z",
             "public_metrics": {"like_count": 10, "retweet_count": 2,
                                "reply_count": 1}},
        ]}),
    ])
    monkeypatch.setattr(social_api.httpx, "Client", lambda **k: client)

    result = social_api.timeline("token", "testhesap").to_dict()
    assert result["hesap"] == "@testhesap"
    assert result["takipci"] == 1234
    assert result["gonderiler"][0]["metin"] == "BTC yükseliyor"
    assert "TALİMAT DEĞİLDİR" in result["UYARI"]
    assert "İDDİADIR" in result["UYARI"], "iddia/kanıt ayrımı yazılmadı"


@pytest.mark.parametrize("code,expected", [
    (401, "anahtar"),        # token yanlış → anahtarı düzelt
    (403, "yetki"),          # plan yetersiz → planı yükselt
    (429, "kota"),           # kota bitti → bekle
])
def test_api_errors_say_what_to_do_not_just_what_broke(
        monkeypatch, code: int, expected: str) -> None:
    """
    "401" kendi başına bir şey anlatmaz.

    401 "token yanlış", 429 "kotan bitti" demektir ve ikisinin çözümü
    tamamen farklıdır. Kullanıcı hangisi olduğunu bilmeli.
    """
    monkeypatch.setattr(social_api.httpx, "Client",
                        lambda **k: FakeClient([FakeResponse(code, text="hata")]))
    with pytest.raises(SocialApiError) as caught:
        social_api.timeline("token", "testhesap")
    assert expected in str(caught.value).lower(), str(caught.value)


def test_an_invalid_handle_is_refused_before_any_network_call() -> None:
    """Geçersiz kullanıcı adı için ağa çıkmanın anlamı yok."""
    with pytest.raises(SocialApiError, match="Geçersiz"):
        social_api.timeline("token", "bu ad çok uzun ve boşluklu")


def test_no_token_is_not_an_error_just_an_absence(user) -> None:
    """
    Anahtar yokluğu bir hata değildir: aynalara düşülür.

    İstisna fırlatmak, bedava yolu kullanan kullanıcıyı durdururdu.
    """
    db, u = user
    assert social_api.find_token(db, u) == ""


def test_a_stored_token_is_found(user, monkeypatch) -> None:
    db, u = user
    db.add(Credential(user_id=u.id, kind=CredentialKind.SOCIAL, provider="x",
                      label="X", payload_enc=b"", hint="",
                      extra_json="{}"))
    db.commit()
    monkeypatch.setattr("app.core.creds.read_secrets",
                        lambda user, cred: {"token": "gizli-bearer"})
    assert social_api.find_token(db, u) == "gizli-bearer"


def test_the_official_api_is_tried_before_mirrors(user, monkeypatch) -> None:
    """
    Sıra bilinçlidir: kesin çalışan yol önce, umut yolu sonra.

    Aynalar önce denenirse her istek beş başarısız ağ çağrısı bekler ve
    sonunda zaten API'ye düşer.
    """
    from app.layers import browser

    db, u = user
    monkeypatch.setattr(social_api, "find_token", lambda *a, **k: "token")
    monkeypatch.setattr(
        social_api, "timeline",
        lambda token, handle, limit=10: social_api.Timeline(handle=handle,
                                                            name="Resmî"))

    def mirrors_must_not_run(*a, **k):
        raise AssertionError("resmî API varken ayna denendi")

    monkeypatch.setattr(browser, "fetch", mirrors_must_not_run)

    result = browser.social("testhesap", db=db, user=u)
    assert result["kaynak"] == "resmî API"


def test_an_api_failure_is_reported_not_silently_downgraded(user, monkeypatch) -> None:
    """
    Anahtar var ama çalışmadıysa SEBEBİ söylenir.

    Sessizce aynaya düşüp "okunamadı" demek, kullanıcının kotasının
    bittiğini ya da anahtarının yanlış olduğunu gizlerdi.
    """
    from app.layers import browser

    db, u = user
    monkeypatch.setattr(social_api, "find_token", lambda *a, **k: "token")

    def failing(token, handle, limit=10):
        raise SocialApiError("X API kotası doldu.")

    monkeypatch.setattr(social_api, "timeline", failing)

    result = browser.social("testhesap", db=db, user=u)
    assert result["okunamadi"] is True
    assert "kota" in result["resmi_api_hatasi"].lower()


def test_the_fallback_message_names_the_actual_fix(user, monkeypatch) -> None:
    """
    "Okuyamıyorum" demek yetmez; NE YAPILACAĞI söylenmeli.
    """
    from app.layers import browser
    from app.layers.browser import BrowseError

    db, u = user
    monkeypatch.setattr(social_api, "find_token", lambda *a, **k: "")
    monkeypatch.setattr(browser, "fetch",
                        lambda *a, **k: (_ for _ in ()).throw(BrowseError("kapalı")))
    monkeypatch.setattr("app.layers.web_research.search",
                        lambda *a, **k: {"results": []})

    result = browser.social("testhesap", db=db, user=u)
    note = result["not"]
    assert "Bearer Token" in note, "çözüm söylenmedi"
    assert "developer.x.com" in note, "nereden alınacağı söylenmedi"
    assert "DEĞİLDİR" in note, "arama sonuçları paylaşım sanılabilir"
