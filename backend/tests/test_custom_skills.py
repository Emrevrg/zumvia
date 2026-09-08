"""
YAPAY ZEKANIN YAZDIĞI BECERİLER

Bu dosyanın koruduğu denge tek cümleyle şudur:

    "Yapay zeka kendi becerisini YAZABİLİR" ile
    "Yapay zeka keyfi kod ÇALIŞTIRAMAZ" aynı anda doğru olmalı.

İkisinden biri bozulursa ürün ya işe yaramaz (yazamıyor) ya da tehlikeli
olur (her şeyi yapabiliyor). Testlerin yarısı birinci yarısını, diğer yarısı
ikincisini korur.
"""
from __future__ import annotations

import json

import pytest

from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.layers import custom_skills
from app.layers.custom_skills import SkillError
from app.models import CustomSkill, User

STEPS = [
    {"tool": "get_market_snapshot",
     "args": {"market": "crypto", "symbol": "{parite}", "timeframe": "4h"},
     "save_as": "foto"},
    {"tool": "run_strategy_engine",
     "args": {"market": "crypto", "symbol": "{parite}", "timeframe": "4h"},
     "save_as": "oy"},
]
INPUTS = [{"name": "parite", "description": "İncelenecek parite", "required": True}]
GOOD = {
    "label": "Hızlı parite kontrolü",
    "description": "Bir paritenin teknik fotoğrafını çeker ve algoritmik oyu alır.",
    "when_used": "Kullanıcı tek bir pariteyi hızlıca sorduğunda; derin analiz "
                 "gerekiyorsa deep_research daha uygundur.",
    "steps": STEPS,
    "inputs": INPUTS,
}


@pytest.fixture()
def user():
    db = SessionLocal()
    row = db.query(User).filter(User.email == "skill@zumvia.com").first()
    if row is None:
        row = User(email="skill@zumvia.com", password_hash=hash_password("skilltest12345"),
                   vault_salt=new_salt())
        db.add(row)
        db.commit()
        db.refresh(row)
    db.query(CustomSkill).filter(CustomSkill.user_id == row.id).delete(
        synchronize_session=False)
    db.commit()
    yield db, row
    db.query(CustomSkill).filter(CustomSkill.user_id == row.id).delete(
        synchronize_session=False)
    db.commit()
    db.close()


# --------------------------------------------------------------------------- #
#  YAZABİLİR
# --------------------------------------------------------------------------- #

def test_a_skill_can_be_written_and_read_back(user) -> None:
    db, u = user
    row = custom_skills.save(db, u, **GOOD, author_model="test/model")

    assert row.slug == "hizli_parite_kontrolu" or row.slug
    described = custom_skills.describe(row)
    assert described["steps"][0]["tool"] == "get_market_snapshot"
    assert described["inputs"][0]["name"] == "parite"
    assert described["author_model"] == "test/model"


def test_a_skill_can_be_improved_without_losing_its_record(user) -> None:
    """
    Sicil, bir beceri hakkındaki EN DEĞERLİ bilgidir.

    Düzenleme sicili silseydi "bu işe yarıyor mu" sorusu her değişiklikten
    sonra sıfırdan sorulurdu — yani hiç cevaplanamazdı.
    """
    db, u = user
    row = custom_skills.save(db, u, **GOOD)
    row.runs, row.failures = 12, 2
    db.commit()

    updated = custom_skills.edit(db, u, row.slug, changes={
        "description": "Bir paritenin fotoğrafını çeker, oyu alır ve haber okur.",
        "steps": [*STEPS, {"tool": "get_news", "args": {"market": "crypto"}}],
    })

    assert updated.version == 2
    assert updated.runs == 12, "sicil silindi"
    assert updated.failures == 2
    assert len(json.loads(updated.steps_json)) == 3


def test_a_skill_records_where_it_came_from(user) -> None:
    """Bir becerinin kökeni kayıtta durmalı: kim yazdı, neyden türedi."""
    db, u = user
    row = custom_skills.save(db, u, **GOOD, author_model="nvidia/nemotron",
                             parent_slug="hazir_beceri")
    assert row.author_model == "nvidia/nemotron"
    assert row.parent_slug == "hazir_beceri"


def test_listing_and_deleting(user) -> None:
    db, u = user
    custom_skills.save(db, u, **GOOD)
    assert len(custom_skills.listing(db, u)) == 1
    assert custom_skills.remove(db, u, custom_skills.listing(db, u)[0].slug)
    assert custom_skills.listing(db, u) == []


# --------------------------------------------------------------------------- #
#  KEYFİ KOD ÇALIŞTIRAMAZ
# --------------------------------------------------------------------------- #

