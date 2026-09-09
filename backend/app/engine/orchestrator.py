"""
MOTOR — 5 KATMANLI BORU HATTININ ORKESTRASYONU
===============================================
Tek bir `run_cycle()` çağrısı şu zinciri uçtan uca çalıştırır:

  Katman 1  Piyasa verisi  →  Katman 2  Deterministik matematik
        →  Katman 2.5  Klasik strateji konsensüsü (AI'sız da çalışır)
        →  Katman 3  Yapay zeka muhakemesi (persona: fon yöneticisi)
        →  Katman 4  Kırılmaz risk kalkanı
        →  Katman 5  İcra + bildirim

Karar mimarileri (`bot.decision_mode`):
  * `algo_only` — Yalnızca kural tabanlı motor. API anahtarı gerekmez, maliyet $0.
  * `ai_only`   — Yalnızca yapay zeka kararı (risk kalkanı yine geçerli).
  * `hybrid`    — YZ ve algoritma AYNI yönü göstermezse işlem açılmaz (varsayılan).
  * `ai_first`  — YZ birincil; hata/kota/JSON sorununda algoritma devralır (kesintisiz).
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..agent.council import (
    ModelCouncil,
    build_council,
    council_models,
    credit_trade_outcome,
    save_decision,
)
from ..core.config import settings
from ..core.creds import read_extra, read_secrets
from ..core.db import session_scope
from ..core.logging import get_logger
from ..core.safety import kill_switch_active, kill_switch_reason, live_authorization_status
from ..layers import notifier
from ..layers.guards import check_guards
from ..layers.l1_market_data import MarketDataError, fetch_ohlcv, fetch_order_book_depth
from ..layers.l2_indicators import build_snapshot, compute_all, technical_bias
from ..layers.l3_llm_gateway import LLMGateway, LLMResult
from ..layers.l4_risk import (
    SizedOrder,
    check_exit,
    check_partial_take_profit,
    check_preconditions,
    effective_min_confidence,
    effective_risk_pct,
    lock_until,
    manage_open_position,
    should_trip_circuit_breaker,
    today_key,
    update_recovery_state,
    validate_and_size,
)
from ..layers.l5_execution import LiveBroker, PaperBroker, build_broker
from ..layers.portfolio_risk import check_portfolio_limits, portfolio_summary
from ..layers.recovery import build_recovery_plan, measure_expectancy
from ..layers.solvency import (
    account_floor_breached,
    clamp_balance,
    floor_message,
)
from ..layers.solvency import check_position as check_solvency
from ..layers.strategies import StrategyEngine
from ..models import (
    Autonomy,
    Bot,
    BotEvent,
    BotStatus,
    Credential,
    EquityPoint,
    Position,
    PositionStatus,
    Side,
    TradingMode,
    User,
)
from .hub import hub

_TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
               "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720,
               "1d": 1440, "1w": 10080}


def _timeframe_minutes(timeframe: str) -> int:
    """Soğuma süresini bar cinsinden hesaplayabilmek için dakika karşılığı."""
    return _TF_MINUTES.get((timeframe or "1h").lower(), 60)



log = get_logger("zumvia.engine")


# --------------------------------------------------------------------------- #
#  Olay kaydı
# --------------------------------------------------------------------------- #


def emit(db: Session, bot: Bot, level: str, category: str, message: str,
         data: dict[str, Any] | None = None) -> None:
    """Olayı veritabanına yazar ve canlı olarak arayüze yayınlar."""
    event = BotEvent(
        bot_id=bot.id, level=level, category=category, message=message,
        data_json=json.dumps(data or {}, ensure_ascii=False, default=str),
    )
    db.add(event)
    db.flush()
    hub.publish(bot.user_id, {
        "type": "event",
        "bot_id": bot.id,
        "bot_name": bot.name,
        "ts": datetime.now(UTC).isoformat(),
        "level": level,
        "category": category,
        "message": message,
        "data": data or {},
    })


def push_state(bot: Bot, payload: dict[str, Any]) -> None:
    hub.publish(bot.user_id, {"type": "state", "bot_id": bot.id, **payload})


# --------------------------------------------------------------------------- #
#  Yardımcılar
# --------------------------------------------------------------------------- #


def _open_positions(db: Session, bot: Bot) -> list[Position]:
    return (
        db.query(Position)
        .filter(Position.bot_id == bot.id, Position.status == PositionStatus.OPEN)
        .all()
    )


# --------------------------------------------------------------------------- #
#  Giriş serileştirme + tekrar-sinyal filtresi (DUPLICATE_ORDER koruması)
# --------------------------------------------------------------------------- #
#
# Videolu deneylerde bile adı geçen klasik arıza: AYNI SİNYALİN İKİ KEZ
# İŞLEME DÖNÜŞMESİ. İki ayrı yoldan olur ve ikisi de burada kapatılır:
#
#   1. EŞZAMANLI ÇİFT TUR — zamanlanmış tur, elle "hemen çalıştır" ve ajan
#      çağrısı üst üste biner; iki tur da "açık pozisyon yok" görür, ikisi
#      de açar. Bot başına kilit (tek süreçli sunucu: uvicorn tek worker,
#      bu yüzden threading kilidi yeterlidir) ikinci girişi kapıda durdurur.
#
#   2. ARDIŞIK TEKRAR SİNYAL — model her tur BUY der, her tur yeni pozisyon
#      açılır; aynı bara ait sinyal yankısı piramit sanılır. Botun o yönde
#      TAZE (bir yoklama dönemi içinde açılmış) pozisyonu/onayı varken aynı
#      yöne giriş, yeni bilgi değil tekrardır ve DUPLICATE koduyla durur.
#      Eski pozisyon + yeni sinyal tekrardan sayılmaz (zaman aşımı).
#
# İkisi de "işlem yok" değil "gerekçeli ret" döner: ret, olay defterine
# koduyla yazılır; ajan/MCP/UI aynı kodu görür.

_ENTRY_LOCKS: dict[int, threading.Lock] = {}
_ENTRY_LOCKS_GUARD = threading.Lock()


def _entry_lock(bot_id: int) -> threading.Lock:
    with _ENTRY_LOCKS_GUARD:
        lock = _ENTRY_LOCKS.get(bot_id)
        if lock is None:
            lock = threading.Lock()
            _ENTRY_LOCKS[bot_id] = lock
            if len(_ENTRY_LOCKS) > 2000:  # silinmiş botların kilidi birikmesin
                _ENTRY_LOCKS.clear()
        return lock


def _side_of(order_side: str) -> Side | None:
    text = (order_side or "").strip().lower()
    if text in ("long", "buy"):
        return Side.LONG
    if text in ("short", "sell"):
        return Side.SHORT
    return None


def duplicate_signal_block(db: Session, bot: Bot, order_side: str,
                           now: datetime | None = None) -> tuple[bool, str]:
    """
    Aynı yönde taze pozisyon/onay varsa (True, gerekçe) döner.

    Tazelik = botun yoklama dönemi: aynı bara ait sinyal yankısı, yeni bilgi
    değildir. Zaman damgası okunamayan satır kanıt sayılmaz ve atlanır
    (yokluğu cezalandırmak, sessiz yalan üretir).
    """
    side = _side_of(order_side)
    if side is None:
        return False, ""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    cooldown_s = max(60, int(getattr(bot, "poll_seconds", 0) or 0))
    fresh = (
        db.query(Position)
        .filter(Position.bot_id == bot.id, Position.side == side,
                Position.status.in_([PositionStatus.OPEN, PositionStatus.PENDING]))
        .order_by(desc(Position.id)).limit(5).all()
    )
    for pos in fresh:
        opened = pos.opened_at
        if opened is None:
            continue
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        age_s = (moment - opened).total_seconds()
        if 0 <= age_s < cooldown_s:
            return True, (
                f"Aynı yönde {int(age_s)} sn önce açılmış pozisyon/onay var "
                f"(#{pos.id}); {cooldown_s} sn içindeki tekrar sinyal yankıdır, "
                "yeni bilgi değil.")
    return False, ""


def _cred(db: Session, cred_id: int | None) -> Credential | None:
    return db.get(Credential, cred_id) if cred_id else None


def _unrealized(positions: list[Position], price: float) -> float:
    total = 0.0
    for p in positions:
        total += ((price - p.entry_price) if p.side == Side.LONG
                  else (p.entry_price - price)) * p.qty
    return total


def _notify(db: Session, bot: Bot, user: User, text: str) -> None:
    cred = _cred(db, bot.telegram_credential_id)
    if cred is None:
        return
    secrets = read_secrets(user, cred)
    extra = read_extra(cred)
    token = secrets.get("token", "")
    chat_id = secrets.get("chat_id") or extra.get("chat_id", "")
    if token and chat_id:
        notifier.send_telegram(token, str(chat_id), text)


def _make_broker(db: Session, bot: Bot, user: User, equity: float):
    """
    Moda göre icra sağlayıcısı.

    Derinlemesine savunma: bot canlı moda alınmış olsa bile, emir anında
    kullanıcının canlı yetkisi geçerli değilse sistem sessizce PAPER'a düşer.
    Süresi dolmuş veya iptal edilmiş bir yetkiyle gerçek emir gönderilemez.
    """
    if bot.mode == TradingMode.LIVE and not settings.force_paper_only:
        status = live_authorization_status(user)
        if not status["authorized"]:
            emit(db, bot, "error", "risk",
                 f"CANLI YETKİ GEÇERSİZ · {status['reason']} → işlem sanal moda düşürüldü.",
                 {"authorization": status})
            return PaperBroker(bot.paper_balance)

        cred = _cred(db, bot.exchange_credential_id)
        secrets = read_secrets(user, cred)
        extra = read_extra(cred)
        return build_broker(
            "live", equity, bot.exchange,
            secrets.get("api_key", ""), secrets.get("secret", ""),
            secrets.get("password", ""), bool(extra.get("sandbox", False)),
        )
    return PaperBroker(bot.paper_balance)


# --------------------------------------------------------------------------- #
#  Pozisyon kapatma
# --------------------------------------------------------------------------- #


def close_position(db: Session, bot: Bot, user: User, position: Position,
                   price: float, reason: str, broker=None) -> Position:
    """Pozisyonu kapatır, PnL'i işler, bakiyeyi günceller, bildirim gönderir."""
    broker = broker or PaperBroker(bot.paper_balance)
    exit_side = "sell" if position.side == Side.LONG else "buy"

    result = broker.market_order(position.symbol, exit_side, position.qty, price)
    fill = result.filled_price if result.ok and result.filled_price > 0 else price
    fee = (result.fee or 0.0) + position.fees

    gross = ((fill - position.entry_price) if position.side == Side.LONG
             else (position.entry_price - fill)) * position.qty
    # Daha önce kısmi kâr alındıysa o da toplam sonuca dahil edilir
    pnl = gross - (result.fee or 0.0) + float(position.realized_partial_pnl or 0.0)

    risk = abs(position.entry_price - (position.initial_stop or position.stop_loss))
    position.exit_price = fill
    position.pnl = pnl
    position.pnl_pct = (pnl / position.notional * 100.0) if position.notional else 0.0
    position.r_multiple = (pnl / position.risk_amount) if position.risk_amount else (
        (gross / (risk * position.qty)) if risk else 0.0
    )
    position.fees = fee
    position.close_reason = reason
    position.status = PositionStatus.CLOSED
    position.closed_at = datetime.now(UTC)

    if bot.mode == TradingMode.PAPER:
        # Sıfır tabanı: negatif bir kâğıt bakiyesi "borçla ticarete devam"
        # gösterir ve kullanıcı gerçekte imkânsız bir toparlanma görür.
        clamped, hit_floor = clamp_balance(round(bot.paper_balance + pnl, 8))
        bot.paper_balance = clamped
        if hit_floor:
            emit(db, bot, "error", "risk",
                 "KASA SIFIRLANDI · Bu işlem bakiyeyi sıfırın altına "
                 "götürüyordu; bakiye sıfırda tutuldu. Gerçek bir hesapta bu "
                 "noktada pozisyonunuz zorla kapatılmış olurdu.",
                 {"position_id": position.id, "pnl": round(pnl, 4)})

    # Üst üste zarar sayacı → toparlanma motorunu besler
    bot.consecutive_losses = 0 if pnl > 0 else bot.consecutive_losses + 1

    # Kararı veren modellerin siciline gerçek sonuç işlenir: doğru karar veren
    # modelin konseydeki oy ağırlığı zamanla artar, yanılanınki azalır.
    try:
        models = json.loads(position.decision_models_json or "[]")
        if models:
            credit_trade_outcome(db, bot.user_id, models, position.r_multiple)
    except Exception as exc:  # noqa: BLE001
        # Model sicili güncellenemedi. Ticareti etkilemez ama sessizce
        # kaybolmamalı: sicil bozulursa konsey ağırlıkları yanlış hesaplanır.
        log.info("model sicili güncellenemedi: %s", exc)

    level = "success" if pnl >= 0 else "warn"
    emit(db, bot, level, "exec",
         f"POZİSYON KAPANDI · {position.symbol} {position.side.value.upper()} · "
         f"PnL {pnl:+.2f} ({position.pnl_pct:+.2f}%) · {position.r_multiple:+.2f}R · {reason}",
         {"position_id": position.id, "pnl": round(pnl, 4), "reason": reason,
          "exit": fill, "balance": bot.paper_balance})

    _notify(db, bot, user, notifier.fmt_close(
        bot.name, position.symbol, position.side.value, bot.mode.value,
        position.entry_price, fill, pnl, position.pnl_pct, position.r_multiple,
        reason, bot.paper_balance,
    ))
    return position


