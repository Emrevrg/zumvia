"""
SERMAYE HARİTASI — "param şu an tam olarak nerede?"

Bu dosyanın koruduğu tek ayrım, aracın en çok yanlış anlaşılan yeri:

    PİYASADA olan para DALGALANIR.
    RİSKTE olan para KAYBEDİLİR.

"Piyasada 8.000 dolarım var" cümlesi korkutucudur ve kullanıcıyı iyi bir
pozisyonu panikle kapatmaya iter. "Riskte 240 dolarım var" cümlesi
gerçektir. İkisini tek sayıda birleştiren bir ekran, kullanıcıya yanlış
kararı verdirir.

İkinci koruma: "ölçülemedi" ile "sıfır" asla karıştırılmaz. Fiyatı
alınamayan bir pozisyonu sıfır değerlemek, kullanıcıya parasının
buharlaştığını gösterir.
"""
from __future__ import annotations

import json

import pytest

from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.layers.treasury import snapshot
from app.models import Autonomy, Bot, BotStatus, Position, PositionStatus, Side, TradingMode, User


@pytest.fixture()
def env(monkeypatch):
    db = SessionLocal()
    user = db.query(User).filter(User.email == "treasury@zumvia.com").first()
    if user is None:
        user = User(email="treasury@zumvia.com",
                    password_hash=hash_password("treasurytest12345"),
                    vault_salt=new_salt())
        db.add(user)
        db.commit()
        db.refresh(user)

    ids = [b.id for b in db.query(Bot).filter(Bot.user_id == user.id).all()]
    if ids:
        db.query(Position).filter(Position.bot_id.in_(ids)).delete(
            synchronize_session=False)
    db.query(Bot).filter(Bot.user_id == user.id).delete(synchronize_session=False)
    db.commit()

    yield db, user

    ids = [b.id for b in db.query(Bot).filter(Bot.user_id == user.id).all()]
    if ids:
        db.query(Position).filter(Position.bot_id.in_(ids)).delete(
            synchronize_session=False)
    db.query(Bot).filter(Bot.user_id == user.id).delete(synchronize_session=False)
    db.commit()
    db.close()


def make_bot(db, user, *, symbol="BTC/USDT", cash=10_000.0, initial=10_000.0,
             mode=TradingMode.PAPER, market="crypto") -> Bot:
    bot = Bot(user_id=user.id, name=f"Hazine {symbol}", market=market,
              exchange="binance", symbol=symbol, timeframe="4h",
              mode=mode, autonomy=Autonomy.FULL, decision_mode="algo_only",
              status=BotStatus.RUNNING,
              strategies_json=json.dumps(["trend_following"]), guards_json="{}",
              risk_pct=0.5, initial_balance=initial, paper_balance=cash,
              peak_equity=initial, day_start_equity=initial)
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return bot


def open_position(db, bot, *, entry=100.0, stop=96.0, qty=10.0,
                  side=Side.LONG) -> Position:
    pos = Position(bot_id=bot.id, symbol=bot.symbol, side=side,
                   status=PositionStatus.OPEN, mode=bot.mode, qty=qty,
                   entry_price=entry, stop_loss=stop, initial_stop=stop,
                   take_profit=entry * 1.12,
                   risk_amount=abs(entry - stop) * qty, notional=entry * qty,
                   confidence=0.8)
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


def stub_price(monkeypatch, price):
    """Anlık fiyatı sabitler; `None` = ölçülemedi."""
    from app.layers import finance_hub

    class Result:
        def __init__(self, p):
            self.price = p
            self.ok = p is not None
            self.error = "" if p is not None else "borsa yanıt vermedi"

    monkeypatch.setattr(finance_hub, "tick", lambda inst, spark=True: Result(price))


# --------------------------------------------------------------------------- #
#  Temel ayrım
# --------------------------------------------------------------------------- #

def test_market_value_and_risk_are_reported_separately(env, monkeypatch) -> None:
    """
    ARACIN EN ÇOK YANLIŞ ANLAŞILAN YERİ.

    10 lot × 100 = 1.000 birim piyasada. Ama stop 96'da: her şey ters
    giderse kaybedilecek olan 40 birim. Bu ikisini tek sayıda birleştiren
    bir ekran, kullanıcıya iyi bir pozisyonu panikle kapattırır.
    """
    db, user = env
    bot = make_bot(db, user, cash=9_000.0)
    open_position(db, bot, entry=100.0, stop=96.0, qty=10.0)
    stub_price(monkeypatch, 100.0)

    data = snapshot(db, user)
    ozet = data["ozet"]

    assert ozet["piyasada"] == pytest.approx(1_000.0)
    assert ozet["riskte"] == pytest.approx(40.0)
    assert ozet["riskte"] < ozet["piyasada"], "risk ile piyasa değeri karışmış"


