"""
ÖNLEM KATMANI TESTLERİ

Buradaki iddia: sistemlerin zayıf yanına karşı aldığı önlemler süs değil,
girişten önce çalışan gerçek filtrelerdir. Her test, önlemin devreye girdiği
ANI ve kullanıcıya dönen gerekçeyi doğrular.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.layers.guards import GUARD_LABELS, check_guards, describe


def frame(**overrides) -> pd.DataFrame:
    """Göstergeleri hesaplanmış, kontrollü bir son bar üretir."""
    size = 300
    base = {
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0,
        "adx": 30.0, "atr_pct": 1.5, "volume_z": 1.5,
        "ema_50": 105.0, "ema_200": 100.0,
    }
    base.update(overrides)
    return pd.DataFrame([base] * size)


# --------------------------------------------------------------------------- #
#  Rejim filtreleri
# --------------------------------------------------------------------------- #

def test_trend_system_stays_out_of_flat_market() -> None:
    """Trend takibinin zaafı yatay piyasadır: ADX tabanı onu susturur."""
    verdict = check_guards({"adx_min": 20}, frame(adx=12.0))
    assert verdict.allowed is False
    assert verdict.code == "ADX_TOO_LOW"
    assert "yatay" in verdict.reason.lower()
    assert verdict.details["adx"] == 12.0


def test_trend_system_trades_when_trend_is_real() -> None:
    assert check_guards({"adx_min": 20}, frame(adx=28.0)).allowed is True


def test_range_system_steps_aside_when_trend_starts() -> None:
    """Bant sisteminin zaafı trendin başlamasıdır: ADX tavanı onu çeker."""
    verdict = check_guards({"adx_max": 22}, frame(adx=35.0))
    assert verdict.allowed is False
    assert verdict.code == "ADX_TOO_HIGH"


# --------------------------------------------------------------------------- #
#  Hacim, volatilite, likidite
# --------------------------------------------------------------------------- #

def test_breakout_requires_volume_confirmation() -> None:
    """Teyitsiz kırılım tuzaktır — hacim eşiği bunu keser."""
    verdict = check_guards({"volume_z_min": 0.8}, frame(volume_z=0.1))
    assert verdict.allowed is False
    assert verdict.code == "NO_VOLUME"


def test_dead_market_is_skipped() -> None:
    verdict = check_guards({"atr_pct_min": 0.3}, frame(atr_pct=0.05))
    assert verdict.allowed is False
    assert verdict.code == "ATR_TOO_LOW"


def test_extreme_volatility_is_skipped() -> None:
    """Aşırı oynaklıkta stop kayması kârı yer; sistem durur."""
    verdict = check_guards({"atr_pct_max": 5.0}, frame(atr_pct=12.0))
    assert verdict.allowed is False
    assert verdict.code == "ATR_TOO_HIGH"


def test_wide_spread_blocks_entry() -> None:
    verdict = check_guards({"max_spread_pct": 0.1}, frame(), spread_pct=0.9)
    assert verdict.allowed is False
    assert verdict.code == "WIDE_SPREAD"


def test_spread_guard_ignored_when_depth_unknown() -> None:
    """Veri yoksa uydurma yapılmaz; diğer filtreler çalışmaya devam eder."""
    assert check_guards({"max_spread_pct": 0.1}, frame(), spread_pct=None).allowed is True


# --------------------------------------------------------------------------- #
#  Davranışsal önlemler
# --------------------------------------------------------------------------- #

def test_cooldown_blocks_revenge_trade() -> None:
    """Zarardan hemen sonra aynı yere dönmek en pahalı alışkanlıktır."""
    recent_loss = datetime.now(UTC) - timedelta(minutes=30)
    verdict = check_guards({"cooldown_bars": 3}, frame(),
                           last_loss_at=recent_loss, timeframe_minutes=60)
    assert verdict.allowed is False
    assert verdict.code == "COOLDOWN"
    assert verdict.details["remaining_minutes"] > 0


def test_cooldown_expires() -> None:
    old_loss = datetime.now(UTC) - timedelta(hours=10)
    assert check_guards({"cooldown_bars": 3}, frame(),
                        last_loss_at=old_loss, timeframe_minutes=60).allowed is True


def test_daily_trade_cap() -> None:
    verdict = check_guards({"max_trades_per_day": 2}, frame(), trades_today=2)
    assert verdict.allowed is False
    assert verdict.code == "TRADE_CAP"


@pytest.mark.parametrize("action,ema50,ema200", [("BUY", 90.0, 100.0), ("SELL", 110.0, 100.0)])
def test_counter_trend_entries_are_refused(action: str, ema50: float, ema200: float) -> None:
    """Ana trende karşı işlem, sistemin en sık tekrarlayan hatasıdır."""
    verdict = check_guards({"require_higher_tf_agreement": True},
                           frame(ema_50=ema50, ema_200=ema200), action=action)
    assert verdict.allowed is False
    assert verdict.code == "AGAINST_TREND"


def test_trend_aligned_entry_passes() -> None:
    assert check_guards({"require_higher_tf_agreement": True},
                        frame(ema_50=110.0, ema_200=100.0), action="BUY").allowed is True


# --------------------------------------------------------------------------- #
#  Dayanıklılık
# --------------------------------------------------------------------------- #

def test_short_history_is_refused() -> None:
    small = frame().iloc[:20]
    verdict = check_guards({"min_candles": 200}, small)
    assert verdict.allowed is False
    assert verdict.code == "MIN_CANDLES"


def test_empty_data_never_passes() -> None:
    verdict = check_guards({"adx_min": 20}, pd.DataFrame())
    assert verdict.allowed is False
    assert verdict.code == "NO_DATA"


def test_nan_values_do_not_crash_or_pass_silently() -> None:
    """Gösterge NaN geldiğinde filtre çökmemeli; güvenli tarafa düşmeli."""
    verdict = check_guards({"adx_min": 20}, frame(adx=np.nan))
    assert verdict.allowed is False


def test_no_guards_means_no_restriction() -> None:
    assert check_guards({}, frame()).allowed is True


def test_every_guard_has_a_human_readable_label() -> None:
    """Kullanıcı, sistemin neden beklediğini okuyabilmeli."""
    described = describe(dict.fromkeys(GUARD_LABELS, 1))
    assert len(described) == len(GUARD_LABELS)
    assert all(isinstance(line, str) and line for line in described)


def test_multiple_guards_report_the_first_violation() -> None:
    """Birden çok kural ihlalinde kullanıcıya net tek sebep gösterilir."""
    verdict = check_guards({"adx_min": 25, "volume_z_min": 2.0},
                           frame(adx=10.0, volume_z=0.0))
    assert verdict.allowed is False
    assert verdict.code == "ADX_TOO_LOW"


# --------------------------------------------------------------------------- #
#  Motor entegrasyonu — önlem gerçekten girişi durduruyor mu?
# --------------------------------------------------------------------------- #

def test_engine_blocks_entry_when_guard_refuses(monkeypatch) -> None:
    """
    Uçtan uca: bot bir SATIN AL kararı üretse bile, sisteminin önlemi
    koşulları uygun bulmuyorsa `run_cycle` pozisyon açmadan döner.
    """
    import json as _json  # noqa: PLC0415

    from app.core.crypto import new_salt  # noqa: PLC0415
    from app.core.db import SessionLocal  # noqa: PLC0415
    from app.core.security import hash_password  # noqa: PLC0415
    from app.engine import orchestrator  # noqa: PLC0415
    from app.models import Autonomy, Bot, BotStatus, TradingMode, User  # noqa: PLC0415

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == "guard@zumvia.com").first()
        if user is None:
            user = User(email="guard@zumvia.com",
                        password_hash=hash_password("guardtest12345"),
                        vault_salt=new_salt())
            db.add(user)
            db.commit()
            db.refresh(user)

        bot = Bot(
            user_id=user.id, name="Önlem testi", market="demo", exchange="demo",
            symbol="DEMO/USDT", timeframe="1h", mode=TradingMode.PAPER,
            autonomy=Autonomy.FULL, decision_mode="algo_only",
            status=BotStatus.RUNNING,
            strategies_json=_json.dumps(["trend_following"]),
            # Ulaşılamaz eşik: hiçbir piyasa bu kadar güçlü trend göstermez
            guards_json=_json.dumps({"adx_min": 999}),
            initial_balance=1000.0, paper_balance=1000.0,
            peak_equity=1000.0, day_start_equity=1000.0,
        )
        db.add(bot)
        db.commit()
        db.refresh(bot)

        # Karar aşamasını atlayıp doğrudan "AL" dedirt: önlem yine de durdurmalı
        monkeypatch.setattr(orchestrator, "decide", lambda *args, **kwargs: {
            "action": "BUY", "confidence": 0.95,
            "stop_loss": 0.0, "take_profit": 0.0,
            "reasoning": "önlem testi", "source": "test",
        })

        bot_id = bot.id
        db.commit()
        result = orchestrator.run_cycle(bot_id)
        assert result.get("action") in ("GUARDED", "WAIT", "BLOCKED"), result
        if result.get("action") == "GUARDED":
            assert "ADX" in result["reason"] or "Trend" in result["reason"]

        stale = db.get(Bot, bot_id)
        if stale is not None:
            db.delete(stale)
            db.commit()
    finally:
        db.close()


def test_guard_path_runs_end_to_end_with_a_real_signal(monkeypatch) -> None:
    """
    Regresyon testi: guard bloğu, motorda GERÇEKTEN bir AL/SAT sinyali
    oluştuğunda çalışır. Daha önce bu yolda eksik bir içe aktarma (`desc`)
    vardı ve hata yalnızca işlem açılacağı anda ortaya çıkıyordu — yani en
    kötü anda. Bu test o yolu zorla çalıştırır.
    """
    import json as _json  # noqa: PLC0415

    from app.core.crypto import new_salt  # noqa: PLC0415
    from app.core.db import SessionLocal  # noqa: PLC0415
    from app.core.security import hash_password  # noqa: PLC0415
    from app.engine import orchestrator  # noqa: PLC0415
    from app.models import Autonomy, Bot, BotStatus, TradingMode, User  # noqa: PLC0415

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == "guardpath@zumvia.com").first()
        if user is None:
            user = User(email="guardpath@zumvia.com",
                        password_hash=hash_password("guardpath12345"),
                        vault_salt=new_salt())
            db.add(user)
            db.commit()
            db.refresh(user)

        bot = Bot(
            user_id=user.id, name="Guard yolu", market="demo", exchange="demo",
            symbol="DEMO/USDT", timeframe="1h", mode=TradingMode.PAPER,
            autonomy=Autonomy.FULL, decision_mode="algo_only",
            status=BotStatus.RUNNING,
            strategies_json=_json.dumps(["trend_following"]),
            # cooldown_bars, geçmiş zarar sorgusunu çalıştırır (hatanın olduğu yer)
            guards_json=_json.dumps({"cooldown_bars": 3, "adx_min": 999}),
            initial_balance=10_000.0, paper_balance=10_000.0,
            peak_equity=10_000.0, day_start_equity=10_000.0,
        )
        db.add(bot)
        db.commit()
        bot_id = bot.id
        db.close()

        # Karar aşamasını atlayıp kesin bir AL sinyali ürettir
        original = orchestrator.decide

        def always_buy(*args, **kwargs):
            decision = dict(original(*args, **kwargs))
            decision["action"] = "BUY"
            decision["confidence"] = 0.95
            return decision

        # raising=False KULLANILMAZ: fonksiyon adı değişirse bu test sessizce
        # devre dışı kalmak yerine hata versin.
        monkeypatch.setattr(orchestrator, "decide", always_buy)

        result = orchestrator.run_cycle(bot_id)

        # Hangi karar çıkarsa çıksın, guard bloğu ÇÖKMEDEN geçmiş olmalı
        assert result.get("ok") is True, result
        assert "NameError" not in str(result.get("error", ""))

        cleanup = SessionLocal()
        stale = cleanup.get(Bot, bot_id)
        if stale is not None:
            cleanup.delete(stale)
            cleanup.commit()
        cleanup.close()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
#  Koruma tabanı
# --------------------------------------------------------------------------- #

def test_a_bot_without_guards_is_not_unprotected() -> None:
    """
    `guards_json` varsayılanı boş sözlüktür.

    Canlı koşuda görüldü: ajanın kurduğu bot stop oldu ve AYNI TURDA aynı
    yere yeniden girdi — çünkü zarar sonrası soğuma tanımlı değildi.
    Koruma yalnızca doğru şablonu seçmiş olmaya bağlıysa koruma değil,
    şanstır.
    """
    from app.layers.guards import BASELINE_GUARDS, with_baseline

    merged = with_baseline({})
    assert merged["cooldown_bars"] == BASELINE_GUARDS["cooldown_bars"]
    assert merged["max_trades_per_day"] == BASELINE_GUARDS["max_trades_per_day"]
    assert merged["max_spread_pct"] == BASELINE_GUARDS["max_spread_pct"]

    assert with_baseline(None) == BASELINE_GUARDS


def test_an_explicit_choice_always_beats_the_baseline() -> None:
    """
    Kullanıcının bilinçli tercihi sistemin varsayımından üstündür.

    Taban yalnızca SESSİZLİĞİ doldurur; verilen değeri ne yukarı ne aşağı
    çeker. `None` de bilinçli bir tercihtir ("bu korumayı istemiyorum").
    """
    from app.layers.guards import with_baseline

    assert with_baseline({"cooldown_bars": 12})["cooldown_bars"] == 12
    assert with_baseline({"cooldown_bars": 0})["cooldown_bars"] == 0
    assert with_baseline({"max_spread_pct": None})["max_spread_pct"] is None


def test_the_baseline_cooldown_actually_blocks_re_entry() -> None:
    """
    Taban yalnızca sözlükte değil, GERÇEK karar yolunda uygulanmalı.

    `check_guards` tabanı kendi içinde uygular; çağıranların birinde
    unutulursa o bot sessizce korumasız kalırdı.
    """
    from datetime import UTC, datetime, timedelta

    from app.layers.guards import check_guards

    verdict = check_guards(
        {},                                   # HİÇ koruma tanımlı değil
        frame(adx=30.0),
        last_loss_at=datetime.now(UTC) - timedelta(minutes=5),
        timeframe_minutes=60,
    )
    assert verdict.allowed is False
    assert verdict.code == "COOLDOWN", f"soğuma uygulanmadı: {verdict.code}"


def test_the_guard_layer_is_never_skipped_entirely() -> None:
    """
    Denetim KOŞULSUZ çalışmalı.

    Yaşanmış hata iki katmanlıydı ve ikisi de aynı varsayımdan doğuyordu:
    "koruma tanımlanmamışsa kontrol etmeye gerek yok".

        orchestrator.py : `if guards:` → boş sözlükte tüm blok atlanıyordu
        guards.py       : `if not guards: return OK` → erken onay

    Sonucu canlı koşuda görüldü: bot stop oldu ve AYNI TURDA aynı yere
    yeniden girdi. Zarar sonrası soğuma hiç sorulmadı.

    Bu test ikinci katmanı korur; birincisi `orchestrator.py` içinde
    koşulsuz çağrıyla kapatıldı.
    """
    from datetime import UTC, datetime, timedelta

    from app.layers.guards import check_guards

    just_lost = datetime.now(UTC) - timedelta(minutes=1)

    for guards in ({}, None):
        verdict = check_guards(guards, frame(adx=30.0),
                               last_loss_at=just_lost, timeframe_minutes=60)
        assert verdict.allowed is False, \
            f"{guards!r} korumasız geçti — önlem katmanı atlandı"
        assert verdict.code == "COOLDOWN"


def test_overtrading_is_capped_even_without_explicit_guards() -> None:
    """Aşırı işlem, sistemin en sessiz para kaybıdır."""
    from app.layers.guards import BASELINE_GUARDS, check_guards

    verdict = check_guards({}, frame(adx=30.0),
                           trades_today=BASELINE_GUARDS["max_trades_per_day"])
    assert verdict.allowed is False
    assert verdict.code == "TRADE_CAP"