def take_partial_profit(db: Session, bot: Bot, user: User, position: Position,
                        price: float, fraction: float, note: str,
                        broker=None) -> float:
    """
    Pozisyonun bir kısmını kapatır (scale-out) ve stopu başabaşa çeker.

    Profesyonel masaların standart uygulaması: kârın bir kısmı realize edilir,
    kalan pozisyon **risksiz** hale gelir ve büyük hareketi yakalamak için
    iz süren stopla açık kalır.
    """
    broker = broker or PaperBroker(bot.paper_balance)
    close_qty = round(position.qty * fraction, 12)
    if close_qty <= 0:
        return 0.0

    exit_side = "sell" if position.side == Side.LONG else "buy"
    result = broker.market_order(position.symbol, exit_side, close_qty, price)
    fill = result.filled_price if result.ok and result.filled_price > 0 else price

    gross = ((fill - position.entry_price) if position.side == Side.LONG
             else (position.entry_price - fill)) * close_qty
    realized = gross - (result.fee or 0.0)

    if not position.original_qty:
        position.original_qty = position.qty
    position.qty = round(position.qty - close_qty, 12)
    position.notional = position.qty * position.entry_price
    position.realized_partial_pnl = float(position.realized_partial_pnl or 0.0) + realized
    position.partial_taken = True
    position.fees = float(position.fees or 0.0) + (result.fee or 0.0)

    # Kalan pozisyonu risksiz yap: stop başabaşa (komisyon payıyla) çekilir
    breakeven = position.entry_price * (1.0005 if position.side == Side.LONG else 0.9995)
    if position.side == Side.LONG:
        position.stop_loss = max(position.stop_loss, breakeven)
    else:
        position.stop_loss = min(position.stop_loss, breakeven)

    if bot.mode == TradingMode.PAPER:
        bot.paper_balance = round(bot.paper_balance + realized, 8)

    db.flush()
    emit(db, bot, "success", "exec",
         f"KISMİ KÂR · {position.symbol} · %{fraction * 100:.0f} kapatıldı @ {fill:.6g} · "
         f"+{realized:.2f} · kalan pozisyon risksiz (stop başabaşta)",
         {"position_id": position.id, "closed_qty": close_qty,
          "realized": round(realized, 4), "remaining_qty": position.qty,
          "new_stop": position.stop_loss})
    return realized


