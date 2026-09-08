"""
ACİL FREN VE ÇALIŞMA MODLARI

İkisi de aynı soruyu cevaplıyor: sistem kimi koruyor?

ACİL FREN eskiden yalnızca yeni emirleri reddediyordu. Açık pozisyonlar
piyasada kalıyor, botlar durduğu için stopları da izlenmiyordu. Frenin ne
zaman kalkacağı bilinmediğine göre bu, kullanıcıyı BİLİNMEYEN BİR SÜRE
boyunca yönetilmeyen riskle baş başa bırakmaktı — sistemi korur, kullanıcıyı
korumaz. Artık fren, durmadan önce her şeyi kapatır.

ÇALIŞMA MODLARI aynı fikrin sohbet tarafı: bir modele "bunu yapma" demek bir
dilektir. Sor ve Planla modlarında durumu değiştiren araçlar KODDA kapalıdır.

Bu dosyadaki en önemli test `test_a_brake_that_cannot_close_says_so`: fren
"her şey güvende" deyip bir pozisyonu açıkta bırakırsa, hiç fren çekmemekten
daha tehlikelidir — çünkü kullanıcı artık bakmaz.
"""
from __future__ import annotations

import json

import pytest

from app.core import emergency
from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.safety import KILL_SWITCH_FILE, kill_switch_active
from app.core.security import hash_password
from app.models import (
    Autonomy,
    Bot,
    BotStatus,
    Position,
    PositionStatus,
    Side,
    TradingMode,
    User,
    WorkingOrder,
)


@pytest.fixture()
def env(monkeypatch):
    db = SessionLocal()
    user = db.query(User).filter(User.email == "brake@zumvia.com").first()
    if user is None:
        user = User(email="brake@zumvia.com",
                    password_hash=hash_password("braketest12345"),
                    vault_salt=new_salt())
        db.add(user)
        db.commit()
        db.refresh(user)

    ids = [b.id for b in db.query(Bot).filter(Bot.user_id == user.id).all()]
    if ids:
        db.query(Position).filter(Position.bot_id.in_(ids)).delete(
            synchronize_session=False)
        db.query(WorkingOrder).filter(WorkingOrder.bot_id.in_(ids)).delete(
            synchronize_session=False)
    db.query(Bot).filter(Bot.user_id == user.id).delete(synchronize_session=False)
    db.commit()

    import app.engine.scheduler as sched
    monkeypatch.setattr(sched, "stop_bot_job", lambda *a, **k: None)
    monkeypatch.setattr(sched, "start_bot_job", lambda *a, **k: None)
    monkeypatch.setattr(emergency, "_notify", lambda *a, **k: None)

    KILL_SWITCH_FILE.unlink(missing_ok=True)
    yield db, user

    KILL_SWITCH_FILE.unlink(missing_ok=True)
    ids = [b.id for b in db.query(Bot).filter(Bot.user_id == user.id).all()]
    if ids:
        db.query(Position).filter(Position.bot_id.in_(ids)).delete(
            synchronize_session=False)
        db.query(WorkingOrder).filter(WorkingOrder.bot_id.in_(ids)).delete(
            synchronize_session=False)
    db.query(Bot).filter(Bot.user_id == user.id).delete(synchronize_session=False)
    db.commit()
    db.close()


def make_bot(db, user, *, symbol="BTC/USDT", running=True) -> Bot:
    bot = Bot(user_id=user.id, name=f"Fren testi {symbol}", market="crypto",
              exchange="binance", symbol=symbol, timeframe="4h",
              mode=TradingMode.PAPER, autonomy=Autonomy.FULL,
              decision_mode="algo_only",
              status=BotStatus.RUNNING if running else BotStatus.STOPPED,
              strategies_json=json.dumps(["trend_following"]), guards_json="{}",
              risk_pct=0.5, initial_balance=10_000.0, paper_balance=10_000.0,
              peak_equity=10_000.0, day_start_equity=10_000.0)
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return bot


def open_position(db, bot, *, entry=100.0, qty=1.0, side=Side.LONG) -> Position:
    pos = Position(bot_id=bot.id, symbol=bot.symbol, side=side,
                   status=PositionStatus.OPEN, mode=bot.mode, qty=qty,
                   entry_price=entry, stop_loss=entry * 0.96,
                   initial_stop=entry * 0.96, take_profit=entry * 1.12,
                   risk_amount=entry * 0.04 * qty, notional=entry * qty,
                   confidence=0.8)
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