def test_the_difference_is_explained_in_words(env, monkeypatch) -> None:
    """Sayıyı göstermek yetmez; ne anlama geldiği yazılmalı."""
    db, user = env
    bot = make_bot(db, user)
    open_position(db, bot)
    stub_price(monkeypatch, 100.0)

    note = snapshot(db, user)["dagilim"]["riskteki_tutar"]["aciklama"]
    assert "dalgalan" in note
    assert "kaybedil" in note


def test_free_cash_and_market_never_exceed_the_balance(env, monkeypatch) -> None:
    """
    Aynı para İKİ KEZ sayılamaz.

    ÖLÇÜLDÜ: bu motorda hesap bakiyesi pozisyon AÇILIRKEN düşmez, yalnızca
    kapanışta kâr/zarar kadar değişir. "Boşta" olarak bakiyenin tamamını
    göstermek, pozisyondaki parayı da boşta saymaktı: ekranda "1.000 boşta"
    ve "213 piyasada" yazıyordu — toplamı 1.213, oysa hesapta 1.000 vardı.

    Kullanıcının en temel sorusuna ("param nerede?") yanlış cevap veren bir
    tablo, hiç olmamasından kötüdür.
    """
    db, user = env
    bot = make_bot(db, user, cash=1_000.0, initial=1_000.0)
    open_position(db, bot, entry=100.0, stop=96.0, qty=2.0)   # 200 birim bağlandı
    stub_price(monkeypatch, 100.0)

    ozet = snapshot(db, user)["ozet"]

    assert ozet["piyasada"] == pytest.approx(200.0)
    assert ozet["nakit"] == pytest.approx(800.0), "pozisyondaki para hâlâ boşta sayılıyor"
    assert ozet["nakit"] + ozet["piyasada"] == pytest.approx(1_000.0), \
        "boşta + piyasada, hesap bakiyesini aşıyor"


def test_the_headline_numbers_are_internally_consistent(env, monkeypatch) -> None:
    """Cümledeki sayılar birbirini tutmalı; kullanıcı toplayıp kontrol eder."""
    db, user = env
    bot = make_bot(db, user, cash=1_000.0, initial=1_000.0)
    open_position(db, bot, entry=100.0, stop=96.0, qty=2.0)
    stub_price(monkeypatch, 100.0)

    data = snapshot(db, user)
    ozet = data["ozet"]

    # Özkaynak = bakiye + açık kâr/zarar (fiyat girişle aynıysa bakiyeye eşit)
    assert ozet["guncel_ozkaynak"] == pytest.approx(1_000.0)
    # Riskteki tutar her zaman piyasadakinden küçük olmalı (stop var)
    assert ozet["riskte"] < ozet["piyasada"]
    # Dağılım payları %100 etmeli
    assert sum(s["pay_pct"] for s in data["dagilim"]["durum"]) == pytest.approx(100.0, abs=0.1)


def test_cash_and_market_add_up(env, monkeypatch) -> None:
    db, user = env
    bot = make_bot(db, user, cash=9_000.0)
    open_position(db, bot, entry=100.0, qty=10.0)
    stub_price(monkeypatch, 100.0)

    data = snapshot(db, user)
    slices = data["dagilim"]["durum"]
    total_share = sum(s["pay_pct"] for s in slices)

    assert total_share == pytest.approx(100.0, abs=0.1)


def test_a_position_without_a_stop_counts_as_fully_at_risk(env, monkeypatch) -> None:
    """
    Stop yoksa pozisyonun TAMAMI risktedir.

    Bilinmeyeni küçük göstermek, en tehlikeli varsayımdır: stopsuz bir
    pozisyonun kaybının sınırı yoktur ve haritada küçük görünmesi
    kullanıcıyı yanıltır.
    """
    db, user = env
    bot = make_bot(db, user)
    open_position(db, bot, entry=100.0, stop=0.0, qty=10.0)
    stub_price(monkeypatch, 100.0)

    ozet = snapshot(db, user)["ozet"]
    assert ozet["riskte"] == pytest.approx(ozet["piyasada"])


# --------------------------------------------------------------------------- #
#  Ölçülemeyen fiyat
# --------------------------------------------------------------------------- #

def test_an_unmeasurable_price_is_not_treated_as_zero(env, monkeypatch) -> None:
    """
    "Ölçülemedi" ile "sıfır" asla aynı şey değildir.

    Sıfır değerlemek, kullanıcıya parasının buharlaştığını gösterir —
    borsanın yanıt vermemesi, paranın yok olması demek değildir.
    """
    db, user = env
    bot = make_bot(db, user, cash=9_000.0)
    open_position(db, bot, entry=100.0, qty=10.0)
    stub_price(monkeypatch, None)

    data = snapshot(db, user)

    assert data["ozet"]["piyasada"] == pytest.approx(1_000.0), \
        "fiyat alınamayınca pozisyon sıfır değerlendi"
    assert any("ölçülemedi" in w for w in data["uyarilar"]), \
        "ölçüm eksikliği kullanıcıya söylenmiyor"


