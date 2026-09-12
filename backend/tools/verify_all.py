"""
BÜTÜN DENETİM — daha önce sorun çıkaran her şey tek tek sınanır
================================================================

"Artık sorun yok" demek ucuzdur. Bu betik onu KANITLAMAYA çalışır: geliştirme
sırasında gerçekten patlamış olan her arıza, kendi senaryosuyla yeniden
denenir.

Birim testlerden farkı: burada parçalar değil ZİNCİRLER sınanır — gerçek
veritabanı, gerçek koruma katmanı, gerçek karar yolu. Testler "bu fonksiyon
doğru mu" diye sorar; bu betik "sistem hâlâ ayakta mı" diye.

Kullanım:
    python tools/verify_all.py

Her madde GEÇTİ ya da KALDI döner. Kalanlar gizlenmez ve çıkış kodu 1 olur.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    """Bir denetim maddesini çalıştırır ve sonucunu kaydeder."""
    def wrapper(fn):
        try:
            detail = fn() or ""
            RESULTS.append((name, True, str(detail)[:140]))
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc)[:220]))
        except Exception as exc:  # noqa: BLE001
            line = traceback.format_exc().splitlines()[-2].strip()[:90]
            RESULTS.append((name, False, f"{type(exc).__name__}: {exc} | {line}"[:220]))
        return fn
    return wrapper


def _user():
    from app.core.db import SessionLocal
    from app.models import User

    db = SessionLocal()
    row = (db.query(User).order_by(User.id.desc()).first())
    return db, row


# =========================================================================== #
#  1. ÇOKLU GÖREV — "database is locked"
# =========================================================================== #

@check("Çoklu görev: eş zamanlı oturumlar birbirini kilitlemiyor")
def _concurrency():
    from app.agent import controller, run_lock
    from app.core.db import SessionLocal
    from app.layers.l3_llm_gateway import ChatTurn
    from app.models import AgentMessage, AgentSession, User

    run_lock.release_all()
    db, user = _user()
    ids = []
    for index in range(4):
        row = AgentSession(user_id=user.id, title=f"Denetim {index}",
                           control_tool="api", control_model="m", status="idle")
        db.add(row)
        db.commit()
        ids.append(row.id)
    db.close()

    class Slow:
        model, provider_id = "sahte", "sahte"

        def chat(self, messages, tools=None, system="", max_tokens=4000):
            time.sleep(0.35)                       # ağ gecikmesi taklidi
            return ChatTurn(True, text="Ölçümler kararsız, piyasa nötr görünüyor.")

    original = controller._gateway_from
    controller._gateway_from = lambda *a, **k: Slow()
    errors: list[str] = []
    lock = threading.Lock()

    def worker(session_id: int):
        own = SessionLocal()
        try:
            session = own.get(AgentSession, session_id)
            out = controller.run_agent(own, own.get(User, session.user_id),
                                       session, "durum")
            if not out.get("ok"):
                with lock:
                    errors.append(str(out.get("error"))[:120])
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}"[:120])
        finally:
            own.close()

    started = time.time()
    threads = [threading.Thread(target=worker, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40)
    elapsed = time.time() - started
    controller._gateway_from = original

    cleanup = SessionLocal()
    cleanup.query(AgentMessage).filter(AgentMessage.session_id.in_(ids)).delete(
        synchronize_session=False)
    cleanup.query(AgentSession).filter(AgentSession.id.in_(ids)).delete(
        synchronize_session=False)
    cleanup.commit()
    cleanup.close()

    assert not any("locked" in e.lower() for e in errors), f"kilit: {errors}"
    assert not errors, f"hata: {errors}"
    assert elapsed < 1.4, f"paralel değil ({elapsed:.2f} sn)"
    return f"4 oturum {elapsed:.2f} sn'de paralel, 0 hata"


# =========================================================================== #
#  2. DURDURMA
# =========================================================================== #

@check("Durdur düğmesi turu gerçekten kesiyor")
def _stop():
    from app.agent import controller, run_lock
    from app.layers.l3_llm_gateway import ChatTurn
    from app.models import AgentMessage, AgentSession

    run_lock.release_all()
    db, user = _user()
    session = AgentSession(user_id=user.id, title="Durdurma denetimi",
                           control_tool="api", control_model="m", status="idle")
    db.add(session)
    db.commit()
    session_id = session.id
    calls = {"n": 0}

    class Chatty:
        model, provider_id = "sahte", "sahte"

        def chat(self, messages, tools=None, system="", max_tokens=4000):
            calls["n"] += 1
            if calls["n"] == 1:
                run_lock.cancel(session_id)        # kullanıcı Durdur'a bastı
            return ChatTurn(True, text="devam",
                            tool_calls=[{"name": "get_portfolio", "arguments": {}}])

    original = controller._gateway_from
    controller._gateway_from = lambda *a, **k: Chatty()
    try:
        out = controller.run_agent(db, user, session, "uzun görev")
    finally:
        controller._gateway_from = original

    db.query(AgentMessage).filter(AgentMessage.session_id == session_id).delete(
        synchronize_session=False)
    db.query(AgentSession).filter(AgentSession.id == session_id).delete(
        synchronize_session=False)
    db.commit()
    db.close()

    assert out.get("stopped") is True, f"durmadı: {out}"
    assert calls["n"] <= 2, f"durdurma sonrası {calls['n']} çağrı"
    return f"{calls['n']} çağrıda durdu"


# =========================================================================== #
#  3. HIZ SINIRI
# =========================================================================== #

@check("429 hız sınırı görevi öldürmüyor")
def _rate_limit():
    from app.agent import controller, run_lock
    from app.layers.l3_llm_gateway import ChatTurn
    from app.models import AgentMessage, AgentSession

    run_lock.release_all()
    db, user = _user()
    session = AgentSession(user_id=user.id, title="Hiz denetimi", control_tool="api",
                           control_model="m", status="idle")
    db.add(session)
    db.commit()
    session_id = session.id
    calls = {"n": 0}

    class Flaky:
        model, provider_id = "sahte", "sahte"

        def chat(self, messages, tools=None, system="", max_tokens=4000):
            calls["n"] += 1
            if calls["n"] == 1:
                return ChatTurn(False, error="Client error '429 Too Many Requests'")
            return ChatTurn(True, text="BTC ölçümlere göre nötr bölgede duruyor.")

    original_gw = controller._gateway_from
    original_backoff = controller.RATE_BACKOFF
    controller._gateway_from = lambda *a, **k: Flaky()
    controller.RATE_BACKOFF = (0.05, 0.05, 0.05, 0.05)
    try:
        out = controller.run_agent(db, user, session, "BTC?")
    finally:
        controller._gateway_from = original_gw
        controller.RATE_BACKOFF = original_backoff

    db.query(AgentMessage).filter(AgentMessage.session_id == session_id).delete(
        synchronize_session=False)
    db.query(AgentSession).filter(AgentSession.id == session_id).delete(
        synchronize_session=False)
    db.commit()
    db.close()

    assert out.get("ok") is True, f"429 sonrası öldü: {out}"
    assert calls["n"] == 2, f"tekrar denenmedi ({calls['n']})"
    return "bekledi ve tekrar denedi"


# =========================================================================== #
#  4. KORUMA KATMANI
# =========================================================================== #

@check("Koruma katmanı, korumasız botta da çalışıyor")
def _guards():
    import pandas as pd

    from app.layers.guards import check_guards

    frame = pd.DataFrame({
        "open": [100] * 30, "high": [101] * 30, "low": [99] * 30,
        "close": [100] * 30, "volume": [1000] * 30, "adx": [30] * 30,
        "rsi": [55] * 30, "atr_pct": [1.0] * 30, "volume_z": [0.5] * 30,
    })
    verdict = check_guards({}, frame,
                           last_loss_at=datetime.now(UTC) - timedelta(minutes=2),
                           timeframe_minutes=60)
    assert verdict.allowed is False, "korumasız bot zarar sonrası hemen girdi"
    assert verdict.code == "COOLDOWN", verdict.code
    return "boş guards → COOLDOWN devrede"


# =========================================================================== #
#  5. İDDİA DOĞRULAMA
# =========================================================================== #

@check("Doğrulama: yalanı yakalıyor, doğruyu suçlamıyor")
def _verify():
    from app.research import derive
    from app.research.ledger import Ledger
    from app.research.verify import Verdict, audit

    led = Ledger()
    for key, value in [("BTC/USDT.fiyat", 78427.40),
                       ("BTC/USDT.indicators.rsi_14", 56.0),
                       ("BTC/USDT.algo.stop_loss", 76286.74),
                       ("BTC/USDT.algo.take_profit", 83243.92),
                       ("BTC/USDT.algo.confidence", 0.948),
                       ("portfoy.sermaye", 10000.0)]:
        led.add(key, value, "denetim", "olcum")
    derive.enrich(led, {"symbols": ["BTC/USDT"]})

    truths = ["BTC fiyatı 78.427 dolar.", "SL 76.286,7 (fiyattan ~%2,7 aşağı)",
              "Algo güveni %94.8.", "R/R oranı ~2,3.", "RSI 14: 56.0"]
    lies = ["BTC fiyatı 95.000 dolar.", "RSI 28 aşırı satım.",
            "Algo güveni %35 gibi düşük."]
    #  İddia bile sayılmaması gerekenler: eşik seviyesi ve liste numarası.
    noise = ["Aşırı alım sayılmaz (>70).", "Point 1: giriş.",
             "Bu rapor 6 olguya dayanır."]

    for text in truths:
        got = [c.verdict for c in audit(text, led).claims]
        assert got and all(v is Verdict.SUPPORTED for v in got), \
            f"doğru yazım suçlandı: {text} → {got}"
    for text in lies:
        got = [c.verdict for c in audit(text, led).claims]
        assert Verdict.CONTRADICTED in got, f"yalan kaçtı: {text} → {got}"
    for text in noise:
        assert not audit(text, led).claims, f"iddia olmayan şey iddia sayıldı: {text}"
    return f"{len(truths)} doğru, {len(lies)} yalan, {len(noise)} gürültü"


# =========================================================================== #
#  6. MODEL DOĞRULAMA
# =========================================================================== #

@check("Model doğrulama: bozuk model varsayılan seçilmiyor")
def _model_verify():
    from app.core.db import SessionLocal
    from app.layers import model_verify
    from app.models import Credential, CredentialKind

    db = SessionLocal()
    creds = db.query(Credential).filter(Credential.kind == CredentialKind.LLM).all()
    for cred in creds:
        known = model_verify.cached(db, cred.id)
        if not known:
            continue
        broken = [m for m, row in known.items() if not row["ok"]]
        working = [m for m, row in known.items() if row["ok"]]
        best = model_verify.best_model(db, cred.id, list(known))
        db.close()
        assert best not in broken, f"bozuk model seçildi: {best}"
        return f"{cred.provider}: {len(working)}/{len(known)} çalışıyor, seçilen {best}"
    db.close()
    return "henüz doğrulama yapılmamış (atlandı)"


# =========================================================================== #
#  7. YÜRÜTME ZİNCİRİ
# =========================================================================== #

def _trend_fixture_ohlcv(symbol: str = "SOL/USDT", bars: int = 400,
                         drift: float = 0.004):
    """
    Deterministik yükseliş serisi (denetimler için).

    Yürütme denetimi daha önce CANLI borsa verisine bağlıydı: piyasada trend
    yoksa sinyal çıkmıyor, denetim haksız yere KALIYORDU. Bu fixture temiz bir
    yükseliş trendi üretir; EMA50>EMA200, Supertrend yukarı, ADX yüksek olur ve
    `trend_following` her çalıştırmada aynı BUY sinyalini üretir. Risk kalkanı,
    stop mantığı ve icra yolu GERÇEKTİR — yalnızca mum verisi sabittir.
    """
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(7)
    noise = rng.normal(0.0, 0.0005, bars)
    closes = 100.0 * np.exp(np.cumsum(np.full(bars, drift) + noise))
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) * 1.001
    lows = np.minimum(opens, closes) * 0.999
    volumes = np.full(bars, 1000.0)
    index = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=bars,
                          freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows,
         "close": closes, "volume": volumes},
        index=index,
    )


def _trend_fixture_depth(market: str = "", exchange: str = "",
                         symbol: str = "", limit: int = 20) -> dict:
    """Fixture'a eşlik eden dar spreadli emir defteri (ağ yok)."""
    return {"available": True, "best_bid": 100.0, "best_ask": 100.02,
            "bid_volume": 500.0, "ask_volume": 500.0,
            "imbalance_pct": 0.0, "pressure": "DENGELI", "spread_pct": 0.02}