def stub_price(monkeypatch, price: float | None):
    """Kapanış fiyatını sabitler; `None` ise borsa yanıt vermiyor demektir."""
    import app.layers.l1_market_data as market

    class Quote:
        def __init__(self, p):
            self.price = p
            self.symbol = "X"
            self.bid = p
            self.ask = p
            self.spread_pct = 0.0

    def fetch(*a, **k):
        if price is None:
            raise RuntimeError("borsa yanıt vermedi")
        return Quote(price)

    monkeypatch.setattr(market, "fetch_quote", fetch)


# --------------------------------------------------------------------------- #
#  Fren gerçekten kapatıyor mu
# --------------------------------------------------------------------------- #

def test_the_brake_closes_every_open_position(env, monkeypatch) -> None:
    """
    Frenin ASIL işi bu.

    "Yeni işlem açmıyoruz" demek, açık olanı korumaz. Frenden sonra sistemin
    ne zaman kalkacağı bilinmez; o süre boyunca stopu izlenmeyen bir pozisyon
    tanımı gereği sınırsız risktir.
    """
    db, user = env
    bot = make_bot(db, user)
    first = open_position(db, bot, entry=100.0)
    second = open_position(db, bot, entry=102.0)
    stub_price(monkeypatch, 105.0)

    result = emergency.engage(db, user, "test")
    db.refresh(first)
    db.refresh(second)

    assert result["engaged"] is True
    assert first.status == PositionStatus.CLOSED
    assert second.status == PositionStatus.CLOSED
    assert first.close_reason == "EMERGENCY_BRAKE"
    assert result["sonuc"]["kapatilan"] == 2
    assert result["sonuc"]["temiz"] is True


def test_the_brake_stops_the_bots(env, monkeypatch) -> None:
    db, user = env
    bot = make_bot(db, user)
    stub_price(monkeypatch, 105.0)

    emergency.engage(db, user, "test")
    db.refresh(bot)

    assert bot.status == BotStatus.STOPPED


def test_the_kill_switch_is_flipped_last(env, monkeypatch) -> None:
    """
    Anahtar EN SONA bırakılır.

    Önce çevrilseydi kapatma emirlerimiz kendi frenimize takılabilirdi —
    riski azaltmak için gönderilen bir emrin, riski durdurmak için konmuş
    bir kapıya çarpması saçmadır.
    """
    db, user = env
    bot = make_bot(db, user)
    open_position(db, bot)
    stub_price(monkeypatch, 105.0)

    # Kapatma ANINDA anahtarın durumunu kaydeder: sıra yanlışsa burada
    # `True` görünür ve test düşer.
    seen: list[bool] = []

    import app.engine.orchestrator as orch
    original = orch.close_position

    def spy(db_, bot_, user_, position, price, reason, broker=None):
        seen.append(kill_switch_active())
        return original(db_, bot_, user_, position, price, reason, broker)

    monkeypatch.setattr(orch, "close_position", spy)
    emergency.engage(db, user, "test")

    assert seen, "hiç pozisyon kapatılmadı"
    assert not any(seen), "anahtar, pozisyonlar kapanmadan önce çevrilmiş"
    assert kill_switch_active() is True


def test_partial_orders_are_cancelled_before_closing(env, monkeypatch) -> None:
    """
    Yarım kalmış büyük emirlerin kalan dilimleri İPTAL edilir.

    Edilmezse bir yandan pozisyon kapatırız, öbür yandan zamanlayıcı yeni
    dilim gönderir; kullanıcı kapattığını sandığı pozisyona geri girer.
    """
    db, user = env
    bot = make_bot(db, user)
    order = WorkingOrder(bot_id=bot.id, symbol=bot.symbol, side=Side.LONG,
                         status="working", total_qty=10.0, filled_qty=3.0,
                         slices=5, slices_done=2, interval_seconds=60,
                         reference_price=100.0)
    db.add(order)
    db.commit()
    stub_price(monkeypatch, 105.0)

    result = emergency.engage(db, user, "test")
    db.refresh(order)

    assert order.status == "cancelled"
    assert result["sonuc"]["iptal_edilen_parcali_emir"] == 1
    assert "ACİL FREN" in order.note


