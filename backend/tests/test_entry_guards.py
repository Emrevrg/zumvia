"""
GİRİŞ KORUMALARI — duplicate order bu dosyada ölür
===================================================
Videolu deneylerde bile adı geçen klasik arıza: AYNI SİNYALİN İKİ KEZ
İŞLEME DÖNÜŞMESİ. İki ayrı yoldan olur:

    1. ARDIŞIK TEKRAR — model her tur BUY der, her tur yeni pozisyon açılır.
       Aynı bara ait sinyal yankısı piramit değildir; DUPLICATE koduyla durur.
    2. EŞZAMANLI ÇİFT TUR — zamanlanmış tur + elle çalıştırma + ajan çağrısı
       üst üste biner; iki tur da "açık pozisyon yok" görür. Bot kilidi
       ikinci girişi durdurur (ENTRY_IN_PROGRESS) ya da kilit ardından
       tekrar-sinyal filtresi yakalar.

Kilitlenen kurallar:

    * Taze aynı-yön pozisyon/onay varken aynı yöne giriş YOKTUR.
    * Eski pozisyon + yeni sinyal tekrardan sayılmaz (zaman aşımı).
    * Ters yön filtrenin dışındadır (filtre, korunma/hedge iddiasına karışmaz).
    * Ret her zaman kodludur: DUPLICATE / DUPLICATE_SIGNAL / ENTRY_IN_PROGRESS.
"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.agent.tools import ToolContext, execute_tool
from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.engine import orchestrator
from app.models import (
    Autonomy,
    Bot,
    BotStatus,
    Position,
    PositionStatus,
    Side,
    TradingMode,
    User,
)


def _frame(rows: int = 320, start: float = 100.0, end: float = 130.0):
    close = np.linspace(start, end, rows)
    noise = np.sin(np.arange(rows) / 7.0) * (start * 0.004)
    close = close + noise
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=rows, freq="4h"),
        "open": close, "high": close * 1.006, "low": close * 0.994,
        "close": close, "volume": np.full(rows, 5_000_000.0),
    })


@pytest.fixture()
def env(monkeypatch):
    """Üç slotlu bot: filtreyi slot yokluğundan ayırt edebilmek için."""
    db = SessionLocal()
    user = db.query(User).filter(User.email == "dupguard@zumvia.com").first()
    if user is None:
        user = User(email="dupguard@zumvia.com",
                    password_hash=hash_password("dupguard12345"),
                    vault_salt=new_salt())
        db.add(user)
        db.commit()
        db.refresh(user)

    db.query(Position).filter(Position.bot_id.in_(
        db.query(Bot.id).filter(Bot.user_id == user.id))).delete(
        synchronize_session=False)
    db.query(Bot).filter(Bot.user_id == user.id).delete(synchronize_session=False)
    db.commit()

    bot = Bot(user_id=user.id, name="Tekrar Kalkanı", market="crypto",
              exchange="binance", symbol="BTC/USDT", timeframe="4h",
              mode=TradingMode.PAPER, autonomy=Autonomy.FULL,
              decision_mode="algo_only", status=BotStatus.RUNNING,
              poll_seconds=300, strategies_json=json.dumps(["trend_following"]),
              guards_json="{}", min_agree=1, risk_pct=0.5,
              max_open_positions=3,
              initial_balance=10_000.0, paper_balance=10_000.0,
              peak_equity=10_000.0, day_start_equity=10_000.0)
    db.add(bot)
    db.commit()
    db.refresh(bot)

    monkeypatch.setattr(orchestrator, "fetch_ohlcv", lambda *a, **k: _frame())
    monkeypatch.setattr(orchestrator, "fetch_order_book_depth",
                        lambda *a, **k: {"available": True, "spread_pct": 0.02})
    monkeypatch.setattr(orchestrator, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "push_state", lambda *a, **k: None)

    yield db, bot, user

    db.query(Position).filter(Position.bot_id == bot.id).delete(
        synchronize_session=False)
    db.query(Bot).filter(Bot.user_id == user.id).delete(
        synchronize_session=False)
    db.commit()
    db.close()


def buy_sinyali(monkeypatch, action: str = "BUY"):
    def fake(db, bot, user, df, snapshot, context):  # noqa: ARG001
        price = float(df.iloc[-1]["close"])
        return {"action": action, "confidence": 0.85,
                "stop_loss": price * 0.97, "take_profit": price * 1.09,
                "reasoning": "test kararı", "source": "test"}
    monkeypatch.setattr(orchestrator, "decide", fake)


def acik_sayisi(db, bot) -> int:
    db.expire_all()
    return db.query(Position).filter(
        Position.bot_id == bot.id,
        Position.status.in_([PositionStatus.OPEN, PositionStatus.PENDING])).count()


# --------------------------------------------------------------------------- #
#  Ardışık tekrar
# --------------------------------------------------------------------------- #

def test_ust_uste_ayni_sinyal_ikinci_pozisyonu_acmaz(env, monkeypatch) -> None:
    """
    KRİTİK: model her tur BUY dese bile ikinci tur pozisyon AÇMAZ.

    Slot boş (3 slot, 1 dolu) — durduran şey slot yokluğu değil, filtrenin
    ta kendisidir. Dönen kod DUPLICATE olur.
    """
    db, bot, _ = env
    buy_sinyali(monkeypatch, "BUY")

    ilk = orchestrator.run_cycle(bot.id)
    assert ilk["action"] == "BUY", ilk
    assert acik_sayisi(db, bot) == 1

    ikinci = orchestrator.run_cycle(bot.id)
    assert ikinci["action"] == "DUPLICATE", ikinci
    assert ikinci.get("reason")
    assert acik_sayisi(db, bot) == 1, "tekrar sinyal ikinci pozisyonu açtı"


def test_eski_pozisyon_yankıdan_sayilmaz_portfoy_karar_verir(env, monkeypatch) -> None:
    """
    2 saat önceki pozisyon yankı DEĞİLDİR: filtre susar, söz portföy
    kapısınındır (aynı sembol/aynı yön küme kuralına takılır).

    İki katmanın teşhisi farklıdır ve farklı kod döner: DUPLICATE (taze
    yankı) vs PORTFOLIO_BLOCKED (aynı bahsin büyütülmesi). Test, katmanların
    birbirine karışmadığını kilitler.
    """
    db, bot, _ = env
    price = float(_frame().iloc[-1]["close"])
    yasli = Position(bot_id=bot.id, symbol=bot.symbol, side=Side.LONG,
                     status=PositionStatus.OPEN, mode=bot.mode, qty=1.0,
                     entry_price=price, stop_loss=price * 0.97,
                     initial_stop=price * 0.97, take_profit=price * 1.09,
                     risk_amount=price * 0.03, notional=price, confidence=0.8,
                     opened_at=datetime.now(UTC) - timedelta(hours=2))
    db.add(yasli)
    db.commit()

    # Filtre hamlesi doğrudan ölçülür: yaşlı pozisyon yankı sayılmaz.
    engel, _ = orchestrator.duplicate_signal_block(
        db, bot, "BUY", datetime.now(UTC))
    assert engel is False

    buy_sinyali(monkeypatch, "BUY")
    sonuc = orchestrator.run_cycle(bot.id)
    assert sonuc["action"] == "PORTFOLIO_BLOCKED", sonuc
    assert acik_sayisi(db, bot) == 1


def test_ters_yon_filtrenin_disindadir(env, monkeypatch) -> None:
    """Taze LONG varken SELL sinyali filtreye takılmaz (korunma ayrı konu)."""
    db, bot, _ = env
    bot.allow_short = True
    db.commit()
    price = float(_frame().iloc[-1]["close"])
    uzun = Position(bot_id=bot.id, symbol=bot.symbol, side=Side.LONG,
                    status=PositionStatus.OPEN, mode=bot.mode, qty=1.0,
                    entry_price=price, stop_loss=price * 0.97,
                    initial_stop=price * 0.97, take_profit=price * 1.09,
                    risk_amount=price * 0.03, notional=price, confidence=0.8,
                    opened_at=datetime.now(UTC))
    db.add(uzun)
    db.commit()

    buy_sinyali(monkeypatch, "SELL")
    # Filtre hamlesi doğrudan ölçülür: ters yön engellenmez.
    engel, _ = orchestrator.duplicate_signal_block(
        db, bot, "SELL", datetime.now(UTC))
    assert engel is False


def test_zaman_damgasiz_satir_kanit_sayilmaz(env) -> None:
    """Açılış zamanı okunamayan satır, yokluğu cezalandırmaz."""
    db, bot, _ = env
    engel, _ = orchestrator.duplicate_signal_block(db, bot, "bambaşka", None)
    assert engel is False


# --------------------------------------------------------------------------- #
#  Eşzamanlı çift tur
# --------------------------------------------------------------------------- #

def test_es_zamanli_cift_tur_tek_pozisyon_acar(env, monkeypatch) -> None:
    """
    İki tur aynı anda koşarsa toplam BİR pozisyon açılır.

    Biri kapıdan (DUPLICATE), diğeri kilitten (ENTRY_IN_PROGRESS) ya da
    kapıdan döner — ikisi de emir iletmez değil, BİRİ iletir.
    """
    buy_sinyali(monkeypatch, "BUY")
    _, bot, _ = env
    bot_id = bot.id
    sonuclar: list[dict] = []
    kilit = threading.Lock()

    def tur():
        sonuc = orchestrator.run_cycle(bot_id)
        with kilit:
            sonuclar.append(sonuc)

    isciler = [threading.Thread(target=tur) for _ in range(2)]
    for isci in isciler:
        isci.start()
    for isci in isciler:
        isci.join(timeout=120)

    assert len(sonuclar) == 2
    db = SessionLocal()
    try:
        adet = db.query(Position).filter(
            Position.bot_id == bot_id,
            Position.status.in_([PositionStatus.OPEN, PositionStatus.PENDING])
        ).count()
    finally:
        db.close()
    assert adet == 1, f"eşzamanlı turlar {adet} pozisyon açtı: {sonuclar}"
    eylemler = {s.get("action") for s in sonuclar}
    assert "BUY" in eylemler


def test_kilitli_bota_ikinci_giris_kapida_durur(env) -> None:
    """Kilit eldeyken `execute_entry` emir iletmez, kodlu olay yazar."""
    from app.engine.orchestrator import _entry_lock

    db, bot, user = env
    kilit = _entry_lock(bot.id)
    assert kilit.acquire(blocking=False) is True
    try:
        sonuc = orchestrator.execute_entry(
            db, bot, user, broker=None,
            order=type("O", (), {"side": "long", "qty": 1.0, "entry": 100.0})(),
            decision={}, snapshot={})
        assert sonuc is None
    finally:
        kilit.release()
    assert acik_sayisi(db, bot) == 0


# --------------------------------------------------------------------------- #
#  Ajan yolu (MCP dahil aynı kapı)
# --------------------------------------------------------------------------- #

def test_ajan_ikinci_ayni_emi_gerekceyle_reddedilir(env) -> None:
    """Ajan/MCP yolundan üst üste aynı emir: ilki açılır, ikincisi kodla durur."""
    db, _, user = env
    ctx = ToolContext(db=db, user=user)

    # Demo piyasa: kotasyon deterministik ve çevrimdışıdır.
    created = execute_tool(ctx, "create_bot", {
        "name": "Ajan Kalkanı", "market": "demo", "symbol": "BTC/USDT",
        "timeframe": "1h", "decision_mode": "algo_only",
    })
    bot_id = created["bot_id"]
    # Üç slot: durduran şey slot yokluğu değil, filtrenin ta kendisi olsun.
    execute_tool(ctx, "update_bot", {"bot_id": bot_id, "max_open_positions": 3,
                                     "reason": "kalkan testi"})
    quote = execute_tool(ctx, "get_quote", {"market": "demo",
                                            "symbol": "BTC/USDT"})
    price = quote["price"]

    emir = {"bot_id": bot_id, "action": "BUY",
            "stop_loss": price * 0.98, "take_profit": price * 1.06,
            "confidence": 0.85, "reasoning": "kalkan testi"}
    ilk = execute_tool(ctx, "open_position", emir)
    assert ilk["opened"] is True, ilk

    ikinci = execute_tool(ctx, "open_position", emir)
    assert ikinci["opened"] is False
    assert ikinci["blocked_by"] == "duplicate_signal"
    assert ikinci["code"] == "DUPLICATE_SIGNAL"

    db.expire_all()
    adet = db.query(Position).filter(
        Position.bot_id == bot_id,
        Position.status.in_([PositionStatus.OPEN, PositionStatus.PENDING])).count()
    assert adet == 1


def test_manuel_bot_ust_uste_onaya_dusmez(env, monkeypatch) -> None:
    """MANUAL modda ilk sinyal onaya düşer, yankısı düşmez."""
    from app.models import Autonomy

    db, bot, _ = env
    bot.autonomy = Autonomy.MANUAL
    db.commit()
    buy_sinyali(monkeypatch, "BUY")

    ilk = orchestrator.run_cycle(bot.id)
    assert ilk["action"] == "PENDING", ilk
    ikinci = orchestrator.run_cycle(bot.id)
    assert ikinci["action"] == "DUPLICATE", ikinci
    assert acik_sayisi(db, bot) == 1


# --------------------------------------------------------------------------- #
#  Kayıt defteri sağlamlığı (MCP/plugin/skill yolu)
# --------------------------------------------------------------------------- #

def test_mcp_yolu_ic_hatada_bile_sozluk_dondurur(env, monkeypatch) -> None:
    """
    MCP istemcisi çökmüş bir araca da denk gelse YANIT ALIR.

    `execute_tool` (MCP sunucusunun da çağırdığı kapı) istisna sızdırmaz:
    iç hata → `error` taşıyan sözlük. Burada her aracın veri kaynağı
    bilerek patlatılır ve kapının söz verdiği sözleşme ölçülür.
    """
    import app.agent.tools_finance as tiny
    import app.layers.macro as makro

    _, _, user = env
    db = SessionLocal()
    try:
        ctx = ToolContext(db=db, user=user)

        def patlayan(*a, **k):
            raise RuntimeError("iç arıza")

        monkeypatch.setattr(tiny.hub, "agent_context", patlayan)
        monkeypatch.setattr(makro, "regime", patlayan)

        for ad, arg in (("market_pulse", {}), ("get_macro_regime", {}),
                        ("get_fundamentals", {}),
                        ("convert_currency", {"amount": "yüz", "base": "USD",
                                              "quote": "TRY"})):
            sonuc = execute_tool(ctx, ad, arg)
            assert isinstance(sonuc, dict), ad
            assert "error" in sonuc, f"{ad} iç hatayı sözlüğe çeviremedi"

        saglik = execute_tool(ctx, "system_health", {"beklenmedik": object()})
        assert "checked_at" in saglik

        bilinmeyen = execute_tool(ctx, "kesinlikle_yok_bu_arac", {})
        assert "error" in bilinmeyen
    finally:
        db.close()