def close_all(db: Session, bot: Bot, user: User, price: float, reason: str,
              broker=None) -> int:
    positions = _open_positions(db, bot)
    for p in positions:
        close_position(db, bot, user, p, price, reason, broker)
    return len(positions)


# --------------------------------------------------------------------------- #
#  Karar üretimi (Katman 2.5 + Katman 3)
# --------------------------------------------------------------------------- #


def _build_llm(db: Session, bot: Bot, user: User) -> LLMGateway | None:
    cred = _cred(db, bot.llm_credential_id)
    if cred is None:
        return None
    secrets = read_secrets(user, cred)
    extra = read_extra(cred)
    try:
        return LLMGateway(
            provider=cred.provider,
            api_key=secrets.get("api_key", ""),
            model=bot.llm_model or extra.get("model", ""),
            base_url=extra.get("base_url", ""),
            temperature=float(extra.get("temperature", 0.2)),
        )
    except ValueError as exc:
        log.warning("LLM yapılandırması geçersiz: %s", exc)
        return None


def _resolve_council(db: Session, bot: Bot, user: User) -> tuple[str, list]:
    """
    Konsey üyelerini belirler.

    Kullanıcı yalnızca **modu** seçer (`solo` / `council` / `strict` / `auto`);
    hangi modelin hangi rolde çalışacağını sistem kendisi kurar:

      * Kayıtlı tüm yapay zeka anahtarları analist olur.
      * `strict` modda en güçlü (sicili en iyi) model risk eleştirmeni,
        ikincisi hakem olarak atanır.
      * `auto` modda: tek model varsa solo, iki+ model varsa council.
    """
    from ..models import CredentialKind  # noqa: PLC0415

    configured = json.loads(bot.council_json or "[]")
    if configured:
        members = build_council(db, user, configured)
    else:
        credentials = (db.query(Credential)
                       .filter(Credential.user_id == user.id,
                               Credential.kind == CredentialKind.LLM)
                       .order_by(Credential.id).all())
        entries: list[dict[str, Any]] = []
        for cred in credentials:
            model = read_extra(cred).get("model", "")
            if cred.id == bot.llm_credential_id and bot.llm_model:
                model = bot.llm_model
            # Model adı tanımsız anahtarlar konseye alınmaz: yanıt veremeyecek
            # bir üye mutabakat oranını haksız yere düşürür.
            if model.strip():
                entries.append({"credential_id": cred.id, "model": model.strip(),
                                "role": "analyst"})
        members = build_council(db, user, entries)

    mode = (bot.council_mode or "auto").lower()
    analysts = [m for m in members if m.role == "analyst"]

    if mode == "auto":
        mode = "council" if len(analysts) >= 2 else "solo"

    if mode == "strict" and analysts:
        # En yüksek sicilli model eleştirmen, ikincisi hakem olur
        ranked = sorted(analysts, key=lambda m: -m.weight)
        if len(ranked) >= 2:
            ranked[0].role = "risk_critic"
            if len(ranked) >= 3:
                ranked[1].role = "arbiter"
        else:
            # Tek model varsa aynı model ikinci kez eleştirmen rolüyle çağrılır
            only = ranked[0]
            members.append(type(only)(
                label=f"{only.label} (risk)", credential_id=only.credential_id,
                provider=only.provider, model=only.model, role="risk_critic",
                weight=only.weight,
            ))

    return mode, members