def test_a_brake_that_cannot_close_says_so(env, monkeypatch) -> None:
    """
    EN ÖNEMLİ TEST.

    Borsa yanıt vermiyorsa pozisyon kapatılamaz. Bu durumda "her şey
    güvende" demek, hiç fren çekmemekten DAHA tehlikelidir: kullanıcı
    güvende olduğunu sanıp bakmayı bırakır.

    Uydurma bir fiyatla kapatmış gibi yapmak da yasak — defterdeki sonuç
    gerçekle uyuşmaz ve kullanıcı pozisyonunun kapandığını sanır.
    """
    db, user = env
    bot = make_bot(db, user)
    pos = open_position(db, bot)
    stub_price(monkeypatch, None)          # borsa düşük

    result = emergency.engage(db, user, "test")
    db.refresh(pos)

    assert pos.status == PositionStatus.OPEN, "kapanmadığı hâlde kapalı sayıldı"
    assert result["sonuc"]["temiz"] is False
    assert result["sonuc"]["kapatilamayan"] == 1
    assert "KAPATILAMADI" in result["kullaniciya_soyle"]
    assert "elle" in result["uyari"].lower()


def test_the_brake_is_written_into_history(env, monkeypatch) -> None:
    """
    Fren tarihe geçer.

    Sessizce kaybolan bir fren, olmamış bir frendir: altı ay sonra "o gün
    ne oldu" sorusunun cevabı bir yerde durmalı.
    """
    from app.models import AuditLog

    db, user = env
    bot = make_bot(db, user)
    open_position(db, bot)
    stub_price(monkeypatch, 105.0)

    emergency.engage(db, user, "disk doldu")
    db.commit()

    actions = [r.action for r in db.query(AuditLog)
               .filter(AuditLog.user_id == user.id)
               .order_by(AuditLog.id.desc()).limit(6).all()]

    assert "EMERGENCY_BRAKE_START" in actions, "fren niyeti kaydedilmedi"
    assert "EMERGENCY_BRAKE_DONE" in actions, "fren sonucu kaydedilmedi"


def test_the_intent_is_recorded_even_if_closing_fails(env, monkeypatch) -> None:
    """
    Niyet, sonuçtan ÖNCE yazılır.

    Kapatma adımı çökse bile tarihte "fren çekildi" izi kalmalı; yoksa
    kullanıcı frene bastığını, sistemin ise hiçbir şey görmediğini sanır.
    """
    from app.models import AuditLog

    db, user = env
    bot = make_bot(db, user)
    open_position(db, bot)
    stub_price(monkeypatch, None)

    emergency.engage(db, user, "borsa düştü")
    db.commit()

    actions = [r.action for r in db.query(AuditLog)
               .filter(AuditLog.user_id == user.id).all()]
    assert "EMERGENCY_BRAKE_START" in actions


def test_releasing_the_brake_does_not_restart_bots(env, monkeypatch) -> None:
    """
    Fren kalkınca botlar KENDİLİĞİNDEN başlamaz.

    Fren, bir şey ters gittiği için çekilir. Sebebi anlaşılmadan otomatik
    devam etmek, aynı hataya geri dönmektir.
    """
    db, user = env
    bot = make_bot(db, user)
    stub_price(monkeypatch, 105.0)
    emergency.engage(db, user, "test")

    result = emergency.release(db, user, "sorun giderildi")
    db.refresh(bot)

    assert result["kill_switch"] is False
    assert bot.status == BotStatus.STOPPED, "bot kendiliğinden başladı"


def test_flatten_without_the_brake_leaves_the_system_running(env, monkeypatch) -> None:
    """
    "Her şeyi kapat" ile "acil fren" farklı isteklerdir.

    Kullanıcı riskten çıkmak isteyip sistemi kapatmak istemeyebilir; ikisini
    aynı düğmeye bağlamak, birini isteyeni diğerine mahkûm eder.
    """
    db, user = env
    bot = make_bot(db, user)
    pos = open_position(db, bot)
    stub_price(monkeypatch, 105.0)

    result = emergency.flatten_all(db, user, stop_bots=True)
    db.refresh(pos)

    assert pos.status == PositionStatus.CLOSED
    assert result.clean is True
    assert kill_switch_active() is False, "sadece kapatma istendi, fren çekildi"