def test_an_invented_tool_is_refused(user) -> None:
    """
    Model araç adı uyduramaz — ve bu KAYIT sırasında reddedilir.

    Bozuk bir beceriyi kaydedip çalışma anında patlatmak, hatayı
    kullanıcının üzerine yıkmaktır.
    """
    db, u = user
    with pytest.raises(SkillError, match="YOK"):
        custom_skills.save(db, u, **{**GOOD, "steps": [
            {"tool": "hepsini_sat_ve_kac", "args": {}}]})


@pytest.mark.parametrize("forbidden", [
    "enable_live_trading",      # gerçek paraya geçiş
    "activate_kill_switch",     # acil fren
    "run_skill",                # özyineleme
])
def test_authority_changing_tools_cannot_be_buried_in_a_skill(user, forbidden) -> None:
    """
    Bu kararları bir becerinin içine gömmek, kullanıcıdan gizlemektir.

    Kullanıcı "şu beceriyi çalıştır" dediğinde gerçek paraya geçmiş
    olmamalı.
    """
    db, u = user
    with pytest.raises(SkillError, match="gömülemez"):
        custom_skills.save(db, u, **{**GOOD, "steps": [
            {"tool": forbidden, "args": {}}]})


def test_a_forward_reference_is_refused(user) -> None:
    """
    Tanımlanmamış bir çıktıya atıf, çalışma anında anlaşılmaz bir hataya
    dönüşür. Kayıt anında yakalanmalı.
    """
    db, u = user
    with pytest.raises(SkillError, match="böyle bir ad yok"):
        custom_skills.save(db, u, **{**GOOD, "inputs": [], "steps": [
            {"tool": "get_news", "args": {"market": "{henuz_yok}"}}]})


def test_duplicate_output_names_are_refused(user) -> None:
    """Aynı ad iki kez kullanılırsa ilk çıktı sessizce kaybolur."""
    db, u = user
    with pytest.raises(SkillError, match="zaten kullanılıyor"):
        custom_skills.save(db, u, **{**GOOD, "steps": [
            {"tool": "get_portfolio", "args": {}, "save_as": "x"},
            {"tool": "system_health", "args": {}, "save_as": "x"},
        ]})


def test_chain_length_is_bounded(user) -> None:
    """
    Uzun zincir hem pahalıdır hem de hatanın nerede olduğunu gizler.

    Bir beceri tariftir; program değil.
    """
    db, u = user
    long_chain = [{"tool": "system_health", "args": {}}] * (custom_skills.MAX_STEPS + 1)
    with pytest.raises(SkillError, match="En fazla"):
        custom_skills.save(db, u, **{**GOOD, "steps": long_chain})


def test_honesty_fields_are_mandatory(user) -> None:
    """
    "Ne yapar" ve "ne zaman kullanılır" yazılmadan beceri kaydedilmez.

    Adı kendini açıklıyor sanmak, altı ay sonra kimsenin ne olduğunu
    bilmediği on beceriyle sonuçlanır.
    """
    db, u = user
    with pytest.raises(SkillError, match="description"):
        custom_skills.save(db, u, **{**GOOD, "description": "kısa"})
    with pytest.raises(SkillError, match="when_used"):
        custom_skills.save(db, u, **{**GOOD, "when_used": "bazen"})


def test_saving_twice_does_not_overwrite_silently(user) -> None:
    db, u = user
    custom_skills.save(db, u, **GOOD)
    with pytest.raises(SkillError, match="zaten var"):
        custom_skills.save(db, u, **GOOD)


# --------------------------------------------------------------------------- #
#  Yürütme
# --------------------------------------------------------------------------- #

def test_references_are_resolved_by_type_not_by_string() -> None:
    """
    `"{oy}"` tek başınaysa NESNE döner; metne gömülüyse metin.

    Ayrım önemlidir: `symbol="{parite}"` bir dize vermeli, `"{n} adet"` ise
    birleştirilmiş metin.
    """
    scope = {"parite": "BTC/USDT", "oy": {"action": "BUY", "score": 0.36},
             "n": 5}
    assert custom_skills.resolve("{parite}", scope) == "BTC/USDT"
    assert custom_skills.resolve("{oy}", scope) == {"action": "BUY", "score": 0.36}
    assert custom_skills.resolve("{oy.action}", scope) == "BUY"
    assert custom_skills.resolve("{n} adet", scope) == "5 adet"
    assert custom_skills.resolve({"a": "{parite}"}, scope) == {"a": "BTC/USDT"}


def test_missing_reference_becomes_none_not_a_crash() -> None:
    assert custom_skills.resolve("{yok.alan}", {}) is None