def _council_decision(db: Session, bot: Bot, user: User, snapshot: dict[str, Any],
                      context: dict[str, Any], consensus, mode: str,
                      members: list) -> dict[str, Any]:
    """Çoklu model konseyini çalıştırır ve karar sözlüğü döner."""
    council = ModelCouncil(db, user, members, mode=mode)
    analysts = [m.model for m in members if m.role == "analyst"]

    emit(db, bot, "info", "ai",
         f"KONSEY TOPLANDI · {len(analysts)} analist paralel çalışıyor: "
         f"{', '.join(analysts[:4])}" + (" …" if len(analysts) > 4 else ""),
         {"mode": mode, "members": [m.to_dict() for m in members]})

    verdict = council.deliberate(
        snapshot, context,
        recovery_mode=bot.recovery_mode,
        extra_rules=bot.strategy_notes or "",
        algo_action=consensus.action if mode == "strict" else "WAIT",
    )

    for opinion in verdict.opinions:
        if opinion.ok:
            emit(db, bot, "info", "ai",
                 f"   {opinion.member.model} ({opinion.member.weight:.2f} ağırlık): "
                 f"{opinion.action} · güven %{opinion.confidence * 100:.0f} · "
                 f"{opinion.reasoning[:110]}",
                 {"opinion": opinion.to_dict()})
        else:
            emit(db, bot, "warn", "ai",
                 f"   {opinion.member.model} yanıt vermedi: {opinion.error[:120]}", {})

    if verdict.critique:
        level = "error" if verdict.critique.get("verdict") == "VETO" else "info"
        emit(db, bot, level, "risk",
             f"RİSK KOMİTESİ ({verdict.critique.get('model', '')}): "
             f"{verdict.critique.get('verdict')} — {verdict.critique.get('reason', '')[:180]}",
             {"critique": verdict.critique})

    if verdict.arbitration:
        emit(db, bot, "info", "ai",
             f"HAKEM ({verdict.arbitration.get('model', '')}): "
             f"{verdict.arbitration.get('action')} — "
             f"{verdict.arbitration.get('reasoning', '')[:160]}",
             {"arbitration": verdict.arbitration})

    emit(db, bot, "success" if verdict.action != "WAIT" else "info", "ai",
         f"KONSEY KARARI · {verdict.action} · mutabakat %{verdict.agreement_pct:.0f} "
         f"({verdict.responded}/{verdict.participating} yanıt)",
         {"verdict": verdict.to_dict()})

    return {
        "action": verdict.action,
        "confidence": verdict.confidence,
        "stop_loss": verdict.stop_loss,
        "take_profit": verdict.take_profit,
        "reasoning": verdict.reasoning or verdict.veto_reason,
        "source": f"KONSEY ({verdict.responded} model, %{verdict.agreement_pct:.0f} mutabakat)",
        "consensus": consensus.to_dict(),
        "council_verdict": verdict,
        "decision_id": verdict.decision_id,
        "decision_models": council_models(verdict),
    }


def decide(db: Session, bot: Bot, user: User, df: pd.DataFrame, snapshot: dict[str, Any],
           context: dict[str, Any]) -> dict[str, Any]:
    """
    Karar mimarisine göre nihai öneriyi üretir.
    Dönen sözlük: action, confidence, stop_loss, take_profit, reasoning, source
    """
    engine = StrategyEngine(json.loads(bot.strategies_json or "[]") or None, bot.min_agree)
    consensus = engine.run(df)
    bias = technical_bias_from_snapshot(snapshot)

    emit(db, bot, "info", "math",
         f"ALGORİTMA MOTORU · {consensus.summary} · teknik skor {bias['score']:+d}",
         {"consensus": consensus.to_dict(), "bias": bias})

    algo_decision = {
        "action": consensus.action,
        "confidence": consensus.confidence,
        "stop_loss": consensus.stop_loss,
        "take_profit": consensus.take_profit,
        "reasoning": consensus.summary + " | " + "; ".join(
            s.reason for s in consensus.signals if s.action == consensus.action
        )[:600],
        "source": "ALGORİTMA",
        "consensus": consensus.to_dict(),
    }

    if bot.decision_mode == "algo_only":
        return algo_decision

    # --- Çoklu model konseyi (kullanıcının seçtiği moda göre) ---
    mode, members = _resolve_council(db, bot, user)
    if mode in ("council", "strict") and any(m.role == "analyst" for m in members):
        decision = _council_decision(db, bot, user, snapshot, context,
                                     consensus, mode, members)
        if decision["action"] in ("BUY", "SELL") and bot.decision_mode == "hybrid" \
                and consensus.action != decision["action"]:
            emit(db, bot, "warn", "risk",
                 f"HİBRİT VETO · Konsey {decision['action']} dedi, algoritmik motor "
                 f"{consensus.action}. Mutabakat yok → işlem açılmadı.",
                 {"council": decision["action"], "algo": consensus.action})
            return {**decision, "action": "WAIT",
                    "reasoning": f"Konsey ({decision['action']}) ile algoritmik motor "
                                 f"({consensus.action}) çelişiyor. Disiplin gereği beklemede.",
                    "source": "HİBRİT-VETO"}
        if decision["action"] == "WAIT" and bot.decision_mode == "ai_first" \
                and not decision.get("council_verdict", None):
            return algo_decision
        return decision

    # --- Tek model (solo) ---
    gateway = _build_llm(db, bot, user)
    if gateway is None:
        emit(db, bot, "warn", "ai",
             "Yapay zeka bağlantısı yapılandırılmamış — algoritmik motor devraldı.",
             {"fallback": True})
        return algo_decision if bot.decision_mode != "ai_only" else {
            **algo_decision, "action": "WAIT",
            "reasoning": "AI-only modda LLM yapılandırılmamış — işlem yok.",
            "source": "YOK",
        }

    # Yapay zeka, algoritmanın çalışmasını GİRDİ olarak okur.
    context = {**context, "algoritmik_motor_sonucu": consensus.to_dict(),
               "kural_tabanli_teknik_skor": bias}

    result: LLMResult = gateway.decide(
        snapshot, context, recovery_mode=bot.recovery_mode,
        extra_rules=bot.strategy_notes or "",
    )

    if not result.ok or result.decision is None:
        emit(db, bot, "error", "ai",
             f"YAPAY ZEKA BAŞARISIZ · {result.error}",
             {"model": result.model, "provider": result.provider,
              "raw": result.raw_text[:400]})
        if bot.decision_mode in ("ai_first", "hybrid"):
            emit(db, bot, "info", "ai",
                 "FAIL-SAFE: Algoritmik motor devraldı, sistem çalışmaya devam ediyor.",
                 {"fallback": True})
            return algo_decision
        return {"action": "WAIT", "confidence": 0.0, "stop_loss": 0.0, "take_profit": 0.0,
                "reasoning": f"Model hatası — fail-safe iptal: {result.error}",
                "source": "FAIL-SAFE"}

    d = result.decision
    emit(db, bot, "info", "ai",
         f"YAPAY ZEKA KARARI · {d.action} · güven %{d.confidence * 100:.0f} · "
         f"{result.model} ({result.latency_ms} ms)",
         {"action": d.action, "confidence": d.confidence, "stop_loss": d.stop_loss,
          "take_profit": d.take_profit, "reasoning": d.reasoning,
          "risk_note": d.risk_note, "model": result.model,
          "latency_ms": result.latency_ms, "usage": result.usage})

    ai_decision = {
        "action": d.action, "confidence": d.confidence,
        "stop_loss": d.stop_loss, "take_profit": d.take_profit,
        "reasoning": d.reasoning, "risk_note": d.risk_note,
        "source": f"YZ:{result.model}", "consensus": consensus.to_dict(),
    }

    if bot.decision_mode in ("ai_only", "ai_first"):
        return ai_decision

    # --- HİBRİT: mutabakat şartı ---
    if d.action in ("BUY", "SELL"):
        if consensus.action != d.action:
            emit(db, bot, "warn", "risk",
                 f"HİBRİT VETO · Yapay zeka {d.action} dedi ancak algoritmik motor "
                 f"{consensus.action} gösteriyor. Mutabakat yok → işlem açılmadı.",
                 {"ai": d.action, "algo": consensus.action})
            return {**ai_decision, "action": "WAIT",
                    "reasoning": f"Mutabakat yok (YZ:{d.action} / Algo:{consensus.action}). "
                                 f"Disiplin gereği beklemede.",
                    "source": "HİBRİT-VETO"}
        # Mutabakat var → güven ortalaması, stop seviyeleri muhafazakâr seçilir
        merged_conf = round((d.confidence + consensus.confidence) / 2 + 0.05, 4)
        stop = (max(d.stop_loss, consensus.stop_loss) if d.action == "BUY"
                else min(d.stop_loss, consensus.stop_loss)) or d.stop_loss
        target = (min(d.take_profit, consensus.take_profit) if d.action == "BUY"
                  else max(d.take_profit, consensus.take_profit)) or d.take_profit
        emit(db, bot, "success", "risk",
             f"MUTABAKAT · Yapay zeka ve {consensus.agree} strateji aynı yönde: {d.action}",
             {"confidence": merged_conf})
        return {**ai_decision, "confidence": min(0.99, merged_conf),
                "stop_loss": stop, "take_profit": target,
                "source": f"HİBRİT ({result.model} + {consensus.agree} strateji)"}

    return ai_decision