@check("Yürütme: pozisyon açılıyor, korumalar geçiyor, stop kapatıyor")
def _execution():
    from app.core.config import settings
    from app.engine import orchestrator
    from app.engine.orchestrator import run_cycle
    from app.models import (
        Autonomy,
        Bot,
        BotEvent,
        BotStatus,
        EquityPoint,
        Position,
        PositionStatus,
        TradingMode,
        WorkingOrder,
    )

    db, user = _user()
    balance = 10_000.0
    # NOT: listedeki isimler GEÇERLİ strateji kimlikleri olmalı. Daha önce
    # burada "pullback", "vwap_revert", "macd_cross" yazıyordu; motor bilinmeyen
    # isimleri sessizce eler, geriye tek strateji kalıyordu. Tek strateji +
    # canlı veri = piyasaya bağlı kırılgan denetim. Geçerli tek strateji ve
    # deterministik fixture ile denetim her koşuda aynı şeyi sınar.
    bot = Bot(user_id=user.id, name="Denetim yurutme", market="crypto",
              exchange="binance", symbol="SOL/USDT", timeframe="1h",
              mode=TradingMode.PAPER, autonomy=Autonomy.FULL,
              decision_mode="algo_only", status=BotStatus.RUNNING,
              strategies_json=json.dumps(["trend_following"]),
              guards_json=json.dumps({"adx_min": 0, "cooldown_bars": 0}),
              initial_balance=balance, paper_balance=balance,
              peak_equity=balance, day_start_equity=balance,
              risk_pct=1.0, min_agree=1)
    db.add(bot)
    db.commit()
    bot_id = bot.id

    # Deterministik veri: canlı borsaya çıkılmaz (ağ yok, her koşuda aynı).
    real_fetch = orchestrator.fetch_ohlcv
    real_depth = orchestrator.fetch_order_book_depth
    fixture = _trend_fixture_ohlcv()

    def canned(*a, **k):
        limit = k.get("limit", a[4] if len(a) > 4 else 320)
        return fixture.tail(max(60, min(int(limit), len(fixture)))).copy()

    orchestrator.fetch_ohlcv = canned
    orchestrator.fetch_order_book_depth = _trend_fixture_depth
    try:
        opened = None
        for _ in range(6):
            run_cycle(bot_id)
            db.expire_all()
            opened = (db.query(Position)
                      .filter(Position.bot_id == bot_id,
                              Position.status == PositionStatus.OPEN).first())
            if opened:
                break
        assert opened is not None, "fixture trende rağmen pozisyon açılmadı"

        entry, stop = opened.entry_price, opened.stop_loss
        risk = opened.risk_amount or abs(entry - stop) * opened.qty
        reward = abs((opened.take_profit or entry) - entry) * opened.qty
        assert stop and stop > 0, "stop yok"
        assert stop < entry, "stop yanlış tarafta"
        assert risk / balance * 100 <= settings.hard_max_risk_pct + 0.01, "risk tavanı aşıldı"
        assert reward / risk >= settings.hard_min_rr_ratio - 0.01, "R/R yetersiz"

        real_fetch = canned
        below = stop * 0.995

        def dipped(*a, **k):
            frame = canned(*a, **k).copy()
            frame.iloc[-1, frame.columns.get_loc("low")] = below
            frame.iloc[-1, frame.columns.get_loc("close")] = below
            return frame

        orchestrator.fetch_ohlcv = dipped
        try:
            run_cycle(bot_id)
        finally:
            orchestrator.fetch_ohlcv = canned

        db.expire_all()
        closed = (db.query(Position)
                  .filter(Position.bot_id == bot_id,
                          Position.status == PositionStatus.CLOSED).all())
        assert closed, "stop tetiklenmedi"
        loss = abs(closed[-1].pnl or 0)
        assert loss <= risk * 1.2, f"kayıp riski aştı: {loss:.2f} > {risk:.2f}"
        return (f"risk %{risk / balance * 100:.2f}, R/R {reward / risk:.2f}, "
                f"stop kaybı {loss:.2f}")
    finally:
        orchestrator.fetch_ohlcv = real_fetch
        orchestrator.fetch_order_book_depth = real_depth
        for model in (Position, WorkingOrder, BotEvent, EquityPoint):
            db.query(model).filter(model.bot_id == bot_id).delete(
                synchronize_session=False)
        db.query(Bot).filter(Bot.id == bot_id).delete(synchronize_session=False)
        db.commit()
        db.close()


