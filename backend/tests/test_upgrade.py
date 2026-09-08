"""
SÜRÜM GEÇİŞİ TESTLERİ

Bir güncelleme sırasında "botuma ne oldu?" sorusunun cevabı belirsiz olamaz.
Bu testler politikayı kilitler: açık pozisyona dokunulmaz, ana sürümde botlar
duraklatılır (kapatılmaz), yamada hiçbir şey durmaz.
"""
from __future__ import annotations

import json

import pytest

from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.core.upgrade import _parts, _write_version
from app.core.upgrade import apply as apply_upgrade
from app.models import (
    Autonomy,
    Bot,
    BotStatus,
    Position,
    PositionStatus,
    Side,
    SystemState,
    TradingMode,
    User,
)


@pytest.fixture()
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def user(db):
    row = db.query(User).filter(User.email == "upgrade@zumvia.com").first()
    if row is None:
        row = User(email="upgrade@zumvia.com",
                   password_hash=hash_password("upgradetest12345"),
                   vault_salt=new_salt())
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _bot(db, user, *, notes: str = "", running: bool = True) -> Bot:
    bot = Bot(
        user_id=user.id, name="Yükseltme testi", market="demo", exchange="demo",
        symbol="DEMO/USDT", timeframe="1h", mode=TradingMode.PAPER,
        autonomy=Autonomy.FULL, decision_mode="algo_only",
        status=BotStatus.RUNNING if running else BotStatus.STOPPED,
        strategies_json=json.dumps(["trend_following"]),
        strategy_notes=notes,
        initial_balance=1000.0, paper_balance=1000.0,
        peak_equity=1000.0, day_start_equity=1000.0,
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return bot


def _cleanup(db, *bots) -> None:
    for bot in bots:
        db.query(Position).filter(Position.bot_id == bot.id).delete()
        fresh = db.get(Bot, bot.id)
        if fresh is not None:
            db.delete(fresh)
    db.commit()


# --------------------------------------------------------------------------- #
#  Sürüm karşılaştırma
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("1.0.0", (1, 0, 0)),
    ("2.5", (2, 5, 0)),
    ("3", (3, 0, 0)),
    ("bozuk", (0, 0, 0)),
    ("", (0, 0, 0)),
])
def test_version_parsing_never_crashes(raw: str, expected: tuple) -> None:
    assert _parts(raw) == expected


# --------------------------------------------------------------------------- #
#  Politika
# --------------------------------------------------------------------------- #

def test_same_version_changes_nothing(db, user) -> None:
    from app.core.config import settings  # noqa: PLC0415

    _write_version(db, settings.version)
    db.commit()
    bot = _bot(db, user)

    report = apply_upgrade(db)
    db.refresh(bot)

    assert report.changed is False
    assert bot.status == BotStatus.RUNNING, "aynı sürümde bot durdurulmamalı"
    _cleanup(db, bot)


def test_patch_upgrade_keeps_bots_running(db, user, monkeypatch) -> None:
    """Yama sürümünde sistem kesintisiz devam eder."""
    from app.core.config import settings  # noqa: PLC0415

    monkeypatch.setattr(settings, "version", "9.1.2")
    _write_version(db, "9.1.1")
    db.commit()
    bot = _bot(db, user)

    report = apply_upgrade(db)
    db.refresh(bot)

    assert report.changed is True
    assert report.major_change is False
    assert not report.paused_bots
    assert bot.status == BotStatus.RUNNING
    _cleanup(db, bot)


def test_major_upgrade_pauses_but_never_closes_positions(db, user, monkeypatch) -> None:
    """
    Ana sürümde botlar duraklatılır — ama AÇIK POZİSYON kapatılmaz.
    Bu, politikanın en kritik maddesidir.
    """
    from app.core.config import settings  # noqa: PLC0415

    monkeypatch.setattr(settings, "version", "9.0.0")
    _write_version(db, "8.4.0")
    db.commit()

    bot = _bot(db, user)
    position = Position(
        bot_id=bot.id, symbol="DEMO/USDT", side=Side.LONG,
        entry_price=100.0, qty=1.0, stop_loss=95.0, take_profit=115.0,
        status=PositionStatus.OPEN, confidence=0.9, reasoning="yükseltme testi",
    )
    db.add(position)
    db.commit()
    db.refresh(position)

    report = apply_upgrade(db)
    db.refresh(bot)
    db.refresh(position)

    assert report.major_change is True
    assert bot.id in report.paused_bots
    assert bot.status == BotStatus.STOPPED, "ana sürümde bot duraklatılmalı"
    assert position.status == PositionStatus.OPEN, "güncelleme pozisyon KAPATMAZ"
    assert "duraklat" in (bot.lock_reason or "").lower()

    _cleanup(db, bot)


def test_bot_with_missing_playbook_is_stopped_with_reason(db, user, monkeypatch) -> None:
    """Kullandığı sistem kaybolmuşsa bot sessizce çalışmaya devam edemez."""
    from app.core.config import settings  # noqa: PLC0415

    monkeypatch.setattr(settings, "version", "9.2.1")
    _write_version(db, "9.2.0")
    db.commit()

    orphan = _bot(db, user, notes="[artik_olmayan_sistem] eski tez")
    healthy = _bot(db, user, notes="[trend_rider] geçerli sistem")

    report = apply_upgrade(db)
    db.refresh(orphan)
    db.refresh(healthy)

    assert orphan.id in report.orphaned_bots
    assert orphan.status == BotStatus.STOPPED
    assert "bulunamadı" in (orphan.lock_reason or "")
    assert healthy.status == BotStatus.RUNNING, "geçerli sistemli bot etkilenmemeli"

    _cleanup(db, orphan, healthy)


def test_first_install_pauses_nothing(db, user) -> None:
    db.query(SystemState).filter(SystemState.key == "app_version").delete()
    db.commit()
    bot = _bot(db, user)

    report = apply_upgrade(db)
    db.refresh(bot)

    assert report.changed is False
    assert bot.status == BotStatus.RUNNING
    _cleanup(db, bot)


def test_version_is_persisted_after_upgrade(db) -> None:
    from app.core.config import settings  # noqa: PLC0415

    _write_version(db, "0.0.1")
    db.commit()
    apply_upgrade(db)

    row = db.query(SystemState).filter(SystemState.key == "app_version").first()
    assert row is not None and row.value == settings.version


@pytest.fixture(autouse=True)
def _restore_version(db):
    """Test sonrası kayıtlı sürümü gerçek sürüme döndür — sonraki testler etkilenmesin."""
    yield
    from app.core.config import settings  # noqa: PLC0415
    _write_version(db, settings.version)
    db.commit()
