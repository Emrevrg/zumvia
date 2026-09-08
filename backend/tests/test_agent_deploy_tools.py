"""
KURULUM ARAÇLARI — hazır sistem ve portföy

`tools_playbook.py` (%22) ve `tools_portfolio.py` (%16) kullanıcının parasını
GERÇEKTEN çalışan botlara bağlayan yol. İkisi de tek cümlelik bir istekten
("kriptoda paramı yönet") canlı bir kuruluma geçiyor; bu yüzden buradaki her
varsayılan bir para kararıdır.

Korunan üç şey:

  1. Bot HER ZAMAN sanal modda doğar. Gerçek paraya geçişi yalnızca kullanıcı
     panelden yapar — hiçbir araç kendi kendine canlıya alamaz.
  2. Risk, sert tavanı aşamaz. Playbook 5 yazsa bile tavan neyse o uygulanır.
  3. Kurulum "kuruldu" diyorsa bot GERÇEKTEN çalışıyordur. Eskiden var olmayan
     bir alan set edilip "kuruldu" deniyordu ve bot hiç dönmüyordu.
"""
from __future__ import annotations

import pytest

from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import Bot, BotStatus, TradingMode, User


@pytest.fixture()
def ctx():
    from app.agent.tools import ToolContext

    db = SessionLocal()
    user = db.query(User).filter(User.email == "deploy@zumvia.com").first()
    if user is None:
        user = User(email="deploy@zumvia.com",
                    password_hash=hash_password("deploytest12345"),
                    vault_salt=new_salt())
        db.add(user)
        db.commit()
        db.refresh(user)
    db.query(Bot).filter(Bot.user_id == user.id).delete(synchronize_session=False)
    db.commit()
    yield ToolContext(db=db, user=user, session_id=None)
    db.query(Bot).filter(Bot.user_id == user.id).delete(synchronize_session=False)
    db.commit()
    db.close()


def call(ctx, name: str, **args):
    from app.agent.tools import execute_tool

    return execute_tool(ctx, name, args)


@pytest.fixture(autouse=True)
def no_scheduler(monkeypatch):
    """Test sırasında gerçek zamanlayıcıya iş eklemeyiz."""
    import app.engine.scheduler as sched

    monkeypatch.setattr(sched, "start_bot_job", lambda *a, **k: None)


def _frame(rows: int = 300):
    import numpy as np
    import pandas as pd

    close = np.linspace(100, 130, rows)
    return pd.DataFrame({
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": [1_000_000.0] * rows,
    })


# --------------------------------------------------------------------------- #
#  Hazır sistem kütüphanesi
# --------------------------------------------------------------------------- #

def test_the_library_states_each_weakness(ctx) -> None:
    """
    Her hazır sistem ZAYIF YANINI söylemek zorunda.

    Yalnızca güçlü yanı anlatılan bir sistem, kullanıcıyı onu her koşulda
    çalışır sanmaya iter — asıl para kaybı orada olur.
    """
    result = call(ctx, "list_playbooks")

    assert result["count"] > 0
    for row in result["playbooks"]:
        assert row.get("weakness"), f"{row['id']} zayıf yanını söylemiyor"
        assert row.get("avoid_when"), f"{row['id']} kaçınma koşulu yok"


def test_tag_filter_is_turkish_safe(ctx) -> None:
    """
    "İ" büyük harfi Türkçede tuzaktır: "İleri".lower() beklenen "ileri"yi
    vermez. Filtre bunu bilmezse kullanıcı boş liste görür.
    """
    everything = call(ctx, "list_playbooks")["playbooks"]
    tags = {t for row in everything for t in row["tags"]}
    assert tags, "hiç etiket yok"

    for tag in sorted(tags):
        for spelling in (tag, tag.upper(), tag.capitalize()):
            filtered = call(ctx, "list_playbooks", tag=spelling)
            assert filtered["count"] > 0, f"{spelling!r} yazılışıyla kayboldu"


