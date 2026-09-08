"""
BECERİ YAZMA ARAÇLARI — ajan kendi yeteneğini kurar ve geliştirir
==================================================================

Platformun araç kutusu sabittir ve olması gerektiği gibidir: her araç
testlidir, her risk aracı kendi korumasından geçer. Ama kullanıcının işi
tekrar eden bir akışsa — "her sabah 5 pariteyi tara, kanıt topla, en iyisini
raporla" — bunu her seferinde elle kurmak yorucu ve hataya açıktır.

Bu araçlar ajanın o akışı BİR KEZ yazıp adlandırmasına izin verir. Yazdığı
şey kod değil, doğrulanmış araçlardan oluşan bir tariftir; bu yüzden
"yapay zeka kendi becerisini yazabilir" ile "yapay zeka keyfi kod
çalıştıramaz" aynı anda doğru kalır.

Beceri geliştirmek de bu döngünün parçasıdır: `inspect_skill` ile tarifi
oku, `edit_skill` ile düzelt. Sicil (kaç kez çalıştı, kaç kez patladı)
korunur — bir becerinin geçmişi, o beceri hakkındaki en değerli bilgidir.
"""
from __future__ import annotations

from typing import Any

from ..core.logging import get_logger
from ..layers import custom_skills
from ..layers.custom_skills import SkillError
from .tools import ToolContext, _obj, tool

log = get_logger("zumvia.agent.skills")

_STEPS_SCHEMA = {
    "type": "array",
    "description": "Sıralı adımlar. Her adım: {tool, args, save_as}. "
                   "Bir adımın çıktısına sonraki adımlarda {save_as.alan} "
                   "yazımıyla ulaşılır.",
    "items": {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "description": "Kayıtlı araç adı"},
            "args": {"type": "object", "description": "Araç argümanları"},
            "save_as": {"type": "string",
                        "description": "Çıktının adı (sonraki adımlar kullanır)"},
        },
        "required": ["tool"],
    },
}

_INPUTS_SCHEMA = {
    "type": "array",
    "description": "Beceri çalışırken istenecek parametreler.",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "required": {"type": "boolean"},
            "default": {"description": "Verilmezse kullanılacak değer"},
        },
        "required": ["name"],
    },
}


