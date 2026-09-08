"""
SİSTEM BOTU (PLAYBOOK) ARAÇLARI
================================
Ajanın **bot yazmasını** değil, kütüphanedeki doğrulanmış sistemlerden birini
seçip kurmasını sağlar.

Akış:
    list_playbooks       -> kütüphanede ne var
    recommend_playbook   -> bu pariteye/koşula hangisi uyar (deterministik skor)
    deploy_playbook      -> seçileni sanal modda kur ve çalıştır

`deploy_playbook`, playbook'un kendi parametrelerini kullanır; ajanın
uydurduğu risk değerleri değil. Risk kalkanı yine son sözü söyler.
"""
from __future__ import annotations

import json
from typing import Any

from ..core.config import settings
from ..core.logging import get_logger
from ..core.text import contains
from ..core.trading_mode import starting_mode
from ..layers.playbooks import (
    PLAYBOOKS,
    all_playbooks_for,
    recommend_playbooks,
    resolve_playbook,
)
from ..models import Autonomy, Bot, BotStatus, Credential, CredentialKind
from .tools import _MARKET, _SYMBOL, ToolContext, _exchange_for, _obj, tool

log = get_logger("zumvia.tools.playbook")


@tool(
    "list_playbooks",
    "Sistemde HAZIR bulunan doğrulanmış ticaret sistemlerini (playbook) listeler. "
    "Kendin strateji tasarlama — buradaki sistemlerden birini seç. Her kayıt "
    "hangi rejimde çalıştığını, güçlü ve ZAYIF yanını açıkça söyler.",
    _obj({"tag": {"type": "string",
                  "description": "İsteğe bağlı etiket filtresi: trend, kırılım, "
                                 "savunma, yedek, başlangıç, gün içi"}}),
)
def _list_playbooks(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rows = all_playbooks_for(ctx.db, ctx.user)
    tag = (args.get("tag") or "").strip()
    if tag:
        # Türkçe-güvenli karşılaştırma: "İleri" araması "ileri" etiketini bulur.
        rows = [r for r in rows if any(contains(t, tag) for t in r["tags"])]
    return {
        "count": len(rows),
        "playbooks": rows,
        "kural": "Bot tasarımı kütüphanededir. Yeni strateji uydurma; "
                 "koşula en uygun playbook'u seç.",
    }


@tool(
    "recommend_playbook",
    "Verilen parite/piyasa için hangi hazır sistemin uygun olduğunu KOD ile "
    "hesaplar (rejim + volatilite + kurtarma fazı). Sonuç modele değil kurallara "
    "dayanır. Bot kurmadan önce bunu çağır.",
    _obj({
        "market": _MARKET, "symbol": _SYMBOL,
        "exchange": {"type": "string"},
        "timeframe": {"type": "string", "description": "Rejim ölçümü için (varsayılan 4h)"},
        "horizon": {"type": "string", "enum": ["kısa", "orta", "uzun"]},
        "bot_id": {"type": "integer",
                   "description": "Verilirse o botun kurtarma fazı hesaba katılır"},
        "limit": {"type": "integer"},
    }, ["market", "symbol"]),
)
def _recommend_playbook(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..layers.l1_market_data import fetch_ohlcv  # noqa: PLC0415
    from ..layers.l2_indicators import build_snapshot  # noqa: PLC0415

    market = args["market"]
    symbol = args["symbol"]
    timeframe = args.get("timeframe") or "4h"
    exchange = _exchange_for(market, args.get("exchange"))

    regime, atr_pct = None, None
    try:
        df = fetch_ohlcv(market, exchange, symbol, timeframe, limit=300)
        snap = build_snapshot(df, symbol, timeframe)
        regime = snap.regime
        atr_pct = snap.indicators.get("atr_percent")
    except Exception as exc:  # noqa: BLE001 — veri yoksa da öneri üretilebilmeli
        log.warning("playbook önerisi için veri alınamadı: %s", exc)

    phase = None
    if args.get("bot_id"):
        from ..layers.recovery import current_phase  # noqa: PLC0415
        from .tools import _user_bot  # noqa: PLC0415
        try:
            bot = _user_bot(ctx, int(args["bot_id"]))
            phase = current_phase(bot, bot.paper_balance).name
        except Exception:  # noqa: BLE001
            phase = None

    has_llm = (ctx.db.query(Credential)
               .filter(Credential.user_id == ctx.user.id,
                       Credential.kind == CredentialKind.LLM).first() is not None)

    picks = recommend_playbooks(
        regime=regime, atr_pct=atr_pct, market=market, phase=phase,
        horizon=args.get("horizon"), ai_available=has_llm,
        limit=int(args.get("limit", 3)),
    )
    return {
        "symbol": symbol, "timeframe": timeframe,
        "olculen_rejim": regime, "atr_pct": atr_pct,
        "kurtarma_fazi": phase,
        "yapay_zeka_var": has_llm,
        "oneriler": picks,
        "not": "Skor deterministiktir; aynı piyasa koşulunda aynı sonucu verir. "
               "Zayıf yanı okumadan kurma.",
    }


@tool(
    "deploy_playbook",
    "Seçilen hazır sistemi bot olarak kurar. Bot HER ZAMAN sanal (paper) modda "
    "başlar. Strateji seti, hemfikirlik eşiği ve risk playbook'tan gelir — "
    "kendi değerlerini uydurma.",
    _obj({
        "playbook_id": {"type": "string",
                        "description": "Hazır sistem kimliği ("
                                       + ", ".join(sorted(PLAYBOOKS)) +
                                       ") veya `design_custom_bot` ile tasarladığın "
                                       "özel sistemin kimliği"},
        "symbol": _SYMBOL, "market": _MARKET,
        "exchange": {"type": "string"},
        "name": {"type": "string", "description": "Boş bırakılırsa otomatik adlandırılır"},
        "initial_balance": {"type": "number"},
        "timeframe": {"type": "string",
                      "description": "Playbook varsayılanını ezmek için (önerilmez)"},
        "start": {"type": "boolean", "description": "Kurulur kurulmaz çalıştır (varsayılan true)"},
    }, ["playbook_id", "symbol", "market"]),
    mutating=True,
)
def _deploy_playbook(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    pb = resolve_playbook(ctx.db, ctx.user, args["playbook_id"])
    if pb is None:
        return {"deployed": False,
                "error": f"Bilinmeyen sistem: {args['playbook_id']}",
                "ipucu": "`list_playbooks` ve `list_custom_bots` ile bak."}

    market = args["market"]
    llm_cred = (ctx.db.query(Credential)
                .filter(Credential.user_id == ctx.user.id,
                        Credential.kind == CredentialKind.LLM).first())

    decision_mode = pb.decision_mode
    if decision_mode != "algo_only" and llm_cred is None:
        decision_mode = "algo_only"

    balance = float(args.get("initial_balance", 1000))
    name = (args.get("name") or f"{pb.label} · {args['symbol']}")[:80]

    bot = Bot(
        user_id=ctx.user.id, name=name, market=market,
        exchange=_exchange_for(market, args.get("exchange")),
        symbol=args["symbol"],
        timeframe=args.get("timeframe") or pb.timeframe,
        mode=starting_mode(ctx.db, ctx.user, exchange_credential_id=None),
        autonomy=Autonomy.FULL,
        decision_mode=decision_mode,
        llm_credential_id=llm_cred.id if (llm_cred and decision_mode != "algo_only") else None,
        poll_seconds=max(60, pb.poll_seconds),
        allow_short=pb.allow_short,
        strategies_json=json.dumps(pb.strategies),
        guards_json=json.dumps(pb.guards),
        council_mode=ctx.council_mode,
        min_agree=pb.min_agree,
        strategy_notes=f"[{pb.id}] {pb.thesis}\nZayıf yanı: {pb.weakness}\n"
                       f"Kaçın: {pb.avoid_when}"[:2000],
        risk_pct=min(pb.risk_pct, settings.hard_max_risk_pct),
        partial_tp_enabled=pb.partial_tp,
        initial_balance=balance, paper_balance=balance,
        peak_equity=balance, day_start_equity=balance,
    )
    ctx.db.add(bot)
    ctx.db.flush()

    # Gerçekten başlat: durum + zamanlayıcı işi. (Eskiden yalnızca Bot üzerinde
    # var olmayan bir `is_active` alanı set ediliyordu; bot hiç çalışmıyordu.)
    started = False
    if args.get("start", True):
        from ..engine import scheduler as sched  # noqa: PLC0415
        bot.status = BotStatus.RUNNING
        ctx.db.flush()
        try:
            sched.start_bot_job(bot.id, bot.poll_seconds)
            started = True
        except Exception as exc:  # noqa: BLE001 — kurulum, zamanlayıcı yüzünden düşmesin
            log.warning("bot zamanlayıcıya eklenemedi (%s): %s", bot.id, exc)
            bot.status = BotStatus.STOPPED

    # Özel sistemse kurulum sayacını artır (kanıt/izlenebilirlik)
    if pb.id not in PLAYBOOKS:
        from ..models import CustomPlaybook  # noqa: PLC0415
        row = (ctx.db.query(CustomPlaybook)
               .filter(CustomPlaybook.user_id == ctx.user.id,
                       CustomPlaybook.slug == pb.id).first())
        if row is not None:
            row.deploy_count += 1

    log.info("playbook kuruldu: %s -> bot %s", pb.id, bot.id)
    return {
        "deployed": True, "bot_id": bot.id, "name": bot.name,
        "playbook": pb.id, "playbook_label": pb.label,
        "mode": "paper", "timeframe": bot.timeframe,
        "strategies": pb.strategies, "min_agree": pb.min_agree,
        "risk_pct": bot.risk_pct, "decision_mode": decision_mode,
        "aktif": started,
        "kullaniciya_soyle": f"{pb.label} sistemi {bot.symbol} için sanal modda kuruldu. "
                             f"Zayıf yanı: {pb.weakness}",
    }