def test_a_price_failure_does_not_crash_the_map(env, monkeypatch) -> None:
    db, user = env
    bot = make_bot(db, user)
    open_position(db, bot)

    from app.layers import finance_hub

    def boom(*a, **k):
        raise RuntimeError("ağ yok")

    monkeypatch.setattr(finance_hub, "tick", boom)
    data = snapshot(db, user)
    assert data["ozet"]["piyasada"] > 0


# --------------------------------------------------------------------------- #
#  Dağılım ve uyarılar
# --------------------------------------------------------------------------- #

def test_concentration_in_one_instrument_is_flagged(env, monkeypatch) -> None:
    """
    Tek varlıkta yoğunlaşma, sayı olarak görülmeden fark edilmez.

    Beş bot kurmuş olmak dağıtım yaptığınız anlamına gelmez; hepsi aynı
    pariteye bakıyorsa bu tek bir bahistir.
    """
    db, user = env
    first = make_bot(db, user, symbol="BTC/USDT")
    second = make_bot(db, user, symbol="ETH/USDT")
    open_position(db, first, entry=100.0, qty=90.0)
    open_position(db, second, entry=100.0, qty=10.0)
    stub_price(monkeypatch, 100.0)

    data = snapshot(db, user)
    assert any("tek bir enstrümanda" in w for w in data["uyarilar"])


def test_real_money_is_called_out(env, monkeypatch) -> None:
    """
    Gerçek para SANALDAN AYRI gösterilir.

    İkisini tek toplamda birleştirmek, kullanıcının gerçekten ne kadar
    kaybedebileceğini gizler.
    """
    db, user = env
    bot = make_bot(db, user, mode=TradingMode.LIVE)
    open_position(db, bot, entry=100.0, qty=10.0)
    stub_price(monkeypatch, 100.0)

    data = snapshot(db, user)
    assert any("GERÇEK PARA" in w for w in data["uyarilar"])
    modes = {row["etiket"] for row in data["dagilim"]["moda_gore"]}
    assert "Gerçek para" in modes


def test_the_breakdown_covers_instrument_market_and_bot(env, monkeypatch) -> None:
    db, user = env
    crypto = make_bot(db, user, symbol="BTC/USDT", market="crypto")
    stock = make_bot(db, user, symbol="AAPL", market="stock")
    open_position(db, crypto, entry=100.0, qty=10.0)
    open_position(db, stock, entry=200.0, qty=5.0)
    stub_price(monkeypatch, 100.0)

    data = snapshot(db, user)

    assert len(data["dagilim"]["enstrumana_gore"]) == 2
    assert len(data["dagilim"]["piyasaya_gore"]) == 2
    assert len(data["botlar"]) == 2
    assert data["botlar"][0]["piyasada"] >= data["botlar"][1]["piyasada"], \
        "botlar büyüklüğe göre sıralı değil"


def test_unrealized_pnl_follows_the_live_price(env, monkeypatch) -> None:
    db, user = env
    bot = make_bot(db, user)
    open_position(db, bot, entry=100.0, qty=10.0)
    stub_price(monkeypatch, 110.0)

    ozet = snapshot(db, user)["ozet"]
    assert ozet["acik_kar_zarar"] == pytest.approx(100.0)
    assert ozet["guncel_ozkaynak"] > ozet["nakit"]


def test_a_short_position_gains_when_price_falls(env, monkeypatch) -> None:
    """Yön işaretini karıştırmak, kazancı zarar gösterir."""
    db, user = env
    bot = make_bot(db, user)
    open_position(db, bot, entry=100.0, stop=104.0, qty=10.0, side=Side.SHORT)
    stub_price(monkeypatch, 90.0)

    assert snapshot(db, user)["ozet"]["acik_kar_zarar"] == pytest.approx(100.0)


def test_an_empty_account_says_so_without_dividing_by_zero(env) -> None:
    db, user = env
    data = snapshot(db, user, live_prices=False)

    assert data["ozet"]["toplam_yatirilan"] == 0
    assert data["ozet"]["getiri_pct"] == 0
    assert "Henüz" in data["kullaniciya_soyle"]


def test_the_headline_is_readable_without_the_table(env, monkeypatch) -> None:
    """
    Kullanıcı tabloya bakmadan durumu anlamalı.

    Sekiz botun teknik dökümü, "param nerede?" sorusunun cevabı değildir.
    """
    db, user = env
    bot = make_bot(db, user, cash=9_000.0, initial=10_000.0)
    open_position(db, bot, entry=100.0, stop=96.0, qty=10.0)
    stub_price(monkeypatch, 100.0)

    line = snapshot(db, user)["kullaniciya_soyle"]

    assert "koydunuz" in line
    assert "boşta" in line
    assert "piyasada" in line
    assert "ters giderse" in line