def test_a_failing_step_stops_the_chain(user, monkeypatch) -> None:
    """
    Bir adım patlarsa sonrakiler ÇALIŞMAMALI.

    Bozuk veriyle devam etmek, yanlış bir sonucu doğru gibi sunmaktır —
    hata vermekten daha kötüdür.
    """
    db, u = user
    row = custom_skills.save(db, u, **{**GOOD, "inputs": [], "steps": [
        {"tool": "get_portfolio", "args": {}, "save_as": "a"},
        {"tool": "system_health", "args": {}, "save_as": "b"},
        {"tool": "get_model_scoreboard", "args": {}, "save_as": "c"},
    ]})

    calls: list[str] = []

    def fake_execute(ctx, name, args):  # noqa: ARG001
        calls.append(name)
        if name == "system_health":
            return {"error": "motor yanıt vermedi"}
        return {"ok": True}

    import app.agent.tools as tools_module
    monkeypatch.setattr(tools_module, "execute_tool", fake_execute)

    result = custom_skills.run(None, db, u, row.slug)

    assert result["ok"] is False
    assert calls == ["get_portfolio", "system_health"], "zincir durmadı"
    assert "2. adım" in result["hata"]
    assert result["tamamlanan_adim"] == 1


def test_a_successful_run_returns_every_step_output(user, monkeypatch) -> None:
    db, u = user
    row = custom_skills.save(db, u, **{**GOOD, "inputs": [], "steps": [
        {"tool": "get_portfolio", "args": {}, "save_as": "portfoy"},
    ]})

    import app.agent.tools as tools_module
    monkeypatch.setattr(tools_module, "execute_tool",
                        lambda ctx, name, args: {"sermaye": 1000})

    result = custom_skills.run(None, db, u, row.slug)
    assert result["ok"] is True
    assert result["cikti"]["portfoy"] == {"sermaye": 1000}


def test_a_required_input_must_be_supplied(user) -> None:
    db, u = user
    row = custom_skills.save(db, u, **GOOD)
    result = custom_skills.run(None, db, u, row.slug, {})
    assert "parite" in result["error"]


def test_run_history_is_recorded(user, monkeypatch) -> None:
    """
    Bir beceri "kaydedildi" diye değil, "çalıştığı ölçüldüğü" için
    güvenilir sayılır.
    """
    db, u = user
    row = custom_skills.save(db, u, **{**GOOD, "inputs": [], "steps": [
        {"tool": "get_portfolio", "args": {}}]})

    import app.agent.tools as tools_module
    monkeypatch.setattr(tools_module, "execute_tool",
                        lambda ctx, name, args: {"ok": True})
    custom_skills.run(None, db, u, row.slug)

    monkeypatch.setattr(tools_module, "execute_tool",
                        lambda ctx, name, args: {"error": "patladı"})
    custom_skills.run(None, db, u, row.slug)

    db.refresh(row)
    assert row.runs == 2
    assert row.failures == 1
    assert "patladı" in row.last_error
    assert custom_skills.describe(row)["basari_orani"] == 0.5


def test_the_namespace_form_models_prefer_is_accepted(user) -> None:
    """
    Modeller girdilere `{inputs.symbol}` diye atıf yapar.

    Canlı denemede model beceriyi kusursuz tasarladı ama bu yazımı
    kullandığı için sekiz kez reddedildi ve pes etti. Sezgisi makuldü:
    şemada girdiler `inputs` adlı bir dizide tanımlanıyor.

    Arayüzü modele uydurmak, modeli arayüze uydurmaktan kolaydır.
    """
    db, u = user
    row = custom_skills.save(db, u, **{
        **GOOD,
        "inputs": [{"name": "market"}, {"name": "parite"}],
        "steps": [{"tool": "get_market_snapshot",
                   "args": {"market": "{inputs.market}", "symbol": "{inputs.parite}",
                            "timeframe": "4h"}}],
    })
    assert row.slug

    scope = {"market": "crypto", "parite": "BTC/USDT"}
    assert custom_skills.resolve("{inputs.parite}", scope) == "BTC/USDT"
    assert custom_skills.resolve("{parite}", scope) == "BTC/USDT"


def test_the_error_message_lists_what_is_available(user) -> None:
    """
    Neyin kullanılabilir olduğunu SÖYLEMEYEN hata mesajı, modeli kör
    denemeye zorlar. Canlı denemede tam olarak bu yaşandı.
    """
    db, u = user
    with pytest.raises(SkillError) as caught:
        custom_skills.save(db, u, **{
            **GOOD,
            "inputs": [{"name": "market"}, {"name": "parite"}],
            "steps": [{"tool": "get_news", "args": {"market": "{yanlis_ad}"}}],
        })
    message = str(caught.value)
    assert "{market}" in message and "{parite}" in message,         "kullanılabilir adlar sayılmadı"
    assert "inputs." in message, "kabul edilen yazım anlatılmadı"
