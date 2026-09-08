"""
PORTFÖY KURULUMU — "kriptoda paramı yönet" cümlesinin karşılığı
================================================================

Kullanıcının tek cümlesi ile çalışan bir portföy arasındaki mesafeyi bu araç
kapatır. Tek başına yaptığı iş:

  1. Piyasayı tarar, en net kurulumları bulur (`scan_markets`),
  2. Her aday için **likidite profilini** çıkarır,
  3. Sermayeyi likidite ve güven ağırlıklı dağıtır (`capital.allocate`),
  4. Her enstrümana koşullara uyan **hazır sistemi** seçer,
  5. Botları kurar ve çalıştırır,
  6. Ne yaptığını tek tabloda anlatır.

Neden tek enstrüman değil? Çünkü sermaye büyüdükçe tek enstrüman iki şeyi
birden kaybettirir: likidite tükendiği için giriş fiyatı bozulur ve tüm bahis
tek hikâyeye bağlanır. Dağıtım kârı azaltmaz, kârın gerçekleşmesini sağlar.
"""
from __future__ import annotations

import json
from typing import Any

from ..core.config import settings
from ..core.logging import get_logger
from ..core.trading_mode import evaluate as evaluate_mode
from ..layers.capital import allocate, liquidity_profile, plan_execution
from ..layers.playbooks import recommend_playbooks, resolve_playbook
from ..models import Autonomy, Bot, BotStatus, Credential, CredentialKind, TradingMode
from .tools import _MARKET, ToolContext, _exchange_for, _obj, tool

log = get_logger("zumvia.tools.portfolio")

MAX_INSTRUMENTS = 8