# =========================================================================== #
#  8. GEZİNTİ GÜVENLİĞİ
# =========================================================================== #

@check("Gezinti: iç ağ adresleri reddediliyor")
def _browse_safety():
    from app.layers.browser import BrowseError, safe_url

    for bad in ("http://localhost:8000/api/keys", "http://127.0.0.1/",
                "http://169.254.169.254/"):
        try:
            safe_url(bad)
        except BrowseError:
            continue
        raise AssertionError(f"iç ağ adresi geçti: {bad}")
    assert safe_url("example.com").startswith("https://")
    return "3 iç adres reddedildi, dış adres geçti"


# =========================================================================== #
#  9. BECERİ YAZMA
# =========================================================================== #

@check("Beceri: yazılabiliyor ama yasak/uydurma araç gömülemiyor")
def _skills():
    from app.layers import custom_skills
    from app.layers.custom_skills import SkillError
    from app.models import CustomSkill

    db, user = _user()
    db.query(CustomSkill).filter(CustomSkill.user_id == user.id,
                                 CustomSkill.slug.like("denetim%")).delete(
        synchronize_session=False)
    db.commit()

    good = {
        "label": "Denetim becerisi",
        "description": "Bir paritenin teknik fotoğrafını çeker ve raporlar.",
        "when_used": "Hızlı bir bakış gerektiğinde; derin analiz için değil.",
        "steps": [{"tool": "get_market_snapshot",
                   "args": {"market": "crypto", "symbol": "{inputs.parite}",
                            "timeframe": "4h"}}],
        "inputs": [{"name": "parite"}],
    }
    custom_skills.save(db, user, **good)

    blocked = 0
    for bad_tool in ("enable_live_trading", "activate_kill_switch",
                     "run_skill", "hepsini_sat_ve_kac"):
        try:
            custom_skills.save(db, user, **{**good, "label": f"denetim {bad_tool}",
                                            "steps": [{"tool": bad_tool, "args": {}}]})
        except SkillError:
            blocked += 1

    db.query(CustomSkill).filter(CustomSkill.user_id == user.id,
                                 CustomSkill.slug.like("denetim%")).delete(
        synchronize_session=False)
    db.commit()
    db.close()

    assert blocked == 4, f"yasak araç geçti ({blocked}/4 engellendi)"
    return "yazıldı; 3 yasak + 1 uydurma araç engellendi"


