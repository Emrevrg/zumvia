"""
YAPAY ZEKANIN YAZDIĞI BECERİLER — doğrulama ve yürütme
=======================================================

Katalogdaki beceriler kodun içinde sabittir; model onları SEÇER. Bu modül
modelin KENDİ becerisini yazmasına izin verir — ama serbest kod yazarak
değil, doğrulanmış araç kutusunu birleştirerek.

Bir beceri, sıralı adımlardan oluşan bir tariftir:

    1. scan_markets(market="crypto", limit=5)          → tarama
    2. run_backtest(symbol={tarama.en_iyi}, ...)       → kanıt
    3. get_portfolio_risk()                            → maruziyet

Her adım kayıtlı bir aracı çağırır. Bir adımın çıktısı `save_as` ile
adlandırılır ve sonraki adımlarda `{ad.alan}` yazımıyla kullanılır.

Değişmez kurallar — ve neden:

  * YALNIZCA KAYITLI ARAÇ. Model araç adı uyduramaz; uydurursa beceri
    kaydedilmez. Yürütme sırasında değil KAYIT sırasında reddedilir, çünkü
    bozuk bir beceriyi kaydedip sonra çalıştırmak, hatayı kullanıcının
    üzerine yıkmaktır.

  * DÖNGÜ YOK, KOŞUL YOK. Beceri bir tariftir, bir program değil. Döngü
    eklemek onu keyfi kod çalıştırmaya çevirir ve tüm güvenlik gerekçesini
    çürütür. Karar gerektiren yerde ajanın kendisi devreye girer.

  * RİSK ARAÇLARI KENDİ KORUMALARINDAN GEÇER. Beceri içinden çağrılan
    `open_position` da tıpkı doğrudan çağrıldığı gibi risk kalkanına,
    stop zorunluluğuna ve devre kesiciye tabidir. Beceri bir kılıf değildir.

  * ADIM SAYISI SINIRLI. Uzun bir zincir hem pahalıdır hem de hatanın
    nerede olduğunu bulmayı imkânsızlaştırır.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..models import CustomSkill, User

log = get_logger("zumvia.custom_skills")

MAX_STEPS = 8               # daha uzunu hem pahalı hem hata ayıklanamaz
MAX_INPUTS = 6
MIN_DESCRIPTION = 20        # "ne yaptığı" yazılmadan beceri kaydedilmez
MIN_WHEN_USED = 20          # "ne zaman kullanılır" da zorunlu

#  Beceri içinden çağrılması YASAK araçlar.
#
#  Gerekçe: bunlar yetki sınırını değiştirir ya da geri alınamaz bir sonuç
#  doğurur. Bir becerinin içine gömülünce kullanıcı ne çalıştırdığını
#  göremez — oysa bu kararların her biri kullanıcının bilerek vermesi
#  gereken kararlardır.
FORBIDDEN_TOOLS = frozenset({
    "enable_live_trading",      # gerçek paraya geçiş yalnızca kullanıcıdan
    "disable_live_trading",
    "activate_kill_switch",     # acil fren bir insan kararıdır
    "create_skill", "edit_skill", "delete_skill", "run_skill",  # özyineleme
})

#  `{ad}` ya da `{ad.alan}` yazımı — önceki adımın çıktısına ya da girdiye atıf.
_REFERENCE = re.compile(r"\{([a-zA-Z_][\w]*)(?:\.([\w.]+))?\}")

#  Modeller girdilere `{inputs.symbol}` diye atıf yapmaya eğilimli: şemada
#  girdiler `inputs` adlı bir dizide tanımlandığı için bu sezgi makuldür.
#  Reddetmek yerine kabul edilir — arayüzü modele uydurmak, modeli arayüze
#  uydurmaktan kolaydır ve daha az hata üretir.
_INPUT_NAMESPACES = ("inputs", "input", "girdi", "girdiler", "params", "args")

_SLUG_OK = re.compile(r"^[a-z0-9_]{3,64}$")


@dataclass(slots=True)
class Step:
    """Bir becerinin tek adımı."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    save_as: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "args": self.args, "save_as": self.save_as}


@dataclass(slots=True)
class SkillInput:
    """Becerinin çalışırken istediği bir parametre."""

    name: str
    description: str = ""
    required: bool = True
    default: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "required": self.required, "default": self.default}


class SkillError(ValueError):
    """Beceri tanımı geçersiz. Mesaj doğrudan modele gösterilir."""


# --------------------------------------------------------------------------- #
#  Doğrulama
# --------------------------------------------------------------------------- #

