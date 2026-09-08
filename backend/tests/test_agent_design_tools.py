"""
AJANIN BOT TASARIM ARAÇLARI

`tools_design.py` %13 kapsamla duruyordu — 210 satır, ve ajanın kullanıcı
adına SİSTEM KURDUĞU yer burası.

Korunan denge, becerilerdekiyle aynı:

    "Yapay zeka bot tasarlayabilir" ile
    "Yapay zeka uydurma strateji çalıştıramaz" aynı anda doğru olmalı.

Ek olarak bir dürüstlük kuralı: tasarlanan her sistem ZAYIF YANINI yazmak
zorundadır. Zayıf yanı olmayan bir sistem, zayıf yanı bilinmeyen bir
sistemdir.
"""
from __future__ import annotations

import json

import pytest

from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import CustomPlaybook, User


@pytest.fixture()
def ctx():
    from app.agent.tools import ToolContext

    db = SessionLocal()
    user = db.query(User).filter(User.email == "design@zumvia.com").first()
    if user is None:
        user = User(email="design@zumvia.com",
                    password_hash=hash_password("designtest12345"),
                    vault_salt=new_salt())
        db.add(user)
        db.commit()
        db.refresh(user)
    db.query(CustomPlaybook).filter(CustomPlaybook.user_id == user.id).delete(
        synchronize_session=False)
    db.commit()
    yield ToolContext(db=db, user=user, session_id=None)
    db.query(CustomPlaybook).filter(CustomPlaybook.user_id == user.id).delete(
        synchronize_session=False)
    db.commit()
    db.close()


def call(ctx, name: str, **args):
    from app.agent.tools import execute_tool

    return execute_tool(ctx, name, args)


def make_bot(ctx, **overrides) -> CustomPlaybook:
    """Doğrudan veritabanına bir özel bot koyar (geri test beklemeden)."""
    row = CustomPlaybook(
        user_id=ctx.user.id, slug=overrides.get("slug", "test_sistem"),
        label=overrides.get("label", "Test sistemi"),
        thesis="Trend güçlüyken geri çekilmelerde alım yapar.",
        weakness="Yatay piyasada sık yanlış sinyal üretir.",
        avoid_when="ADX 20'nin altındayken kullanılmamalı.",
        strategies_json=json.dumps(overrides.get(
            "strategies", ["trend_following", "pullback_ema"])),
        min_agree=overrides.get("min_agree", 2),
        timeframe="4h", risk_pct=0.7, validated=overrides.get("validated", True),
    )
    ctx.db.add(row)
    ctx.db.commit()
    ctx.db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
#  Uydurma strateji çalıştırılamaz
# --------------------------------------------------------------------------- #

def test_an_invented_strategy_is_refused(ctx) -> None:
    """
    Model strateji adı uyduramaz.

    "momentum_ninja" diye bir strateji yoksa, onu içeren bir sistem
    kaydedilirse her turda sessizce hiçbir şey yapmaz — ya da beklenmedik
    davranır. Kayıt anında reddedilir.
    """
    result = call(ctx, "design_custom_bot",
                  label="Uydurma", market="crypto", symbol="BTC/USDT",
                  timeframe="4h", validate_on="BTC/USDT",
                  strategies=["momentum_ninja", "trend_following"],
                  thesis="Bu tez yeterince uzun bir açıklamadır.",
                  weakness="Zayıf yanı yatay piyasada kaybetmesidir.",
                  avoid_when="Hacim düşükken kullanılmamalıdır.",
                  reason="Denemek için tasarlandı ve gerekçesi budur.")

    assert result.get("created") is False
    assert "UYDURAMAZSIN" in result["error"]
    assert result.get("gecerli_stratejiler"), "geçerli seçenekler gösterilmedi"


def test_a_single_strategy_system_is_refused(ctx) -> None:
    """
    Tek stratejiye güvenmek sistemi kırılgan yapar.

    O strateji rejim değiştirdiğinde sistemin başka dayanağı kalmaz.
    """
    result = call(ctx, "design_custom_bot",
                  label="Tekli", market="crypto", symbol="BTC/USDT",
                  timeframe="4h", validate_on="BTC/USDT",
                  strategies=["trend_following"],
                  thesis="Bu tez yeterince uzun bir açıklamadır.",
                  weakness="Zayıf yanı yatay piyasada kaybetmesidir.",
                  avoid_when="Hacim düşükken kullanılmamalıdır.",
                  reason="Denemek için tasarlandı ve gerekçesi budur.")

    assert result.get("created") is False
    assert "Strateji sayısı" in result["error"]


@pytest.mark.parametrize("missing", ["thesis", "weakness", "avoid_when", "reason"])
def test_honesty_fields_are_mandatory(ctx, missing: str) -> None:
    """
    Zayıf yanı yazılmayan sistem kaydedilmez.

    Zayıf yanı olmayan bir sistem yoktur; yalnızca zayıf yanı bilinmeyen
    sistemler vardır. Kullanıcı neyi satın aldığını bilmeli.
    """
    args = {
        "label": "Eksik", "market": "crypto", "symbol": "BTC/USDT",
        "timeframe": "4h", "validate_on": "BTC/USDT",
        "strategies": ["trend_following", "pullback_ema"],
        "thesis": "Bu tez yeterince uzun bir açıklamadır.",
        "weakness": "Zayıf yanı yatay piyasada kaybetmesidir.",
        "avoid_when": "Hacim düşükken kullanılmamalıdır.",
        "reason": "Denemek için tasarlandı ve gerekçesi budur.",
    }
    args[missing] = "kısa"
    result = call(ctx, "design_custom_bot", **args)

    assert result.get("created") is False
    assert missing in result["error"]