# =========================================================================== #
#  10. OTOMASYON
# =========================================================================== #

@check("Otomasyon: kuruluyor, sınırları uygulanıyor")
def _automations():
    from app.layers import automations as auto
    from app.layers.automations import AutomationError
    from app.models import CustomAutomation

    db, user = _user()
    db.query(CustomAutomation).filter(
        CustomAutomation.user_id == user.id,
        CustomAutomation.slug.like("denetim%")).delete(synchronize_session=False)
    db.commit()

    row = auto.save(db, user, label="Denetim otomasyonu",
                    purpose="Denetim için kurulmuş, portföyü kontrol eden otomasyon.",
                    action="task", task_prompt="Portföyü kontrol et ve özetle.",
                    schedule="daily", at_hour=9)
    assert row.next_run_at is not None, "zamanlanmadı"

    blocked = 0
    try:
        auto.save(db, user, label="Denetim cok sik",
                  purpose="Çok sık çalışan otomasyon kotayı boşuna tüketir.",
                  action="task", task_prompt="Fiyata bak ve söyle.",
                  schedule="interval", every_minutes=1)
    except AutomationError:
        blocked += 1
    try:
        auto.save(db, user, label="Denetim olmayan beceri",
                  purpose="Var olmayan bir beceriye bağlı otomasyon reddedilmeli.",
                  action="skill", skill_slug="hic_olmayan")
    except AutomationError:
        blocked += 1

    db.query(CustomAutomation).filter(
        CustomAutomation.user_id == user.id,
        CustomAutomation.slug.like("denetim%")).delete(synchronize_session=False)
    db.commit()
    db.close()

    assert blocked == 2, f"sınırlar uygulanmadı ({blocked}/2)"
    return "kuruldu; aşırı sıklık ve olmayan beceri reddedildi"


