"""
RAPORLAMA VE DENETİM ÇIKTISI
=============================
Her rapor iki formatta üretilir:
  * **JSON** — makine tarafından okunabilir, denetlenebilir, arşivlenir.
  * **Markdown** — insan tarafından okunabilir, doğrudan paylaşılabilir.

Raporlar `reports/YYYY-MM-DD/<run_id>.{json,md}` altında saklanır.

İlkeler:
  1. Her sayı, üretildiği ham girdiye kadar izlenebilir olmalıdır.
  2. Kaynaklı veri ile model yorumu **asla** aynı şeymiş gibi sunulmaz.
  3. İşlem yapılmadıysa **neden yapılmadığı** açıkça yazılır — sessizlik değil,
     gerekçe raporlanır.
  4. Paper / canlı ayrımı her raporda açıkça etiketlenir.
"""
from __future__ import annotations

import contextlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..api.deps import num
from ..core.config import BASE_DIR
from ..core.logging import get_logger
from ..core.safety import safety_snapshot
from ..layers.portfolio_risk import portfolio_summary
from ..layers.recovery import build_recovery_plan, measure_expectancy
from ..models import (
    AuditLog,
    Bot,
    BotEvent,
    DecisionRecord,
    Position,
    PositionStatus,
    TradingMode,
    User,
)

log = get_logger("zumvia.reporting")

REPORTS_DIR = BASE_DIR / "reports"

DISCLAIMER = ("Bu rapor araştırma ve analiz amaçlıdır; kişiselleştirilmiş finansal "
              "tavsiye değildir. Geçmiş performans gelecek sonuçları garanti etmez.")