@tool(
    "list_tools",
    "Bir beceri yazarken kullanabileceğin TÜM araçların adını ve ne işe "
    "yaradığını listeler. Beceri yazmadan ÖNCE bunu çağır: araç adı "
    "uyduramazsın, uydurursan beceri kaydedilmez.",
    _obj({}, []),
)
def _list_tools(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    from .tools import tool_manifest  # noqa: PLC0415

    forbidden = custom_skills.FORBIDDEN_TOOLS
    rows = [{"arac": t["name"], "ne_yapar": t["description"]}
            for t in tool_manifest() if t["name"] not in forbidden]
    return {
        "araclar": rows,
        "toplam": len(rows),
        "beceriye_gomulemez": sorted(forbidden),
        "not": ("Yasaklı araçlar bir becerinin içine gömülemez: bunlar "
                "kullanıcının bilerek vermesi gereken kararlardır "
                "(gerçek paraya geçiş, acil fren)."),
    }


@tool(
    "create_skill",
    "KENDİ BECERİNİ YAZ. Tekrar eden bir iş akışını adlandırılmış, yeniden "
    "kullanılabilir bir beceriye dönüştürür. Serbest kod yazmazsın: yalnızca "
    "kayıtlı araçları sıralarsın. Bir adımın çıktısını sonraki adımda "
    "{ad.alan} yazımıyla kullanabilirsin. Kullanıcı aynı işi tekrar tekrar "
    "istiyorsa ya da sen bir akışı ikinci kez kuruyorsan bunu çağır.",
    _obj({
        "label": {"type": "string", "description": "Becerinin adı"},
        "description": {"type": "string",
                        "description": "NE YAPTIĞI (en az 20 karakter)"},
        "when_used": {"type": "string",
                      "description": "NE ZAMAN kullanılacağı — kullanılmayacağı "
                                     "durumu da yaz (en az 20 karakter)"},
        "steps": _STEPS_SCHEMA,
        "inputs": _INPUTS_SCHEMA,
        "category": {"type": "string",
                     "description": "risk | analiz | yürütme | veri | otomasyon"},
    }, ["label", "description", "when_used", "steps"]),
    mutating=True,
)
def _create_skill(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        row = custom_skills.save(
            ctx.db, ctx.user,
            label=str(args["label"]),
            description=str(args["description"]),
            when_used=str(args["when_used"]),
            steps=args.get("steps") or [],
            inputs=args.get("inputs") or [],
            category=str(args.get("category", "otomasyon")),
            author_model=_author(ctx),
        )
    except SkillError as exc:
        return {"created": False, "error": str(exc)}

    return {
        "created": True,
        "beceri": custom_skills.describe(row),
        "not": ("Beceri kaydedildi ama HENÜZ ÇALIŞTIRILMADI. Güvenilir "
                "sayılması için `run_skill` ile en az bir kez denenmeli — "
                "kaydedilmiş olması çalıştığını göstermez."),
    }


@tool(
    "list_skills",
    "Bu kullanıcı için daha önce YAZILMIŞ becerileri listeler: ne yaptıkları, "
    "ne zaman kullanıldıkları ve sicilleri (kaç kez çalıştı, kaç kez hata "
    "verdi). Yeni bir beceri yazmadan önce bunu çağır — aynısı zaten olabilir.",
    _obj({}, []),
)
def _list_skills(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    rows = custom_skills.listing(ctx.db, ctx.user)
    return {
        "beceriler": [{
            "slug": r.slug, "label": r.label, "ne_yapar": r.description,
            "ne_zaman": r.when_used, "adim": len(r.steps_json.split('"tool"')) - 1,
            "surum": r.version, "calisma": r.runs, "hata": r.failures,
        } for r in rows],
        "toplam": len(rows),
    }


@tool(
    "inspect_skill",
    "Bir becerinin TAM TARİFİNİ gösterir: hangi araçlar, hangi sırayla, hangi "
    "argümanlarla çağrılıyor. Bir beceriyi geliştirmeden ya da benzerini "
    "yazmadan önce çağır — neyi değiştireceğini görmeden değiştirme.",
    _obj({"slug": {"type": "string", "description": "Beceri kimliği"}}, ["slug"]),
)
def _inspect_skill(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    row = custom_skills.find(ctx.db, ctx.user, str(args["slug"]))
    if row is None:
        return {"error": f"'{args['slug']}' adında bir beceriniz yok."}
    return custom_skills.describe(row)


@tool(
    "edit_skill",
    "VAR OLAN BİR BECERİYİ GELİŞTİR. Yalnızca değiştirmek istediğin alanları "
    "ver; gerisi korunur. Sicil (çalışma sayısı, hata sayısı) SİLİNMEZ ve "
    "sürüm numarası artar. Bir beceri hata veriyorsa önce `inspect_skill` ile "
    "tarifi oku, sonra bozuk adımı burada düzelt.",
    _obj({
        "slug": {"type": "string", "description": "Geliştirilecek beceri"},
        "label": {"type": "string"},
        "description": {"type": "string"},
        "when_used": {"type": "string"},
        "steps": _STEPS_SCHEMA,
        "inputs": _INPUTS_SCHEMA,
        "category": {"type": "string"},
        "reason": {"type": "string",
                   "description": "Neden değiştirdiğin (kayda geçer)"},
    }, ["slug"]),
    mutating=True,
)
def _edit_skill(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    changes = {k: v for k, v in args.items()
               if k in {"label", "description", "when_used", "steps",
                        "inputs", "category"} and v is not None}
    if not changes:
        return {"updated": False,
                "error": "Değiştirilecek alan verilmedi."}

    try:
        row = custom_skills.edit(ctx.db, ctx.user, str(args["slug"]),
                                 changes=changes, author_model=_author(ctx))
    except SkillError as exc:
        return {"updated": False, "error": str(exc)}

    log.info("beceri geliştirildi: %s — %s", row.slug,
             str(args.get("reason", ""))[:120])
    return {"updated": True, "surum": row.version,
            "beceri": custom_skills.describe(row),
            "not": "Değişiklikten sonra `run_skill` ile bir kez dene: "
                   "düzelttiğini sandığın adım hâlâ bozuk olabilir."}


@tool(
    "run_skill",
    "Yazılmış bir beceriyi çalıştırır. Adımlar sırayla yürütülür; bir adım "
    "hata verirse zincir DURUR ve hatanın hangi adımda olduğu bildirilir. "
    "Risk ve emir araçları beceri içinden çağrılsa da kendi korumalarından "
    "geçmeye devam eder.",
    _obj({
        "slug": {"type": "string", "description": "Çalıştırılacak beceri"},
        "inputs": {"type": "object",
                   "description": "Becerinin istediği parametreler"},
    }, ["slug"]),
    mutating=True,
)
def _run_skill(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return custom_skills.run(ctx, ctx.db, ctx.user, str(args["slug"]),
                             args.get("inputs") or {})


@tool(
    "delete_skill",
    "Bir beceriyi kalıcı olarak siler. Sicili de gider. Yalnızca kullanıcı "
    "açıkça istediğinde ya da beceri onarılamaz biçimde bozuksa kullan — "
    "bozuk bir beceriyi silmek yerine `edit_skill` ile düzeltmek yeğdir.",
    _obj({"slug": {"type": "string"}}, ["slug"]),
    mutating=True,
)
def _delete_skill(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ok = custom_skills.remove(ctx.db, ctx.user, str(args["slug"]))
    return {"deleted": ok} if ok else {
        "deleted": False, "error": f"'{args['slug']}' bulunamadı."}


def _author(ctx: ToolContext) -> str:
    """Beceriyi yazan modelin kimliği — kökeni kayıtta dursun."""
    from ..models import AgentSession  # noqa: PLC0415

    session = ctx.db.get(AgentSession, ctx.session_id) if ctx.session_id else None
    return session.control_model if session else ""