def validate(label: str, description: str, when_used: str,
             steps: list[dict[str, Any]],
             inputs: list[dict[str, Any]] | None = None,
             ) -> tuple[list[Step], list[SkillInput]]:
    """
    Beceri tanımını sınar ve normalleştirir.

    Hata mesajları doğrudan modele gider; bu yüzden NE yapması gerektiğini
    söylerler, yalnızca "geçersiz" demezler. Model hatayı okuyup düzeltebilmeli.
    """
    from ..agent.tools import REGISTRY  # noqa: PLC0415

    if len(label.strip()) < 3:
        raise SkillError("Beceri adı çok kısa.")
    if len(description.strip()) < MIN_DESCRIPTION:
        raise SkillError(
            f"'description' en az {MIN_DESCRIPTION} karakter olmalı. Becerinin "
            f"NE YAPTIĞINI yaz; adı tek başına yeterli değil.")
    if len(when_used.strip()) < MIN_WHEN_USED:
        raise SkillError(
            f"'when_used' en az {MIN_WHEN_USED} karakter olmalı. Bu becerinin "
            f"NE ZAMAN kullanılacağını yaz — kullanılmayacağı durumu da söyle.")

    if not steps:
        raise SkillError("Beceri en az bir adım içermeli.")
    if len(steps) > MAX_STEPS:
        raise SkillError(
            f"En fazla {MAX_STEPS} adım olabilir. Daha uzun bir iş, ayrı "
            f"becerilere bölünmeli: uzun zincirde hatanın nerede olduğu "
            f"bulunamaz.")

    declared: list[SkillInput] = []
    for raw in (inputs or [])[:MAX_INPUTS]:
        name = str(raw.get("name", "")).strip()
        if not name.isidentifier():
            raise SkillError(f"Geçersiz girdi adı: {name!r}. Harf ve alt çizgi kullan.")
        declared.append(SkillInput(
            name=name,
            description=str(raw.get("description", ""))[:200],
            required=bool(raw.get("required", True)),
            default=raw.get("default"),
        ))

    known_names = {i.name for i in declared}
    parsed: list[Step] = []

    for index, raw in enumerate(steps, start=1):
        tool = str(raw.get("tool", "")).strip()
        if tool not in REGISTRY:
            raise SkillError(
                f"{index}. adımdaki '{tool}' diye bir araç YOK. Araç adı "
                f"uyduramazsın. Kullanılabilir araçları `list_tools` ile gör.")
        if tool in FORBIDDEN_TOOLS:
            raise SkillError(
                f"'{tool}' bir becerinin içine gömülemez. Bu karar kullanıcının "
                f"bilerek vermesi gereken bir karardır; beceri onu gizlerdi.")

        args = raw.get("args") or {}
        if not isinstance(args, dict):
            raise SkillError(f"{index}. adımın 'args' alanı sözlük olmalı.")

        # Atıflar ÖNCEDEN tanımlı olmalı: ileri atıf, çalışma anında
        # anlaşılmaz bir hataya dönüşür.
        for reference in _references_in(args):
            if reference not in known_names:
                # Kullanılabilir adları SAYMAYAN bir hata mesajı, modeli kör
                # denemeye zorlar. Canlı denemede model sekiz kez aynı hatayı
                # aldı çünkü neyin var olduğunu göremiyordu.
                available = (", ".join(f"{{{n}}}" for n in sorted(known_names))
                             or "(henüz hiçbiri)")
                raise SkillError(
                    f"{index}. adım {{{reference}}} diyor ama böyle bir ad yok.\n"
                    f"Kullanabileceklerin: {available}\n"
                    f"Girdi kullanmak için önce 'inputs' listesinde tanımla; "
                    f"önceki adımın çıktısını kullanmak için o adıma 'save_as' "
                    f"ver. Not: {{inputs.symbol}} ile {{symbol}} aynı şeydir.")

        save_as = str(raw.get("save_as", "")).strip()
        if save_as:
            if not save_as.isidentifier():
                raise SkillError(f"Geçersiz 'save_as': {save_as!r}")
            if save_as in known_names:
                raise SkillError(
                    f"'{save_as}' adı zaten kullanılıyor. Her adımın çıktısı "
                    f"ayrı bir ad almalı, yoksa üzerine yazılır.")
            known_names.add(save_as)

        parsed.append(Step(tool=tool, args=args, save_as=save_as))

    return parsed, declared


def _root_of(match: re.Match[str]) -> str:
    """
    Bir atfın gerçek kök adı.

    `{inputs.symbol}` ile `{symbol}` aynı şeye işaret eder: ad alanı
    yazımı ayıklanır ve gerçek ad döner.
    """
    name, path = match.group(1), match.group(2)
    if name in _INPUT_NAMESPACES and path:
        return path.split(".")[0]
    return name