def _fmt(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def build_report(db: Session, user: User, *, period_days: int = 7,
                 run_id: str | None = None) -> dict[str, Any]:
    """Portföyün tam durum raporunu (veri yapısı olarak) üretir."""
    now = datetime.now(UTC)
    run_id = run_id or uuid.uuid4().hex[:12]

    bots = db.query(Bot).filter(Bot.user_id == user.id).all()
    ids = [b.id for b in bots]

    closed: list[Position] = []
    open_positions: list[Position] = []
    decisions: list[DecisionRecord] = []
    errors: list[BotEvent] = []

    if ids:
        closed = (db.query(Position)
                  .filter(Position.bot_id.in_(ids),
                          Position.status == PositionStatus.CLOSED)
                  .order_by(desc(Position.id)).limit(200).all())
        open_positions = (db.query(Position)
                          .filter(Position.bot_id.in_(ids),
                                  Position.status == PositionStatus.OPEN).all())
        decisions = (db.query(DecisionRecord)
                     .filter(DecisionRecord.user_id == user.id)
                     .order_by(desc(DecisionRecord.id)).limit(40).all())
        errors = (db.query(BotEvent)
                  .filter(BotEvent.bot_id.in_(ids), BotEvent.level == "error")
                  .order_by(desc(BotEvent.id)).limit(20).all())

    audits = (db.query(AuditLog)
              .filter(AuditLog.user_id == user.id)
              .order_by(desc(AuditLog.id)).limit(20).all())

    equity = sum(num(b.paper_balance) for b in bots)
    initial = sum(num(b.initial_balance) for b in bots)
    wins = [p for p in closed if num(p.pnl) > 0]
    gross_win = sum(num(p.pnl) for p in wins)
    gross_loss = abs(sum(num(p.pnl) for p in closed if num(p.pnl) <= 0))

    sum(
        ((p.take_profit - p.entry_price) * 0.0) for p in open_positions
    )  # gerçekleşmemiş PnL anlık fiyat gerektirir; rapor anında hesaplanmaz

    live_bots = [b for b in bots if b.mode == TradingMode.LIVE]

    per_bot = []
    for bot in bots:
        bot_closed = [p for p in closed if p.bot_id == bot.id]
        bal = num(bot.paper_balance)
        init = num(bot.initial_balance)
        plan = build_recovery_plan(bot, bal,
                                   expectancy_r=measure_expectancy(bot_closed))
        per_bot.append({
            "id": bot.id, "name": bot.name, "symbol": bot.symbol,
            "timeframe": bot.timeframe, "market": bot.market,
            "status": bot.status.value, "mode": bot.mode.value,
            "decision_mode": bot.decision_mode, "council_mode": bot.council_mode,
            "balance": round(bal, 2),
            "initial_balance": round(init, 2),
            "return_pct": round((bal - init) / init * 100, 2) if init else 0.0,
            "trades": len(bot_closed),
            "recovery": plan.to_dict(),
            "risk": {
                "risk_pct": num(bot.risk_pct, 1.0),
                "daily_loss_limit_pct": num(bot.daily_loss_limit_pct, 3.0),
                "max_drawdown_pct": num(bot.max_drawdown_pct, 15.0),
                "max_portfolio_heat_pct": num(bot.max_portfolio_heat_pct, 3.0),
                "locked_until": bot.locked_until.isoformat() if bot.locked_until else None,
                "lock_reason": bot.lock_reason,
            },
        })

    return {
        "meta": {
            "run_id": run_id,
            "reference_date": now.isoformat(),
            "timezone": "UTC",
            "period_days": period_days,
            "generated_by": "ZUMVIA",
            "user": user.email,
            "mode_label": "CANLI + SANAL" if live_bots else "YALNIZCA SANAL (paper)",
            "disclaimer": DISCLAIMER,
            "paper_notice": ("Paper moddaki sonuçlar sanaldır; gerçek kâr kanıtı değildir. "
                             "Geçmiş performans gelecek sonuçları garanti etmez."),
            "evaluation_note": ("8 saatlik gerçek zamanlı forward gözlem yapılmadı; "
                                "hızlandırılmış geçmiş-veri değerlendirmesi kullanılır."),
        },
        "safety": safety_snapshot(db, user),
        "capital": {
            "total_balance": round(equity, 2),
            "total_initial": round(initial, 2),
            "net_pnl": round(equity - initial, 2),
            "return_pct": round((equity - initial) / initial * 100, 3) if initial else 0.0,
            "unrealized_note": "Gerçekleşmemiş PnL canlı fiyat gerektirir; panelde görülür.",
        },
        "performance": {
            "closed_trades": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 2) if closed else 0.0,
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else (
                999.0 if gross_win else 0.0),
            "expectancy_r": round(num(measure_expectancy(closed)), 3),
            "best_trade": round(max((num(p.pnl) for p in closed), default=0.0), 2),
            "worst_trade": round(min((num(p.pnl) for p in closed), default=0.0), 2),
        },
        "portfolio_risk": {**portfolio_summary(open_positions, equity),
                           "equity": round(equity, 2)},
        "bots": per_bot,
        "open_positions": [{
            "bot_id": p.bot_id, "symbol": p.symbol, "side": p.side.value,
            "entry": num(p.entry_price), "stop_loss": num(p.stop_loss),
            "take_profit": num(p.take_profit), "qty": num(p.qty),
            "risk_amount": round(num(p.risk_amount), 4),
            "partial_taken": p.partial_taken,
            "opened_at": p.opened_at.isoformat() if p.opened_at else None,
        } for p in open_positions],
        "recent_trades": [{
            "bot_id": p.bot_id, "symbol": p.symbol, "side": p.side.value,
            "entry": num(p.entry_price), "exit": num(p.exit_price),
            "pnl": round(num(p.pnl), 4), "r": round(num(p.r_multiple), 3),
            "reason": p.close_reason or "",
            "closed_at": p.closed_at.isoformat() if p.closed_at else None,
        } for p in closed[:25]],
        "decisions": [{
            "decision_id": d.decision_id, "ts": d.ts.isoformat() if d.ts else None,
            "symbol": d.symbol, "action": d.action,
            "confidence": round(num(d.confidence), 3), "executed": d.executed,
            "veto_reason": d.veto_reason or "",
        } for d in decisions],
        "errors": [{
            "bot_id": e.bot_id, "ts": e.ts.isoformat() if e.ts else None,
            "message": e.message[:300],
        } for e in errors],
        "audit": [{
            "ts": a.ts.isoformat() if a.ts else None, "action": a.action,
            "actor": a.actor, "detail": a.detail[:200],
        } for a in audits],
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Raporu insan okunur Markdown'a çevirir."""
    meta = report["meta"]
    capital = report["capital"]
    perf = report["performance"]
    risk = report["portfolio_risk"]
    safety = report["safety"]

    lines: list[str] = [
        "# ZUMVIA — Portföy Raporu",
        "",
        f"**Referans zamanı:** {meta['reference_date']} ({meta['timezone']})  ",
        f"**Çalışma kimliği:** `{meta['run_id']}`  ",
        f"**Mod:** {meta['mode_label']}  ",
        f"**Not:** {meta.get('paper_notice', meta['disclaimer'])}  ",
        f"**Acil fren:** {'AÇIK — yeni işlem yok' if safety['kill_switch'] else 'kapalı'}  ",
        f"**Canlı yetki:** "
        f"{'aktif · üst limit ' + _fmt(safety['live_authorization'].get('max_capital', 0)) if safety['live_authorization']['authorized'] else 'yok'}",
        "",
        "---",
        "",
        "## 1. Sermaye",
        "",
        "| Ölçüt | Değer |",
        "|---|---|",
        f"| Başlangıç | {_fmt(capital['total_initial'])} |",
        f"| Güncel bakiye | {_fmt(capital['total_balance'])} |",
        f"| Net kâr/zarar | {_fmt(capital['net_pnl'])} |",
        f"| Getiri | %{_fmt(capital['return_pct'])} |",
        "",
        "## 2. Performans",
        "",
        "| Ölçüt | Değer |",
        "|---|---|",
        f"| Kapanan işlem | {perf['closed_trades']} |",
        f"| Kazanma oranı | %{_fmt(perf['win_rate_pct'])} |",
        f"| Kâr faktörü | {_fmt(perf['profit_factor'], 3)} |",
        f"| Beklenti (R) | {_fmt(perf['expectancy_r'], 3)} |",
        f"| En iyi / en kötü işlem | {_fmt(perf['best_trade'])} / {_fmt(perf['worst_trade'])} |",
        "",
        "## 3. Portföy Riski",
        "",
        f"- Açık pozisyon: **{risk['open_positions']}** "
        f"(risksiz hale gelmiş: {risk['risk_free_positions']})",
        f"- Portföy ısısı: **%{_fmt(risk['heat_pct'], 3)}** "
        f"(riske edilen toplam: {_fmt(risk['risk_amount'])})",
        f"- Yön dağılımı: {risk['long']} long / {risk['short']} short",
        f"- Kümeler: {', '.join(f'{k}×{v}' for k, v in risk['clusters'].items()) or '—'}",
        "",
        "## 4. Botlar",
        "",
        "| Bot | Parite | Mod | Durum | Bakiye | Getiri | İşlem | Aşama |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for bot in report["bots"]:
        lines.append(
            f"| {bot['name']} | {bot['symbol']} {bot['timeframe']} | "
            f"{'CANLI' if bot['mode'] == 'live' else 'sanal'} | {bot['status']} | "
            f"{_fmt(bot['balance'])} | %{_fmt(bot['return_pct'])} | {bot['trades']} | "
            f"{bot['recovery']['phase_label']} |"
        )

    lines += ["", "## 5. Açık Pozisyonlar", ""]
    if report["open_positions"]:
        lines += ["| Parite | Yön | Giriş | Stop | Hedef | Risk | Kısmi |",
                  "|---|---|---|---|---|---|---|"]
        for p in report["open_positions"]:
            lines.append(
                f"| {p['symbol']} | {p['side']} | {_fmt(p['entry'], 6)} | "
                f"{_fmt(p['stop_loss'], 6)} | {_fmt(p['take_profit'], 6)} | "
                f"{_fmt(p['risk_amount'])} | {'evet' if p['partial_taken'] else 'hayır'} |")
    else:
        lines.append("_Açık pozisyon yok._")

    lines += ["", "## 6. Son İşlemler", ""]
    if report["recent_trades"]:
        lines += ["| Parite | Yön | Giriş → Çıkış | PnL | R | Sebep |",
                  "|---|---|---|---|---|---|"]
        for t in report["recent_trades"][:12]:
            lines.append(
                f"| {t['symbol']} | {t['side']} | {_fmt(t['entry'], 6)} → "
                f"{_fmt(t['exit'], 6)} | {_fmt(t['pnl'])} | {_fmt(t['r'], 2)} | "
                f"{t['reason']} |")
    else:
        lines.append("_Bu dönemde kapanan işlem yok._")

    lines += ["", "## 7. Kararlar ve Vetolar", ""]
    if report["decisions"]:
        for d in report["decisions"][:10]:
            status = "uygulandı" if d["executed"] else "uygulanmadı"
            veto = f" — veto: {d['veto_reason']}" if d["veto_reason"] else ""
            lines.append(f"- `{d['decision_id']}` {d['symbol']} **{d['action']}** "
                         f"(güven %{_fmt(d['confidence'] * 100, 0)}) {status}{veto}")
    else:
        lines.append("_Kayıtlı konsey kararı yok._")

    if not report["recent_trades"] and not report["open_positions"]:
        lines += ["", "> **İşlem yapılmama gerekçesi:** Tarama ve doğrulama süreçlerinde "
                  "risk/ödül, konsensüs ve doğrulama eşiklerini geçen kurulum oluşmadı. "
                  "İşlem yapmamak da bir karardır."]

    lines += ["", "## 8. Hatalar ve Denetim İzi", ""]
    if report["errors"]:
        for e in report["errors"][:8]:
            lines.append(f"- bot #{e['bot_id']} · {e['message']}")
    else:
        lines.append("_Kayıtlı hata yok._")

    if report["audit"]:
        lines += ["", "**Kritik olaylar:**", ""]
        for a in report["audit"][:8]:
            lines.append(f"- `{a['ts']}` **{a['action']}** ({a['actor']}) — {a['detail']}")

    lines += ["", "---", "", f"> {meta['disclaimer']}", ""]
    return "\n".join(lines)


def save_report(report: dict[str, Any], markdown: str) -> dict[str, str]:
    """Raporu diske yazar ve dosya yollarını döner."""
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    folder = REPORTS_DIR / day
    folder.mkdir(parents=True, exist_ok=True)

    run_id = report["meta"]["run_id"]
    json_path = folder / f"{run_id}.json"
    md_path = folder / f"{run_id}.md"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    log.info("Rapor yazıldı: %s", md_path)

    return {"json": str(json_path), "markdown": str(md_path), "run_id": run_id}


def generate(db: Session, user: User, *, period_days: int = 7) -> dict[str, Any]:
    """Rapor üretir, diske yazar ve içeriği döner."""
    report = build_report(db, user, period_days=period_days)
    markdown = render_markdown(report)
    paths = save_report(report, markdown)
    return {"report": report, "markdown": markdown, "files": paths}


def list_reports(limit: int = 30) -> list[dict[str, Any]]:
    """Kayıtlı raporları listeler (yeniden eskiye)."""
    if not REPORTS_DIR.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(REPORTS_DIR.glob("*/*.md"), reverse=True)[:limit]:
        stat = path.stat()
        items.append({
            "run_id": path.stem,
            "date": path.parent.name,
            "size_kb": round(stat.st_size / 1024, 1),
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        })
    return items


def read_report(date: str, run_id: str) -> dict[str, Any] | None:
    """Kayıtlı raporu okur."""
    safe_date = Path(date).name
    safe_run = Path(run_id).name
    md_path = REPORTS_DIR / safe_date / f"{safe_run}.md"
    json_path = REPORTS_DIR / safe_date / f"{safe_run}.json"
    if not md_path.exists():
        return None
    payload: dict[str, Any] = {"markdown": md_path.read_text(encoding="utf-8")}
    if json_path.exists():
        with contextlib.suppress(json.JSONDecodeError):
            payload["report"] = json.loads(json_path.read_text(encoding="utf-8"))
    return payload
