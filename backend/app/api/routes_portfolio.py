"""
Portföy Genel Bakış
====================
Tüm botların birleşik performansı — panelin üst kartlarını besler.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..core.db import get_db
from ..models import Bot, BotStatus, EquityPoint, Position, PositionStatus, User
from .deps import current_user, num

router = APIRouter(prefix="/api/portfolio", tags=["Portföy"])


@router.get("/overview")
def overview(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    bots = db.query(Bot).filter(Bot.user_id == user.id).all()
    bot_ids = [b.id for b in bots]

    closed: list[Position] = []
    open_positions: list[Position] = []
    if bot_ids:
        closed = (
            db.query(Position)
            .filter(Position.bot_id.in_(bot_ids), Position.status == PositionStatus.CLOSED)
            .all()
        )
        open_positions = (
            db.query(Position)
            .filter(Position.bot_id.in_(bot_ids), Position.status == PositionStatus.OPEN)
            .all()
        )

    wins = [p for p in closed if num(p.pnl) > 0]
    losses = [p for p in closed if num(p.pnl) <= 0]
    gross_win = sum(num(p.pnl) for p in wins)
    gross_loss = abs(sum(num(p.pnl) for p in losses))

    total_balance = sum(num(b.paper_balance) for b in bots)
    total_initial = sum(num(b.initial_balance) for b in bots)
    peak = sum(num(b.peak_equity) for b in bots)

    # Son 30 günün günlük PnL dağılımı
    since = datetime.now(UTC) - timedelta(days=30)
    daily: dict[str, float] = defaultdict(float)
    for p in closed:
        if p.closed_at:
            closed_at = p.closed_at if p.closed_at.tzinfo else p.closed_at.replace(tzinfo=UTC)
            if closed_at >= since:
                daily[closed_at.strftime("%Y-%m-%d")] += num(p.pnl)

    return {
        "bot_count": len(bots),
        "running": sum(1 for b in bots if b.status == BotStatus.RUNNING),
        "locked": sum(1 for b in bots if b.status == BotStatus.LOCKED),
        "recovery": sum(1 for b in bots if b.recovery_mode),
        "capital": {
            "total_balance": round(total_balance, 2),
            "total_initial": round(total_initial, 2),
            "peak_equity": round(peak, 2),
            "net_pnl": round(total_balance - total_initial, 2),
            "return_pct": round((total_balance - total_initial) / total_initial * 100.0, 3)
            if total_initial else 0.0,
            "drawdown_pct": round((peak - total_balance) / peak * 100.0, 3)
            if peak > 0 else 0.0,
        },
        "performance": {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(closed) * 100.0, 2) if closed else 0.0,
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0
            else (999.0 if gross_win > 0 else 0.0),
            "avg_r": round(sum(num(p.r_multiple) for p in closed) / len(closed), 3) if closed else 0.0,
            "best_trade": round(max((num(p.pnl) for p in closed), default=0.0), 2),
            "worst_trade": round(min((num(p.pnl) for p in closed), default=0.0), 2),
            "open_positions": len(open_positions),
        },
        "daily_pnl": [{"date": k, "pnl": round(v, 2)} for k, v in sorted(daily.items())],
        "bots": [
            {
                "id": b.id, "name": b.name, "symbol": b.symbol, "status": b.status.value,
                "mode": b.mode.value, "decision_mode": b.decision_mode,
                "balance": round(num(b.paper_balance), 2),
                "return_pct": round(
                    (num(b.paper_balance) - num(b.initial_balance)) / num(b.initial_balance) * 100.0, 2
                ) if num(b.initial_balance) else 0.0,
                "recovery_mode": b.recovery_mode,
            }
            for b in bots
        ],
    }


@router.get("/equity")
def combined_equity(db: Session = Depends(get_db), user: User = Depends(current_user),
                    limit: int = 400) -> list[dict]:
    """Tüm botların birleşik sermaye eğrisi (zaman ekseninde toplanır)."""
    bot_ids = [b.id for b in db.query(Bot).filter(Bot.user_id == user.id).all()]
    if not bot_ids:
        return []

    rows = (
        db.query(EquityPoint)
        .filter(EquityPoint.bot_id.in_(bot_ids))
        .order_by(desc(EquityPoint.id))
        .limit(limit * max(len(bot_ids), 1))
        .all()
    )
    buckets: dict[str, float] = defaultdict(float)
    for point in rows:
        key = point.ts.strftime("%Y-%m-%dT%H:%M") if point.ts else ""
        buckets[key] += num(point.equity)
    return [{"t": k, "equity": round(v, 4)} for k, v in sorted(buckets.items())][-limit:]


@router.get("/treasury")
def treasury(live: bool = True, db: Session = Depends(get_db),
             user: User = Depends(current_user)) -> dict:
    """
    SERMAYE HARİTASI — "param şu an tam olarak nerede?"

    Özet "ne kadar kazandın" der; bu uç daha temel bir soruyu cevaplar:
    ne kadarı boşta, ne kadarı piyasada, ne kadarı gerçekten riskte.

    Üçü farklı şeydir. "Piyasada 8.000 dolarım var" korkutucudur;
    "riskte 240 dolarım var" gerçektir.
    """
    from ..layers.treasury import snapshot  # noqa: PLC0415

    return snapshot(db, user, live_prices=live)