# =========================================================================== #
#  11. SIR SIZINTISI
# =========================================================================== #

@check("Anahtarlar hiçbir API yanıtında görünmüyor")
def _secrets():
    from fastapi.testclient import TestClient

    from app.core.security import create_access_token
    from app.main import app

    db, user = _user()
    token = create_access_token(user.id, user.email)
    db.close()

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    leaked = []
    for path in ("/api/keys", "/api/portfolio/overview", "/api/safety/status",
                 "/api/workshop/summary", "/api/bots"):
        body = client.get(path, headers=headers).text
        for marker in ("nvapi-", "sk-or-v1", "sk-proj", "sk-ant"):
            if marker in body:
                leaked.append(f"{path}:{marker}")
    assert not leaked, f"sızıntı: {leaked}"
    return "5 uç denetlendi, sızıntı yok"


# =========================================================================== #
#  12. UYUM DÖNGÜSÜ
# =========================================================================== #

@check("Uyum: yönergeye uymayan çıktı düzeltiliyor")
def _compliance():
    from app.research import compliance
    from app.research.pipeline import strip_thinking

    thinking = ("Wait, the prompt says: write 4 items.\n"
                "I need to output them in Turkish.\n"
                "The user wants bullet points.\n"
                "- Yön: yükseliş.")
    good = ("- Yön: yükseliş. Fiyat EMA200 üstünde, ADX güçlü trend gösteriyor.\n"
            "- Kritik seviye: destek 76.945, direnç 80.285 civarında.\n"
            "- Momentum: RSI 56 ile nötr bölgede, aşırı alım yok.\n"
            "- Zayıf taraf: MACD negatif, hacim ortalamanın altında.")

    bad_verdict = compliance.check(thinking, strip_thinking=strip_thinking)
    good_verdict = compliance.check(good, strip_thinking=strip_thinking)

    assert not bad_verdict.ok, "süreç anlatımı yakalanmadı"
    assert good_verdict.ok, f"sağlam çıktı boşuna reddedildi: {good_verdict.problems}"
    assert "KULLANILAMAZ" in bad_verdict.instruction()
    return "ihlal yakalandı, sağlam çıktı dokunulmadan geçti"