def technical_bias_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """`technical_bias` sarmalayıcısı — snapshot sözlüğünden çalışır."""
    from ..layers.l2_indicators import MarketSnapshot  # noqa: PLC0415

    snap = MarketSnapshot(
        symbol=snapshot.get("symbol", ""), timeframe=snapshot.get("timeframe", ""),
        price=snapshot.get("price", 0.0), indicators=snapshot.get("indicators", {}),
        structure=snapshot.get("structure", {}), regime=snapshot.get("regime", ""),
    )
    return technical_bias(snap)


# --------------------------------------------------------------------------- #
#  Ana döngü
# --------------------------------------------------------------------------- #


def run_cycle(bot_id: int) -> dict[str, Any]:
    """Bir botun tek turunu çalıştırır. Zamanlayıcı tarafından periyodik çağrılır."""
    with session_scope() as db:
        bot = db.get(Bot, bot_id)
        if bot is None:
            return {"ok": False, "error": "Bot bulunamadı."}
        user = db.get(User, bot.user_id)
        if user is None:
            return {"ok": False, "error": "Kullanıcı bulunamadı."}

        now = datetime.now(UTC)
        bot.last_run_at = now

        # ---------- KATMAN 1: Veri ---------- #
        try:
            raw = fetch_ohlcv(bot.market, bot.exchange, bot.symbol, bot.timeframe, 320)
        except MarketDataError as exc:
            emit(db, bot, "error", "data", f"VERİ HATASI · {exc}", {})
            return {"ok": False, "error": str(exc)}

        if len(raw) < 60:
            emit(db, bot, "warn", "data",
                 f"Yetersiz mum verisi ({len(raw)}) — bu tur atlandı.", {})
            return {"ok": False, "error": "insufficient_data"}

        # ---------- KATMAN 2: Matematik ---------- #
        df = compute_all(raw)
        last = df.iloc[-1]
        price = float(last["close"])
        atr_v = float(last["atr_14"]) if pd.notna(last.get("atr_14")) else 0.0
        snap = build_snapshot(df, bot.symbol, bot.timeframe)
        snapshot = snap.to_dict()

        depth = fetch_order_book_depth(bot.market, bot.exchange, bot.symbol)
        if depth.get("available"):
            snapshot["order_book"] = depth

        emit(db, bot, "info", "data",
             f"VERİ · {bot.symbol} {bot.timeframe} · fiyat {price:.6g} · "
             f"RSI {snapshot['indicators'].get('rsi_14', 0):.1f} · "
             f"ATR% {snapshot['indicators'].get('atr_percent', 0):.2f} · {snap.regime}",
             {"price": price, "regime": snap.regime,
              "indicators": snapshot["indicators"]})

        broker = _make_broker(db, bot, user, bot.paper_balance)

        # ---------- Açık pozisyon yönetimi ---------- #
        positions = _open_positions(db, bot)
        bar_high, bar_low = float(last["high"]), float(last["low"])

        for pos in list(positions):
            hit = check_exit(pos, bar_high, bar_low, price)
            if hit:
                reason, exit_price = hit
                close_position(db, bot, user, pos, exit_price, reason, broker)
                continue

            # --- Kısmi kâr alma: kazanan işlemin bir kısmını realize et ---
            partial = check_partial_take_profit(bot, pos, price)
            if partial:
                fraction, note = partial
                take_partial_profit(db, bot, user, pos, price, fraction, note, broker)

            new_stop, note = manage_open_position(bot, pos, price, atr_v)
            if new_stop is not None:
                pos.stop_loss = new_stop
                emit(db, bot, "info", "risk",
                     f"STOP GÜNCELLENDİ · {pos.symbol} → {new_stop:.6g} · {note}",
                     {"position_id": pos.id, "stop_loss": new_stop})

        positions = _open_positions(db, bot)

        # ---------- Kasa ve gün penceresi ---------- #
        balance = bot.paper_balance
        equity = round(balance + _unrealized(positions, price), 8)

        if bot.day_key != today_key(now):
            bot.day_key = today_key(now)
            bot.day_start_equity = equity
            bot.day_trades = 0
            emit(db, bot, "info", "engine",
                 f"YENİ İŞLEM GÜNÜ · Başlangıç kasası {equity:.2f}", {"equity": equity})

        bot.peak_equity = max(bot.peak_equity, equity)

        db.add(EquityPoint(bot_id=bot.id, equity=equity, balance=balance))

        # ---------- Devre kesici ---------- #
        trip, message = should_trip_circuit_breaker(bot, equity, now)
        if trip:
            closed = close_all(db, bot, user, price, "CIRCUIT_BREAKER", broker)
            bot.locked_until = lock_until(now=now)
            bot.lock_reason = message
            bot.status = BotStatus.LOCKED
            emit(db, bot, "error", "risk", f"{message} ({closed} pozisyon kapatıldı)",
                 {"equity": equity, "locked_until": bot.locked_until.isoformat()})
            _notify(db, bot, user, notifier.fmt_circuit_breaker(
                bot.name, message, equity, settings.circuit_breaker_lock_hours))
            push_state(bot, {"status": "locked", "equity": equity})
            return {"ok": True, "action": "CIRCUIT_BREAKER", "equity": equity}

        # ---------- KASA TABANI: erimeyi durdur ---------- #
        #
        # Devre kesici GÜNLÜK kaybı sınırlar; bu ise TOPLAM erimeyi. İkisi
        # farklı şeyler: her gün sınırın hemen altında kaybeden bir sistem
        # devre kesiciyi hiç tetiklemeden hesabı bitirebilir.
        if account_floor_breached(bot.initial_balance, equity):
            closed = close_all(db, bot, user, price, "ACCOUNT_FLOOR", broker)
            message = floor_message(bot.initial_balance, equity)
            bot.locked_until = lock_until(now=now)
            bot.lock_reason = message
            bot.status = BotStatus.LOCKED
            emit(db, bot, "error", "risk",
                 f"{message} ({closed} pozisyon kapatıldı)",
                 {"equity": equity, "initial": bot.initial_balance})
            _notify(db, bot, user, message)
            push_state(bot, {"status": "locked", "equity": equity})
            return {"ok": True, "action": "ACCOUNT_FLOOR", "equity": equity}

        # ---------- Toparlanma planı ---------- #
        _, recovery_msg = update_recovery_state(bot, equity)
        closed_history = (db.query(Position)
                          .filter(Position.bot_id == bot.id,
                                  Position.status == PositionStatus.CLOSED)
                          .order_by(Position.id.desc()).limit(60).all())
        plan = build_recovery_plan(bot, equity,
                                   expectancy_r=measure_expectancy(closed_history))
        if recovery_msg:
            emit(db, bot, "warn", "risk", recovery_msg,
                 {"recovery_mode": bot.recovery_mode, "plan": plan.to_dict()})
            _notify(db, bot, user, notifier.fmt_recovery(bot.name, recovery_msg))
        if plan.phase.id != "normal":
            emit(db, bot, "info", "risk", f"TOPARLANMA PLANI · {plan.headline}",
                 {"plan": plan.to_dict()})

        # ---------- Karar bağlamı ---------- #
        context = {
            "kasa_bakiyesi": round(balance, 2),
            "guncel_ozkaynak": round(equity, 2),
            "baslangic_bakiyesi": bot.initial_balance,
            "zirve_ozkaynak": round(bot.peak_equity, 2),
            "gunluk_degisim_yuzde": round(
                (equity - bot.day_start_equity) / bot.day_start_equity * 100.0, 3
            ) if bot.day_start_equity else 0.0,
            "toplam_getiri_yuzde": round(
                (equity - bot.initial_balance) / bot.initial_balance * 100.0, 3
            ) if bot.initial_balance else 0.0,
            "islem_basina_risk_yuzde": effective_risk_pct(bot),
            "gereken_min_guven": effective_min_confidence(bot),
            "gereken_min_risk_odul": max(bot.min_rr, settings.hard_min_rr_ratio),
            "toparlanma_plani": plan.to_dict(),
            "portfoy_riski": portfolio_summary(positions, equity),
            "ust_uste_zarar": bot.consecutive_losses,
            "bugunku_islem_sayisi": bot.day_trades,
            "acik_pozisyonlar": [
                {
                    "yon": p.side.value, "giris": p.entry_price, "stop": p.stop_loss,
                    "hedef": p.take_profit,
                    "anlik_kar_zarar": round(
                        ((price - p.entry_price) if p.side == Side.LONG
                         else (p.entry_price - price)) * p.qty, 4),
                    "acik_R": round(
                        (((price - p.entry_price) if p.side == Side.LONG
                          else (p.entry_price - price)) * p.qty) / p.risk_amount, 2
                    ) if p.risk_amount else 0.0,
                }
                for p in positions
            ],
            "short_izinli": bot.allow_short,
        }

        # ---------- Karar ---------- #
        decision = decide(db, bot, user, df, snapshot, context)
        action = (decision.get("action") or "WAIT").upper()

        # CLOSE: yapay zeka tezin bozulduğunu söylüyor
        if action == "CLOSE" and positions:
            for pos in positions:
                close_position(db, bot, user, pos, price, "AI_CLOSE", broker)
            push_state(bot, {"equity": equity, "balance": bot.paper_balance})
            return {"ok": True, "action": "CLOSE", "equity": equity}

        if action not in ("BUY", "SELL"):
            emit(db, bot, "info", "engine",
                 f"KARAR: BEKLE · {decision.get('reasoning', '')[:220]}",
                 {"source": decision.get("source", "")})
            push_state(bot, {"equity": equity, "balance": bot.paper_balance,
                             "price": price})
            return {"ok": True, "action": "WAIT", "equity": equity}

        # ---------- KILL SWITCH: yeni risk almayı durdurur ---------- #
        if kill_switch_active():
            emit(db, bot, "error", "risk",
                 f"KILL SWITCH AKTİF · {kill_switch_reason()} "
                 "Yeni pozisyon açılmıyor; mevcut pozisyonlar izlenmeye devam ediyor.",
                 {"kill_switch": True})
            return {"ok": True, "action": "KILL_SWITCH", "reason": kill_switch_reason()}

        # ---------- SİSTEM ÖNLEMLERİ (zayıf yanı kapatan filtreler) ---------- #
        # Sistemin kendi zaafına karşı aldığı önlemler burada uygulanır: yatay
        # piyasada trend sistemi, trendde bant sistemi, hacimsizken kırılım
        # sistemi susar. Reddedilirse pozisyon AÇILMAZ.
        guards = {}
        try:
            guards = json.loads(bot.guards_json or "{}")
        except (TypeError, ValueError):
            guards = {}

        # Denetim KOŞULSUZ çalışır.
        #
        # Burada eskiden `if guards:` vardı: koruma tanımlanmamış bir bot tüm
        # önlem katmanını atlıyordu. Canlı koşuda sonucu görüldü — bot stop
        # oldu ve AYNI TURDA aynı yere yeniden girdi, çünkü zarar sonrası
        # soğuma hiç sorulmadı. `check_guards` boş sözlüğe taban değerleri
        # kendisi uygular; koşul, o tabanın çalışmasını da engelliyordu.
        last_loss = (db.query(Position)
                     .filter(Position.bot_id == bot.id,
                             Position.status == PositionStatus.CLOSED,
                             Position.pnl < 0)
                     .order_by(desc(Position.closed_at)).first())
        guard_verdict = check_guards(
            guards, df, action=action,
            spread_pct=depth.get("spread_pct") if isinstance(depth, dict) else None,
            last_loss_at=last_loss.closed_at if last_loss else None,
            timeframe_minutes=_timeframe_minutes(bot.timeframe),
            trades_today=bot.day_trades if bot.day_key == today_key(now) else 0,
        )
        if not guard_verdict.allowed:
            emit(db, bot, "info", "risk", f"SİSTEM ÖNLEMİ · {guard_verdict.reason}",
                 {"code": guard_verdict.code, **guard_verdict.details})
            return {"ok": True, "action": "GUARDED", "reason": guard_verdict.reason}

        # ---------- KATMAN 4: Risk kalkanı ---------- #
        gate = check_preconditions(bot, equity, len(positions), now)
        if not gate.allowed:
            emit(db, bot, "warn", "risk", f"RİSK KAPISI · {gate.reason}",
                 {"code": gate.code, **gate.details})
            return {"ok": True, "action": "BLOCKED", "reason": gate.reason}

        verdict, order = validate_and_size(
            bot, action, float(decision.get("confidence", 0.0)), price,
            float(decision.get("stop_loss", 0.0)), float(decision.get("take_profit", 0.0)),
            equity, atr_v,
        )
        if not verdict.allowed or order is None:
            emit(db, bot, "warn", "risk", f"RİSK KALKANI REDDETTİ · {verdict.reason}",
                 {"code": verdict.code, **verdict.details})
            return {"ok": True, "action": "REJECTED", "reason": verdict.reason}

        emit(db, bot, "success", "risk",
             f"RİSK ONAYI · {verdict.reason} · lot {order.qty:.8g}",
             {"qty": order.qty, "risk_amount": order.risk_amount, "rr": order.rr_ratio})

        # ---------- ÖDEME GÜCÜ: stop hiç çalışmasa ne olurdu? ---------- #
        #
        # Risk kalkanı stop'un ÇALIŞACAĞINI varsayar ve çoğu zaman haklıdır.
        # Tam olarak bu yüzden tehlikelidir: hesabı bitiren, stop'un
        # çalışmadığı nadir gündür (boşluk, likidite kopuşu, işlem durdurma).
        # Kısa pozisyonda o günün kaybı teorik olarak sınırsızdır ve bakiyenin
        # altına inebilir — bakiyenin altına inen hesap borçtur.
        solvency_verdict = check_solvency(
            equity, order.side, order.entry, order.qty, positions)
        if not solvency_verdict.allowed:
            emit(db, bot, "warn", "risk",
                 f"ÖDEME GÜCÜ KAPISI · {solvency_verdict.reason}",
                 solvency_verdict.to_dict())
            return {"ok": True, "action": "SOLVENCY_BLOCKED",
                    "reason": solvency_verdict.reason}

        # ---------- TEKRAR SİNYAL FİLTRESİ: duplicate order burada ölür ---------- #
        #
        # Portföy kapısından ÖNCE sorulur: taze tekrarın teşhisi "yankı"dır,
        # "küme riski" değil. İkisi de engeller; ama yanlış teşhis, yanlış
        # düzeltmeye götürür (kullanıcı kümeyi dağıtmaya çalışır, oysa tek
        # tur beklemek yeterdi).
        dup_blocked, dup_reason = duplicate_signal_block(db, bot, order.side, now)
        if dup_blocked:
            emit(db, bot, "info", "risk", f"TEKRAR SİNYAL · {dup_reason}",
                 {"code": "DUPLICATE_SIGNAL"})
            return {"ok": True, "action": "DUPLICATE", "reason": dup_reason}

        # ---------- PORTFÖY KAPISI: küme riski ve toplam ısı ---------- #
        all_open = (db.query(Position)
                    .join(Bot, Position.bot_id == Bot.id)
                    .filter(Bot.user_id == user.id,
                            Position.status == PositionStatus.OPEN).all())
        total_equity = sum(b.paper_balance for b in
                           db.query(Bot).filter(Bot.user_id == user.id).all()) or equity

        portfolio_gate = check_portfolio_limits(
            equity=total_equity, new_risk_amount=order.risk_amount,
            open_positions=all_open, new_symbol=bot.symbol, new_side=order.side,
            max_heat_pct=bot.max_portfolio_heat_pct,
            spread_pct=depth.get("spread_pct") if isinstance(depth, dict) else None,
            max_spread_pct=bot.max_spread_pct,
        )
        if not portfolio_gate.allowed:
            emit(db, bot, "warn", "risk",
                 f"PORTFÖY KAPISI · {portfolio_gate.reason}",
                 {"code": portfolio_gate.code, **portfolio_gate.details})
            return {"ok": True, "action": "PORTFOLIO_BLOCKED",
                    "reason": portfolio_gate.reason}

        # ---------- KARAR DEFTERİ (audit) ---------- #
        run_id = uuid.uuid4().hex[:12]
        council_verdict = decision.get("council_verdict")
        if council_verdict is not None:
            save_decision(
                db, user, council_verdict, run_id=run_id, bot_id=bot.id,
                symbol=bot.symbol, timeframe=bot.timeframe,
                evidence={"snapshot": snapshot, "consensus": decision.get("consensus"),
                          "risk": verdict.to_dict(), "portfolio": portfolio_gate.to_dict()},
                executed=True,
            )

        # ---------- MANUEL mod: onaya düşür ---------- #
        if bot.autonomy == Autonomy.MANUAL:
            pending = Position(
                bot_id=bot.id, symbol=bot.symbol,
                side=Side.LONG if order.side == "long" else Side.SHORT,
                status=PositionStatus.PENDING, mode=bot.mode, qty=order.qty,
                entry_price=order.entry, stop_loss=order.stop_loss,
                take_profit=order.take_profit, initial_stop=order.stop_loss,
                risk_amount=order.risk_amount, notional=order.notional,
                confidence=float(decision.get("confidence", 0.0)),
                reasoning=str(decision.get("reasoning", ""))[:1000],
                snapshot_json=json.dumps(snapshot, ensure_ascii=False, default=str),
            )
            db.add(pending)
            db.flush()
            emit(db, bot, "trade", "exec",
                 f"ONAY BEKLİYOR · {action} {bot.symbol} @ {price:.6g}",
                 {"position_id": pending.id})
            _notify(db, bot, user, notifier.fmt_signal(
                bot.name, bot.symbol, action, float(decision.get("confidence", 0.0)),
                str(decision.get("reasoning", ""))))
            return {"ok": True, "action": "PENDING", "position_id": pending.id}

        # ---------- KATMAN 5: İcra ---------- #
        position = execute_entry(db, bot, user, broker, order, decision, snapshot,
                                 df=df, depth=depth)
        if position is None:
            return {"ok": False, "error": "execution_failed"}

        push_state(bot, {"equity": equity, "balance": bot.paper_balance, "price": price})
        return {"ok": True, "action": action, "position_id": position.id}