def test_recommendation_is_computed_not_guessed(ctx, monkeypatch) -> None:
    """
    Öneri KODLA hesaplanır: ölçülen rejim ve volatilite girdi olur.

    Modelin "bence trend sistemi uygun" demesi öneri değil tahmindir; aynı
    piyasada aynı sonucu vermesi gerekir.
    """
    import app.layers.l1_market_data as market

    monkeypatch.setattr(market, "fetch_ohlcv", lambda *a, **k: _frame())
    result = call(ctx, "recommend_playbook", market="crypto", symbol="BTC/USDT")

    assert result["oneriler"], "hiç öneri yok"
    assert result["olculen_rejim"], "rejim ölçülmedi"
    assert result["symbol"] == "BTC/USDT"


def test_a_recommendation_survives_missing_data(ctx, monkeypatch) -> None:
    """
    Veri alınamazsa öneri motoru ÇÖKMEZ, ölçemediğini söyler.

    Borsa yanıt vermediği için kullanıcının hiçbir şey görememesi, sistemin
    kırılganlığıdır — piyasanın değil.
    """
    import app.layers.l1_market_data as market

    def boom(*a, **k):
        raise RuntimeError("borsa yanıt vermedi")

    monkeypatch.setattr(market, "fetch_ohlcv", boom)
    result = call(ctx, "recommend_playbook", market="crypto", symbol="BTC/USDT")

    assert result["oneriler"], "veri yokken öneri hiç üretilmedi"
    assert result["olculen_rejim"] is None


# --------------------------------------------------------------------------- #
#  Kurulum
# --------------------------------------------------------------------------- #

def test_a_deployed_bot_is_born_in_paper_mode(ctx) -> None:
    """
    Gerçek paraya geçiş YALNIZCA kullanıcının panelden verdiği yetkiyle olur.

    Bir aracın kendi kendine canlı moda kurması, bu sistemdeki en ağır
    ihlaldir; bot doğduğu anda sanal olmak zorundadır.
    """
    from app.layers.playbooks import PLAYBOOKS

    pb_id = next(iter(PLAYBOOKS))
    result = call(ctx, "deploy_playbook", playbook_id=pb_id,
                  symbol="BTC/USDT", market="crypto", initial_balance=1000)

    assert result["deployed"] is True
    bot = ctx.db.get(Bot, result["bot_id"])
    assert bot.mode == TradingMode.PAPER


def test_a_deployed_bot_actually_runs(ctx) -> None:
    """
    "Kuruldu" demek, "çalışıyor" demektir.

    Bir zamanlar yalnızca Bot üzerinde var olmayan bir alan set ediliyordu:
    araç başarı bildiriyor, bot hiç dönmüyordu. En sinsi hata sınıfı budur —
    kullanıcı sistemin çalıştığını sanarak bekler.
    """
    from app.layers.playbooks import PLAYBOOKS

    result = call(ctx, "deploy_playbook", playbook_id=next(iter(PLAYBOOKS)),
                  symbol="BTC/USDT", market="crypto", start=True)
    bot = ctx.db.get(Bot, result["bot_id"])

    assert bot.status == BotStatus.RUNNING


def test_start_false_leaves_the_bot_stopped(ctx) -> None:
    from app.layers.playbooks import PLAYBOOKS

    result = call(ctx, "deploy_playbook", playbook_id=next(iter(PLAYBOOKS)),
                  symbol="BTC/USDT", market="crypto", start=False)
    assert ctx.db.get(Bot, result["bot_id"]).status != BotStatus.RUNNING