# =========================================================================== #
#  13. DİSK YÖNETİMİ
# =========================================================================== #

@check("Disk: sıkışınca lüks kesiliyor, zorunlu korunuyor")
def _disk():
    from app.core import storage
    from app.core.db import SessionLocal

    state = storage.status()
    assert state.level in ("ok", "low", "critical"), state.level

    # Temizlik, silinecek bir şey yokken de patlamamalı.
    db = SessionLocal()
    report = storage.housekeeping(db)
    db.close()
    assert "removed" in report and "disk" in report

    #  Kritik seviyede rapor yazımının durduğunu doğrula: bu ayrım olmadan
    #  sistem ya gereksiz durur ya da pozisyon kaydı tutamaz hâle gelir.
    class Squeezed:
        total = 100 * 1_048_576 * 1024
        free = 50 * 1_048_576
        used = total - free

    original = storage.shutil.disk_usage
    storage.shutil.disk_usage = lambda p: Squeezed
    try:
        squeezed = storage.status()
    finally:
        storage.shutil.disk_usage = original

    assert squeezed.level == "critical"
    assert squeezed.can_write_optional is False, "kritik diskte rapor yazımı sürüyor"
    return f"{state.free_mb:.0f} MB boş ({state.level}); kritik simülasyonu geçti"


# =========================================================================== #
#  14. RESMÎ SOSYAL API
# =========================================================================== #

@check("Sosyal: resmî API aynalardan ÖNCE deneniyor")
def _social_order():
    from app.layers import browser, social_api

    db, user = _user()

    original_find = social_api.find_token
    original_timeline = social_api.timeline
    original_fetch = browser.fetch

    social_api.find_token = lambda *a, **k: "token"
    social_api.timeline = lambda token, handle, limit=10: social_api.Timeline(
        handle=handle, name="Resmî")

    def mirrors_must_not_run(*a, **k):
        raise AssertionError("resmî API varken ayna denendi")

    browser.fetch = mirrors_must_not_run
    try:
        result = browser.social("testhesap", db=db, user=user)
    finally:
        social_api.find_token = original_find
        social_api.timeline = original_timeline
        browser.fetch = original_fetch
        db.close()

    assert result.get("kaynak") == "resmî API", result
    return "anahtar varsa aynaya hiç bakılmıyor"


# =========================================================================== #
#  ACIL FREN — durmadan once her seyi kapatiyor mu
# =========================================================================== #