def test_a_brake_with_nothing_open_is_still_honest(env, monkeypatch) -> None:
    db, user = env
    make_bot(db, user)
    stub_price(monkeypatch, 105.0)

    result = emergency.engage(db, user, "test")

    assert result["sonuc"]["kapatilan"] == 0
    assert result["sonuc"]["temiz"] is True
    assert "yoktu" in result["kullaniciya_soyle"]


def test_one_failing_position_does_not_block_the_others(env, monkeypatch) -> None:
    """
    Bir pozisyon kapatılamıyorsa diğerleri yine kapatılır.

    Tek bir hatanın tüm freni durdurması, kullanıcıyı kapatılabilecek
    pozisyonlarla birlikte açıkta bırakırdı.
    """
    db, user = env
    good = make_bot(db, user, symbol="BTC/USDT")
    bad = make_bot(db, user, symbol="ZZZ/USDT")
    good_pos = open_position(db, good)
    bad_pos = open_position(db, bad)

    import app.layers.l1_market_data as market

    class Quote:
        def __init__(self, p):
            self.price = p

    def fetch(market_, exchange, symbol):
        if symbol == "ZZZ/USDT":
            raise RuntimeError("bilinmeyen parite")
        return Quote(105.0)

    monkeypatch.setattr(market, "fetch_quote", fetch)

    result = emergency.engage(db, user, "test")
    db.refresh(good_pos)
    db.refresh(bad_pos)

    assert good_pos.status == PositionStatus.CLOSED
    assert bad_pos.status == PositionStatus.OPEN
    assert result["sonuc"]["kapatilan"] == 1
    assert result["sonuc"]["kapatilamayan"] == 1


# --------------------------------------------------------------------------- #
#  Çalışma modları
# --------------------------------------------------------------------------- #

def test_ask_mode_cannot_change_anything() -> None:
    """
    Sor modunda durumu değiştiren araçlar KAPALIDIR.

    Modele "bunu yapma" demek bir dilektir: unutur, yanlış anlar ya da
    kullanıcının cümlesini izin sanır. Kapı kodda olmalı.
    """
    from app.agent.tools import REGISTRY
    from app.agent.work_mode import _ALWAYS_ALLOWED, allowed

    for name, tool in REGISTRY.items():
        ok, why = allowed("ask", name, tool.mutating)
        # Risk AZALTAN araçlar her modda açıktır; listesi modülün kendisinde
        # tutulur, burada elle tekrarlanmaz — tekrarlanırsa listeye eklenen
        # yeni bir araç bu testi yanlış yere düşürür.
        if tool.mutating and name not in _ALWAYS_ALLOWED:
            assert not ok, f"{name} Sor modunda çalışabiliyor"
            assert why, f"{name} reddedildi ama gerekçe verilmedi"


def test_plan_mode_cannot_change_anything_either() -> None:
    from app.agent.tools import REGISTRY
    from app.agent.work_mode import allowed

    blocked = [n for n, t in REGISTRY.items()
               if t.mutating and not allowed("plan", n, True)[0]]
    assert len(blocked) > 5, "Planla modu neredeyse hiçbir şeyi engellemiyor"


def test_agent_mode_can_use_everything() -> None:
    from app.agent.tools import REGISTRY
    from app.agent.work_mode import allowed

    for name, tool in REGISTRY.items():
        assert allowed("agent", name, tool.mutating)[0], f"{name} Uygula'da kapalı"


def test_the_brake_works_in_every_mode() -> None:
    """
    Riski AZALTAN bir araç "yetkin yok" diye reddedilemez.

    Reddedildiği an kullanıcı korumasız kalır — ve bu tam olarak korumanın
    en gerekli olduğu andır.
    """
    from app.agent.tools import REGISTRY
    from app.agent.work_mode import _ALWAYS_ALLOWED, allowed

    for mode in ("ask", "plan", "agent"):
        for name in _ALWAYS_ALLOWED:
            if name in REGISTRY:
                assert allowed(mode, name, True)[0], f"{name} · {mode}"

    # Bu iki araç GERÇEKTEN var olmalı: listede olup kayıtta olmayan bir ad,
    # koruma varmış gibi görünüp hiçbir şey yapmaz.
    assert "activate_kill_switch" in REGISTRY
    assert "emergency_flatten" in REGISTRY