def _references_in(value: Any) -> list[str]:
    """İç içe yapıdaki tüm `{ad}` atıflarının kök adlarını toplar."""
    found: list[str] = []
    if isinstance(value, str):
        found += [_root_of(m) for m in _REFERENCE.finditer(value)]
    elif isinstance(value, dict):
        for item in value.values():
            found += _references_in(item)
    elif isinstance(value, list):
        for item in value:
            found += _references_in(item)
    return found


def slugify(label: str) -> str:
    """
    Türkçe adı ASCII bir kimliğe çevirir.

    Kullanıcı ve ajan becerileri Türkçe adlandırır ("Hızlı Parite Kontrolü").
    Harf çevirisi yapılmazsa kimlik `hızlı_parite_kontrolü` olur ve ASCII
    bekleyen her yerde (URL, dosya adı, kimlik deseni) patlar — yani ilk
    Türkçe adlı beceri hiç kaydedilemezdi.
    """
    from ..core.text import fold  # noqa: PLC0415

    text = "".join(c if c.isalnum() else "_" for c in fold(label).strip())
    text = re.sub(r"_+", "_", text).strip("_")[:64]
    # Çeviriden sonra ASCII dışı bir şey kaldıysa (Arapça, Çince ad) at.
    text = "".join(c for c in text if c.isascii())
    return text or "beceri"


# --------------------------------------------------------------------------- #
#  Kayıt
# --------------------------------------------------------------------------- #

