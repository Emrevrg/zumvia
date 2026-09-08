"""
OTOMASYON ARAÇLARI — ajan kendi zamanlamasını kurar
====================================================

Beceri "nasıl" yapılacağını bilir; otomasyon "ne zaman". Kullanıcı "her
sabah portföyümü kontrol et, bir şey değişirse haber ver" dediğinde bunu
her gün elle tetiklemek zorunda kalmamalı.

Değişmez ilke: **otomasyon YENİ YETKİ VERMEZ.** Çalıştırdığı beceri hangi
kısıtlara tabiyse otomasyon da aynısına tabidir. Zamanlamayı
otomatikleştirir, izni değil.
"""
from __future__ import annotations

from typing import Any

from ..core.logging import get_logger
from ..layers import automations
from ..layers.automations import AutomationError
from .tools import ToolContext, _obj, tool

log = get_logger("zumvia.agent.automation")


@tool(
    "create_automation",
    "KENDİ OTOMASYONUNU KUR. Bir beceriyi ya da serbest bir görevi düzenli "
    "aralıklarla çalıştırır. Kullanıcı 'her sabah', 'düzenli olarak', "
    "'sen takip et' gibi bir şey söylediğinde bunu çağır. Otomasyon YENİ "
    "YETKİ VERMEZ: çalıştırdığı iş, sen elle çalıştırmış gibi aynı risk "
    "kalkanından geçer.",
    _obj({
        "label": {"type": "string", "description": "Otomasyonun adı"},
        "purpose": {"type": "string",
                    "description": "NE İŞE YARADIĞI (en az 20 karakter)"},
        "action": {"type": "string",
                   "description": "'skill' (yazılmış bir beceri) ya da "
                                  "'task' (ajana serbest görev)"},
        "skill_slug": {"type": "string",
                       "description": "action='skill' ise çalıştırılacak beceri"},
        "task_prompt": {"type": "string",
                        "description": "action='task' ise ajana verilecek görev"},
        "inputs": {"type": "object", "description": "Beceriye geçilecek parametreler"},
        "schedule": {"type": "string", "description": "'interval' ya da 'daily'"},
        "every_minutes": {"type": "integer",
                          "description": "interval için: kaç dakikada bir (en az 15)"},
        "at_hour": {"type": "integer", "description": "daily için: saat (0-23)"},
        "at_minute": {"type": "integer", "description": "daily için: dakika (0-59)"},
        "notify": {"type": "boolean",
                   "description": "Sonucu Telegram'a yaz (varsayılan hayır)"},
    }, ["label", "purpose", "action"]),
    mutating=True,
)
def _create_automation(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        row = automations.save(
            ctx.db, ctx.user,
            label=str(args["label"]), purpose=str(args["purpose"]),
            action=str(args["action"]),
            skill_slug=str(args.get("skill_slug", "")),
            task_prompt=str(args.get("task_prompt", "")),
            inputs=args.get("inputs") or {},
            schedule=str(args.get("schedule", "interval")),
            every_minutes=int(args.get("every_minutes", 60)),
            at_hour=int(args.get("at_hour", 9)),
            at_minute=int(args.get("at_minute", 0)),
            notify=bool(args.get("notify", False)),
            author_model=_author(ctx),
        )
    except AutomationError as exc:
        return {"created": False, "error": str(exc)}

    return {"created": True, "otomasyon": automations.describe(row),
            "not": "Otomasyon kuruldu ve zamanlandı. `run_automation_now` ile "
                   "hemen bir kez deneyip çalıştığını görmen iyi olur — "
                   "kaydedilmiş olması çalıştığını göstermez."}


@tool(
    "list_automations",
    "Kurulmuş otomasyonları ve sicillerini listeler: ne zaman çalışıyorlar, "
    "kaç kez çalıştılar, kaç kez hata verdiler. Yeni bir otomasyon kurmadan "
    "önce çağır — aynısı zaten olabilir.",
    _obj({}, []),
)
def _list_automations(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    rows = automations.listing(ctx.db, ctx.user)
    return {"otomasyonlar": [automations.describe(r) for r in rows],
            "toplam": len(rows), "sinir": automations.MAX_PER_USER}


@tool(
    "edit_automation",
    "Var olan bir otomasyonu değiştirir: zamanlamasını, çalıştırdığı işi ya "
    "da açık/kapalı durumunu. Sicil korunur. Bir otomasyon işe yaramıyorsa "
    "silmek yerine önce burada düzeltmeyi dene.",
    _obj({
        "slug": {"type": "string"},
        "label": {"type": "string"},
        "purpose": {"type": "string"},
        "action": {"type": "string"},
        "skill_slug": {"type": "string"},
        "task_prompt": {"type": "string"},
        "inputs": {"type": "object"},
        "schedule": {"type": "string"},
        "every_minutes": {"type": "integer"},
        "at_hour": {"type": "integer"},
        "at_minute": {"type": "integer"},
        "enabled": {"type": "boolean", "description": "Geçici olarak durdur/başlat"},
        "notify": {"type": "boolean"},
    }, ["slug"]),
    mutating=True,
)
def _edit_automation(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    changes = {k: v for k, v in args.items() if k != "slug" and v is not None}
    if not changes:
        return {"updated": False, "error": "Değiştirilecek alan verilmedi."}
    try:
        row = automations.edit(ctx.db, ctx.user, str(args["slug"]),
                               changes, author_model=_author(ctx))
    except AutomationError as exc:
        return {"updated": False, "error": str(exc)}
    return {"updated": True, "otomasyon": automations.describe(row)}


@tool(
    "run_automation_now",
    "Bir otomasyonu ZAMANINI BEKLEMEDEN hemen çalıştırır. Yeni kurduğun bir "
    "otomasyonu denemek ya da kullanıcı 'şimdi çalıştır' dediğinde kullan.",
    _obj({"slug": {"type": "string"}}, ["slug"]),
    mutating=True,
)
def _run_automation_now(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    row = automations.find(ctx.db, ctx.user, str(args["slug"]))
    if row is None:
        return {"error": f"'{args['slug']}' adında bir otomasyonunuz yok."}
    result = automations.run_one(ctx.db, ctx.user, row)
    return {"otomasyon": row.slug, "sonuc": result,
            "sicil": automations.describe(row)}


@tool(
    "delete_automation",
    "Bir otomasyonu kalıcı olarak siler. Geçici olarak durdurmak istiyorsan "
    "silmek yerine `edit_automation` ile enabled=false yap — sicili korunur.",
    _obj({"slug": {"type": "string"}}, ["slug"]),
    mutating=True,
)
def _delete_automation(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ok = automations.remove(ctx.db, ctx.user, str(args["slug"]))
    return {"deleted": ok} if ok else {
        "deleted": False, "error": f"'{args['slug']}' bulunamadı."}


def _author(ctx: ToolContext) -> str:
    from ..models import AgentSession  # noqa: PLC0415

    session = ctx.db.get(AgentSession, ctx.session_id) if ctx.session_id else None
    return session.control_model if session else ""
