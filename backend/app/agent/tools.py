"""
ARAÇ KAYIT DEFTERİ (Tool Registry)
===================================
Komuta ajanının elindeki tüm yetenekler. Her araç:
  * JSON şeması ile tanımlıdır (OpenAI/Anthropic tool-calling uyumlu),
  * saf Python'da çalışır — LLM hiçbir hesap yapmaz,
  * Katman 4 risk kalkanını ASLA atlayamaz.

Kalıcı güvenlik sınırları (ajan bunları değiştiremez):
  - `mode` (paper → live) geçişi YALNIZCA kullanıcı arayüzünden yapılır.
  - Risk yüzdesi, günlük kayıp limiti, min. R/R ve min. güven sistem
    tavanlarının üstüne çıkarılamaz.
  - Pozisyon açma her zaman `validate_and_size()` süzgecinden geçer.
  - API anahtarları araçlara düz metin olarak asla dönmez.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.creds import public_view, read_secrets
from ..core.logging import get_logger
from ..engine.backtest import run_backtest
from ..engine.orchestrator import _make_broker, close_position, execute_entry, run_cycle
from ..layers.l1_market_data import (
    MarketDataError,
    fetch_ohlcv,
    fetch_order_book_depth,
    fetch_quote,
)
from ..layers.l2_indicators import build_snapshot, compute_all, technical_bias
from ..layers.l4_risk import check_preconditions, validate_and_size
from ..layers.notifier import send_telegram
from ..layers.strategies import DEFAULT_ACTIVE, StrategyEngine, strategy_catalog
from ..models import (
    Autonomy,
    Bot,
    BotEvent,
    BotStatus,
    Credential,
    CredentialKind,
    Position,
    PositionStatus,
    Side,
    TradingMode,
    User,
)
from .news import fetch_news

log = get_logger("zumvia.tools")


@dataclass(slots=True)
class ToolContext:
    """Araçların çalıştığı kapsam: hangi kullanıcı, hangi oturum."""
    db: Session
    user: User
    session_id: int | None = None
    sub_model_call: Callable[[str, str], dict[str, Any]] | None = None
    council_mode: str = "auto"     # kullanicinin sectigi karar kalitesi modu
    model_name: str = ""           # kararı veren model (kanıt kaydı için)
    work_mode: str = "agent"       # ask | plan | agent — araç kapısını belirler
    #
    # Neden burada varsayılan `agent`? Çünkü `ToolContext` sohbet dışında da
    # kullanılıyor (MCP, otomasyonlar, zamanlayıcı) ve oralarda yetki zaten
    # başka bir kapıdan geçmiş oluyor. Sohbet tarafı modu AÇIKÇA geçirir;
    # geçirmezse oturumun kendi `work_mode` alanı okunur.


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], dict[str, Any]]
    mutating: bool = False          # durumu değiştiriyor mu (onay/log için)

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, parameters: dict[str, Any],
         mutating: bool = False):
    def wrap(fn: Callable[[ToolContext, dict[str, Any]], dict[str, Any]]):
        REGISTRY[name] = Tool(name, description, parameters, fn, mutating)
        return fn
    return wrap


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


_MARKET = {"type": "string", "enum": ["crypto", "stock", "demo"],
           "description": "Piyasa türü"}
_SYMBOL = {"type": "string", "description": "Parite/sembol, örn. BTC/USDT veya AAPL"}
_TF = {"type": "string", "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]}


def _user_bot(ctx: ToolContext, bot_id: int) -> Bot:
    bot = ctx.db.get(Bot, int(bot_id))
    if bot is None or bot.user_id != ctx.user.id:
        raise ValueError(f"Bot bulunamadı: {bot_id}")
    return bot


def _exchange_for(market: str, exchange: str | None) -> str:
    if exchange:
        return exchange
    return {"crypto": "binance", "stock": "yfinance", "demo": "demo"}.get(market, "binance")


# =========================================================================== #
#  1) PİYASA VE ANALİZ ARAÇLARI
# =========================================================================== #

@tool(
    "get_market_snapshot",
    "Bir varlığın deterministik teknik fotoğrafını döndürür: RSI, EMA50/200, MACD, "
    "Bollinger, ATR, ADX, Supertrend, rejim sınıfı, destek/direnç ve kural tabanlı "
    "teknik skor. Tüm sayılar Python ile hesaplanır, tahmin değildir. "
    "Herhangi bir karar vermeden ÖNCE bunu çağır.",
    _obj({
        "market": _MARKET, "symbol": _SYMBOL, "timeframe": _TF,
        "exchange": {"type": "string", "description": "Borsa kimliği (opsiyonel)"},
    }, ["market", "symbol", "timeframe"]),
)
def _get_market_snapshot(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    market = args["market"]
    exchange = _exchange_for(market, args.get("exchange"))
    df = compute_all(fetch_ohlcv(market, exchange, args["symbol"], args["timeframe"], 320))
    snap = build_snapshot(df, args["symbol"], args["timeframe"])
    depth = fetch_order_book_depth(market, exchange, args["symbol"])
    return {
        "snapshot": snap.to_dict(),
        "technical_bias": technical_bias(snap),
        "order_book": depth,
    }


@tool(
    "run_strategy_engine",
    "8 klasik algoritmik stratejiyi (trend takibi, geri çekilme, kırılım, MACD, "
    "sıkışma, ortalamaya dönüş, VWAP, RSI uyumsuzluğu) çalıştırır ve ağırlıklı "
    "konsensüs üretir. Yapay zeka kullanmaz. Kendi görüşünü bu bağımsız oyla "
    "ÇAPRAZ DOĞRULAMAK için kullan.",
    _obj({
        "market": _MARKET, "symbol": _SYMBOL, "timeframe": _TF,
        "exchange": {"type": "string"},
        "min_agree": {"type": "integer", "description": "Kaç strateji aynı yönde olmalı (1-8)"},
        "strategies": {"type": "array", "items": {"type": "string"},
                       "description": "Boş bırakılırsa varsayılan set kullanılır"},
    }, ["market", "symbol", "timeframe"]),
)
def _run_strategy_engine(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    market = args["market"]
    exchange = _exchange_for(market, args.get("exchange"))
    df = fetch_ohlcv(market, exchange, args["symbol"], args["timeframe"], 320)
    engine = StrategyEngine(args.get("strategies") or None, int(args.get("min_agree", 2)))
    return engine.run(df).to_dict()


@tool(
    "get_news",
    "Ücretsiz RSS kaynaklarından güncel haber başlıklarını ve deterministik "
    "anahtar-kelime duygu skorunu getirir. Başlıkları KENDİN yorumla; skor sadece "
    "kaba bir sayımdır. Büyük fiyat hareketlerinin sebebini anlamak için kullan.",
    _obj({"symbol": _SYMBOL, "market": _MARKET}, ["market"]),
)
def _get_news(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return fetch_news(args.get("symbol", ""), args.get("market", "crypto"))


@tool(
    "get_quote",
    "Anlık fiyat, alış/satış ve spread bilgisini döndürür.",
    _obj({"market": _MARKET, "symbol": _SYMBOL, "exchange": {"type": "string"}},
         ["market", "symbol"]),
)
def _get_quote(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    market = args["market"]
    q = fetch_quote(market, _exchange_for(market, args.get("exchange")), args["symbol"])
    return {"symbol": q.symbol, "price": q.price, "bid": q.bid, "ask": q.ask,
            "spread_pct": q.spread_pct}


@tool(
    "run_backtest",
    "Strateji setini geçmiş veride bar-bar simüle eder (look-ahead yok, komisyon ve "
    "slipaj dahil). Kazanma oranı, kâr faktörü, drawdown, Sharpe ve canlıya "
    "hazırlık kararı döner. Bir pariteye/zaman dilimine geçmeden ÖNCE kanıt topla.",
    _obj({
        "market": _MARKET, "symbol": _SYMBOL, "timeframe": _TF,
        "exchange": {"type": "string"},
        "candles": {"type": "integer", "description": "300-1000 arası"},
        "initial_balance": {"type": "number"},
        "risk_pct": {"type": "number"},
        "min_agree": {"type": "integer"},
        "allow_short": {"type": "boolean"},
        "strategies": {"type": "array", "items": {"type": "string"},
                       "description": "Test edilecek strateji seti (boşsa varsayılan)"},
    }, ["market", "symbol", "timeframe"]),
)
def _run_backtest(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    market = args["market"]
    df = fetch_ohlcv(market, _exchange_for(market, args.get("exchange")),
                     args["symbol"], args["timeframe"], int(args.get("candles", 1000)))
    report = run_backtest(
        df,
        initial_balance=float(args.get("initial_balance", 1000)),
        risk_pct=min(float(args.get("risk_pct", 1.0)), settings.hard_max_risk_pct),
        min_agree=int(args.get("min_agree", 2)),
        allow_short=bool(args.get("allow_short", False)),
        strategies=args.get("strategies") or None,
    )
    data = report.to_dict()
    data.pop("trades", None)          # bağlam şişmesin; metrikler yeterli
    data["equity_curve"] = data["equity_curve"][-40:]
    return data


# =========================================================================== #
#  2) PORTFÖY VE BOT DENETİMİ
# =========================================================================== #

@tool(
    "get_portfolio",
    "Tüm botların birleşik durumu: sermaye, açık pozisyonlar, kazanma oranı, "
    "kâr faktörü, drawdown, kilitli/toparlanma durumundaki botlar. "
    "Her denetim turuna bununla başla.",
    _obj({}),
)
def _get_portfolio(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    bots = ctx.db.query(Bot).filter(Bot.user_id == ctx.user.id).all()
    ids = [b.id for b in bots]
    closed = open_positions = []
    if ids:
        closed = (ctx.db.query(Position)
                  .filter(Position.bot_id.in_(ids), Position.status == PositionStatus.CLOSED).all())
        open_positions = (ctx.db.query(Position)
                          .filter(Position.bot_id.in_(ids), Position.status == PositionStatus.OPEN).all())

    wins = [p for p in closed if p.pnl > 0]
    gross_win = sum(p.pnl for p in wins)
    gross_loss = abs(sum(p.pnl for p in closed if p.pnl <= 0))
    balance = sum(b.paper_balance for b in bots)
    initial = sum(b.initial_balance for b in bots)

    return {
        "bot_count": len(bots),
        "running": sum(1 for b in bots if b.status == BotStatus.RUNNING),
        "locked": [{"id": b.id, "name": b.name, "reason": b.lock_reason}
                   for b in bots if b.status == BotStatus.LOCKED],
        "recovery": [b.id for b in bots if b.recovery_mode],
        "total_balance": round(balance, 2),
        "total_initial": round(initial, 2),
        "net_pnl": round(balance - initial, 2),
        "return_pct": round((balance - initial) / initial * 100, 3) if initial else 0.0,
        "total_trades": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 2) if closed else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else 0.0,
        "open_positions": len(open_positions),
        "bots": [{"id": b.id, "name": b.name, "symbol": b.symbol, "timeframe": b.timeframe,
                  "status": b.status.value, "mode": b.mode.value,
                  "decision_mode": b.decision_mode, "autonomy": b.autonomy.value,
                  "balance": round(b.paper_balance, 2),
                  "return_pct": round((b.paper_balance - b.initial_balance) /
                                      b.initial_balance * 100, 2) if b.initial_balance else 0.0,
                  "recovery_mode": b.recovery_mode,
                  "consecutive_losses": b.consecutive_losses}
                 for b in bots],
    }


@tool(
    "get_bot_detail",
    "Tek bir botun tam durumu: risk ayarları, açık pozisyonlar, son işlemler ve "
    "son motor olayları. Bir botu değiştirmeden veya sorunu teşhis etmeden önce oku.",
    _obj({"bot_id": {"type": "integer"},
          "event_limit": {"type": "integer", "description": "Kaç olay (varsayılan 25)"}},
         ["bot_id"]),
)
def _get_bot_detail(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    bot = _user_bot(ctx, args["bot_id"])
    limit = int(args.get("event_limit", 25))

    positions = (ctx.db.query(Position).filter(Position.bot_id == bot.id)
                 .order_by(desc(Position.id)).limit(25).all())
    events = (ctx.db.query(BotEvent).filter(BotEvent.bot_id == bot.id)
              .order_by(desc(BotEvent.id)).limit(limit).all())

    return {
        "id": bot.id, "name": bot.name, "symbol": bot.symbol, "timeframe": bot.timeframe,
        "market": bot.market, "exchange": bot.exchange, "status": bot.status.value,
        "mode": bot.mode.value, "autonomy": bot.autonomy.value,
        "decision_mode": bot.decision_mode, "poll_seconds": bot.poll_seconds,
        "allow_short": bot.allow_short, "llm_model": bot.llm_model,
        "strategies": json.loads(bot.strategies_json or "[]") or DEFAULT_ACTIVE,
        "min_agree": bot.min_agree, "strategy_notes": bot.strategy_notes,
        "risk": {"risk_pct": bot.risk_pct, "daily_loss_limit_pct": bot.daily_loss_limit_pct,
                 "min_confidence": bot.min_confidence, "min_rr": bot.min_rr,
                 "max_open_positions": bot.max_open_positions,
                 "max_trades_per_day": bot.max_trades_per_day,
                 "max_drawdown_pct": bot.max_drawdown_pct,
                 "recovery_mode": bot.recovery_mode,
                 "consecutive_losses": bot.consecutive_losses,
                 "locked_until": bot.locked_until.isoformat() if bot.locked_until else None,
                 "lock_reason": bot.lock_reason},
        "capital": {"initial": bot.initial_balance, "balance": round(bot.paper_balance, 4),
                    "peak": round(bot.peak_equity, 4)},
        "open_positions": [{"id": p.id, "side": p.side.value, "entry": p.entry_price,
                            "stop_loss": p.stop_loss, "take_profit": p.take_profit,
                            "qty": p.qty, "opened_at": p.opened_at.isoformat() if p.opened_at else None}
                           for p in positions if p.status == PositionStatus.OPEN],
        "recent_closed": [{"id": p.id, "side": p.side.value, "pnl": round(p.pnl, 4),
                           "r": round(p.r_multiple, 2), "reason": p.close_reason}
                          for p in positions if p.status == PositionStatus.CLOSED][:10],
        "recent_events": [{"ts": e.ts.isoformat() if e.ts else None, "level": e.level,
                           "category": e.category, "message": e.message} for e in reversed(events)],
    }


@tool(
    "create_bot",
    "Yeni bir işlem botu oluşturur. Bot HER ZAMAN paper (sanal) modda başlar — "
    "gerçek paraya geçişi yalnızca kullanıcı arayüzden yapabilir.",
    _obj({
        "name": {"type": "string"}, "market": _MARKET, "symbol": _SYMBOL,
        "timeframe": _TF, "exchange": {"type": "string"},
        "decision_mode": {"type": "string",
                          "enum": ["hybrid", "ai_first", "algo_only", "ai_only"]},
        "initial_balance": {"type": "number"},
        "risk_pct": {"type": "number", "description": f"En fazla {settings.hard_max_risk_pct}"},
        "min_agree": {"type": "integer"},
        "allow_short": {"type": "boolean"},
        "poll_seconds": {"type": "integer", "description": "En az 60"},
        "strategy_notes": {"type": "string", "description": "Bota özel strateji talimatı"},
    }, ["name", "market", "symbol", "timeframe"]),
    mutating=True,
)
def _create_bot(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    market = args["market"]
    llm_cred = (ctx.db.query(Credential)
                .filter(Credential.user_id == ctx.user.id,
                        Credential.kind == CredentialKind.LLM).first())
    decision_mode = args.get("decision_mode", "hybrid" if llm_cred else "algo_only")
    if decision_mode != "algo_only" and llm_cred is None:
        decision_mode = "algo_only"

    balance = float(args.get("initial_balance", 1000))
    bot = Bot(
        user_id=ctx.user.id, name=args["name"][:80], market=market,
        exchange=_exchange_for(market, args.get("exchange")), symbol=args["symbol"],
        timeframe=args["timeframe"], mode=TradingMode.PAPER, autonomy=Autonomy.FULL,
        decision_mode=decision_mode,
        llm_credential_id=llm_cred.id if (llm_cred and decision_mode != "algo_only") else None,
        poll_seconds=max(60, int(args.get("poll_seconds", 300))),
        allow_short=bool(args.get("allow_short", False)),
        strategies_json=json.dumps(DEFAULT_ACTIVE),
        council_mode=ctx.council_mode,
        min_agree=int(args.get("min_agree", 2)),
        strategy_notes=str(args.get("strategy_notes", ""))[:2000],
        risk_pct=min(float(args.get("risk_pct", 1.0)), settings.hard_max_risk_pct),
        initial_balance=balance, paper_balance=balance,
        peak_equity=balance, day_start_equity=balance,
    )
    ctx.db.add(bot)
    ctx.db.flush()
    return {"created": True, "bot_id": bot.id, "name": bot.name, "mode": "paper",
            "decision_mode": bot.decision_mode,
            "note": "Bot durdurulmuş halde oluşturuldu. Çalıştırmak için start_bot çağır."}


@tool(
    "update_bot",
    "Bot ayarlarını günceller (risk, karar mimarisi, stratejiler, periyot, notlar). "
    "Sistem tavanları aşılamaz ve paper→live geçişi bu araçla YAPILAMAZ. "
    "Performans düşükse ayarları buradan ince ayarla.",
    _obj({
        "bot_id": {"type": "integer"},
        "risk_pct": {"type": "number"}, "min_confidence": {"type": "number"},
        "min_rr": {"type": "number"}, "min_agree": {"type": "integer"},
        "max_open_positions": {"type": "integer"}, "max_trades_per_day": {"type": "integer"},
        "max_drawdown_pct": {"type": "number"}, "poll_seconds": {"type": "integer"},
        "decision_mode": {"type": "string", "enum": ["hybrid", "ai_first", "algo_only", "ai_only"]},
        "autonomy": {"type": "string", "enum": ["manual", "semi", "full"]},
        "timeframe": _TF, "allow_short": {"type": "boolean"},
        "trailing_stop": {"type": "boolean"},
        "strategies": {"type": "array", "items": {"type": "string"}},
        "strategy_notes": {"type": "string"},
        "llm_model": {"type": "string", "description": "Botun kullanacağı alt model"},
        "reason": {"type": "string", "description": "Bu değişikliği neden yapıyorsun"},
    }, ["bot_id", "reason"]),
    mutating=True,
)
def _update_bot(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    bot = _user_bot(ctx, args["bot_id"])
    changed: dict[str, Any] = {}

    def apply(field: str, value: Any) -> None:
        if value is None:
            return
        old = getattr(bot, field)
        if old != value:
            setattr(bot, field, value)
            changed[field] = {"eski": old, "yeni": value}

    if "risk_pct" in args:
        apply("risk_pct", min(float(args["risk_pct"]), settings.hard_max_risk_pct))
    if "min_confidence" in args:
        apply("min_confidence", max(float(args["min_confidence"]), settings.hard_min_confidence))
    if "min_rr" in args:
        apply("min_rr", max(float(args["min_rr"]), settings.hard_min_rr_ratio))
    if "max_drawdown_pct" in args:
        apply("max_drawdown_pct", max(1.0, min(float(args["max_drawdown_pct"]), 50.0)))
    for field in ("min_agree", "max_open_positions", "max_trades_per_day"):
        if field in args:
            apply(field, max(1, int(args[field])))
    if "poll_seconds" in args:
        apply("poll_seconds", max(60, int(args["poll_seconds"])))
    for field in ("decision_mode", "timeframe", "llm_model", "strategy_notes"):
        if field in args:
            apply(field, args[field])
    for field in ("allow_short", "trailing_stop"):
        if field in args:
            apply(field, bool(args[field]))
    if "autonomy" in args:
        apply("autonomy", Autonomy(args["autonomy"]))
    if args.get("strategies"):
        apply("strategies_json", json.dumps(args["strategies"]))

    ctx.db.flush()
    if bot.status == BotStatus.RUNNING and "poll_seconds" in changed:
        from ..engine import scheduler as sched  # noqa: PLC0415
        sched.start_bot_job(bot.id, bot.poll_seconds)

    return {"updated": bool(changed), "bot_id": bot.id, "changes": changed,
            "reason": args["reason"]}


@tool(
    "control_bot",
    "Botu başlatır, durdurur veya devre kesici kilidini kaldırır. "
    "Kilidi ancak sebebi anlayıp gerekli ayarı yaptıktan sonra kaldır.",
    _obj({"bot_id": {"type": "integer"},
          "action": {"type": "string", "enum": ["start", "stop", "unlock"]},
          "reason": {"type": "string"}},
         ["bot_id", "action", "reason"]),
    mutating=True,
)
def _control_bot(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..engine import scheduler as sched  # noqa: PLC0415

    bot = _user_bot(ctx, args["bot_id"])
    action = args["action"]

    if action == "start":
        if bot.locked_until and bot.locked_until.replace(tzinfo=UTC) > datetime.now(UTC):
            return {"ok": False, "error": f"Bot kilitli: {bot.lock_reason}. Önce unlock çağır."}
        if bot.decision_mode != "algo_only" and not bot.llm_credential_id:
            return {"ok": False,
                    "error": "Bu karar modu için LLM anahtarı yok. decision_mode='algo_only' yap."}
        bot.status = BotStatus.RUNNING
        ctx.db.flush()
        sched.start_bot_job(bot.id, bot.poll_seconds)
    elif action == "stop":
        sched.stop_bot_job(bot.id)
        bot.status = BotStatus.STOPPED
    else:
        bot.locked_until = None
        bot.lock_reason = ""
        bot.day_key = ""
        bot.status = BotStatus.STOPPED

    ctx.db.flush()
    return {"ok": True, "bot_id": bot.id, "action": action, "status": bot.status.value,
            "reason": args["reason"]}


@tool(
    "run_bot_cycle",
    "Bir botun 5 katmanlı karar turunu HEMEN çalıştırır (zamanlayıcıyı beklemeden) "
    "ve sonucu döner. Botun kendi gözüyle ne gördüğünü öğrenmek ve kendi analizinle "
    "ÇAPRAZ DOĞRULAMAK için kullan.",
    _obj({"bot_id": {"type": "integer"}}, ["bot_id"]),
    mutating=True,
)
def _run_bot_cycle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    bot = _user_bot(ctx, args["bot_id"])
    ctx.db.commit()               # motor kendi oturumunu açar
    result = run_cycle(bot.id)
    ctx.db.expire_all()
    return result


@tool(
    "open_position",
    "Kendi kararınla bir pozisyon açar. Emir KATMAN 4 RİSK KALKANINDAN geçer: "
    "stop-loss zorunlu, R/R en az 1:2, lot otomatik hesaplanır, tavanlar uygulanır. "
    "Reddedilirse sebebi döner — reddi tartışma, kabul et.",
    _obj({
        "bot_id": {"type": "integer", "description": "İşlemin açılacağı bot (kasa sahibi)"},
        "action": {"type": "string", "enum": ["BUY", "SELL"]},
        "stop_loss": {"type": "number"}, "take_profit": {"type": "number"},
        "confidence": {"type": "number", "description": "0.0-1.0"},
        "reasoning": {"type": "string", "description": "Kısa Türkçe gerekçe"},
    }, ["bot_id", "action", "stop_loss", "take_profit", "confidence", "reasoning"]),
    mutating=True,
)
def _open_position(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    bot = _user_bot(ctx, args["bot_id"])
    quote = fetch_quote(bot.market, bot.exchange, bot.symbol)
    price = quote.price

    df = compute_all(fetch_ohlcv(bot.market, bot.exchange, bot.symbol, bot.timeframe, 260))
    atr_v = float(df["atr_14"].iloc[-1])
    snapshot = build_snapshot(df, bot.symbol, bot.timeframe).to_dict()

    open_positions = (ctx.db.query(Position)
                      .filter(Position.bot_id == bot.id, Position.status == PositionStatus.OPEN)
                      .all())
    equity = bot.paper_balance + sum(
        ((price - p.entry_price) if p.side == Side.LONG else (p.entry_price - price)) * p.qty
        for p in open_positions
    )

    gate = check_preconditions(bot, equity, len(open_positions))
    if not gate.allowed:
        return {"opened": False, "blocked_by": "risk_preconditions",
                "reason": gate.reason, "code": gate.code}

    verdict, order = validate_and_size(
        bot, args["action"], float(args["confidence"]), price,
        float(args["stop_loss"]), float(args["take_profit"]), equity, atr_v,
    )
    if not verdict.allowed or order is None:
        return {"opened": False, "blocked_by": "risk_shield",
                "reason": verdict.reason, "code": verdict.code, "price": price}

    broker = _make_broker(ctx.db, bot, ctx.user, bot.paper_balance)
    decision = {"confidence": float(args["confidence"]),
                "reasoning": args["reasoning"], "source": "KOMUTA AJANI"}
    position = execute_entry(ctx.db, bot, ctx.user, broker, order, decision, snapshot)
    if position is None:
        return {"opened": False, "blocked_by": "execution", "reason": "Emir gerçekleşmedi."}

    return {"opened": True, "position_id": position.id, "side": order.side,
            "entry": position.entry_price, "stop_loss": order.stop_loss,
            "take_profit": order.take_profit, "qty": position.qty,
            "risk_amount": round(order.risk_amount, 4), "rr": order.rr_ratio}


@tool(
    "close_position",
    "Açık bir pozisyonu güncel piyasa fiyatından kapatır. Tez bozulduysa veya "
    "risk arttıysa kullan.",
    _obj({"bot_id": {"type": "integer"}, "position_id": {"type": "integer"},
          "reason": {"type": "string"}},
         ["bot_id", "position_id", "reason"]),
    mutating=True,
)
def _close_position(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    bot = _user_bot(ctx, args["bot_id"])
    position = ctx.db.get(Position, int(args["position_id"]))
    if position is None or position.bot_id != bot.id or position.status != PositionStatus.OPEN:
        return {"closed": False, "error": "Açık pozisyon bulunamadı."}

    price = fetch_quote(bot.market, bot.exchange, bot.symbol).price
    broker = _make_broker(ctx.db, bot, ctx.user, bot.paper_balance)
    closed = close_position(ctx.db, bot, ctx.user, position, price, "AI_CLOSE", broker)
    return {"closed": True, "pnl": round(closed.pnl, 4),
            "r_multiple": round(closed.r_multiple, 3), "reason": args["reason"]}


# =========================================================================== #
#  3) ALT MODEL, BİLDİRİM VE SİSTEM SAĞLIĞI
# =========================================================================== #

@tool(
    "ask_sub_model",
    "Alt modele (analist) bir soru delege eder. Derin teknik yorum, senaryo analizi "
    "veya ikinci görüş için kullan. Alt model araç çağıramaz; ona gereken TÜM "
    "sayısal veriyi `context` içinde ver.",
    _obj({"question": {"type": "string", "description": "Net, tek bir soru"},
          "context": {"type": "string", "description": "İlgili sayısal veriler (JSON/metin)"}},
         ["question"]),
)
def _ask_sub_model(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.sub_model_call is None:
        return {"available": False,
                "reason": "Alt model tanımlı değil. Oturum ayarlarından bir alt model seçin."}
    return ctx.sub_model_call(args["question"], args.get("context", ""))


@tool(
    "send_notification",
    "Kullanıcının Telegram kanalına mesaj gönderir. Önemli olaylar, günlük özet "
    "veya acil uyarılar için kullan. Gereksiz bildirim gönderme.",
    _obj({"message": {"type": "string"}}, ["message"]),
    mutating=True,
)
def _send_notification(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    cred = (ctx.db.query(Credential)
            .filter(Credential.user_id == ctx.user.id,
                    Credential.kind == CredentialKind.TELEGRAM).first())
    if cred is None:
        return {"sent": False, "reason": "Telegram yapılandırılmamış."}
    secrets = read_secrets(ctx.user, cred)
    ok, message = send_telegram(secrets.get("token", ""), secrets.get("chat_id", ""),
                                f"*ZUMVIA* · Komuta Ajanı\n\n{args['message'][:3000]}")
    return {"sent": ok, "detail": message}


@tool(
    "system_health",
    "Sistemin genel sağlığı: hata veren botlar, kilitler, son motor hataları, "
    "yapılandırılmış anahtarlar (maskeli) ve sistem risk tavanları. "
    "Otonom denetim turlarında ilk çağırdığın araçlardan biri olsun.",
    _obj({}),
)
def _system_health(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    bots = ctx.db.query(Bot).filter(Bot.user_id == ctx.user.id).all()
    ids = [b.id for b in bots]
    errors = []
    if ids:
        errors = (ctx.db.query(BotEvent)
                  .filter(BotEvent.bot_id.in_(ids), BotEvent.level == "error")
                  .order_by(desc(BotEvent.id)).limit(15).all())

    credentials = ctx.db.query(Credential).filter(Credential.user_id == ctx.user.id).all()
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "bots_total": len(bots),
        "bots_running": sum(1 for b in bots if b.status == BotStatus.RUNNING),
        "bots_locked": [{"id": b.id, "name": b.name, "reason": b.lock_reason}
                        for b in bots if b.status == BotStatus.LOCKED],
        "bots_stale": [{"id": b.id, "name": b.name,
                        "last_run": b.last_run_at.isoformat() if b.last_run_at else None}
                       for b in bots
                       if b.status == BotStatus.RUNNING and b.last_run_at and
                       (datetime.now(UTC) - b.last_run_at.replace(tzinfo=UTC)
                        ).total_seconds() > b.poll_seconds * 3],
        "recent_errors": [{"bot_id": e.bot_id, "ts": e.ts.isoformat() if e.ts else None,
                           "message": e.message[:220]} for e in errors],
        "credentials": [{"kind": c.kind.value, "provider": c.provider, "label": c.label,
                         "hint": c.hint} for c in credentials],
        "hard_limits": {
            "max_risk_pct": settings.hard_max_risk_pct,
            "max_daily_loss_pct": settings.hard_daily_loss_limit_pct,
            "min_confidence": settings.hard_min_confidence,
            "min_rr": settings.hard_min_rr_ratio,
            "live_trading_globally_disabled": settings.force_paper_only,
        },
        "available_strategies": strategy_catalog(),
    }


@tool(
    "list_credentials",
    "Kullanıcının kayıtlı anahtarlarını (maskeli) listeler. Gerçek anahtar değerleri "
    "asla döndürülmez.",
    _obj({}),
)
def _list_credentials(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    credentials = ctx.db.query(Credential).filter(Credential.user_id == ctx.user.id).all()
    return {"credentials": [public_view(c) for c in credentials]}


# =========================================================================== #
#  Çalıştırıcı
# =========================================================================== #


def tool_schemas(names: list[str] | None = None) -> list[dict[str, Any]]:
    tools = REGISTRY.values() if names is None else [REGISTRY[n] for n in names if n in REGISTRY]
    return [t.schema() for t in tools]


def tool_manifest() -> list[dict[str, Any]]:
    """Arayüzde gösterilecek insan-okur araç listesi."""
    return [{"name": t.name, "description": t.description.split(".")[0] + ".",
             "mutating": t.mutating} for t in REGISTRY.values()]


def execute_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Aracı çalıştırır. Hata fırlatmaz — ajan hatayı okuyup düzeltebilsin diye döndürür."""
    tool_obj = REGISTRY.get(name)
    if tool_obj is None:
        return {"error": f"Bilinmeyen araç: {name}",
                "available": sorted(REGISTRY.keys())}

    # ÇALIŞMA MODU KAPISI — prompt'ta rica değil, burada zorlama.
    #
    # Modele "bunu yapma" demek bir dilektir: unutur, yanlış anlar ya da
    # kullanıcının cümlesini izin sanır. Sor/Planla modunda durumu değiştiren
    # araçlar model istese de çalışmaz.
    from .work_mode import allowed  # noqa: PLC0415
    ok, why = allowed(ctx.work_mode, name, tool_obj.mutating)
    if not ok:
        return {"error": why, "mod": ctx.work_mode, "engellendi": True}

    try:
        result = tool_obj.handler(ctx, args or {})
        ctx.db.commit()
        return result
    except MarketDataError as exc:
        ctx.db.rollback()
        return {"error": f"Piyasa verisi hatası: {exc}"}
    except ValueError as exc:
        ctx.db.rollback()
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        ctx.db.rollback()
        log.exception("Araç hatası %s", name)
        return {"error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
#  Profesyonel arac seti (tarama, dogrulama, toparlanma, guvenlik) kayit olur.
#  Import en altta: dairesel bagimliligi onlemek icin.
# --------------------------------------------------------------------------- #
from . import (
    tools_automation,  # noqa: E402,F401  (ajanin kendi otomasyonunu kurmasi)
    tools_browse,  # noqa: E402,F401  (web'de gezinme)
    tools_design,  # noqa: E402,F401  (ozel sistem tasarimi)
    tools_finance,  # noqa: E402,F401  (canli piyasa ve capraz dogrulama)
    tools_playbook,  # noqa: E402,F401  (sistem botlari)
    tools_portfolio,  # noqa: E402,F401  (coklu enstruman kurulumu)
    tools_pro,  # noqa: E402,F401  (REGISTRY'ye kayit icin)
    tools_research,  # noqa: E402,F401  (dogrulamali derin arastirma)
    tools_skills,  # noqa: E402,F401  (ajanin kendi becerisini yazmasi)
)
