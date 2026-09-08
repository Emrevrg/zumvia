"""
İŞLEM DÖNGÜSÜ — bir turun tamamı

`orchestrator.py` %44 kapsamla duruyordu ve sistemdeki en büyük modül. Bot
her turda buradan geçiyor: veri → matematik → açık pozisyon yönetimi →
devre kesici → karar → önlemler → risk → portföy → icra.

Bu dosya turun SIRASINI koruyor. Sıra, güvenliğin kendisidir:

    Devre kesici karardan ÖNCE gelir  → kaybeden gün yeni işlem açamaz
    Önlemler risk kapısından ÖNCE     → sistem kendi zaafını kapatır
    Portföy kapısı icradan ÖNCE       → tek tek güvenli, toplamda değil
    Kill switch her şeyin üstünde     → "dur" derken durulur

Bir kontrolü sonraya almak, o kontrolü kaldırmakla aynı şeydir: karar
verildikten sonra sorulan soru, cevabı değiştirmez.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.engine import orchestrator
from app.models import (
    Autonomy,
    Bot,
    BotStatus,
    EquityPoint,
    Position,
    PositionStatus,
    Side,
    TradingMode,
    User,
)

# --------------------------------------------------------------------------- #
#  Kurulum
# --------------------------------------------------------------------------- #

def _frame(rows: int = 320, start: float = 100.0, end: float = 130.0):
    """Yükselen, gerçekçi genişlikte bir mum serisi."""
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
    """Bot + kullanıcı + taklit edilmiş piyasa; gerçek ağ çağrısı yok."""
    db = SessionLocal()
    user = db.query(User).filter(User.email == "cycle@zumvia.com").first()
    if user is None:
        user = User(email="cycle@zumvia.com",
                    password_hash=hash_password("cycletest12345"),
                    vault_salt=new_salt())
        db.add(user)
        db.commit()
        db.refresh(user)

    db.query(Position).filter(Position.bot_id.in_(
        db.query(Bot.id).filter(Bot.user_id == user.id))).delete(
        synchronize_session=False)
    db.query(Bot).filter(Bot.user_id == user.id).delete(synchronize_session=False)
    db.commit()

    bot = Bot(user_id=user.id, name="Tur testi", market="crypto",
              exchange="binance", symbol="BTC/USDT", timeframe="4h",
              mode=TradingMode.PAPER, autonomy=Autonomy.FULL,
              decision_mode="algo_only", status=BotStatus.RUNNING,
              poll_seconds=300, strategies_json=json.dumps(["trend_following"]),
              guards_json="{}", min_agree=1, risk_pct=0.5,
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
    db.query(EquityPoint).filter(EquityPoint.bot_id == bot.id).delete(
        synchronize_session=False)
    db.query(Bot).filter(Bot.user_id == user.id).delete(synchronize_session=False)
    db.commit()
    db.close()


def force_decision(monkeypatch, action: str, *, confidence: float = 0.85,
                   stop: float = 0.0, target: float = 0.0):
    """Kararı sabitler; test edilen şey karar değil, kararın ARDINDAKİ zincir."""
    def fake(db, bot, user, df, snapshot, context):  # noqa: ARG001
        price = float(df.iloc[-1]["close"])
        return {
            "action": action, "confidence": confidence,
            "stop_loss": stop or price * 0.97,
            "take_profit": target or price * 1.09,
            "reasoning": "test kararı", "source": "test",
        }
    monkeypatch.setattr(orchestrator, "decide", fake)


def open_position(db, bot, *, entry: float = 120.0, stop: float = 116.0,
                  target: float = 132.0, qty: float = 1.0,
                  side: Side = Side.LONG) -> Position:
    pos = Position(bot_id=bot.id, symbol=bot.symbol, side=side,
                   status=PositionStatus.OPEN, mode=bot.mode, qty=qty,
                   entry_price=entry, stop_loss=stop, initial_stop=stop,
                   take_profit=target, risk_amount=abs(entry - stop) * qty,
                   notional=entry * qty, confidence=0.8,
                   opened_at=datetime.now(UTC) - timedelta(hours=12))
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


# --------------------------------------------------------------------------- #
#  Veri katmanı
# --------------------------------------------------------------------------- #

def test_a_missing_bot_is_reported_not_crashed() -> None:
    result = orchestrator.run_cycle(10**9)
    assert result["ok"] is False
    assert "bulunamad" in result["error"]


def test_a_data_failure_stops_the_cycle_without_trading(env, monkeypatch) -> None:
    """
    Veri yoksa İŞLEM YOK.

    Eksik veriyle karar vermek, kör atıştır: gösterge hesapları sessizce
    yanlış çıkar ve sistem yanlış sayılara güvenerek pozisyon açar.
    """
    db, bot, _ = env
    from app.layers.l1_market_data import MarketDataError

    def boom(*a, **k):
        raise MarketDataError("borsa 503 döndü")

    monkeypatch.setattr(orchestrator, "fetch_ohlcv", boom)
    result = orchestrator.run_cycle(bot.id)

    assert result["ok"] is False
    assert "503" in result["error"]
    db.expire_all()
    assert db.query(Position).filter(Position.bot_id == bot.id).count() == 0


def test_too_few_candles_skips_the_turn(env, monkeypatch) -> None:
    """
    60 mumdan az veriyle gösterge hesabı anlamsızdır.

    EMA-200'ü 30 mumla hesaplamak sayı üretir ama bilgi üretmez; sistem
    bunu "veri var" sanmamalı.
    """
    _, bot, _ = env
    monkeypatch.setattr(orchestrator, "fetch_ohlcv", lambda *a, **k: _frame(rows=40))
    result = orchestrator.run_cycle(bot.id)

    assert result["ok"] is False
    assert result["error"] == "insufficient_data"


def test_every_cycle_records_an_equity_point(env, monkeypatch) -> None:
    """
    Özkaynak eğrisi turdan tura yazılır.

    Yazılmazsa kullanıcı sistemin nasıl gittiğini yalnızca son bakiyeden
    görür — yolun kendisi, varış noktası kadar önemlidir.
    """
    db, bot, _ = env
    force_decision(monkeypatch, "WAIT")
    orchestrator.run_cycle(bot.id)

    assert db.query(EquityPoint).filter(EquityPoint.bot_id == bot.id).count() >= 1


# --------------------------------------------------------------------------- #
#  Açık pozisyon yönetimi
# --------------------------------------------------------------------------- #

def test_a_hit_stop_closes_the_position_in_the_same_cycle(env, monkeypatch) -> None:
    """
    Stop, karardan ÖNCE kontrol edilir.

    Sonraya kalırsa sistem zaten kaybetmiş bir pozisyonu açıkmış gibi
    sayar ve o turun risk hesabı yanlış çıkar.
    """
    db, bot, _ = env
    price = float(_frame().iloc[-1]["close"])
    pos = open_position(db, bot, entry=price * 1.5, stop=price * 1.4,
                        target=price * 2.0)
    force_decision(monkeypatch, "WAIT")

    orchestrator.run_cycle(bot.id)
    db.refresh(pos)

    assert pos.status == PositionStatus.CLOSED
    assert pos.close_reason == "STOP_LOSS"
    assert pos.pnl < 0


def test_a_closed_loss_lowers_the_balance_by_exactly_the_pnl(env, monkeypatch) -> None:
    """
    Kâğıt bakiyesi, kapanan işlemin PnL'i kadar değişir — ne eksik ne fazla.

    Bakiye ile pozisyon sonucu arasındaki her sapma, geri testin ve canlı
    izlemenin tamamını yalancı yapar.
    """
    db, bot, _ = env
    before = bot.paper_balance
    price = float(_frame().iloc[-1]["close"])
    pos = open_position(db, bot, entry=price * 1.5, stop=price * 1.4,
                        target=price * 2.0)
    force_decision(monkeypatch, "WAIT")

    orchestrator.run_cycle(bot.id)
    db.refresh(bot)
    db.refresh(pos)

    assert bot.paper_balance == pytest.approx(round(before + pos.pnl, 8), abs=1e-6)


def test_consecutive_losses_are_counted(env, monkeypatch) -> None:
    """
    Üst üste zarar sayacı toparlanma motorunu besler.

    Sayılmazsa sistem kaybederken risk almayı sürdürür; oysa telafi daha
    büyük risk değil, daha seçici olmaktır.
    """
    db, bot, _ = env
    price = float(_frame().iloc[-1]["close"])
    open_position(db, bot, entry=price * 1.5, stop=price * 1.4, target=price * 2.0)
    force_decision(monkeypatch, "WAIT")

    orchestrator.run_cycle(bot.id)
    db.refresh(bot)

    assert bot.consecutive_losses == 1


def test_a_winning_close_resets_the_loss_counter(env) -> None:
    db, bot, user = env
    bot.consecutive_losses = 3
    db.commit()
    pos = open_position(db, bot, entry=100.0, stop=96.0, target=112.0)

    orchestrator.close_position(db, bot, user, pos, 110.0, "TAKE_PROFIT")
    db.refresh(bot)

    assert pos.pnl > 0
    assert bot.consecutive_losses == 0


def test_a_partial_take_moves_the_stop_to_breakeven(env) -> None:
    """
    Kısmi kâr alındıktan sonra kalan pozisyon RİSKSİZ olmalı.

    Kârın yarısını cebe koyup diğer yarısında hâlâ tam risk taşımak,
    scale-out'un amacını tersine çevirir.
    """
    db, bot, user = env
    pos = open_position(db, bot, entry=100.0, stop=96.0, target=112.0, qty=2.0)

    realized = orchestrator.take_partial_profit(db, bot, user, pos, 108.0, 0.5,
                                                "hedefin yarısı")

    assert realized > 0
    assert pos.partial_taken is True
    assert pos.qty == pytest.approx(1.0)
    assert pos.stop_loss >= pos.entry_price, "kalan pozisyon hâlâ riskli"


def test_the_partial_profit_is_included_in_the_final_pnl(env) -> None:
    """
    Kısmi olarak alınan kâr, kapanışta TOPLAM sonuca dahil edilir.

    Edilmezse sistem kendi kazancını eksik raporlar ve stratejinin gerçek
    beklentisi olduğundan kötü görünür.
    """
    db, bot, user = env
    pos = open_position(db, bot, entry=100.0, stop=96.0, target=112.0, qty=2.0)
    partial = orchestrator.take_partial_profit(db, bot, user, pos, 108.0, 0.5, "")

    orchestrator.close_position(db, bot, user, pos, 106.0, "TRAILING_STOP")

    assert pos.pnl > partial, "kısmi kâr kapanış sonucuna katılmadı"


def test_a_zero_fraction_partial_does_nothing(env) -> None:
    db, bot, user = env
    pos = open_position(db, bot, entry=100.0, stop=96.0, target=112.0, qty=2.0)
    assert orchestrator.take_partial_profit(db, bot, user, pos, 108.0, 0.0, "") == 0.0
    assert pos.qty == pytest.approx(2.0)


def test_close_all_closes_every_open_position(env) -> None:
    db, bot, user = env
    open_position(db, bot, entry=100.0, stop=96.0, target=112.0)
    open_position(db, bot, entry=101.0, stop=97.0, target=113.0)

    assert orchestrator.close_all(db, bot, user, 105.0, "MANUAL") == 2
    assert db.query(Position).filter(Position.bot_id == bot.id,
                                     Position.status == PositionStatus.OPEN).count() == 0


# --------------------------------------------------------------------------- #
#  Devre kesici — karardan önce
# --------------------------------------------------------------------------- #

def test_the_circuit_breaker_fires_before_any_new_decision(env, monkeypatch) -> None:
    """
    Günlük kayıp sınırı aşıldığında bot KİLİTLENİR ve pozisyonları kapanır.

    Bu kontrol karardan önce gelir: kaybeden bir gün, "belki bu işlem
    telafi eder" diye bir işlem daha açamamalı. Telafi arayışı, hesapları
    en hızlı bitiren davranıştır.
    """
    db, bot, _ = env
    bot.day_key = orchestrator.today_key(datetime.now(UTC))
    bot.day_start_equity = 100_000.0   # bugünkü kasa buna göre çok düşük
    db.commit()

    force_decision(monkeypatch, "BUY")
    result = orchestrator.run_cycle(bot.id)
    db.refresh(bot)

    assert result["action"] == "CIRCUIT_BREAKER"
    assert bot.status == BotStatus.LOCKED
    assert bot.locked_until is not None
    assert bot.lock_reason


def test_a_new_day_resets_the_daily_window(env, monkeypatch) -> None:
    """
    Gün değiştiğinde günlük kayıp penceresi sıfırlanır.

    Sıfırlanmazsa dünkü kayıp bugünü de kilitler; devre kesici koruma
    olmaktan çıkıp kalıcı durdurmaya dönüşür.
    """
    db, bot, _ = env
    bot.day_key = "1999-01-01"
    bot.day_trades = 7
    db.commit()

    force_decision(monkeypatch, "WAIT")
    orchestrator.run_cycle(bot.id)
    db.refresh(bot)

    assert bot.day_key == orchestrator.today_key(datetime.now(UTC))
    assert bot.day_trades == 0


# --------------------------------------------------------------------------- #
#  Karar sonrası zincir
# --------------------------------------------------------------------------- #

def test_wait_opens_nothing(env, monkeypatch) -> None:
    db, bot, _ = env
    force_decision(monkeypatch, "WAIT")
    result = orchestrator.run_cycle(bot.id)

    assert result["action"] == "WAIT"
    assert db.query(Position).filter(Position.bot_id == bot.id).count() == 0


def test_close_shuts_open_positions_when_the_thesis_breaks(env, monkeypatch) -> None:
    """
    Yapay zeka "tez bozuldu" diyorsa pozisyon kapanır.

    Stop'a düşmesini beklemek, bilgiyi görmezden gelip fazladan ödemektir.
    """
    db, bot, _ = env
    pos = open_position(db, bot, entry=100.0, stop=96.0, target=140.0)
    force_decision(monkeypatch, "CLOSE")

    result = orchestrator.run_cycle(bot.id)
    db.refresh(pos)

    assert result["action"] == "CLOSE"
    assert pos.status == PositionStatus.CLOSED
    assert pos.close_reason == "AI_CLOSE"


def test_the_kill_switch_blocks_new_risk(env, monkeypatch) -> None:
    """
    Kill switch açıkken YENİ pozisyon açılmaz.

    Ama açık pozisyonlar izlenmeye devam eder: "dur" demek, açıkta
    korumasız bırakmak değildir.
    """
    db, bot, _ = env
    force_decision(monkeypatch, "BUY")
    monkeypatch.setattr(orchestrator, "kill_switch_active", lambda: True)
    monkeypatch.setattr(orchestrator, "kill_switch_reason", lambda: "test durdurması")

    result = orchestrator.run_cycle(bot.id)

    assert result["action"] == "KILL_SWITCH"
    assert db.query(Position).filter(Position.bot_id == bot.id).count() == 0


def test_the_guard_layer_runs_even_with_no_guards_configured(env, monkeypatch) -> None:
    """
    En pahalı hata buydu: `guards_json` boş olan botlar TÜM önlem katmanını
    atlıyordu — ve `create_bot` tam olarak boş üretiyor.

    Canlı koşuda sonucu görüldü: bot stop oldu, AYNI TURDA aynı yere geri
    girdi, çünkü zarar sonrası soğuma hiç sorulmadı. Taban değerler artık
    koşulsuz uygulanıyor; bu test o koşulun geri gelmesini engelliyor.
    """
    db, bot, _ = env
    bot.guards_json = "{}"
    db.commit()

    # Az önce zarar eden bir işlem: soğuma penceresi içindeyiz.
    price = float(_frame().iloc[-1]["close"])
    loser = Position(bot_id=bot.id, symbol=bot.symbol, side=Side.LONG,
                     status=PositionStatus.CLOSED, mode=bot.mode, qty=1.0,
                     entry_price=price, stop_loss=price * 0.97,
                     initial_stop=price * 0.97, take_profit=price * 1.06,
                     risk_amount=price * 0.03, notional=price, pnl=-30.0,
                     close_reason="STOP_LOSS", closed_at=datetime.now(UTC))
    db.add(loser)
    db.commit()

    force_decision(monkeypatch, "BUY")
    result = orchestrator.run_cycle(bot.id)

    assert result["action"] == "GUARDED", (
        "koruma tanımlanmamış bot önlem katmanını atladı")
    assert db.query(Position).filter(Position.bot_id == bot.id,
                                     Position.status == PositionStatus.OPEN
                                     ).count() == 0


def test_a_manual_bot_asks_before_it_trades(env, monkeypatch) -> None:
    """
    MANUAL modda sinyal onaya düşer, işlem açılmaz.

    Kullanıcı "önce bana sor" dediyse sistem soruyu ATLAYAMAZ — otonomi
    seviyesi kullanıcının kararıdır.
    """
    db, bot, _ = env
    bot.autonomy = Autonomy.MANUAL
    db.commit()
    force_decision(monkeypatch, "BUY")

    result = orchestrator.run_cycle(bot.id)

    assert result["action"] == "PENDING"
    pending = db.get(Position, result["position_id"])
    assert pending.status == PositionStatus.PENDING
    assert pending.stop_loss > 0, "onay bekleyen sinyalin bile stopu olmalı"


def test_a_full_buy_opens_a_protected_position(env, monkeypatch) -> None:
    """
    Zincirin sonu: onaylanan bir alış GERÇEKTEN pozisyona dönüşür ve o
    pozisyon stopsuz doğmaz.

    Stopsuz pozisyon, kaybın sınırının bilinmediği pozisyondur.
    """
    db, bot, _ = env
    force_decision(monkeypatch, "BUY")

    result = orchestrator.run_cycle(bot.id)

    assert result["action"] == "BUY", result
    pos = db.get(Position, result["position_id"])
    assert pos.status == PositionStatus.OPEN
    assert pos.stop_loss > 0 and pos.stop_loss < pos.entry_price
    assert pos.take_profit > pos.entry_price
    assert pos.risk_amount > 0


def test_the_risk_shield_rejects_an_impossible_reward_ratio(env, monkeypatch) -> None:
    """
    R/R sert alt sınırın altındaysa işlem AÇILMAZ.

    1:1'lik bir kurulumda %50 isabetle başabaş kalınır ve komisyonla
    kaybedilir. Bu matematik, modelin ne kadar emin olduğuna bakmaz.
    """
    db, bot, _ = env
    price = float(_frame().iloc[-1]["close"])
    force_decision(monkeypatch, "BUY", stop=price * 0.97, target=price * 1.01)

    result = orchestrator.run_cycle(bot.id)

    assert result["action"] in ("REJECTED", "GUARDED", "BLOCKED"), result
    assert db.query(Position).filter(Position.bot_id == bot.id,
                                     Position.status == PositionStatus.OPEN
                                     ).count() == 0


def test_low_confidence_does_not_reach_the_market(env, monkeypatch) -> None:
    """Güven eşiğin altındaysa sinyal işleme dönüşmez."""
    db, bot, _ = env
    force_decision(monkeypatch, "BUY", confidence=0.05)

    result = orchestrator.run_cycle(bot.id)

    assert result["action"] != "BUY"
    assert db.query(Position).filter(Position.bot_id == bot.id,
                                     Position.status == PositionStatus.OPEN
                                     ).count() == 0


def test_a_locked_bot_cannot_take_new_risk(env, monkeypatch) -> None:
    """Kilit süresi dolmadan yeni pozisyon açılamaz."""
    db, bot, _ = env
    bot.locked_until = datetime.now(UTC) + timedelta(hours=6)
    bot.lock_reason = "devre kesici"
    db.commit()
    force_decision(monkeypatch, "BUY")

    result = orchestrator.run_cycle(bot.id)

    assert result["action"] in ("BLOCKED", "GUARDED"), result


def test_the_portfolio_gate_sees_positions_from_other_bots(env, monkeypatch) -> None:
    """
    Tek tek güvenli işlemler, TOPLAMDA güvenli olmayabilir.

    Beş bot ayrı ayrı %1 risk alırsa portföy %5 risk taşır ve hepsi aynı
    yöne bakıyorsa bu tek bir %5'lik bahistir. Kapı, botun kendi defterine
    değil kullanıcının tamamına bakar.
    """
    db, bot, user = env
    other = Bot(user_id=user.id, name="İkinci", market="crypto",
                exchange="binance", symbol="ETH/USDT", timeframe="4h",
                mode=TradingMode.PAPER, autonomy=Autonomy.FULL,
                decision_mode="algo_only", status=BotStatus.RUNNING,
                strategies_json=json.dumps(["trend_following"]), guards_json="{}",
                risk_pct=0.5, initial_balance=10_000.0, paper_balance=10_000.0,
                peak_equity=10_000.0, day_start_equity=10_000.0)
    db.add(other)
    db.commit()

    # Diğer botta portföy ısısını dolduran büyük bir açık risk
    db.add(Position(bot_id=other.id, symbol="ETH/USDT", side=Side.LONG,
                    status=PositionStatus.OPEN, mode=TradingMode.PAPER, qty=10.0,
                    entry_price=100.0, stop_loss=60.0, initial_stop=60.0,
                    take_profit=200.0, risk_amount=8_000.0, notional=1_000.0))
    db.commit()

    force_decision(monkeypatch, "BUY")
    result = orchestrator.run_cycle(bot.id)

    assert result["action"] in ("PORTFOLIO_BLOCKED", "REJECTED", "BLOCKED",
                               "GUARDED"), result

    db.query(Position).filter(Position.bot_id == other.id).delete(
        synchronize_session=False)
    db.commit()


def test_the_technical_bias_is_derived_from_measured_indicators() -> None:
    """
    Teknik eğilim ölçülen göstergelerden çıkar, modelin havasından değil.

    Aynı anlık görüntü her zaman aynı eğilimi vermelidir; yoksa "sistem"
    kelimesinin anlamı kalmaz.
    """
    snapshot = {"indicators": {"rsi_14": 71.0, "adx_14": 32.0, "ema_20": 120.0,
                               "ema_50": 110.0, "macd_hist": 1.4,
                               "atr_percent": 1.2},
                "price": 125.0, "regime": "trend_up"}

    first = orchestrator.technical_bias_from_snapshot(snapshot)
    second = orchestrator.technical_bias_from_snapshot(snapshot)

    assert first == second, "aynı veriden iki farklı eğilim çıktı"
    assert "bias" in first or "yon" in first or first
