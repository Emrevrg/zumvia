"""
KİŞİYE ÖZEL SİSTEM TASARIMI
============================
Ajan, hazır kütüphanedeki hiçbir sistem kullanıcının durumuna uymuyorsa
**kendi sistemini tasarlayabilir**. Ama serbestçe strateji kodu yazarak değil,
platformun doğrulanmış yapı taşlarını birleştirerek:

    design_custom_bot  ->  tasarla + geçmiş veride DOĞRULA + kaydet
    deploy_playbook    ->  hazır veya özel, farkı yok: kur

Kod seviyesinde zorlanan sınırlar:
  * Yalnızca var olan stratejiler seçilebilir (uydurma isim reddedilir).
  * Hemfikirlik eşiği strateji sayısını aşamaz (ulaşılamaz kurulum olmaz).
  * Risk, sistem tavanının üstüne çıkamaz.
  * Tez, zayıf yan ve kaçınma koşulu yazılmak ZORUNDADIR.
  * Doğrulama başarısızsa sistem "doğrulanmadı" damgası alır ve bu bilgi
    hem araç çıktısında hem arayüzde görünür.

Yani "yapay zeka bot yazabilir" ile "yapay zeka uydurma strateji çalıştıramaz"
aynı anda doğrudur.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..core.config import settings
from ..core.logging import get_logger
from ..layers.playbooks import PLAYBOOKS
from ..layers.strategies import STRATEGIES
from ..models import CustomPlaybook
from .tools import _MARKET, _SYMBOL, ToolContext, _obj, tool

log = get_logger("zumvia.tools.design")

MIN_STRATEGIES = 2
MAX_STRATEGIES = 6
_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slugify(text: str) -> str:
    table = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    slug = _SLUG_RE.sub("_", text.translate(table).lower()).strip("_")
    return (slug or "ozel_sistem")[:60]


@tool(
    "design_custom_bot",
    "HAZIR SİSTEMLERİN HİÇBİRİ uymuyorsa kullanıcıya özel bir sistem tasarlar. "
    "Önce `recommend_playbook` çalıştır; skoru 0.6'nın altındaysa veya kullanıcının "
    "özel bir talebi varsa bunu kullan. Sistem, seçtiğin stratejilerle geçmiş veride "
    "OTOMATİK doğrulanır; sonuç kaydedilir. Zayıf yanını dürüstçe yazmak zorundasın.",
    _obj({
        "label": {"type": "string", "description": "Kısa, açıklayıcı ad"},
        "reason": {"type": "string",
                   "description": "Hazır sistemler neden yetmedi? Somut gerekçe."},
        "thesis": {"type": "string", "description": "Sistem hangi koşulda neyi hedefler (tek cümle; getiri vaat etme)"},
        "strength": {"type": "string", "description": "Güçlü olduğu ortam"},
        "weakness": {"type": "string", "description": "Nerede para kaybettirir (ZORUNLU, dürüst)"},
        "avoid_when": {"type": "string", "description": "Hangi koşulda kullanılmamalı"},
        "strategies": {"type": "array", "items": {"type": "string"},
                       "description": f"{MIN_STRATEGIES}-{MAX_STRATEGIES} adet. "
                                      f"Geçerli olanlar: {', '.join(sorted(STRATEGIES))}"},
        "min_agree": {"type": "integer", "description": "Kaç strateji hemfikir olmalı"},
        "timeframe": {"type": "string", "enum": ["5m", "15m", "30m", "1h", "4h", "1d"]},
        "risk_pct": {"type": "number",
                     "description": f"İşlem başına risk, en fazla {settings.hard_max_risk_pct}"},
        "allow_short": {"type": "boolean"},
        "horizon": {"type": "string", "enum": ["kısa", "orta", "uzun"]},
        "validate_on": _SYMBOL,
        "market": _MARKET,
    }, ["label", "reason", "thesis", "weakness", "avoid_when", "strategies",
        "timeframe", "validate_on", "market"]),
    mutating=True,
)
def _design_custom_bot(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    # ---------------------------------------------------------------- doğrula
    raw = [str(s).strip() for s in (args.get("strategies") or [])]
    unknown = [s for s in raw if s not in STRATEGIES]
    if unknown:
        return {
            "created": False,
            "error": f"Bilinmeyen strateji: {unknown}. Strateji UYDURAMAZSIN.",
            "gecerli_stratejiler": sorted(STRATEGIES),
        }

    strategies = list(dict.fromkeys(raw))           # tekrarları at, sırayı koru
    if not (MIN_STRATEGIES <= len(strategies) <= MAX_STRATEGIES):
        return {"created": False,
                "error": f"Strateji sayısı {MIN_STRATEGIES}-{MAX_STRATEGIES} arasında olmalı "
                         f"(tek stratejiye güvenmek sistemi kırılgan yapar)."}

    for field in ("thesis", "weakness", "avoid_when", "reason"):
        if len(str(args.get(field, "")).strip()) < 15:
            return {"created": False,
                    "error": f"'{field}' alanı çok kısa. Zayıf yanını ve gerekçeni "
                             f"dürüstçe yazmadan sistem kaydedilmez."}

    min_agree = max(1, min(int(args.get("min_agree", 2)), len(strategies)))
    risk_pct = min(float(args.get("risk_pct", 0.7)), settings.hard_max_risk_pct)
    timeframe = args["timeframe"]
    symbol = args["validate_on"]
    market = args["market"]

    slug = _slugify(args["label"])
    if slug in PLAYBOOKS:
        slug = f"{slug}_ozel"

    # ------------------------------------------------------- geçmişte doğrula
    validation, validated = _validate_setup(ctx, market, symbol, timeframe,
                                            strategies, min_agree, risk_pct)

    # ------------------------------------------------------------------ kaydet
    existing = (ctx.db.query(CustomPlaybook)
                .filter(CustomPlaybook.user_id == ctx.user.id,
                        CustomPlaybook.slug == slug).first())
    book = existing or CustomPlaybook(user_id=ctx.user.id, slug=slug)

    book.label = str(args["label"])[:96]
    book.thesis = str(args["thesis"])[:1000]
    book.strength = str(args.get("strength", ""))[:1000]
    book.weakness = str(args["weakness"])[:1000]
    book.avoid_when = str(args["avoid_when"])[:1000]
    book.strategies_json = json.dumps(strategies)
    book.min_agree = min_agree
    book.timeframe = timeframe
    book.risk_pct = risk_pct
    book.decision_mode = "hybrid"
    book.poll_seconds = {"5m": 180, "15m": 300, "30m": 600,
                         "1h": 900, "4h": 1800, "1d": 3600}.get(timeframe, 900)
    book.allow_short = bool(args.get("allow_short", False))
    book.partial_tp = True
    book.fits_markets_json = json.dumps([market])
    book.horizon = str(args.get("horizon", "orta"))
    # Özgün sistem de korumasız sahaya çıkmaz: seçilen stratejilerin doğasına
    # göre makul bir koruma seti otomatik türetilir.
    book.guards_json = json.dumps(_default_guards(strategies, timeframe))
    book.designed_by = (getattr(ctx, "model_name", "") or "ajan")[:96]
    book.design_reason = str(args["reason"])[:1000]
    book.validation_json = json.dumps(validation, ensure_ascii=False)[:4000]
    book.validated = validated

    if existing is None:
        ctx.db.add(book)
    ctx.db.flush()

    log.info("özel sistem tasarlandı: %s (doğrulandı=%s)", slug, validated)
    return {
        "created": True,
        "playbook_id": slug,
        "label": book.label,
        "strategies": strategies,
        "min_agree": min_agree,
        "timeframe": timeframe,
        "risk_pct": risk_pct,
        "dogrulandi": validated,
        "dogrulama": validation,
        "sonraki_adim": f"`deploy_playbook` ile playbook_id='{slug}' vererek kurabilirsin."
                        if validated else
                        "Doğrulama yetersiz. Ya stratejileri değiştir ya da kullanıcıya "
                        "kanıtın zayıf olduğunu söyleyerek hazır bir sistemi öner.",
        "kullaniciya_soyle": f"Size özel bir sistem tasarladım: {book.label}. "
                             f"Zayıf yanı: {book.weakness}",
    }


@tool(
    "list_custom_bots",
    "Bu kullanıcı için daha önce tasarlanmış özel sistemleri listeler. Yeni bir "
    "tasarım yapmadan önce bak — aynı şeyi ikinci kez tasarlama.",
    _obj({}),
)
def _list_custom_bots(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    rows = (ctx.db.query(CustomPlaybook)
            .filter(CustomPlaybook.user_id == ctx.user.id)
            .order_by(CustomPlaybook.id.desc()).all())
    return {
        "count": len(rows),
        "custom_playbooks": [{
            "playbook_id": r.slug, "label": r.label, "thesis": r.thesis,
            "strategies": json.loads(r.strategies_json), "min_agree": r.min_agree,
            "timeframe": r.timeframe, "risk_pct": r.risk_pct,
            "weakness": r.weakness, "dogrulandi": r.validated,
            "tasarlayan": r.designed_by, "kurulum_sayisi": r.deploy_count,
        } for r in rows],
    }


@tool(
    "update_custom_bot",
    "KENDİ TASARLADIĞIN BOTU GELİŞTİR. Yalnızca değiştirmek istediğin alanları "
    "ver; gerisi korunur. Bir sistem beklediğin gibi çalışmıyorsa yenisini "
    "sıfırdan yazmak yerine bunu kullan: sicili ve doğrulama geçmişi korunur, "
    "aynı hataları tekrar yapmazsın. Strateji seti değişirse sistem yeniden "
    "DOĞRULANMAMIŞ sayılır — çünkü eski kanıt yeni kurulumu bağlamaz.",
    _obj({
        "playbook_id": {"type": "string", "description": "Geliştirilecek özel bot"},
        "label": {"type": "string"},
        "strategies": {"type": "array", "items": {"type": "string"},
                       "description": "Yeni strateji seti (sadece kayıtlı olanlar)"},
        "min_agree": {"type": "integer"},
        "timeframe": {"type": "string"},
        "risk_pct": {"type": "number"},
        "thesis": {"type": "string"},
        "weakness": {"type": "string", "description": "Zayıf yanı — dürüstlük kuralı"},
        "avoid_when": {"type": "string", "description": "Ne zaman KULLANILMAMALI"},
        "allow_short": {"type": "boolean"},
        "reason": {"type": "string",
                   "description": "Neden değiştirdiğin — somut yaz (zorunlu)"},
    }, ["playbook_id", "reason"]),
    mutating=True,
)
def _update_custom_bot(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    row = (ctx.db.query(CustomPlaybook)
           .filter(CustomPlaybook.user_id == ctx.user.id,
                   CustomPlaybook.slug == str(args["playbook_id"])).first())
    if row is None:
        return {"updated": False,
                "error": f"'{args['playbook_id']}' adında bir özel botunuz yok. "
                         f"Hazır sistemleri değiştirmek için `fork_playbook` kullan."}

    if len(str(args.get("reason", "")).strip()) < 15:
        return {"updated": False,
                "error": "'reason' çok kısa. Neyi neden değiştirdiğini somut yaz; "
                         "gerekçesiz değişiklik, öğrenme değil rastgele deneme olur."}

    changed: list[str] = []

    if args.get("strategies"):
        raw = [str(x).strip() for x in args["strategies"]]
        unknown = [x for x in raw if x not in STRATEGIES]
        if unknown:
            return {"updated": False,
                    "error": f"Bilinmeyen strateji: {unknown}. Strateji UYDURAMAZSIN.",
                    "gecerli_stratejiler": sorted(STRATEGIES)}
        strategies = list(dict.fromkeys(raw))
        if not (MIN_STRATEGIES <= len(strategies) <= MAX_STRATEGIES):
            return {"updated": False,
                    "error": f"Strateji sayısı {MIN_STRATEGIES}-{MAX_STRATEGIES} "
                             f"arasında olmalı."}
        row.strategies_json = json.dumps(strategies)
        row.min_agree = max(1, min(row.min_agree, len(strategies)))
        # Strateji seti değiştiyse eski kanıt bu kurulumu BAĞLAMAZ.
        row.validated = False
        changed.append("strategies")

    if args.get("min_agree") is not None:
        current = json.loads(row.strategies_json or "[]")
        row.min_agree = max(1, min(int(args["min_agree"]), len(current) or 1))
        changed.append("min_agree")

    if args.get("risk_pct") is not None:
        row.risk_pct = min(float(args["risk_pct"]), settings.hard_max_risk_pct)
        changed.append("risk_pct")

    for field_name in ("label", "timeframe", "thesis", "weakness", "avoid_when"):
        value = args.get(field_name)
        if value:
            setattr(row, field_name, str(value)[:400] if field_name != "label"
                    else str(value)[:96])
            changed.append(field_name)

    if args.get("allow_short") is not None:
        row.allow_short = bool(args["allow_short"])
        changed.append("allow_short")

    if not changed:
        return {"updated": False, "error": "Değiştirilecek alan verilmedi."}

    ctx.db.commit()
    log.info("özel bot geliştirildi: %s — %s (%s)", row.slug,
             ", ".join(changed), str(args["reason"])[:100])

    return {
        "updated": True,
        "playbook_id": row.slug,
        "degisen": changed,
        "dogrulandi": row.validated,
        "not": ("Strateji seti değişti; sistem artık DOĞRULANMAMIŞ sayılıyor. "
                "Kurmadan önce `run_backtest` ya da `validate_strategy` ile "
                "yeniden kanıt topla."
                if "strategies" in changed else
                "Değişiklik kaydedildi. Kurmadan önce kanıtı gözden geçir."),
    }


@tool(
    "delete_custom_bot",
    "Kendi tasarladığın bir özel botu siler. Sicili ve doğrulama geçmişi de "
    "gider. Yalnızca kullanıcı açıkça istediğinde kullan — bozuk bir sistemi "
    "silmek yerine `update_custom_bot` ile düzeltmek neredeyse her zaman "
    "daha iyidir.",
    _obj({"playbook_id": {"type": "string"}}, ["playbook_id"]),
    mutating=True,
)
def _delete_custom_bot(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    row = (ctx.db.query(CustomPlaybook)
           .filter(CustomPlaybook.user_id == ctx.user.id,
                   CustomPlaybook.slug == str(args["playbook_id"])).first())
    if row is None:
        return {"deleted": False, "error": f"'{args['playbook_id']}' bulunamadı."}
    ctx.db.delete(row)
    ctx.db.commit()
    log.info("özel bot silindi: %s", args["playbook_id"])
    return {"deleted": True, "playbook_id": args["playbook_id"]}


@tool(
    "fork_playbook",
    "Hazır bir sistemi TEMEL ALIP değiştirir ve kullanıcının 'Özel botlarım' "
    "listesine kaydeder. Sıfırdan tasarlamak yerine çalıştığı bilinen bir "
    "sistemi ayarlamak daha güvenlidir: sadece değiştirmek istediğin alanları "
    "ver, gerisi orijinalinden gelir. Değişiklik geçmiş veride doğrulanır.",
    _obj({
        "playbook_id": {"type": "string", "description": "Temel alınacak hazır sistem"},
        "label": {"type": "string", "description": "Yeni sistemin adı"},
        "reason": {"type": "string",
                   "description": "Neden değiştirdin? (kullanıcının somut ihtiyacı)"},
        "strategies": {"type": "array", "items": {"type": "string"},
                       "description": "Boş bırakılırsa orijinal set korunur"},
        "min_agree": {"type": "integer"},
        "timeframe": {"type": "string", "enum": ["5m", "15m", "30m", "1h", "4h", "1d"]},
        "risk_pct": {"type": "number"},
        "allow_short": {"type": "boolean"},
        "max_trades_per_day": {"type": "integer", "description": "Günlük işlem tavanı"},
        "validate_on": _SYMBOL,
        "market": _MARKET,
    }, ["playbook_id", "label", "reason", "validate_on", "market"]),
    mutating=True,
)
def _fork_playbook(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """
    Var olan sistemi klonlayıp değiştirir.

    Neden ayrı bir araç? Çünkü sıfırdan tasarlamak her seferinde tüm alanları
    doldurmayı gerektirir ve ajanın hata yapma alanı büyür. Fork'ta yalnızca
    DEĞİŞEN alan verilir; korumalar (guards) ve doğrulanmış varsayılanlar
    orijinalinden miras alınır.
    """
    from ..layers.playbooks import resolve_playbook  # noqa: PLC0415

    base = resolve_playbook(ctx.db, ctx.user, args["playbook_id"])
    if base is None:
        return {"created": False, "error": f"Bilinmeyen sistem: {args['playbook_id']}"}

    strategies = [str(s).strip() for s in (args.get("strategies") or base.strategies)]
    unknown = [s for s in strategies if s not in STRATEGIES]
    if unknown:
        return {"created": False,
                "error": f"Bilinmeyen strateji: {unknown}. Strateji UYDURAMAZSIN.",
                "gecerli_stratejiler": sorted(STRATEGIES)}

    if len(str(args.get("reason", "")).strip()) < 15:
        return {"created": False,
                "error": "'reason' çok kısa. Neden değiştirdiğini somut yaz."}

    min_agree = max(1, min(int(args.get("min_agree", base.min_agree)), len(strategies)))
    risk_pct = min(float(args.get("risk_pct", base.risk_pct)), settings.hard_max_risk_pct)
    timeframe = args.get("timeframe") or base.timeframe
    symbol = args["validate_on"]
    market = args["market"]

    slug = _slugify(args["label"])
    if slug in PLAYBOOKS:
        slug = f"{slug}_ozel"

    # Korumalar orijinalinden gelir; yalnızca işlem tavanı istenirse değişir
    guards = dict(base.guards)
    if args.get("max_trades_per_day"):
        guards["max_trades_per_day"] = max(1, int(args["max_trades_per_day"]))

    validation, validated = _validate_setup(ctx, market, symbol, timeframe,
                                            strategies, min_agree, risk_pct)

    existing = (ctx.db.query(CustomPlaybook)
                .filter(CustomPlaybook.user_id == ctx.user.id,
                        CustomPlaybook.slug == slug).first())
    book = existing or CustomPlaybook(user_id=ctx.user.id, slug=slug)

    book.label = str(args["label"])[:96]
    book.thesis = base.thesis
    book.strength = base.strength
    book.weakness = base.weakness
    book.avoid_when = base.avoid_when
    book.strategies_json = json.dumps(strategies)
    book.min_agree = min_agree
    book.timeframe = timeframe
    book.risk_pct = risk_pct
    book.decision_mode = base.decision_mode
    book.poll_seconds = base.poll_seconds
    book.allow_short = bool(args.get("allow_short", base.allow_short))
    book.partial_tp = base.partial_tp
    book.fits_regimes_json = json.dumps(base.fits_regimes)
    book.fits_markets_json = json.dumps([market])
    book.volatility = base.volatility
    book.horizon = base.horizon
    book.guards_json = json.dumps(guards)
    book.forked_from = base.id
    book.designed_by = (getattr(ctx, "model_name", "") or "ajan")[:96]
    book.design_reason = str(args["reason"])[:1000]
    book.validation_json = json.dumps(validation, ensure_ascii=False)[:4000]
    book.validated = validated

    if existing is None:
        ctx.db.add(book)
    ctx.db.flush()

    changes = []
    if strategies != list(base.strategies):
        changes.append(f"strateji seti: {len(strategies)} strateji")
    if min_agree != base.min_agree:
        changes.append(f"teyit eşiği {base.min_agree} → {min_agree}")
    if timeframe != base.timeframe:
        changes.append(f"zaman dilimi {base.timeframe} → {timeframe}")
    if abs(risk_pct - base.risk_pct) > 1e-9:
        changes.append(f"risk %{base.risk_pct} → %{risk_pct}")

    log.info("sistem çatallandı: %s -> %s", base.id, slug)
    return {
        "created": True,
        "playbook_id": slug,
        "label": book.label,
        "temel_alinan": base.label,
        "degisiklikler": changes or ["yalnızca ad değişti"],
        "korumalar_miras_alindi": len(guards),
        "dogrulandi": validated,
        "dogrulama": validation,
        "kullaniciya_soyle": (
            f"{base.label} sistemini temel alıp size özel bir sürüm hazırladım: "
            f"{book.label}. Değişen: {', '.join(changes) if changes else 'ad'}."
        ),
    }


def _default_guards(strategies: list[str], timeframe: str) -> dict[str, Any]:
    """
    Ajanın tasarladığı sisteme, seçtiği stratejilerin doğasına uygun korumalar
    türetir. Amaç: özgün sistemin de bilinen tuzaklara karşı kapalı olması.
    """
    guards: dict[str, Any] = {
        "min_candles": 220,
        "max_spread_pct": {"5m": 0.06, "15m": 0.08, "30m": 0.10,
                           "1h": 0.12, "4h": 0.18, "1d": 0.25}.get(timeframe, 0.12),
        "max_trades_per_day": {"5m": 4, "15m": 4, "30m": 3,
                               "1h": 3, "4h": 2, "1d": 1}.get(timeframe, 3),
        "cooldown_bars": 3,
    }

    trend_like = {"trend_following", "turtle_55", "chandelier_trend",
                  "pullback_ema", "stoch_pullback"}
    range_like = {"mean_reversion", "vwap_reversion", "rsi_divergence"}
    breakout_like = {"breakout", "squeeze_expansion", "obv_thrust"}
    picked = set(strategies)

    # Trend ağırlıklıysa yatay piyasada susar
    if len(picked & trend_like) >= len(picked & range_like):
        guards["adx_min"] = 18
        guards["require_higher_tf_agreement"] = True
    # Ortalamaya dönüş ağırlıklıysa trend başlayınca çekilir
    if picked & range_like and not (picked & trend_like):
        guards["adx_max"] = 24
    # Kırılım varsa hacim teyidi ister
    if picked & breakout_like:
        guards["volume_z_min"] = 0.6

    return guards

def _validate_setup(ctx: ToolContext, market: str, symbol: str, timeframe: str,
                    strategies: list[str], min_agree: int,
                    risk_pct: float) -> tuple[dict[str, Any], bool]:
    """Ortak doğrulama: geçmiş veride kâr faktörü ve işlem sayısı eşiği."""
    try:
        from .tools import _run_backtest  # noqa: PLC0415
        result = _run_backtest(ctx, {
            "market": market, "symbol": symbol, "timeframe": timeframe,
            "strategies": strategies, "min_agree": min_agree,
            "risk_pct": risk_pct, "candles": 1500,
        })
        result["ran"] = True
        metrics = result.get("metrics") or {}
        pf = float(metrics.get("profit_factor") or 0.0)
        trades = int(metrics.get("trade_count") or 0)
        passed = trades >= 8 and pf >= 1.2
        result["verdict"] = ("geçti" if passed
                             else f"yetersiz kanıt (işlem {trades}, kâr faktörü {pf:.2f})")
        return result, passed
    except Exception as exc:  # noqa: BLE001
        log.warning("doğrulama yapılamadı: %s", exc)
        return {"ran": False, "error": str(exc)[:300]}, False
