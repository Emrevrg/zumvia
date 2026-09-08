"""
Bot Yönetimi Uçları
====================
Oluşturma, ayar, başlat/durdur, pozisyonlar, olay logu, istatistikler.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.db import get_db
from ..core.logging import get_logger
from ..core.trading_mode import starting_mode
from ..engine import scheduler as sched
from ..engine.orchestrator import close_position, execute_entry, run_cycle
from ..layers.l1_market_data import fetch_quote
from ..layers.l4_risk import SizedOrder, effective_min_confidence, effective_risk_pct
from ..layers.playbooks import all_playbooks_for, resolve_playbook
from ..layers.strategies import DEFAULT_ACTIVE, strategy_catalog
from ..models import (
    Autonomy,
    Bot,
    BotEvent,
    BotStatus,
    Credential,
    CredentialKind,
    EquityPoint,
    Position,
    PositionStatus,
    Side,
    TradingMode,
    User,
)
from ..schemas import (
    BotIn,
    BotPatch,
    ClosePositionIn,
    GenericOut,
    ManualTradeIn,
    PlaybookDeployIn,
    QuickStartIn,
)
from .deps import current_user, user_bot

log = get_logger("zumvia.api.bots")

router = APIRouter(prefix="/api/bots", tags=["Bot"])

# Hızlı kurulum risk profilleri — tavan kodda 1.5 ile sınırlı (hard_max_risk_pct)
# Kullanıcı 0 görmemeli: %0 = işlem yok. En düşük 0.25 ile “neredeyse risksiz” simülasyon.
RISK_PRESETS: dict[str, dict[str, Any]] = {
    "ultra_korumaci": {"risk_pct": 0.15, "daily_loss_limit_pct": 1.0, "min_confidence": 0.88,
                       "min_rr": 3.0, "max_drawdown_pct": 6.0, "min_agree": 4,
                       "max_trades_per_day": 2, "label": "Ultra Korumacı — maksimum garanti"},
    "korumaci": {"risk_pct": 0.25, "daily_loss_limit_pct": 2.0, "min_confidence": 0.85,
                 "min_rr": 2.5, "max_drawdown_pct": 10.0, "min_agree": 3,
                 "max_trades_per_day": 4, "label": "Korumacı — önce sermaye"},
    "dengeli": {"risk_pct": 0.5, "daily_loss_limit_pct": 3.0, "min_confidence": 0.78,
                "min_rr": 2.2, "max_drawdown_pct": 12.0, "min_agree": 3,
                "max_trades_per_day": 6, "label": "Dengeli — önerilen (varsayılan)"},
    "dinamik": {"risk_pct": 0.75, "daily_loss_limit_pct": 3.0, "min_confidence": 0.75,
                "min_rr": 2.0, "max_drawdown_pct": 15.0, "min_agree": 2,
                "max_trades_per_day": 8, "label": "Dinamik — fırsatlarda büyür"},
    "agresif": {"risk_pct": 1.0, "daily_loss_limit_pct": 3.0, "min_confidence": 0.75,
                "min_rr": 2.0, "max_drawdown_pct": 20.0, "min_agree": 2,
                "max_trades_per_day": 12, "label": "Agresif — sistem tavanında (1.5%)"},
}


# --------------------------------------------------------------------------- #
#  Serileştirme
# --------------------------------------------------------------------------- #


def serialize_bot(db: Session, bot: Bot) -> dict[str, Any]:
    open_positions = (
        db.query(Position)
        .filter(Position.bot_id == bot.id, Position.status == PositionStatus.OPEN)
        .all()
    )
    closed = (
        db.query(Position)
        .filter(Position.bot_id == bot.id, Position.status == PositionStatus.CLOSED)
        .all()
    )
    wins = [p for p in closed if p.pnl > 0]
    realized = sum(p.pnl for p in closed)

    return {
        "id": bot.id,
        "name": bot.name,
        "market": bot.market,
        "exchange": bot.exchange,
        "symbol": bot.symbol,
        "timeframe": bot.timeframe,
        "mode": bot.mode.value,
        "autonomy": bot.autonomy.value,
        "decision_mode": bot.decision_mode,
        "status": bot.status.value,
        "poll_seconds": bot.poll_seconds,
        "allow_short": bot.allow_short,
        "llm_credential_id": bot.llm_credential_id,
        "llm_model": bot.llm_model,
        "exchange_credential_id": bot.exchange_credential_id,
        "telegram_credential_id": bot.telegram_credential_id,
        "strategies": json.loads(bot.strategies_json or "[]") or DEFAULT_ACTIVE,
        "min_agree": bot.min_agree,
        "strategy_notes": bot.strategy_notes,
        "risk": {
            "risk_pct": bot.risk_pct,
            "effective_risk_pct": effective_risk_pct(bot),
            "daily_loss_limit_pct": bot.daily_loss_limit_pct,
            "min_confidence": bot.min_confidence,
            "effective_min_confidence": effective_min_confidence(bot),
            "min_rr": bot.min_rr,
            "max_open_positions": bot.max_open_positions,
            "max_trades_per_day": bot.max_trades_per_day,
            "max_drawdown_pct": bot.max_drawdown_pct,
            "trailing_stop": bot.trailing_stop,
            "breakeven_at_r": bot.breakeven_at_r,
            "recovery_mode": bot.recovery_mode,
            "consecutive_losses": bot.consecutive_losses,
            "locked_until": bot.locked_until.isoformat() if bot.locked_until else None,
            "lock_reason": bot.lock_reason,
            "day_trades": bot.day_trades,
        },
        "capital": {
            "initial_balance": bot.initial_balance,
            "balance": round(bot.paper_balance, 6),
            "peak_equity": round(bot.peak_equity, 6),
            "day_start_equity": round(bot.day_start_equity, 6),
            "realized_pnl": round(realized, 6),
            "total_return_pct": round(
                (bot.paper_balance - bot.initial_balance) / bot.initial_balance * 100.0, 3
            ) if bot.initial_balance else 0.0,
        },
        "stats": {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "win_rate_pct": round(len(wins) / len(closed) * 100.0, 2) if closed else 0.0,
            "open_positions": len(open_positions),
        },
        "last_run_at": bot.last_run_at.isoformat() if bot.last_run_at else None,
        "next_run_at": sched.next_run(bot.id),
        "created_at": bot.created_at.isoformat() if bot.created_at else None,
    }


def serialize_position(p: Position, price: float | None = None) -> dict[str, Any]:
    unrealized = None
    if price and p.status == PositionStatus.OPEN:
        unrealized = ((price - p.entry_price) if p.side == Side.LONG
                      else (p.entry_price - price)) * p.qty
    return {
        "id": p.id, "bot_id": p.bot_id, "symbol": p.symbol, "side": p.side.value,
        "status": p.status.value, "mode": p.mode.value, "qty": p.qty,
        "entry_price": p.entry_price, "stop_loss": p.stop_loss,
        "take_profit": p.take_profit, "initial_stop": p.initial_stop,
        "risk_amount": p.risk_amount, "notional": p.notional,
        "exit_price": p.exit_price, "pnl": round(p.pnl, 6),
        "pnl_pct": round(p.pnl_pct, 4), "r_multiple": round(p.r_multiple, 3),
        "fees": round(p.fees, 6), "confidence": p.confidence,
        "reasoning": p.reasoning, "close_reason": p.close_reason,
        "unrealized_pnl": round(unrealized, 6) if unrealized is not None else None,
        "opened_at": p.opened_at.isoformat() if p.opened_at else None,
        "closed_at": p.closed_at.isoformat() if p.closed_at else None,
    }


# --------------------------------------------------------------------------- #
#  Katalog
# --------------------------------------------------------------------------- #


@router.get("/catalog")
def catalog() -> dict:
    """Arayüzün ihtiyaç duyduğu seçenek listeleri."""
    return {
        "strategies": strategy_catalog(),
        "risk_presets": [
            {"id": key, **dict(val.items())} for key, val in RISK_PRESETS.items()
        ],
        "decision_modes": [
            {"id": "hybrid", "label": "Hibrit — yapay zeka + algoritma mutabakatı", "icon": "shieldCheck",
             "desc": "İkisi aynı yönde hemfikir değilse işlem yok. En düşük hata, en yüksek disiplin — önerilen."},
            {"id": "ai_first", "label": "Yapay zeka öncelikli — kesintisiz", "icon": "brain",
             "desc": "Model birincil; kota/hata olursa 12 strateji devralır. 7/24 kesinti yok."},
            {"id": "algo_only", "label": "Yalnızca algoritma — $0", "icon": "scales",
             "desc": "Tamamen deterministik, anahtar gerekmez. Model çökse bile çalışır."},
            {"id": "ai_only", "label": "Yalnızca yapay zeka", "icon": "council",
             "desc": "Kararı modele bırakır; risk kalkanı SL/R/R’yi yine kısıtlar."},
        ],
        "autonomy_modes": [
            {"id": "full", "label": "Tam otonom — açar, yönetir, kapatır"},
            {"id": "semi", "label": "Yarı otonom — açar, kapanışa siz de karışırsınız"},
            {"id": "manual", "label": "Manuel onay — her işlem için onayınız istenir"},
        ],
        "hard_limits": {
            "max_risk_pct": settings.hard_max_risk_pct,
            "max_daily_loss_pct": settings.hard_daily_loss_limit_pct,
            "min_confidence": settings.hard_min_confidence,
            "min_rr": settings.hard_min_rr_ratio,
            "circuit_breaker_lock_hours": settings.circuit_breaker_lock_hours,
            "force_paper_only": settings.force_paper_only,
        },
    }


# --------------------------------------------------------------------------- #
#  CRUD
# --------------------------------------------------------------------------- #


@router.get("")
def list_bots(db: Session = Depends(get_db),
              user: User = Depends(current_user)) -> list[dict]:
    bots = db.query(Bot).filter(Bot.user_id == user.id).order_by(Bot.id.desc()).all()
    return [serialize_bot(db, b) for b in bots]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_bot(payload: BotIn, db: Session = Depends(get_db),
               user: User = Depends(current_user)) -> dict:
    if payload.mode == "live" and settings.force_paper_only:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Bu kurulumda canlı ticaret kapalıdır (VQ_FORCE_PAPER_ONLY).")

    bot = Bot(
        user_id=user.id, name=payload.name, market=payload.market,
        exchange=payload.exchange, symbol=payload.symbol, timeframe=payload.timeframe,
        quote_currency=payload.quote_currency, mode=TradingMode(payload.mode),
        autonomy=Autonomy(payload.autonomy), decision_mode=payload.decision_mode,
        poll_seconds=payload.poll_seconds, allow_short=payload.allow_short,
        llm_credential_id=payload.llm_credential_id, llm_model=payload.llm_model,
        exchange_credential_id=payload.exchange_credential_id,
        telegram_credential_id=payload.telegram_credential_id,
        strategies_json=json.dumps(payload.strategies or DEFAULT_ACTIVE),
        min_agree=payload.min_agree, strategy_notes=payload.strategy_notes,
        risk_pct=payload.risk_pct, daily_loss_limit_pct=payload.daily_loss_limit_pct,
        min_confidence=payload.min_confidence, min_rr=payload.min_rr,
        max_open_positions=payload.max_open_positions,
        max_trades_per_day=payload.max_trades_per_day,
        max_drawdown_pct=payload.max_drawdown_pct, trailing_stop=payload.trailing_stop,
        breakeven_at_r=payload.breakeven_at_r,
        initial_balance=payload.initial_balance, paper_balance=payload.initial_balance,
        peak_equity=payload.initial_balance, day_start_equity=payload.initial_balance,
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return serialize_bot(db, bot)


@router.post("/quickstart", status_code=status.HTTP_201_CREATED)
def quickstart(payload: QuickStartIn, db: Session = Depends(get_db),
               user: User = Depends(current_user)) -> dict:
    """3 adımlık kurulum: parite + risk profili + (varsa) model → çalışmaya hazır bot."""
    preset = RISK_PRESETS.get(payload.risk_level, RISK_PRESETS["dengeli"])
    bot = Bot(
        user_id=user.id,
        name=f"{payload.symbol} · {preset['label'].split(' ')[0]}",
        market=payload.market, exchange=payload.exchange, symbol=payload.symbol,
        timeframe=payload.timeframe, mode=TradingMode.PAPER, autonomy=Autonomy.FULL,
        decision_mode="hybrid" if payload.llm_credential_id else "algo_only",
        poll_seconds=300, llm_credential_id=payload.llm_credential_id,
        llm_model=payload.llm_model,
        strategies_json=json.dumps(DEFAULT_ACTIVE), min_agree=preset["min_agree"],
        risk_pct=preset["risk_pct"], daily_loss_limit_pct=preset["daily_loss_limit_pct"],
        min_confidence=preset["min_confidence"], min_rr=preset["min_rr"],
        max_drawdown_pct=preset["max_drawdown_pct"],
        max_trades_per_day=preset["max_trades_per_day"],
        initial_balance=payload.initial_balance, paper_balance=payload.initial_balance,
        peak_equity=payload.initial_balance, day_start_equity=payload.initial_balance,
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return serialize_bot(db, bot)


@router.delete("/custom-playbooks/{slug}")
def delete_custom_playbook(slug: str, db: Session = Depends(get_db),
                           user: User = Depends(current_user)) -> dict:
    """
    Özel sistemi (şablonu) siler.

    Bu şablonla kurulmuş BOTLAR etkilenmez: onların strateji seti ve korumaları
    kendi kayıtlarında saklıdır. Silinen yalnızca yeniden kurulum şablonudur.
    """
    from ..models import CustomPlaybook  # noqa: PLC0415

    row = (db.query(CustomPlaybook)
           .filter(CustomPlaybook.user_id == user.id,
                   CustomPlaybook.slug == slug).first())
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Özel sistem bulunamadı.")

    db.delete(row)
    db.commit()
    return {"ok": True, "deleted": slug,
            "not": "Bu şablonla kurulmuş botlar çalışmaya devam ediyor."}


@router.get("/evidence")
def evidence_report() -> dict:
    """
    En son üretilen görülmemiş veri (walk-forward) raporu.

    Rapor yoksa `available: false` döner — boş bir tablo göstermek yerine
    kullanıcıya raporun henüz üretilmediği açıkça söylenir.
    """
    from ..engine.evidence import latest  # noqa: PLC0415

    report = latest()
    if report is None:
        return {"available": False,
                "note": "Henüz görülmemiş veri raporu üretilmedi."}
    return {"available": True, **report}


@router.get("/playbooks")
def list_playbooks(db: Session = Depends(get_db),
                   user: User = Depends(current_user)) -> dict:
    """Hazır sistemler + bu kullanıcı için tasarlanmış özel sistemler."""
    rows = all_playbooks_for(db, user)
    return {
        "count": len(rows),
        "builtin": sum(1 for r in rows if not r.get("custom")),
        "custom": sum(1 for r in rows if r.get("custom")),
        "playbooks": rows,
    }


@router.post("/from-playbook", status_code=status.HTTP_201_CREATED)
def create_from_playbook(payload: PlaybookDeployIn, db: Session = Depends(get_db),
                         user: User = Depends(current_user)) -> dict:
    """
    Kütüphanedeki hazır sistemlerden birini bot olarak kurar.

    Strateji seti, hemfikirlik eşiği, zaman dilimi ve risk playbook'tan gelir;
    kullanıcı yalnızca parite ve kasa büyüklüğü seçer. Bot her zaman sanal
    (paper) modda başlar.
    """
    pb = resolve_playbook(db, user, payload.playbook_id)
    if pb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"Bilinmeyen sistem: {payload.playbook_id}")

    llm = (db.query(Credential)
           .filter(Credential.user_id == user.id,
                   Credential.kind == CredentialKind.LLM).first())
    decision_mode = pb.decision_mode
    if decision_mode != "algo_only" and llm is None:
        decision_mode = "algo_only"

    balance = float(payload.initial_balance)
    exchange = payload.exchange or ("binance" if payload.market == "crypto" else "yahoo")

    bot = Bot(
        user_id=user.id,
        name=(payload.name or f"{pb.label} · {payload.symbol}")[:80],
        market=payload.market, exchange=exchange, symbol=payload.symbol,
        timeframe=payload.timeframe or pb.timeframe,
        mode=starting_mode(db, user, exchange_credential_id=None),
        autonomy=Autonomy.FULL,
        decision_mode=decision_mode,
        llm_credential_id=llm.id if (llm and decision_mode != "algo_only") else None,
        poll_seconds=max(60, pb.poll_seconds),
        allow_short=pb.allow_short,
        strategies_json=json.dumps(pb.strategies),
        guards_json=json.dumps(pb.guards),
        min_agree=pb.min_agree,
        strategy_notes=f"[{pb.id}] {pb.thesis} | Zayıf yanı: {pb.weakness}"[:2000],
        risk_pct=min(pb.risk_pct, settings.hard_max_risk_pct),
        partial_tp_enabled=pb.partial_tp,
        initial_balance=balance, paper_balance=balance,
        peak_equity=balance, day_start_equity=balance,
    )
    db.add(bot)

    from ..layers.playbooks import PLAYBOOKS  # noqa: PLC0415
    if pb.id not in PLAYBOOKS:
        from ..models import CustomPlaybook  # noqa: PLC0415
        row = (db.query(CustomPlaybook)
               .filter(CustomPlaybook.user_id == user.id,
                       CustomPlaybook.slug == pb.id).first())
        if row is not None:
            row.deploy_count += 1

    db.commit()
    db.refresh(bot)

    data = serialize_bot(db, bot)
    data["bot_id"] = bot.id
    data["playbook"] = pb.id
    data["playbook_label"] = pb.label
    data["weakness"] = pb.weakness
    return data


@router.get("/{bot_id}")
def get_bot(bot: Bot = Depends(user_bot), db: Session = Depends(get_db)) -> dict:
    return serialize_bot(db, bot)


@router.patch("/{bot_id}")
def update_bot(payload: BotPatch, bot: Bot = Depends(user_bot),
               db: Session = Depends(get_db)) -> dict:
    data = payload.model_dump(exclude_unset=True)

    if data.get("mode") == "live" and settings.force_paper_only:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Bu kurulumda canlı ticaret kapalıdır.")

    # Sert tavanlar burada da uygulanır
    if "risk_pct" in data:
        data["risk_pct"] = min(float(data["risk_pct"]), settings.hard_max_risk_pct)
    if "daily_loss_limit_pct" in data:
        data["daily_loss_limit_pct"] = min(float(data["daily_loss_limit_pct"]),
                                           settings.hard_daily_loss_limit_pct)
    if "min_confidence" in data:
        data["min_confidence"] = max(float(data["min_confidence"]),
                                     settings.hard_min_confidence)
    if "min_rr" in data:
        data["min_rr"] = max(float(data["min_rr"]), settings.hard_min_rr_ratio)

    if "strategies" in data:
        bot.strategies_json = json.dumps(data.pop("strategies") or DEFAULT_ACTIVE)
    if "mode" in data:
        bot.mode = TradingMode(data.pop("mode"))
    if "autonomy" in data:
        bot.autonomy = Autonomy(data.pop("autonomy"))
    if "initial_balance" in data and bot.status == BotStatus.STOPPED:
        new_balance = float(data.pop("initial_balance"))
        bot.initial_balance = new_balance
        bot.paper_balance = new_balance
        bot.peak_equity = new_balance
        bot.day_start_equity = new_balance
    else:
        data.pop("initial_balance", None)

    for key, value in data.items():
        if value is not None and hasattr(bot, key):
            setattr(bot, key, value)

    db.commit()
    db.refresh(bot)

    if bot.status == BotStatus.RUNNING:   # periyot değişmişse yeniden zamanla
        sched.start_bot_job(bot.id, bot.poll_seconds)
    return serialize_bot(db, bot)


@router.delete("/{bot_id}", response_model=GenericOut)
def delete_bot(bot: Bot = Depends(user_bot), db: Session = Depends(get_db)) -> GenericOut:
    sched.stop_bot_job(bot.id)
    db.delete(bot)
    db.commit()
    return GenericOut(message="Bot ve tüm geçmişi silindi.")


# --------------------------------------------------------------------------- #
#  Çalıştırma kontrolü
# --------------------------------------------------------------------------- #


@router.post("/{bot_id}/start", response_model=GenericOut)
def start_bot(bot: Bot = Depends(user_bot), db: Session = Depends(get_db)) -> GenericOut:
    if bot.mode == TradingMode.LIVE:
        if settings.force_paper_only:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Canlı ticaret kapalı.")
        if not bot.exchange_credential_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Canlı mod için borsa API anahtarı seçilmelidir.")
    if bot.decision_mode in ("ai_only", "hybrid", "ai_first") and not bot.llm_credential_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Bu karar modu için bir yapay zeka anahtarı seçin ya da "
            "'Sadece Algoritma' moduna geçin.",
        )

    if bot.locked_until:
        locked = bot.locked_until.replace(tzinfo=UTC) if bot.locked_until.tzinfo is None \
            else bot.locked_until
        if locked > datetime.now(UTC):
            raise HTTPException(status.HTTP_423_LOCKED,
                                f"Bot devre kesici ile kilitli: {bot.lock_reason}")

    bot.status = BotStatus.RUNNING
    db.commit()
    sched.start_bot_job(bot.id, bot.poll_seconds)
    return GenericOut(message=f"{bot.name} çalışıyor. Her {bot.poll_seconds} saniyede "
                              f"piyasa taranacak.")


@router.post("/{bot_id}/stop", response_model=GenericOut)
def stop_bot(bot: Bot = Depends(user_bot), db: Session = Depends(get_db)) -> GenericOut:
    sched.stop_bot_job(bot.id)
    bot.status = BotStatus.STOPPED
    db.commit()
    return GenericOut(message=f"{bot.name} durduruldu. Açık pozisyonlar korunuyor.")


@router.post("/{bot_id}/unlock", response_model=GenericOut)
def unlock_bot(bot: Bot = Depends(user_bot), db: Session = Depends(get_db)) -> GenericOut:
    """Devre kesici kilidini kullanıcı bilinçli olarak kaldırır."""
    bot.locked_until = None
    bot.lock_reason = ""
    bot.status = BotStatus.STOPPED
    bot.day_key = ""            # gün penceresi sıfırlanır
    db.commit()
    return GenericOut(message="Kilit kaldırıldı. Botu tekrar başlatabilirsiniz.")


@router.post("/{bot_id}/run-once")
def run_once(bot: Bot = Depends(user_bot)) -> dict:
    """Zamanlayıcıyı beklemeden tek tur çalıştırır (test ve teşhis için)."""
    return run_cycle(bot.id)


# --------------------------------------------------------------------------- #
#  Pozisyonlar
# --------------------------------------------------------------------------- #


@router.get("/{bot_id}/positions")
def positions(bot: Bot = Depends(user_bot), db: Session = Depends(get_db),
              limit: int = Query(default=100, le=500)) -> dict:
    price = None
    try:
        price = fetch_quote(bot.market, bot.exchange, bot.symbol).price
    except Exception as exc:  # noqa: BLE001
        # Anlık fiyat alınamazsa pozisyon listesi yine dönmeli; yalnızca
        # "güncel fiyat" sütunu boş kalır.
        log.info("anlık fiyat alınamadı (%s): %s", bot.symbol, exc)

    rows = (
        db.query(Position)
        .filter(Position.bot_id == bot.id)
        .order_by(desc(Position.id))
        .limit(limit)
        .all()
    )
    return {
        "price": price,
        "open": [serialize_position(p, price) for p in rows
                 if p.status == PositionStatus.OPEN],
        "pending": [serialize_position(p, price) for p in rows
                    if p.status == PositionStatus.PENDING],
        "closed": [serialize_position(p) for p in rows
                   if p.status == PositionStatus.CLOSED],
    }


@router.post("/{bot_id}/positions/approve", response_model=GenericOut)
def approve_position(payload: ManualTradeIn, bot: Bot = Depends(user_bot),
                     db: Session = Depends(get_db),
                     user: User = Depends(current_user)) -> GenericOut:
    """Manuel modda bekleyen sinyali onaylar veya reddeder."""
    pos = db.get(Position, payload.position_id)
    if pos is None or pos.bot_id != bot.id or pos.status != PositionStatus.PENDING:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bekleyen sinyal bulunamadı.")

    if not payload.approve:
        pos.status = PositionStatus.REJECTED
        pos.close_reason = "USER_REJECTED"
        db.commit()
        return GenericOut(message="Sinyal reddedildi.")

    from ..engine.orchestrator import _make_broker  # noqa: PLC0415

    broker = _make_broker(db, bot, user, bot.paper_balance)
    order = SizedOrder(
        side=pos.side.value, entry=pos.entry_price, stop_loss=pos.stop_loss,
        take_profit=pos.take_profit, qty=pos.qty, notional=pos.notional,
        risk_amount=pos.risk_amount,
        rr_ratio=abs(pos.take_profit - pos.entry_price) /
        max(abs(pos.entry_price - pos.stop_loss), 1e-12),
        risk_pct_used=bot.risk_pct,
    )
    decision = {"confidence": pos.confidence, "reasoning": pos.reasoning,
                "source": "KULLANICI ONAYI"}
    snapshot = json.loads(pos.snapshot_json or "{}")

    db.delete(pos)          # bekleyen kayıt yerine gerçek pozisyon açılır
    db.flush()
    created = execute_entry(db, bot, user, broker, order, decision, snapshot)
    db.commit()
    if created is None:
        return GenericOut(ok=False, message="Emir borsada gerçekleşmedi.")
    return GenericOut(message="İşlem onaylandı ve açıldı.", data={"position_id": created.id})


@router.post("/{bot_id}/positions/close", response_model=GenericOut)
def close_open_position(payload: ClosePositionIn, bot: Bot = Depends(user_bot),
                        db: Session = Depends(get_db),
                        user: User = Depends(current_user)) -> GenericOut:
    pos = db.get(Position, payload.position_id)
    if pos is None or pos.bot_id != bot.id or pos.status != PositionStatus.OPEN:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Açık pozisyon bulunamadı.")

    from ..engine.orchestrator import _make_broker  # noqa: PLC0415

    price = fetch_quote(bot.market, bot.exchange, bot.symbol).price
    broker = _make_broker(db, bot, user, bot.paper_balance)
    closed = close_position(db, bot, user, pos, price, "MANUAL", broker)
    db.commit()
    return GenericOut(message=f"Pozisyon kapatıldı. PnL {closed.pnl:+.2f}",
                      data={"pnl": closed.pnl})


# --------------------------------------------------------------------------- #
#  Log ve sermaye eğrisi
# --------------------------------------------------------------------------- #


@router.get("/{bot_id}/events")
def events(bot: Bot = Depends(user_bot), db: Session = Depends(get_db),
           limit: int = Query(default=120, le=500)) -> list[dict]:
    rows = (
        db.query(BotEvent)
        .filter(BotEvent.bot_id == bot.id)
        .order_by(desc(BotEvent.id))
        .limit(limit)
        .all()
    )
    return [
        {
            "id": e.id, "ts": e.ts.isoformat() if e.ts else None,
            "level": e.level, "category": e.category, "message": e.message,
            "data": json.loads(e.data_json or "{}"),
        }
        for e in reversed(rows)
    ]


@router.get("/{bot_id}/equity")
def equity_curve(bot: Bot = Depends(user_bot), db: Session = Depends(get_db),
                 limit: int = Query(default=500, le=5000)) -> list[dict]:
    rows = (
        db.query(EquityPoint)
        .filter(EquityPoint.bot_id == bot.id)
        .order_by(desc(EquityPoint.id))
        .limit(limit)
        .all()
    )
    return [
        {"t": p.ts.isoformat() if p.ts else None,
         "equity": round(p.equity, 6), "balance": round(p.balance, 6)}
        for p in reversed(rows)
    ]