def test_an_unknown_mode_falls_to_the_safest_one() -> None:
    """
    Bilinmeyen değer güvenli tarafa düşer — asla Uygula'ya değil.

    Bozuk bir veri ya da eski bir istemci yüzünden ajanın tam yetkiyle
    uyanması, sessiz ve pahalı bir hata olurdu.
    """
    from app.agent.work_mode import ASK, resolve

    for value in ("", "  ", "uydurma", None, "AGENTT"):
        assert resolve(value).id == ASK
        assert resolve(value).can_mutate is False


def test_only_agent_mode_is_autonomous() -> None:
    """
    Otonomluk moddan TÜRER, ayrı bir düğme değildir.

    Ayrı düğme iki çelişkili durum üretiyordu: Uygula modunda otonomu
    kapatmak (kurduğunu izlemeyen ajan) ve Sor modunda açmak (hiçbir şey
    yapamayan ama sürekli uyanan ajan).
    """
    from app.agent.work_mode import MODES

    assert MODES["agent"].autonomous is True
    assert MODES["ask"].autonomous is False
    assert MODES["plan"].autonomous is False


def test_every_mode_explains_itself() -> None:
    from app.agent.work_mode import MODES, prompt_block

    for mode in MODES.values():
        assert len(mode.hint) > 15, f"{mode.id} açıklaması yetersiz"
        assert len(prompt_block(mode.id)) > 100, f"{mode.id} prompt bloğu zayıf"


def test_the_refusal_tells_the_model_what_to_do_instead() -> None:
    """
    "Yapamazsın" tek başına modeli aynı aracı tekrar denemeye iter.

    Reddin gerekçesi, çıkış yolunu da göstermeli.
    """
    from app.agent.work_mode import allowed

    _, why = allowed("ask", "create_bot", True)
    assert "Uygula" in why
    assert "kendin geçemezsin" in why or "geçmesini iste" in why


def test_every_state_changing_tool_is_marked_as_such() -> None:
    """
    `mutating` işareti, çalışma modu kapısının DAYANDIĞI tek şeydir.

    ÖLÇÜLDÜ: on araç durumu değiştirdiği hâlde işaretlenmemişti — Sor
    modundaki bir ajan beceri yazabiliyor, otomasyon düzenleyebiliyor ve bot
    silebiliyordu. Kapı çalışıyordu; ona hangi araçların kapatılacağı yanlış
    söyleniyordu.

    Bu test adından anlaşılan her eylemi tarar. Yeni bir araç işaret
    unutularak eklenirse burada düşer.
    """
    from app.agent.tools import REGISTRY

    # Adında bu eylemlerden biri geçen araç, sistemde bir şey DEĞİŞTİRİR.
    verbs = ("create_", "update_", "edit_", "delete_", "remove_", "set_",
             "enable_", "disable_", "start_", "stop_", "control_", "deploy_",
             "design_", "activate_", "add_", "fork_", "close_", "open_",
             "send_", "run_skill", "run_automation", "run_bot_cycle")

    missed = [name for name, tool in REGISTRY.items()
              if not tool.mutating and any(v in name for v in verbs)]

    assert not missed, (
        "Durumu değiştirdiği hâlde `mutating=True` işareti olmayan araçlar: "
        + ", ".join(sorted(missed))
        + " — bunlar Sor/Planla modunda ÇALIŞABİLİR.")


def test_the_mode_gate_actually_blocks_the_newly_marked_tools() -> None:
    """İşaretlemek yetmez; kapının o işareti okuduğu da doğrulanmalı."""
    from app.agent.tools import REGISTRY
    from app.agent.work_mode import allowed

    for name in ("create_skill", "edit_skill", "delete_skill", "run_skill",
                 "create_automation", "edit_automation", "run_automation_now",
                 "delete_automation", "update_custom_bot", "delete_custom_bot"):
        assert name in REGISTRY, f"{name} kayıtta yok"
        for mode in ("ask", "plan"):
            ok, _ = allowed(mode, name, REGISTRY[name].mutating)
            assert not ok, f"{name} hâlâ {mode} modunda çalışabiliyor"
        assert allowed("agent", name, True)[0], f"{name} Uygula'da da kapalı"