def _execution_plan(bot: Bot, order: SizedOrder, df: Any,
                    depth: dict[str, Any] | None):
    """
    Emrin piyasaya sığıp sığmadığını ölçer.

    Veri yoksa `None` döner ve emir tek parça gider — ölçemediğimiz için
    bölmek, ölçemediğimiz için bölmemekten daha iyi değildir.
    """
    if df is None or order.entry <= 0 or order.qty <= 0:
        return None
    try:
        from ..layers.capital import liquidity_profile, plan_execution  # noqa: PLC0415

        profile = liquidity_profile(
            df, bot.symbol, bot.timeframe,
            spread_pct=depth.get("spread_pct") if isinstance(depth, dict) else None,
        )
        return plan_execution(order.qty * order.entry, profile,
                              timeframe=bot.timeframe)
    except Exception as exc:  # noqa: BLE001 — plan çıkarılamazsa tek parça
        log.info("yürütme planı çıkarılamadı: %s", exc)
        return None


def execute_entry(db: Session, bot: Bot, user: User, broker, order: SizedOrder,
                  decision: dict[str, Any], snapshot: dict[str, Any],
                  df: Any = None, depth: dict[str, Any] | None = None) -> Position | None:
    """
    Onaylanmış emri iletir ve pozisyonu kaydeder.

    Emir, enstrümanın taşıyabileceğinden büyükse tek seferde gönderilmez:
    parçalı yürütücüye (TWAP) devredilir. Bu durumda ilk parça hemen gider,
    kalanı zamanlayıcı gönderir ve pozisyon dolum ilerledikçe güncellenir.

    Eşzamanlı çift çağrı kilitte serileşir; kilidi alamayan tur emir
    İLETMEZ (None + kodlu olay). Kilitten geçen tur, tekrar-sinyal filtresini
    YENİDEN okur — kapı kontrolüyle icra arasına giren tura karşı (TOCTOU)
    son söz buradadır.
    """
    lock = _entry_lock(bot.id)
    if not lock.acquire(blocking=False):
        emit(db, bot, "warn", "exec",
             "GİRİŞ KİLİTLİ · Bu bot için bir emir iletimi sürüyor; "
             "üst üste binen tur emir iletmedi (duplicate koruması).",
             {"code": "ENTRY_IN_PROGRESS"})
        return None
    try:
        dup_blocked, dup_reason = duplicate_signal_block(
            db, bot, order.side, datetime.now(UTC))
        if dup_blocked:
            emit(db, bot, "info", "exec", f"TEKRAR SİNYAL · {dup_reason}",
                 {"code": "DUPLICATE_SIGNAL"})
            return None
        return _execute_entry_inner(db, bot, user, broker, order, decision,
                                    snapshot, df=df, depth=depth)
    finally:
        lock.release()