def test_risk_never_exceeds_the_hard_cap(ctx) -> None:
    """Sert tavan, playbook'un kendi değerinden üstündür."""
    from app.core.config import settings
    from app.layers.playbooks import PLAYBOOKS

    for pb_id in PLAYBOOKS:
        result = call(ctx, "deploy_playbook", playbook_id=pb_id,
                      symbol="BTC/USDT", market="crypto")
        if result.get("deployed"):
            bot = ctx.db.get(Bot, result["bot_id"])
            assert bot.risk_pct <= settings.hard_max_risk_pct, pb_id
            assert bot.risk_pct > 0


def test_an_unknown_playbook_is_refused_with_a_way_forward(ctx) -> None:
    """
    Hata mesajı NE YAPACAĞINI söylemeli.

    "Bilinmeyen sistem" tek başına modeli tahmin etmeye iter; hangi aracın
    doğru listeyi verdiğini söylemek döngüyü kapatır.
    """
    result = call(ctx, "deploy_playbook", playbook_id="hayali_sistem",
                  symbol="BTC/USDT", market="crypto")

    assert result["deployed"] is False
    assert "list_playbooks" in result["ipucu"]


def test_the_bot_carries_its_weakness_into_its_notes(ctx) -> None:
    """
    Sistemin zayıf yanı bot kaydına yazılır.

    Altı ay sonra bot kaybettiğinde "bu neden oldu" sorusunun cevabı botun
    kendi notunda durmalı, kütüphanenin derinliğinde değil.
    """
    from app.layers.playbooks import PLAYBOOKS

    pb_id = next(iter(PLAYBOOKS))
    result = call(ctx, "deploy_playbook", playbook_id=pb_id,
                  symbol="BTC/USDT", market="crypto")
    notes = ctx.db.get(Bot, result["bot_id"]).strategy_notes

    assert "Zayıf yanı" in notes
    assert "Kaçın" in notes


def test_without_an_llm_key_the_bot_falls_back_to_pure_algorithm(ctx) -> None:
    """
    Yapay zeka anahtarı yoksa bot durmaz — algoritmayla çalışır.

    "Anahtar yok" bir hata değil, bir koşuldur: sistem elindekiyle en iyisini
    yapar ve kullanıcıya ne yaptığını söyler.
    """
    from app.layers.playbooks import PLAYBOOKS

    for pb_id in PLAYBOOKS:
        result = call(ctx, "deploy_playbook", playbook_id=pb_id,
                      symbol="BTC/USDT", market="crypto")
        if result.get("deployed"):
            bot = ctx.db.get(Bot, result["bot_id"])
            assert bot.decision_mode == "algo_only", pb_id
            assert bot.llm_credential_id is None


# --------------------------------------------------------------------------- #
#  Portföy
# --------------------------------------------------------------------------- #

def _stub_scan(monkeypatch, candidates):
    import app.engine.scanner as scanner
    import app.layers.l1_market_data as market

    monkeypatch.setattr(scanner, "scan_markets",
                        lambda **k: {"candidates": candidates})
    monkeypatch.setattr(market, "fetch_ohlcv", lambda *a, **k: _frame())


def test_zero_capital_is_refused(ctx) -> None:
    result = call(ctx, "deploy_portfolio", market="crypto", capital=0)
    assert result["deployed"] is False
    assert "sermaye" in result["error"].lower()


def test_no_setup_means_no_trade_not_a_forced_one(ctx, monkeypatch) -> None:
    """
    Tarama net kurulum bulamadıysa sistem BEKLER.

    Zorlama işlem, en pahalı işlemdir: kullanıcı "bir şey yap" dediğinde
    kötü bir kurulum kurmak, hiçbir şey yapmamaktan daha kötüdür.
    """
    _stub_scan(monkeypatch, [])
    result = call(ctx, "deploy_portfolio", market="crypto", capital=10_000)

    assert result["deployed"] is False
    assert "bekliyorum" in result["kullaniciya_soyle"].lower()


