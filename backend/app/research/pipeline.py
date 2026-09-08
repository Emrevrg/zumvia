"""
ARAŞTIRMA BORU HATTI — altı aşamalı, denetlenebilir üretim hattı
=================================================================

Aşamalar sabittir ve sırası değişmez. Bu bir kısıt değil, tasarımın kendisi:
veri toplanmadan analiz yapılamaz, analiz doğrulanmadan risk konuşulamaz,
risk ölçülmeden rapor yazılamaz.

    1. BRİF        kullanıcının cümlesi → ölçülebilir araştırma sorusu
    2. VERİ        deterministik ölçümler → OLGU DEFTERİ
    3. ANALİZ      uzman roller paralel çalışır (farklı modeller/sağlayıcılar)
    4. DOĞRULAMA   her sayısal iddia deftere karşı sınanır
    5. RİSK        maruziyet, likidite, en kötü senaryo — KODLA hesaplanır
    6. RAPOR       desteklenen / çelişen / dayanaksız ayrı ayrı yazılır

Her aşama, bitişinde olay yayınlar; yan panel bunları canlı gösterir.
Kullanıcı modelin nerede ne yaptığını her an görür — kapalı kutu yoktur.

Hata felsefesi: bir aşama çökerse boru hattı DURMAZ. Eksik veriyle devam
eder ve eksikliği rapora yazar. Yarım bir rapor, hiç rapor olmamasından
iyidir — ama eksikliği gizleyen bir rapor ikisinden de kötüdür.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ..core.logging import get_logger
from . import compliance, derive
from .ledger import Ledger
from .roles import PARALLEL_ROLES, SKEPTIC, SYNTHESIZER, Role
from .verify import Audit, audit

log = get_logger("zumvia.research")

#  Uzmanlar aynı anda kaç tane çalışsın. Sağlayıcı hız sınırları zaten alt
#  katmanda uygulanır; buradaki sayı yalnızca gereksiz kuyruk oluşturmamak
#  içindir.
MAX_PARALLEL = 4


class Stage(StrEnum):
    BRIEF = "brif"
    DATA = "veri"
    ANALYSIS = "analiz"
    VERIFICATION = "dogrulama"
    RISK = "risk"
    REPORT = "rapor"


STAGE_LABELS: dict[Stage, str] = {
    Stage.BRIEF: "Soruyu netleştiriyorum",
    Stage.DATA: "Veri topluyorum",
    Stage.ANALYSIS: "Uzmanlar çalışıyor",
    Stage.VERIFICATION: "Sayıları doğruluyorum",
    Stage.RISK: "Riski ölçüyorum",
    Stage.REPORT: "Raporu yazıyorum",
}


#  DÜŞÜNCE ZİNCİRİ SIZINTISI
#
#  Küçük modeller yönergeyi ve kendi süreçlerini yüksek sesle düşünür:
#  "Wait, the prompt says…", "Must not use 0.36 etc?", "I need to output 5
#  items". Bu satırlar rapora girdiğinde iki zarar verir: okunmaz hâle
#  getirir ve içlerindeki madde numaraları sahte ölçüm iddiası üretir.
#
#  Rol yönergesine "sürecini anlatma" kuralı eklendi; yine de sızanlar
#  burada süzülür. Süzgeç BİLEREK dardır — yalnızca modelin kendi sürecinden
#  ya da yönergeden bahseden satırlar atılır. Şüpheli satır KORUNUR: içerik
#  kaybetmek, biraz gürültüden daha kötüdür.
_THINKING = re.compile(
    # Madde imiyle başlayan düşünce satırları da vardır ("- I need to …").
    r"^[\s\-*•>]*"
    r"(wait[,.]|but wait|actually[,;]|hold on|let me |let's |okay[,.]|"
    r"first,? i |i need to |i should |i must |i will |i'll|i can |i'm going|"
    r"(but |and )?the (system |user )?prompt |the instruction|per the |as per |"
    r"according to the (system )?prompt|"
    r"must (not |also )?(use|add|include|have|write|answer|output)|"
    r"the user('s)? (actual )?(question|wants|said|asks|prompt)|"
    r"they (said|want|ask|list|provide|give|gave)|answer \d+ specific|"
    r"we (need|should|must|have) to|so (likely|we|i)\b|then they\b|"
    r"our task|the (report|answer|output) (should|must)|"
    r"my task is|görevim |yönerge |talimat |şimdi (şunu|bunu) |"
    r"kullanıcının (sorusu|isteği)|önce şunu (yapmalı|yazmalı))",
    re.IGNORECASE,
)


def strip_thinking(text: str) -> str:
    """Modelin kendi süreci hakkındaki satırlarını çıkarır."""
    kept = [line for line in text.splitlines() if not _THINKING.match(line)]
    cleaned = "\n".join(kept).strip()
    # Her şey silindiyse orijinali koru: boş bir bölüm, gürültülü bir
    # bölümden daha kötüdür.
    return cleaned or text.strip()


@dataclass(slots=True)
class RoleOutput:
    """Bir uzmanın çıktısı ve doğrulama sonucu."""

    role: str
    label: str
    text: str = ""
    ok: bool = True
    error: str = ""
    model: str = ""
    provider: str = ""
    duration_ms: int = 0
    complied: bool = True                 # yönergeye uydu mu
    problems: list[str] = field(default_factory=list)
    attempts: int = 1                     # kaç kez denendi
    audit: Audit | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role, "label": self.label, "text": self.text,
            "ok": self.ok, "error": self.error, "model": self.model,
            "provider": self.provider, "duration_ms": self.duration_ms,
            "complied": self.complied, "problems": self.problems,
            "attempts": self.attempts,
            "audit": self.audit.to_dict() if self.audit else None,
        }


@dataclass(slots=True)
class ResearchResult:
    """Tamamlanmış bir araştırmanın tüm çıktısı."""

    question: str
    brief: dict[str, Any] = field(default_factory=dict)
    ledger: Ledger = field(default_factory=Ledger)
    outputs: list[RoleOutput] = field(default_factory=list)
    risk: dict[str, Any] = field(default_factory=dict)
    report: str = ""
    warnings: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_ms: int = 0

    @property
    def has_analysis(self) -> bool:
        """En az bir uzman gerçekten metin üretti mi?"""
        return any(o.ok and o.text.strip() for o in self.outputs)

    @property
    def trust_score(self) -> float:
        """
        Tüm bölümlerin ağırlıklı güveni.

        Hiç analiz üretilmediyse güven 1.0 DEĞİLDİR. "Kimse bir şey
        söylemedi" durumunu "her söylenen doğrulandı" gibi göstermek,
        raporun en üstüne yalan bir başlık yazmak olurdu.
        """
        if not self.has_analysis:
            return 0.0
        audits = [o.audit for o in self.outputs if o.audit is not None]
        total = sum(len(a.claims) for a in audits)
        if total == 0:
            # Metin var ama sayısal iddia yok: yanıltma da yok.
            return 1.0
        return sum(a.trust_score * len(a.claims) for a in audits) / total

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "brief": self.brief,
            "ledger": self.ledger.to_dict(),
            "outputs": [o.to_dict() for o in self.outputs],
            "risk": self.risk,
            "report": self.report,
            "warnings": self.warnings,
            "trust_score": round(self.trust_score, 3),
            "started_at": self.started_at.isoformat(),
            "duration_ms": self.duration_ms,
        }


class ResearchPipeline:
    """
    Araştırmayı yürüten orkestratör.

    Dışarıdan üç şey ister ve gerisini kendi yönetir:

        gateway_for(role)  → o rol için bir sohbet geçidi (model)
        collect(question)  → deterministik ölçümleri deftere yazan işlev
        emit(event)        → ilerleme olayı (yan panel için)

    Bu ayrım bilinçlidir: boru hattı hangi modelin kullanıldığını, verinin
    nereden geldiğini ve olayların nereye gittiğini BİLMEZ. Böylece test
    edilebilir kalır ve sağlayıcı değişimi boru hattını etkilemez.
    """

    def __init__(self, *,
                 gateway_for: Callable[[Role], Any],
                 collect: Callable[[str, Ledger], dict[str, Any]],
                 measure_risk: Callable[[Ledger, dict[str, Any]], dict[str, Any]] | None = None,
                 emit: Callable[[dict[str, Any]], None] | None = None,
                 cancelled: Callable[[], bool] | None = None,
                 max_parallel: int = MAX_PARALLEL) -> None:
        self.gateway_for = gateway_for
        self.collect = collect
        self.measure_risk = measure_risk
        self.emit = emit or (lambda event: None)
        self.cancelled = cancelled or (lambda: False)
        self.max_parallel = max(1, max_parallel)

    # -- olaylar ------------------------------------------------------------ #

    def _stage(self, stage: Stage, status: str, **extra: Any) -> None:
        self.emit({"type": "research_stage", "stage": str(stage),
                   "label": STAGE_LABELS[stage], "status": status, **extra})

    # -- 1. brif ------------------------------------------------------------ #

    def _make_brief(self, question: str) -> dict[str, Any]:
        """
        Kullanıcının cümlesini ölçülebilir bir araştırma sorusuna çevirir.

        "Bugün hisselerde fırsat var mı" cümlesi tek başına araştırılamaz:
        hangi piyasa, hangi zaman ufku, hangi kısıtlar? Brif bu boşlukları
        AÇIKÇA doldurur ve varsayımlarını rapora yazar — sessizce varsaymak
        yerine.
        """
        self._stage(Stage.BRIEF, "running")
        brief = {
            "question": question,
            "asked_at": datetime.now(UTC).isoformat(),
            "assumptions": [],
        }
        self._stage(Stage.BRIEF, "done", brief=brief)
        return brief

    # -- 2. veri ------------------------------------------------------------ #

    def _gather(self, question: str, ledger: Ledger,
                result: ResearchResult) -> dict[str, Any]:
        self._stage(Stage.DATA, "running")
        try:
            context = self.collect(question, ledger)
        except Exception as exc:  # noqa: BLE001 — veri eksikse araştırma yine sürer
            log.exception("veri toplama hatası")
            result.warnings.append(
                f"Veri toplama kısmen başarısız oldu ({type(exc).__name__}). "
                f"Rapor eksik ölçümlerle yazıldı.")
            context = {}

        # HAM ölçümlerden ÇIKARILABİLEN her şeyi burada hesapla.
        #
        # Sebep: uzman "stop fiyattan %2.7 aşağı" yazdığında bu sayı defterde
        # yoksa iddia DAYANAKSIZ sayılıyordu — oysa fiyat da stop da
        # defterdeydi, aradaki yüzde tek bölmeydi. Sistem hesaplamadığı şey
        # için uzmanı suçluyordu. Türevler analizden ÖNCE üretilir ki
        # uzmanlar da onları görsün ve kendileri hesaplamak zorunda kalmasın.
        try:
            derived = derive.enrich(ledger, context)
        except Exception:  # noqa: BLE001 — türev üretilemezse ham ölçümler yeter
            log.exception("türev ölçüm hatası")
            derived = 0

        self._stage(Stage.DATA, "done", facts=len(ledger),
                    derived=derived, sources=ledger.sources())
        return context

    # -- 3. analiz ---------------------------------------------------------- #

    def _run_compliant(self, role: Role, question: str, ledger: Ledger,
                       context: dict[str, Any], prior: str = "") -> RoleOutput:
        """
        Rolü çalıştırır ve YÖNERGEYE UYAN bir çıktı elde edene kadar düzeltir.

        Üç aşama, en ucuzdan pahalıya:

            1. TEMİZLE   süreç satırları atılır (`_run_role` içinde, bedava)
            2. UYAR      aynı modele somut düzeltme talimatıyla tekrar sorulur
            3. DEĞİŞTİR  hâlâ uymuyorsa BAŞKA modele geçilir

        Küçük modeller için talimat bir öneridir, kısıt değil. Kısıt burada,
        dışarıdan uygulanır. Her aşama bir model çağrısı yaktığı için
        sıralama ucuzdan pahalıya kurulmuştur ve en fazla iki ek deneme
        yapılır: mükemmeliyetçilik kotayı tüketir.
        """
        out = self._run_role(role, question, ledger, context, prior)
        if out.ok and out.complied:
            return out

        # 2. UYAR — aynı model, somut düzeltme talimatı
        if out.ok and out.problems and not self.cancelled():
            correction = compliance.Verdict(False, out.problems).instruction()
            self.emit({"type": "research_role", "role": role.id,
                       "label": role.label, "status": "correcting",
                       "problems": out.problems})
            retry = self._run_role(role, question, ledger, context,
                                   prior, correction=correction)
            retry.attempts = out.attempts + 1
            if retry.ok and retry.complied:
                log.info("%s: uyarı sonrası düzeldi", role.id)
                return retry
            out = retry if retry.ok else out

        # 3. DEĞİŞTİR — başka model
        if not self.cancelled():
            self.emit({"type": "research_role", "role": role.id,
                       "label": role.label, "status": "switching",
                       "problems": out.problems})
            other = self._run_role(role, question, ledger, context, prior,
                                   fresh_model=True)
            other.attempts = out.attempts + 1
            if other.ok and (other.complied or not out.ok):
                log.info("%s: model değişimiyle düzeldi", role.id)
                return other

        return out

    def _run_role(self, role: Role, question: str, ledger: Ledger,
                  context: dict[str, Any], prior: str = "",
                  fresh_model: bool = False,
                  correction: str = "") -> RoleOutput:
        """
        Tek bir uzmanı çalıştırır. Çöküş, boru hattını durdurmaz.

        `fresh_model` verilirse rol için önbelleğe alınmış geçit atlanır ve
        SIRADAKİ model istenir: aynı modelle tekrar denemenin faydası yoktur.
        """
        out = RoleOutput(role=role.id, label=role.label)
        started = time.perf_counter()
        self.emit({"type": "research_role", "role": role.id,
                   "label": role.label, "status": "running",
                   "mission": role.mission})

        try:
            gateway = self.gateway_for(role, fresh=fresh_model) \
                if fresh_model else self.gateway_for(role)
            if gateway is None:
                out.ok = False
                out.error = "Bu rol için kullanılabilir model bulunamadı."
            else:
                out.model = getattr(gateway, "model", "")
                out.provider = getattr(gateway, "provider_id", "")
                user_message = _role_prompt(question, ledger, context, prior)
                if correction:
                    user_message = correction + "\n\n" + user_message
                turn = gateway.chat([{"role": "user", "content": user_message}],
                                    system=role.system_prompt(),
                                    max_tokens=role.max_tokens)
                if turn.ok:
                    verdict = compliance.check(turn.text or "", language="tr",
                                               strip_thinking=strip_thinking)
                    out.text = verdict.cleaned
                    out.complied = verdict.ok
                    compliance.note(out.model, role.id, verdict.ok)
                    if not verdict.ok:
                        out.problems = list(verdict.problems)
                    if not out.text:
                        out.ok = False
                        out.error = "Model boş yanıt döndürdü."
                else:
                    out.ok = False
                    out.error = str(turn.error)[:300]
        except Exception as exc:  # noqa: BLE001 — bir uzmanın düşmesi diğerlerini etkilemez
            log.exception("uzman hatası: %s", role.id)
            out.ok = False
            out.error = f"{type(exc).__name__}: {exc}"[:300]

        out.duration_ms = int((time.perf_counter() - started) * 1000)
        self.emit({"type": "research_role", "role": role.id, "label": role.label,
                   "status": "done" if out.ok else "failed",
                   "ok": out.ok, "error": out.error,
                   "duration_ms": out.duration_ms,
                   "preview": out.text[:280]})
        return out

    def _analyze(self, question: str, ledger: Ledger, context: dict[str, Any],
                 result: ResearchResult) -> None:
        """
        Uzmanları paralel çalıştırır.

        Paralellik burada gerçek bir kazançtır: dört uzman sırayla çalışsa
        araştırma dört kat uzun sürerdi. Hız sınırları alt katmanda zaten
        uygulandığı için sağlayıcıyı kızdırma riski yoktur.
        """
        self._stage(Stage.ANALYSIS, "running", roles=[r.id for r in PARALLEL_ROLES])

        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            futures = {
                pool.submit(self._run_compliant, role, question, ledger, context): role
                for role in PARALLEL_ROLES
            }
            for future in futures:
                result.outputs.append(future.result())

        # Sıra korunur: rapor her seferinde aynı düzende okunsun.
        order = {role.id: index for index, role in enumerate(PARALLEL_ROLES)}
        result.outputs.sort(key=lambda o: order.get(o.role, 99))

        stubborn = [o.label for o in result.outputs
                    if o.ok and not o.complied]
        if stubborn:
            result.warnings.append(
                f"Şu uzmanların modeli yönergeye tam uymadı: "
                f"{', '.join(stubborn)}. Metinleri temizlendi ama eksik "
                f"kalmış olabilir; güven skoru buna göre okunmalı.")

        failed = [o.label for o in result.outputs if not o.ok]
        if failed:
            result.warnings.append(
                f"Şu uzmanlar çalışamadı: {', '.join(failed)}. "
                f"Rapor onların katkısı olmadan yazıldı.")

        # Şüpheci, diğerlerinin çıktısını GÖRDÜKTEN sonra çalışır: itiraz
        # edeceği bir tez olmadan şüphecilik yapılamaz.
        if not self.cancelled():
            prior = _joined(result.outputs)
            result.outputs.append(
                self._run_compliant(SKEPTIC, question, ledger, context, prior))

        self._stage(Stage.ANALYSIS, "done",
                    completed=len([o for o in result.outputs if o.ok]),
                    failed=len(failed))

    # -- 4. doğrulama ------------------------------------------------------- #

    def _verify(self, ledger: Ledger, result: ResearchResult) -> None:
        """
        Her uzmanın metnindeki sayısal iddiaları deftere karşı sınar.

        Bu aşama, sistemi "akıllı sohbet"ten ayıran şeydir: modelin ne
        söylediği değil, söylediğinin ölçümle örtüşüp örtüşmediği raporlanır.
        """
        self._stage(Stage.VERIFICATION, "running")

        contradicted = 0
        unsupported = 0
        for output in result.outputs:
            if not output.ok or not output.text:
                continue
            output.audit = audit(output.text, ledger)
            contradicted += len(output.audit.contradicted)
            unsupported += len(output.audit.unsupported)
            self.emit({"type": "research_audit", "role": output.role,
                       "label": output.label, **output.audit.to_dict()})

        if contradicted:
            result.warnings.append(
                f"{contradicted} sayısal iddia ölçümlerle ÇELİŞİYOR. Bunlar "
                f"raporda işaretlendi; karar verirken önce onları inceleyin.")

        self._stage(Stage.VERIFICATION, "done",
                    contradicted=contradicted, unsupported=unsupported,
                    trust_score=round(result.trust_score, 3))

    # -- 5. risk ------------------------------------------------------------ #

    def _risk(self, ledger: Ledger, context: dict[str, Any],
              result: ResearchResult) -> None:
        """
        Riski KOD hesaplar, model değil.

        Risk müdürü rolü riski yorumlar; buradaki sayılar deterministiktir ve
        modelin görüşünden bağımsızdır. Bir çelişki varsa kod kazanır.
        """
        self._stage(Stage.RISK, "running")
        if self.measure_risk is None:
            result.risk = {}
            self._stage(Stage.RISK, "skipped")
            return
        try:
            result.risk = self.measure_risk(ledger, context)
        except Exception as exc:  # noqa: BLE001
            log.exception("risk ölçümü hatası")
            result.risk = {}
            result.warnings.append(
                f"Risk ölçümü başarısız oldu ({type(exc).__name__}); rapordaki "
                f"risk bölümü yalnızca yoruma dayanıyor.")
        self._stage(Stage.RISK, "done", **{k: v for k, v in result.risk.items()
                                           if isinstance(v, (int, float, str))})

    # -- 6. rapor ----------------------------------------------------------- #

    def _report(self, question: str, ledger: Ledger, context: dict[str, Any],
                result: ResearchResult) -> None:
        self._stage(Stage.REPORT, "running")

        prior = _joined(result.outputs, with_audit=True)
        synthesis_context = {**context, "risk": result.risk}
        synthesis = self._run_compliant(SYNTHESIZER, question, ledger,
                                        synthesis_context, prior)

        # Rapor, araştırmanın ÜRÜNÜDÜR. Beş uzman çalışmış ve onlarca ölçüm
        # alınmışken, tek bir modelin bir kereliğine boş dönmesiyle bunu
        # kaybetmek kabul edilemez. Farklı bir modelle bir kez daha denenir.
        if not synthesis.ok or not synthesis.text.strip():
            log.warning("sentez başarısız (%s); başka modelle tekrar deneniyor",
                        synthesis.error or "boş yanıt")
            self.emit({"type": "research_stage", "stage": str(Stage.REPORT),
                       "label": STAGE_LABELS[Stage.REPORT], "status": "retry",
                       "reason": synthesis.error or "boş yanıt"})
            retry = self._run_role(SYNTHESIZER, question, ledger,
                                   synthesis_context, prior, fresh_model=True)
            if retry.ok and retry.text.strip():
                synthesis = retry

        if synthesis.ok and synthesis.text:
            synthesis.audit = audit(synthesis.text, ledger)
        result.outputs.append(synthesis)

        result.report = synthesis.text if synthesis.ok else ""
        if not result.report:
            result.warnings.append(
                "Sentez adımı çalışamadı; aşağıda uzman raporları ham hâliyle "
                "sunuluyor.")

        self._stage(Stage.REPORT, "done", length=len(result.report))

    # -- çalıştır ----------------------------------------------------------- #

    def run(self, question: str) -> ResearchResult:
        """Tüm boru hattını yürütür ve sonucu döndürür."""
        started = time.perf_counter()
        result = ResearchResult(question=question)
        ledger = result.ledger

        result.brief = self._make_brief(question)
        if self.cancelled():
            return _stop(result, started)

        context = self._gather(question, ledger, result)
        if self.cancelled():
            return _stop(result, started)

        self._analyze(question, ledger, context, result)
        if self.cancelled():
            return _stop(result, started)

        self._verify(ledger, result)
        self._risk(ledger, context, result)
        if self.cancelled():
            return _stop(result, started)

        self._report(question, ledger, context, result)

        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log.info("araştırma bitti: %d olgu, %d uzman, güven %.2f, %d ms",
                 len(ledger), len(result.outputs), result.trust_score,
                 result.duration_ms)
        return result


# --------------------------------------------------------------------------- #
#  Yardımcılar
# --------------------------------------------------------------------------- #

def _stop(result: ResearchResult, started: float) -> ResearchResult:
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    result.warnings.append("Araştırma kullanıcı isteğiyle durduruldu.")
    return result


def _joined(outputs: list[RoleOutput], with_audit: bool = False) -> str:
    """Uzman çıktılarını sonraki role verilecek biçimde birleştirir."""
    parts: list[str] = []
    for output in outputs:
        if not output.ok or not output.text:
            continue
        block = f"### {output.label}\n{output.text}"
        if with_audit and output.audit is not None:
            block += f"\n[doğrulama: {output.audit.summary()}]"
        parts.append(block)
    return "\n\n".join(parts)


def _role_prompt(question: str, ledger: Ledger, context: dict[str, Any],
                 prior: str) -> str:
    """
    Uzmana verilecek mesaj: soru + ÖLÇÜM DEFTERİ + varsa önceki görüşler.

    Defter metne gömülür çünkü modelin başvurabileceği tek sayı kaynağı
    budur. Dışarıdan sayı getirmesi hâlinde doğrulama aşaması yakalar.
    """
    facts = ledger.all()
    lines = [f"- {f.key} = {f.value}"
             + (f" {f.unit}" if f.unit else "")
             + f"  {f.cite()}" for f in facts]
    measured = "\n".join(lines) if lines else "(ölçüm yok)"

    extra = ""
    if context:
        trimmed = {k: v for k, v in context.items()
                   if isinstance(v, (str, int, float, bool))}
        if trimmed:
            extra = "\n\nEK BAĞLAM\n" + json.dumps(trimmed, ensure_ascii=False)[:1500]

    prior_block = f"\n\nDİĞER UZMANLARIN GÖRÜŞLERİ\n{prior[:6000]}" if prior else ""

    return (f"ARAŞTIRMA SORUSU\n{question}\n\n"
            f"ÖLÇÜM DEFTERİ ({len(facts)} olgu — kullanabileceğin TEK sayı kaynağı)\n"
            f"{measured}{extra}{prior_block}")