@check("Acil fren: durmadan ÖNCE tüm pozisyonları kapatıyor")
def _emergency_brake():
    import json

    import app.core.emergency as em
    from app.core import emergency
    from app.core.safety import KILL_SWITCH_FILE, kill_switch_active
    from app.models import Autonomy, Bot, BotStatus, Position, PositionStatus, Side, TradingMode

    db, user = _user()
    KILL_SWITCH_FILE.unlink(missing_ok=True)
    bot = Bot(user_id=user.id, name="fren denetimi", market="crypto",
              exchange="binance", symbol="BTC/USDT", timeframe="4h",
              mode=TradingMode.PAPER, autonomy=Autonomy.FULL,
              decision_mode="algo_only", status=BotStatus.RUNNING,
              strategies_json=json.dumps(["trend_following"]), guards_json="{}",
              risk_pct=0.5, initial_balance=10_000.0, paper_balance=10_000.0,
              peak_equity=10_000.0, day_start_equity=10_000.0)
    db.add(bot)
    db.commit()
    pos = Position(bot_id=bot.id, symbol=bot.symbol, side=Side.LONG,
                   status=PositionStatus.OPEN, mode=bot.mode, qty=0.01,
                   entry_price=79_000.0, stop_loss=76_000.0,
                   initial_stop=76_000.0, take_profit=88_000.0,
                   risk_amount=30.0, notional=790.0, confidence=0.8)
    db.add(pos)
    db.commit()

    original = em._notify
    em._notify = lambda *a, **k: None
    try:
        result = emergency.engage(db, user, "bütün denetim")
    finally:
        em._notify = original

    db.refresh(pos)
    db.refresh(bot)
    assert pos.status == PositionStatus.CLOSED, "fren pozisyonu açık bıraktı"
    assert pos.close_reason == "EMERGENCY_BRAKE"
    assert bot.status == BotStatus.STOPPED, "fren botu durdurmadı"
    assert kill_switch_active(), "anahtar çevrilmedi"

    emergency.release(db, user, "denetim bitti")
    db.query(Position).filter(Position.bot_id == bot.id).delete(
        synchronize_session=False)
    db.query(Bot).filter(Bot.id == bot.id).delete(synchronize_session=False)
    db.commit()
    db.close()
    return (f"{result['sonuc']['kapatilan']} pozisyon kapatıldı, "
            f"{result['sonuc']['durdurulan_bot']} bot durduruldu, "
            f"{result['sure_ms']} ms")


# =========================================================================== #
#  CALISMA MODLARI — Sor/Planla hicbir seyi degistiremiyor mu
# =========================================================================== #

@check("Çalışma modları: Sor/Planla durumu değiştiremiyor")
def _work_modes():
    from app.agent.tools import REGISTRY
    from app.agent.work_mode import _ALWAYS_ALLOWED, allowed, resolve

    blocked = 0
    for name, tool in REGISTRY.items():
        if not tool.mutating or name in _ALWAYS_ALLOWED:
            continue
        for mode in ("ask", "plan"):
            ok, why = allowed(mode, name, True)
            assert not ok, f"{name} {mode} modunda çalışabiliyor"
            assert why, f"{name} reddedildi ama gerekçesiz"
        blocked += 1

    assert blocked > 5, "neredeyse hiçbir araç engellenmiyor"
    assert allowed("agent", "create_bot", True)[0], "Uygula modu da engelliyor"
    for bad in ("", "uydurma", None, "AGENTT"):
        assert resolve(bad).can_mutate is False, "bilinmeyen mod Uygula'ya düştü"

    for name in ("activate_kill_switch", "emergency_flatten"):
        assert name in REGISTRY, f"{name} kayıtlı değil"
        for mode in ("ask", "plan", "agent"):
            assert allowed(mode, name, True)[0], f"{name} {mode} modunda kapalı"

    return (f"{blocked} değiştirici araç Sor/Planla modunda kapalı, "
            f"fren her modda açık")


# =========================================================================== #
#  ODEME GUCU — kullanici borclanamaz
# =========================================================================== #

@check("Ödeme gücü: kullanıcı borçlanamıyor")
def _solvency():
    from app.layers.solvency import (
        account_floor_breached,
        check_position,
        clamp_balance,
        worst_case_loss,
    )

    # Kisa pozisyonun kaybi pozisyon degeriyle SINIRLI DEGIL
    short = worst_case_loss("short", 100.0, 10.0, gap_pct=200.0)
    assert short > 1000.0, "kısa pozisyon riski pozisyon değeriyle sınırlanmış"
    # Uzun pozisyon en fazla degerini kaybeder
    long_ = worst_case_loss("long", 100.0, 10.0, gap_pct=500.0)
    assert long_ == 1000.0, "uzun pozisyon değerinden fazlasını kaybediyor"

    wipe = check_position(10_000.0, "short", 100.0, 200.0)
    assert not wipe.allowed, "kasayı silecek pozisyon geçti"

    normal = check_position(10_000.0, "long", 100.0, 10.0)
    assert normal.allowed, "normal pozisyon reddedildi"

    value, clamped = clamp_balance(-500.0)
    assert value == 0.0 and clamped, "bakiye sıfırın altına inebiliyor"
    assert account_floor_breached(10_000.0, 2_000.0), "kasa tabanı tetiklenmiyor"
    assert not account_floor_breached(10_000.0, 8_000.0), \
        "taban normal düşüşte tetikleniyor"
    return "kısa pozisyon stresi ölçülüyor, sıfır tabanı ve kasa tabanı çalışıyor"


# =========================================================================== #
#  SERMAYE HARITASI — "piyasada" ile "riskte" ayri mi
# =========================================================================== #