def test_capital_is_spread_across_instruments(ctx, monkeypatch) -> None:
    """
    Sermaye büyüdükçe tek enstrüman iki şeyi birden kaybettirir: likidite
    tükendiği için giriş fiyatı bozulur ve tüm bahis tek hikâyeye bağlanır.
    """
    _stub_scan(monkeypatch, [
        {"symbol": "BTC/USDT", "score": 0.8, "action": "BUY"},
        {"symbol": "ETH/USDT", "score": 0.7, "action": "BUY"},
        {"symbol": "SOL/USDT", "score": 0.6, "action": "BUY"},
    ])
    result = call(ctx, "deploy_portfolio", market="crypto", capital=200_000,
                  instruments=3)

    assert result["deployed"] is True
    assert result["instruments"] > 1, "büyük sermaye tek enstrümana yığıldı"
    total = sum(b["capital"] for b in result["bots"])
    assert total <= 200_000 + 1e-6, "dağıtılan sermaye toplamı aşıyor"


def test_small_capital_is_not_split_into_dust(ctx, monkeypatch) -> None:
    """
    1.000 birimi beş parçaya bölmek, beş kez komisyon ödemektir.

    Dağıtım bir amaç değil araçtır; küçük sermayede zarar verir.
    """
    _stub_scan(monkeypatch, [
        {"symbol": "BTC/USDT", "score": 0.8, "action": "BUY"},
        {"symbol": "ETH/USDT", "score": 0.7, "action": "BUY"},
    ])
    result = call(ctx, "deploy_portfolio", market="crypto", capital=1_000)

    if result.get("deployed"):
        assert result["instruments"] == 1


def test_portfolio_bots_are_paper_too(ctx, monkeypatch) -> None:
    """Portföy yolu, tekli kurulum yolundan daha gevşek olamaz."""
    _stub_scan(monkeypatch, [{"symbol": "BTC/USDT", "score": 0.8, "action": "BUY"}])
    result = call(ctx, "deploy_portfolio", market="crypto", capital=5_000)

    assert result["deployed"] is True
    for row in result["bots"]:
        assert ctx.db.get(Bot, row["bot_id"]).mode == TradingMode.PAPER


def test_instrument_count_is_bounded(ctx, monkeypatch) -> None:
    """Sınırsız enstrüman, sermayeyi komisyona çevirir."""
    from app.agent.tools_portfolio import MAX_INSTRUMENTS

    _stub_scan(monkeypatch, [{"symbol": f"C{i}/USDT", "score": 0.7, "action": "BUY"}
                             for i in range(30)])
    result = call(ctx, "deploy_portfolio", market="crypto", capital=5_000_000,
                  instruments=99)

    assert result["instruments"] <= MAX_INSTRUMENTS


def test_a_scan_failure_is_reported_not_swallowed(ctx, monkeypatch) -> None:
    import app.engine.scanner as scanner

    def boom(**k):
        raise RuntimeError("borsa 503 döndü")

    monkeypatch.setattr(scanner, "scan_markets", boom)
    result = call(ctx, "deploy_portfolio", market="crypto", capital=10_000)

    assert result["deployed"] is False
    assert "503" in result["error"]


def test_the_portfolio_explains_itself_in_plain_turkish(ctx, monkeypatch) -> None:
    """
    Kullanıcı ne kurulduğunu tabloya bakmadan anlamalı.

    Sekiz botun teknik dökümü, "ne oldu?" sorusunun cevabı değildir.
    """
    _stub_scan(monkeypatch, [
        {"symbol": "BTC/USDT", "score": 0.8, "action": "BUY"},
        {"symbol": "ETH/USDT", "score": 0.7, "action": "BUY"},
    ])
    result = call(ctx, "deploy_portfolio", market="crypto", capital=50_000)

    assert result["deployed"] is True
    for key in ("kullaniciya_soyle", "dagitim_notu", "yurutme_notu", "mode_reason"):
        assert result.get(key), f"{key} boş"
    assert "sanal" in result["kullaniciya_soyle"].lower()