def _execute_entry_inner(db: Session, bot: Bot, user: User, broker, order: SizedOrder,
                         decision: dict[str, Any], snapshot: dict[str, Any],
                         df: Any = None,
                         depth: dict[str, Any] | None = None) -> Position | None:
    """`execute_entry`nin kilit altındaki gerçek gövdesi (doğrudan çağrılmaz)."""
    side = "buy" if order.side == "long" else "sell"

    # ------------------------------------------------ büyük emir mi, tek parça mı?
    plan = _execution_plan(bot, order, df, depth)
    if plan is not None and plan.slices > 1:
        from .slicer import create as create_working_order  # noqa: PLC0415

        qty = order.qty
        if plan.style == "reduced" and order.entry > 0:
            # Likiditeye sığmıyor: pozisyon küçültülür, zorlanmaz.
            qty = min(order.qty, plan.total_quote / order.entry)
            emit(db, bot, "warn", "exec",
                 f"EMİR KÜÇÜLTÜLDÜ · {plan.reason}",
                 {"reduced_from": plan.reduced_from, "new_quote": plan.total_quote})

        working = create_working_order(
            db, bot,
            side=Side.LONG if order.side == "long" else Side.SHORT,
            total_qty=qty, reference_price=order.entry,
            slices=plan.slices, interval_seconds=plan.interval_seconds,
            stop_loss=order.stop_loss, take_profit=order.take_profit,
            risk_amount=order.risk_amount, decision=decision, snapshot=snapshot,
        )
        emit(db, bot, "info", "exec",
             f"PARÇALI EMİR BAŞLADI · {plan.slices} parça × "
             f"{plan.interval_seconds}s · tahmini etki %{plan.impact_pct} · "
             f"{plan.reason}",
             {"working_order_id": working.id, "slices": plan.slices,
              "interval_seconds": plan.interval_seconds,
              "impact_pct": plan.impact_pct})

        from .slicer import execute_slice  # noqa: PLC0415
        execute_slice(db, working, emit)          # ilk parça hemen gider
        return db.get(Position, working.position_id) if working.position_id else None

    result = broker.market_order(bot.symbol, side, order.qty, order.entry)

    if not result.ok:
        emit(db, bot, "error", "exec", f"EMİR REDDEDİLDİ · {result.error}",
             {"error": result.error})
        return None

    entry = result.filled_price or order.entry
    position = Position(
        bot_id=bot.id, symbol=bot.symbol,
        side=Side.LONG if order.side == "long" else Side.SHORT,
        status=PositionStatus.OPEN, mode=bot.mode,
        qty=result.filled_qty or order.qty, entry_price=entry,
        stop_loss=order.stop_loss, take_profit=order.take_profit,
        initial_stop=order.stop_loss, risk_amount=order.risk_amount,
        notional=(result.filled_qty or order.qty) * entry,
        fees=result.fee or 0.0,
        confidence=float(decision.get("confidence", 0.0)),
        reasoning=str(decision.get("reasoning", ""))[:1000],
        snapshot_json=json.dumps(snapshot, ensure_ascii=False, default=str),
        exchange_order_id=result.order_id,
        original_qty=result.filled_qty or order.qty,
        decision_models_json=json.dumps(decision.get("decision_models", []),
                                        ensure_ascii=False),
        decision_id=str(decision.get("decision_id", "")),
    )
    db.add(position)
    bot.day_trades += 1
    db.flush()

    # Borsa destekliyorsa koruyucu stop emri de bırakılır
    if isinstance(broker, LiveBroker):
        broker.place_protective_stop(bot.symbol, side, position.qty, order.stop_loss)

    emit(db, bot, "trade", "exec",
         f"POZİSYON AÇILDI · {order.side.upper()} {bot.symbol} @ {entry:.6g} · "
         f"SL {order.stop_loss:.6g} · TP {order.take_profit:.6g} · "
         f"lot {position.qty:.8g} · R/R 1:{order.rr_ratio:.2f}",
         {"position_id": position.id, "entry": entry, "stop_loss": order.stop_loss,
          "take_profit": order.take_profit, "qty": position.qty,
          "source": decision.get("source", "")})

    _notify(db, bot, user, notifier.fmt_open(
        bot.name, bot.symbol, order.side, bot.mode.value, entry, order.stop_loss,
        order.take_profit, position.qty, order.risk_amount, order.rr_ratio,
        float(decision.get("confidence", 0.0)),
        str(decision.get("reasoning", "")), str(decision.get("source", "")),
    ))
    return position