@tool(
    "deploy_portfolio",
    "Kullanıcı 'kriptoda paramı yönet' ya da 'hisse tarafına bak' dediğinde "
    "kullanılacak ANA araç. Piyasayı tarar, sermayeyi likiditeye göre birden "
    "çok enstrümana dağıtır, her birine uygun hazır sistemi kurar ve çalıştırır. "
    "Tek tek bot kurmak yerine bunu çağır.",
    _obj({
        "market": _MARKET,
        "capital": {"type": "number", "description": "Yönetilecek toplam sermaye"},
        "exchange": {"type": "string"},
        "instruments": {"type": "integer",
                        "description": f"Kaç enstrümana dağıtılsın (1-{MAX_INSTRUMENTS}, "
                                       f"boşsa sermayeye göre otomatik)"},
        "timeframe": {"type": "string", "description": "Tarama zaman dilimi (varsayılan 4h)"},
        "symbols": {"type": "array", "items": {"type": "string"},
                    "description": "Belirli pariteler; boşsa popüler evren taranır"},
        "horizon": {"type": "string", "enum": ["kısa", "orta", "uzun"]},
        "start": {"type": "boolean", "description": "Kurulunca çalıştır (varsayılan true)"},
    }, ["market", "capital"]),
    mutating=True,
)
def _deploy_portfolio(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..engine import scheduler as sched  # noqa: PLC0415
    from ..engine.scanner import scan_markets  # noqa: PLC0415
    from ..layers.l1_market_data import fetch_ohlcv  # noqa: PLC0415

    market = args["market"]
    capital = float(args.get("capital") or 0.0)
    if capital <= 0:
        return {"deployed": False, "error": "Yönetilecek sermaye belirtilmedi."}

    timeframe = args.get("timeframe") or "4h"
    exchange = _exchange_for(market, args.get("exchange"))

    # Sermaye büyüdükçe daha çok enstrümana yayılır: 1.000 $ için tek enstrüman
    # mantıklı, 1.000.000 $ için değil.
    auto_count = 1 if capital < 5_000 else 2 if capital < 25_000 else \
        3 if capital < 100_000 else 5 if capital < 1_000_000 else MAX_INSTRUMENTS
    wanted = int(args.get("instruments") or auto_count)
    wanted = max(1, min(wanted, MAX_INSTRUMENTS))

    # ---------------------------------------------------------------- 1. tara
    try:
        scan_result = scan_markets(market=market, exchange=exchange,
                                   timeframe=timeframe,
                                   symbols=args.get("symbols") or None,
                                   top=wanted * 3, min_agree=2)
    except Exception as exc:  # noqa: BLE001
        log.warning("portföy taraması başarısız: %s", exc)
        return {"deployed": False, "error": f"Piyasa taranamadı: {exc}"}

    candidates = scan_result.get("candidates") or []
    if not candidates:
        return {"deployed": False,
                "error": "Tarama net bir kurulum bulamadı.",
                "kullaniciya_soyle": ("Şu an işlem açmaya değer net bir kurulum yok. "
                                      "Zorlama işlem, en pahalı işlemdir — bekliyorum.")}

    # ------------------------------------------------- 2. likidite + dağıtım
    enriched: list[dict[str, Any]] = []
    for row in candidates[:wanted * 3]:
        symbol = row.get("symbol")
        if not symbol:
            continue
        try:
            df = fetch_ohlcv(market, exchange, symbol, timeframe, limit=200)
            profile = liquidity_profile(df, symbol, timeframe)
        except Exception as exc:  # noqa: BLE001
            # Likiditesi ölçülemeyen aday sessizce atlanmaz: sebebi kayda geçer,
            # yoksa "neden bu parite seçilmedi?" sorusu cevapsız kalır.
            log.info("likidite ölçülemedi, aday atlandı (%s): %s", symbol, exc)
            continue
        enriched.append({
            "symbol": symbol,
            "score": float(row.get("score") or row.get("confidence") or 0.5),
            "liquidity": profile,
            "action": row.get("action", "WAIT"),
        })
        if len(enriched) >= wanted:
            break

    if not enriched:
        return {"deployed": False, "error": "Adayların likiditesi ölçülemedi."}

    plan = allocate(capital, enriched)
    if not plan["allocations"]:
        return {"deployed": False,
                "error": "Sermaye dağıtılamadı: adayların likiditesi yetersiz.",
                "dagitim": plan}

    # --------------------------------------------------------- 3. mod ve kur
    mode_info = evaluate_mode(ctx.db, ctx.user)
    trading_mode = TradingMode.LIVE if mode_info["mode"] == "live" else TradingMode.PAPER

    llm = (ctx.db.query(Credential)
           .filter(Credential.user_id == ctx.user.id,
                   Credential.kind == CredentialKind.LLM).first())

    deployed: list[dict[str, Any]] = []
    for row in plan["allocations"]:
        symbol = row["symbol"]
        amount = row["amount"]
        profile = next((c["liquidity"] for c in enriched if c["symbol"] == symbol), None)

        picks = recommend_playbooks(
            regime=None, atr_pct=profile.volatility_pct if profile else None,
            market=market, horizon=args.get("horizon"),
            ai_available=llm is not None, limit=1,
        )
        pb = resolve_playbook(ctx.db, ctx.user, picks[0]["id"]) if picks else None
        if pb is None:
            continue

        decision_mode = pb.decision_mode
        if decision_mode != "algo_only" and llm is None:
            decision_mode = "algo_only"

        bot = Bot(
            user_id=ctx.user.id,
            name=f"{pb.label} · {symbol}"[:80],
            market=market, exchange=exchange, symbol=symbol,
            timeframe=pb.timeframe, mode=trading_mode, autonomy=Autonomy.FULL,
            decision_mode=decision_mode,
            llm_credential_id=llm.id if (llm and decision_mode != "algo_only") else None,
            poll_seconds=max(60, pb.poll_seconds),
            allow_short=pb.allow_short,
            strategies_json=json.dumps(pb.strategies),
            guards_json=json.dumps(pb.guards),
            council_mode=ctx.council_mode,
            min_agree=pb.min_agree,
            strategy_notes=f"[{pb.id}] {pb.thesis}"[:2000],
            risk_pct=min(pb.risk_pct, settings.hard_max_risk_pct),
            partial_tp_enabled=pb.partial_tp,
            initial_balance=amount, paper_balance=amount,
            peak_equity=amount, day_start_equity=amount,
        )
        ctx.db.add(bot)
        ctx.db.flush()

        execution = plan_execution(amount, profile, timeframe=pb.timeframe) if profile else None

        if args.get("start", True):
            bot.status = BotStatus.RUNNING
            ctx.db.flush()
            try:
                sched.start_bot_job(bot.id, bot.poll_seconds)
            except Exception as exc:  # noqa: BLE001
                log.warning("bot zamanlayıcıya eklenemedi (%s): %s", bot.id, exc)
                bot.status = BotStatus.STOPPED

        deployed.append({
            "bot_id": bot.id, "symbol": symbol, "system": pb.label,
            "capital": amount, "share_pct": row["share_pct"],
            "timeframe": pb.timeframe, "risk_pct": bot.risk_pct,
            "liquidity_tier": row.get("liquidity_tier"),
            "limited_by": row.get("limited_by"),
            "execution": execution.to_dict() if execution else None,
            "running": bot.status == BotStatus.RUNNING,
        })

    if not deployed:
        return {"deployed": False, "error": "Hiçbir bot kurulamadı."}

    log.info("portföy kuruldu: %d enstrüman, %.2f sermaye", len(deployed), capital)

    sliced = [d for d in deployed if d.get("execution", {}) and
              d["execution"].get("style") in ("sliced", "reduced")]

    return {
        "deployed": True,
        "mode": mode_info["mode"],
        "mode_reason": mode_info["reason"],
        "instruments": len(deployed),
        "total_capital": capital,
        "unallocated": plan["unallocated"],
        "bots": deployed,
        "dagitim_notu": plan["note"],
        "yurutme_notu": (
            f"{len(sliced)} enstrümanda emir piyasayı itmemek için parçalara bölündü."
            if sliced else "Emirler tek parçada geçebilecek büyüklükte."
        ),
        "kullaniciya_soyle": (
            f"{capital:,.0f} birimlik sermayeyi {len(deployed)} enstrümana dağıttım ve "
            f"her birine koşullara uyan sistemi kurdum. "
            f"{'Gerçek para modunda' if mode_info['mode'] == 'live' else 'Sanal modda'} "
            f"çalışıyorlar."
        ),
    }