@check("Sermaye haritası: 'piyasada' ile 'riskte' ayrı gösteriliyor")
def _treasury():
    import json

    from app.layers.treasury import snapshot
    from app.models import Autonomy, Bot, BotStatus, Position, PositionStatus, Side, TradingMode

    db, user = _user()
    bot = Bot(user_id=user.id, name="harita denetimi", market="crypto",
              exchange="binance", symbol="BTC/USDT", timeframe="4h",
              mode=TradingMode.PAPER, autonomy=Autonomy.FULL,
              decision_mode="algo_only", status=BotStatus.RUNNING,
              strategies_json=json.dumps(["trend_following"]), guards_json="{}",
              risk_pct=0.5, initial_balance=10_000.0, paper_balance=9_000.0,
              peak_equity=10_000.0, day_start_equity=10_000.0)
    db.add(bot)
    db.commit()
    db.add(Position(bot_id=bot.id, symbol=bot.symbol, side=Side.LONG,
                    status=PositionStatus.OPEN, mode=bot.mode, qty=10.0,
                    entry_price=100.0, stop_loss=96.0, initial_stop=96.0,
                    take_profit=112.0, risk_amount=40.0, notional=1000.0,
                    confidence=0.8))
    db.commit()

    data = snapshot(db, user, live_prices=False)
    ozet = data["ozet"]
    assert ozet["piyasada"] >= 1000.0 - 1e-6, "piyasadaki tutar hesaplanmadı"
    assert abs(ozet["riskte"] - 40.0) < 1e-6, f"riskteki tutar yanlış: {ozet['riskte']}"
    assert ozet["riskte"] < ozet["piyasada"], "risk ile piyasa değeri karışmış"
    assert "dalgalan" in data["dagilim"]["riskteki_tutar"]["aciklama"]
    assert "ters giderse" in data["kullaniciya_soyle"]

    db.query(Position).filter(Position.bot_id == bot.id).delete(
        synchronize_session=False)
    db.query(Bot).filter(Bot.id == bot.id).delete(synchronize_session=False)
    db.commit()
    db.close()
    return (f"piyasada {ozet['piyasada']:.0f} · riskte {ozet['riskte']:.0f} "
            f"(%{ozet['riskte_pct']:.2f}) — ayrı raporlanıyor")


# =========================================================================== #
#  MCP — masaustu ajanlar baglanabiliyor mu
# =========================================================================== #

@check("MCP: Claude Code / Codex bağlanıp tüm araçları görüyor")
def _mcp():
    import json
    import subprocess

    from app.agent.tools import REGISTRY
    from app.core.config import settings

    requests = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "get_safety_status", "arguments": {}}}),
    ]) + "\n"

    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "mcp_server.py")],
        input=requests, capture_output=True, text=True, timeout=180,
        encoding="utf-8", errors="replace", cwd=str(ROOT))

    assert "Traceback" not in proc.stderr, \
        f"MCP sunucusu çöktü: {proc.stderr[-200:]}"

    seen = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        parsed = json.loads(line)
        seen[parsed.get("id")] = parsed.get("result", {})

    assert 1 in seen, "initialize yanıtı yok"
    assert 2 in seen, "tools/list yanıtı yok"
    assert 3 in seen, "tools/call yanıtı yok"

    names = {t["name"] for t in seen[2]["tools"]}
    assert len(names) == len(REGISTRY), \
        f"MCP {len(names)} araç veriyor, kayıtta {len(REGISTRY)} var"
    for needed in ("capital_map", "cross_check", "market_pulse",
                   "activate_kill_switch", "emergency_flatten"):
        assert needed in names, f"{needed} MCP'de yok"

    limits = seen[1]["instructions"]
    assert str(settings.hard_max_risk_pct) in limits, \
        "MCP talimatları gerçek risk tavanını söylemiyor"

    return f"{len(names)} araç, gerçek limitler bildiriliyor, tools/call çalışıyor"


# =========================================================================== #
#  Rapor
# =========================================================================== #

def main() -> None:
    print("=" * 76)
    print("BÜTÜN DENETİM — daha önce sorun çıkaran her şey")
    print("=" * 76)
    for name, ok, detail in RESULTS:
        print(f"  [{'GECTI' if ok else 'KALDI'}]  {name}")
        if detail:
            print(f"           {detail}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("-" * 76)
    print(f"  {passed}/{len(RESULTS)} denetim geçti")
    if passed < len(RESULTS):
        print("\n  KALANLAR:")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"    · {name}\n      {detail}")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