# --------------------------------------------------------------------------- #
#  Listeleme ve düzenleme
# --------------------------------------------------------------------------- #

def test_listing_shows_the_weakness_too(ctx) -> None:
    """
    Liste yalnızca "ne yapar" demez, "nerede kaybeder" de der.

    Zayıf yanı gizleyen bir liste, kullanıcıyı sistemleri güçlü sanmaya
    iter.
    """
    make_bot(ctx)
    result = call(ctx, "list_custom_bots")

    assert result["count"] == 1
    row = result["custom_playbooks"][0]
    assert row["weakness"], "zayıf yan listede yok"
    assert "dogrulandi" in row


def test_an_existing_bot_can_be_improved(ctx) -> None:
    """
    Bir sistem beklendiği gibi çalışmıyorsa sıfırdan yazmak yerine
    DÜZELTİLEBİLMELİ — yoksa aynı hatalar tekrarlanır.
    """
    row = make_bot(ctx)
    result = call(ctx, "update_custom_bot", playbook_id=row.slug,
                  risk_pct=0.4,
                  reason="Geri testte drawdown yüksekti, riski düşürüyorum.")

    assert result.get("updated") is True
    assert "risk_pct" in result["degisen"]
    ctx.db.refresh(row)
    assert row.risk_pct == pytest.approx(0.4)


def test_changing_the_strategies_invalidates_old_evidence(ctx) -> None:
    """
    Eski kanıt YENİ kurulumu bağlamaz.

    Strateji seti değiştiyse geçmiş geri test başka bir sistemin karnesidir;
    onu yeni sisteme mal etmek, kanıt uydurmaktır.
    """
    row = make_bot(ctx, validated=True)
    result = call(ctx, "update_custom_bot", playbook_id=row.slug,
                  strategies=["breakout", "momentum_macd", "vwap_reversion"],
                  reason="Trend stratejileri bu pariteye uymadı, kırılıma geçiyorum.")

    assert result.get("updated") is True
    ctx.db.refresh(row)
    assert row.validated is False, "strateji değişti ama sistem hâlâ doğrulanmış"
    assert "DOĞRULANMAMIŞ" in result["not"]


def test_an_edit_without_a_reason_is_refused(ctx) -> None:
    """
    Gerekçesiz değişiklik, öğrenme değil rastgele denemedir.

    Altı ay sonra "bunu neden değiştirmiştik" sorusunun cevabı olmalı.
    """
    row = make_bot(ctx)
    result = call(ctx, "update_custom_bot", playbook_id=row.slug,
                  risk_pct=0.4, reason="deneme")

    assert result.get("updated") is False
    assert "reason" in result["error"]


def test_editing_someone_elses_bot_is_impossible(ctx) -> None:
    """Kullanıcı yalıtımı burada da geçerlidir."""
    result = call(ctx, "update_custom_bot", playbook_id="baskasinin_botu",
                  risk_pct=0.4, reason="Bu değişikliğin somut bir gerekçesi var.")
    assert result.get("updated") is False
    assert "yok" in result["error"]


def test_an_invented_strategy_is_refused_on_edit_too(ctx) -> None:
    """Düzenleme yolu, tasarım yolundan daha gevşek olamaz."""
    row = make_bot(ctx)
    result = call(ctx, "update_custom_bot", playbook_id=row.slug,
                  strategies=["hayali_strateji", "trend_following"],
                  reason="Yeni bir strateji denemek istiyorum bu yüzden.")

    assert result.get("updated") is False
    assert "UYDURAMAZSIN" in result["error"]


def test_risk_stays_under_the_hard_cap_on_edit(ctx) -> None:
    """
    Sert tavan, düzenleme yoluyla aşılamaz.

    Bir kısıt yalnızca bir yolda uygulanıyorsa kısıt değildir.
    """
    from app.core.config import settings

    row = make_bot(ctx)
    call(ctx, "update_custom_bot", playbook_id=row.slug, risk_pct=99.0,
         reason="Riski yükseltmeyi deniyorum, tavan tutmalı.")
    ctx.db.refresh(row)

    assert row.risk_pct <= settings.hard_max_risk_pct


def test_a_bot_can_be_deleted(ctx) -> None:
    row = make_bot(ctx)
    assert call(ctx, "delete_custom_bot", playbook_id=row.slug)["deleted"] is True
    assert call(ctx, "list_custom_bots")["count"] == 0


def test_deleting_a_missing_bot_says_so(ctx) -> None:
    result = call(ctx, "delete_custom_bot", playbook_id="olmayan")
    assert result["deleted"] is False
    assert "bulunamadı" in result["error"]