def save(db: Session, user: User, *, label: str, description: str,
         when_used: str, steps: list[dict[str, Any]],
         inputs: list[dict[str, Any]] | None = None,
         category: str = "otomasyon", author_model: str = "",
         parent_slug: str = "", slug: str = "") -> CustomSkill:
    """Yeni beceri kaydeder (aynı slug varsa hata verir)."""
    parsed_steps, parsed_inputs = validate(label, description, when_used,
                                           steps, inputs)
    final_slug = slug or slugify(label)
    if not _SLUG_OK.match(final_slug):
        raise SkillError(f"Geçersiz beceri kimliği: {final_slug!r}")

    if find(db, user, final_slug) is not None:
        raise SkillError(
            f"'{final_slug}' adında bir beceriniz zaten var. Değiştirmek için "
            f"`edit_skill` kullan; üzerine yazmak sicili siler.")

    row = CustomSkill(
        user_id=user.id, slug=final_slug, label=label.strip()[:96],
        category=category[:32], description=description.strip(),
        when_used=when_used.strip(),
        steps_json=json.dumps([s.to_dict() for s in parsed_steps], ensure_ascii=False),
        inputs_json=json.dumps([i.to_dict() for i in parsed_inputs], ensure_ascii=False),
        author_model=author_model[:120], parent_slug=parent_slug[:64],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    log.info("beceri yazıldı: %s (%d adım, model: %s)",
             final_slug, len(parsed_steps), author_model or "?")
    return row


def edit(db: Session, user: User, slug: str, *, changes: dict[str, Any],
         author_model: str = "") -> CustomSkill:
    """
    Var olan beceriyi günceller ve sürümü artırır.

    Sicil (kaç kez çalıştı, kaç kez hata verdi) KORUNUR: bir becerinin
    geçmişi, o becerinin en değerli bilgisidir. Silinirse "bu işe yarıyor
    mu" sorusu bir daha cevaplanamaz.
    """
    row = find(db, user, slug)
    if row is None:
        raise SkillError(f"'{slug}' adında bir beceriniz yok.")

    label = str(changes.get("label", row.label))
    description = str(changes.get("description", row.description))
    when_used = str(changes.get("when_used", row.when_used))
    steps = changes.get("steps", json.loads(row.steps_json or "[]"))
    inputs = changes.get("inputs", json.loads(row.inputs_json or "[]"))

    parsed_steps, parsed_inputs = validate(label, description, when_used,
                                           steps, inputs)

    row.label = label.strip()[:96]
    row.description = description.strip()
    row.when_used = when_used.strip()
    row.category = str(changes.get("category", row.category))[:32]
    row.steps_json = json.dumps([s.to_dict() for s in parsed_steps], ensure_ascii=False)
    row.inputs_json = json.dumps([i.to_dict() for i in parsed_inputs], ensure_ascii=False)
    row.version += 1
    row.updated_at = datetime.now(UTC)
    if author_model:
        row.author_model = author_model[:120]
    db.commit()
    db.refresh(row)
    log.info("beceri güncellendi: %s (sürüm %d)", slug, row.version)
    return row


def find(db: Session, user: User, slug: str) -> CustomSkill | None:
    return (db.query(CustomSkill)
            .filter(CustomSkill.user_id == user.id, CustomSkill.slug == slug)
            .first())


def listing(db: Session, user: User) -> list[CustomSkill]:
    return (db.query(CustomSkill)
            .filter(CustomSkill.user_id == user.id)
            .order_by(CustomSkill.updated_at.desc()).all())


def remove(db: Session, user: User, slug: str) -> bool:
    row = find(db, user, slug)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    log.info("beceri silindi: %s", slug)
    return True


def describe(row: CustomSkill) -> dict[str, Any]:
    """Modele ve arayüze gösterilecek biçim."""
    runs = row.runs or 0
    return {
        "slug": row.slug, "label": row.label, "category": row.category,
        "description": row.description, "when_used": row.when_used,
        "steps": json.loads(row.steps_json or "[]"),
        "inputs": json.loads(row.inputs_json or "[]"),
        "version": row.version, "parent": row.parent_slug,
        "author_model": row.author_model,
        "runs": runs, "failures": row.failures or 0,
        "basari_orani": round((runs - (row.failures or 0)) / runs, 3) if runs else None,
        "last_error": row.last_error,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# --------------------------------------------------------------------------- #
#  Yürütme
# --------------------------------------------------------------------------- #

def resolve(value: Any, scope: dict[str, Any]) -> Any:
    """
    `{ad}` ve `{ad.alan}` atıflarını gerçek değerlerle değiştirir.

    Metin TAMAMEN tek bir atıftan ibaretse değer olduğu gibi (sayı, liste,
    sözlük) yerine konur. Aksi hâlde metne gömülür. Bu ayrım önemlidir:
    `"{tarama.en_iyi}"` bir sembol dizesi vermeli, `"{n} adet"` ise metin.
    """
    if isinstance(value, dict):
        return {k: resolve(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, scope) for v in value]
    if not isinstance(value, str):
        return value

    whole = _REFERENCE.fullmatch(value.strip())
    if whole:
        return _lookup(whole.group(1), whole.group(2), scope)

    def replace(match: re.Match[str]) -> str:
        found = _lookup(match.group(1), match.group(2), scope)
        return "" if found is None else str(found)

    return _REFERENCE.sub(replace, value)


def _lookup(name: str, path: str | None, scope: dict[str, Any]) -> Any:
    # `{inputs.symbol}` → `{symbol}`: ad alanı yazımı da kabul edilir.
    if name in _INPUT_NAMESPACES and path:
        parts = path.split(".")
        name, path = parts[0], ".".join(parts[1:]) or None

    current = scope.get(name)
    if path:
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                index = int(part)
                current = current[index] if index < len(current) else None
            else:
                return None
    return current


def run(ctx: Any, db: Session, user: User, slug: str,
        inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Beceriyi adım adım çalıştırır.

    Bir adım hata verirse zincir DURUR: sonraki adımlar bozuk veriyle
    çalışıp yanlış sonuç üretmesin. O ana kadarki çıktılar korunur ve
    hatanın hangi adımda olduğu açıkça bildirilir.
    """
    from ..agent.tools import execute_tool  # noqa: PLC0415

    row = find(db, user, slug)
    if row is None:
        return {"error": f"'{slug}' adında bir beceriniz yok."}

    declared = [SkillInput(**i) for i in json.loads(row.inputs_json or "[]")]
    scope: dict[str, Any] = {}
    given = inputs or {}

    for item in declared:
        if item.name in given:
            scope[item.name] = given[item.name]
        elif item.default is not None:
            scope[item.name] = item.default
        elif item.required:
            return {"error": f"'{item.name}' girdisi zorunlu: {item.description}"}

    steps = json.loads(row.steps_json or "[]")
    trace: list[dict[str, Any]] = []
    row.runs = (row.runs or 0) + 1
    row.last_run_at = datetime.now(UTC)

    for index, step in enumerate(steps, start=1):
        tool = step["tool"]
        args = resolve(step.get("args") or {}, scope)
        result = execute_tool(ctx, tool, args)
        failed = isinstance(result, dict) and "error" in result

        trace.append({"adim": index, "arac": tool, "ok": not failed,
                      "sonuc": result})

        if failed:
            row.failures = (row.failures or 0) + 1
            row.last_error = f"{index}. adım ({tool}): {result.get('error', '')}"[:400]
            db.commit()
            return {
                "beceri": slug, "ok": False,
                "hata": row.last_error,
                "tamamlanan_adim": index - 1,
                "adimlar": trace,
                "not": "Zincir durduruldu: sonraki adımlar bozuk veriyle "
                       "çalışıp yanlış sonuç üretmesin.",
            }

        if step.get("save_as"):
            scope[step["save_as"]] = result

    row.last_error = ""
    db.commit()
    return {"beceri": slug, "ok": True, "adim_sayisi": len(steps),
            "adimlar": trace,
            "cikti": {k: v for k, v in scope.items()
                      if k not in {i.name for i in declared}}}
