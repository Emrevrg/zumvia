"""
ÇOKLU MODEL KONSEYİ (Model Council)
====================================
Tek bir dil modeline güvenmek tek bir analiste güvenmektir. Bu modül aynı karara
**birden fazla modeli aynı anda** baktırır, oyları deterministik olarak birleştirir
ve anlaşmazlık varsa işlem açtırmaz.

Roller:
  * `analyst`     — Kararı üretir. Kaç tane olursa olsun paralel çalışır.
  * `risk_critic` — Analistlerin ortak önerisini yıkmaya çalışır. Veto yetkisi vardır.
  * `arbiter`     — Analistler bölündüğünde hangi tarafın daha güçlü olduğunu söyler.
                    **Sayı üretemez**; yalnızca taraf seçer.

Değişmez ilkeler:
  1. Nihai sayılar (stop, hedef, lot) **her zaman** deterministik hesaplanır —
     anlaşan üyelerin medyanı alınır, ATR ile makullük kontrolünden geçirilir.
     Hiçbir modelin serbest metni doğrudan emre dönüşmez.
  2. Anlaşma yoksa karar **WAIT**'tir. Bölünmüş konsey işlem açmaz.
  3. Risk eleştirmeni veto ederse işlem açılmaz — analistler hemfikir olsa bile.
  4. Bir modelin oy ağırlığı **gerçek siciline** göre belirlenir (ModelScore).
     Sürekli yanılan modelin sözü zamanla azalır; kayırma yoktur.
  5. Her karar `DecisionRecord` olarak audit defterine yazılır.
"""
from __future__ import annotations

import json
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from ..core.creds import read_extra, read_secrets
from ..core.logging import get_logger
from ..layers.l3_llm_gateway import (
    LLMGateway,
    extract_json,
)
from ..models import Credential, DecisionRecord, ModelScore, User

log = get_logger("zumvia.council")

# Üyelerin stop seviyeleri bu orandan fazla ayrışırsa "kanaat zayıf" sayılır
MAX_STOP_DISPERSION = 0.35          # medyanın %35'i
MIN_WEIGHTED_AGREEMENT = 0.60       # ağırlıklı çoğunluk eşiği
COUNCIL_TIMEOUT = 90.0


# --------------------------------------------------------------------------- #
#  Risk eleştirmeni şeması
# --------------------------------------------------------------------------- #


class CritiqueVerdict(BaseModel):
    """Risk eleştirmeninin dönmek zorunda olduğu yapı."""

    model_config = {"extra": "ignore"}

    verdict: Literal["APPROVE", "VETO", "REDUCE"] = Field(
        description="APPROVE=onayla, VETO=işlemi engelle, REDUCE=riski düşürerek onayla"
    )
    reason: str = Field(default="", max_length=600)
    biggest_risk: str = Field(default="", max_length=300)

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip().upper()
            return {"ONAY": "APPROVE", "ONAYLA": "APPROVE", "OK": "APPROVE",
                    "RED": "VETO", "REDDET": "VETO", "ENGELLE": "VETO",
                    "AZALT": "REDUCE", "KUCULT": "REDUCE"}.get(v, v)
        return v


CRITIC_PROMPT = """Sen bir hedge fonun RİSK KOMİTESİ üyesisin. Görevin işlem
açmak değil, **kötü işlemi engellemektir**. Analistlerin önerisini kabul etmek
zorunda değilsin; aksine hatayı bulmakla görevlisin.

Sana verilenler: deterministik piyasa verileri (Python ile kesin hesaplanmış),
algoritmik motorun bağımsız oyu ve analist modellerin önerileri.

Şunları ara:
- Öneri trendin tersine mi? (en pahalı hata)
- Stop seviyesi teknik olarak anlamsız bir yerde mi (destek/direncin yanlış tarafı)?
- Volatilite stop mesafesine göre aşırı mı? (stop avlanır)
- Analistler birbiriyle çelişiyor mu, gerekçeler zayıf mı?
- Haber/olay riski var mı, likidite yeterli mi?
- Portföy zaten aynı yönde yüklü mü?

Şüphedeysen VETO ver. İşlem yapmamanın maliyeti sıfırdır; kötü işlemin maliyeti
gerçek paradır.

Yanıtın SADECE şu JSON olacak:
{"verdict": "APPROVE" | "VETO" | "REDUCE", "reason": "kısa Türkçe gerekçe",
 "biggest_risk": "bu işlemin en büyük riski tek cümle"}"""


ARBITER_PROMPT = """Sen bir yatırım komitesinin BAŞKANISIN. Analistler bölündü.
Görevin kimin haklı olduğuna karar vermek — ya da ikisi de yeterince güçlü
değilse beklemeye karar vermek.

Sana sayısal veriler ve her analistin gerekçesi verilecek. **Yeni sayı üretme**;
yalnızca hangi tarafın kanıtının daha güçlü olduğunu söyle.

Yanıtın SADECE şu JSON olacak:
{"action": "BUY" | "SELL" | "WAIT", "confidence": 0.0-1.0,
 "reasoning": "hangi tarafı neden seçtiğin, kısa Türkçe"}

Kanıtlar dengeliyse veya ikisi de zayıfsa "WAIT" de. Beklemek bir başarısızlık
değil, disiplindir."""


# --------------------------------------------------------------------------- #
#  Üye tanımı
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CouncilMember:
    label: str
    credential_id: int
    provider: str
    model: str
    role: str = "analyst"           # analyst | risk_critic | arbiter
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "provider": self.provider, "model": self.model,
                "role": self.role, "weight": round(self.weight, 3)}


@dataclass(slots=True)
class MemberOpinion:
    member: CouncilMember
    ok: bool
    action: str = "WAIT"
    confidence: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    reasoning: str = ""
    risk_note: str = ""
    latency_ms: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.member.model, "role": self.member.role,
            "weight": round(self.member.weight, 3), "ok": self.ok,
            "action": self.action, "confidence": round(self.confidence, 3),
            "stop_loss": self.stop_loss, "take_profit": self.take_profit,
            "reasoning": self.reasoning[:500], "risk_note": self.risk_note[:300],
            "latency_ms": self.latency_ms,
            **({"error": self.error[:200]} if self.error else {}),
        }


@dataclass(slots=True)
class CouncilVerdict:
    decision_id: str
    action: str
    confidence: float
    stop_loss: float
    take_profit: float
    reasoning: str
    agreement_pct: float
    participating: int
    responded: int
    veto: bool = False
    veto_reason: str = ""
    opinions: list[MemberOpinion] = field(default_factory=list)
    critique: dict[str, Any] | None = None
    arbitration: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "action": self.action,
            "confidence": round(self.confidence, 3),
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "reasoning": self.reasoning,
            "agreement_pct": round(self.agreement_pct, 1),
            "participating": self.participating,
            "responded": self.responded,
            "veto": self.veto,
            "veto_reason": self.veto_reason,
            "opinions": [o.to_dict() for o in self.opinions],
            "critique": self.critique,
            "arbitration": self.arbitration,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
#  Sicil → ağırlık
# --------------------------------------------------------------------------- #


def member_weight(db: Session, user_id: int, provider: str, model: str) -> float:
    """
    Modelin oy ağırlığı. Yeterli örnek yoksa 1.0 (eşit söz hakkı).
    Sicil biriktikçe ağırlık gerçek performansa göre 0.4 – 1.6 arasında oynar.
    """
    score = (db.query(ModelScore)
             .filter(ModelScore.user_id == user_id,
                     ModelScore.provider == provider,
                     ModelScore.model == model).first())
    if score is None or score.trades < 10:
        return 1.0

    expectancy = score.total_r / max(score.trades, 1)
    win_rate = score.wins / max(score.trades, 1)

    weight = 1.0 + expectancy * 0.6 + (win_rate - 0.5) * 0.6
    if score.schema_failures > 0:
        weight -= min(0.3, score.schema_failures / max(score.decisions, 1))
    return round(max(0.4, min(1.6, weight)), 3)


def record_schema_failure(db: Session, user_id: int, provider: str, model: str) -> None:
    score = _get_or_create_score(db, user_id, provider, model)
    score.schema_failures += 1
    score.updated_at = datetime.now(UTC)
    db.flush()


def _get_or_create_score(db: Session, user_id: int, provider: str, model: str) -> ModelScore:
    score = (db.query(ModelScore)
             .filter(ModelScore.user_id == user_id,
                     ModelScore.provider == provider,
                     ModelScore.model == model).first())
    if score is None:
        score = ModelScore(user_id=user_id, provider=provider, model=model)
        db.add(score)
        db.flush()
    return score


def credit_decision(db: Session, user_id: int, members: list[CouncilMember],
                    latency_by_model: dict[str, int] | None = None) -> None:
    """Karara katılan modellerin sayaçlarını artırır."""
    latency = latency_by_model or {}
    for member in members:
        score = _get_or_create_score(db, user_id, member.provider, member.model)
        score.decisions += 1
        if member.model in latency:
            previous = score.avg_latency_ms * max(score.decisions - 1, 0)
            score.avg_latency_ms = (previous + latency[member.model]) / score.decisions
        score.updated_at = datetime.now(UTC)
    db.flush()


def credit_trade_outcome(db: Session, user_id: int, models: list[dict[str, str]],
                         r_multiple: float) -> None:
    """
    Kapanan işlemin sonucunu, kararı veren modellerin siciline işler.
    Böylece ağırlıklar zamanla gerçek performansa yakınsar.
    """
    for entry in models or []:
        provider = entry.get("provider", "")
        model = entry.get("model", "")
        if not model:
            continue
        score = _get_or_create_score(db, user_id, provider, model)
        score.trades += 1
        score.total_r += float(r_multiple)
        if r_multiple > 0:
            score.wins += 1
        else:
            score.losses += 1
        score.updated_at = datetime.now(UTC)
    db.flush()


# --------------------------------------------------------------------------- #
#  Konsey
# --------------------------------------------------------------------------- #


class ModelCouncil:
    """Aynı karara birden çok modeli baktıran, deterministik birleştiren konsey."""

    def __init__(self, db: Session, user: User, members: list[CouncilMember],
                 mode: str = "council") -> None:
        self.db = db
        self.user = user
        self.members = members
        # "strict" modda analistler ikinci turda birbirinin gerekçesini okur.
        self.mode = mode
        self.analysts = [m for m in members if m.role == "analyst"]
        self.critic = next((m for m in members if m.role == "risk_critic"), None)
        self.arbiter = next((m for m in members if m.role == "arbiter"), None)

    # -- düşük seviye ------------------------------------------------------- #
    def _gateway(self, member: CouncilMember) -> LLMGateway | None:
        cred = self.db.get(Credential, member.credential_id)
        if cred is None or cred.user_id != self.user.id:
            return None
        secrets = read_secrets(self.user, cred)
        extra = read_extra(cred)
        try:
            return LLMGateway(
                provider=cred.provider,
                api_key=secrets.get("api_key", ""),
                model=member.model or extra.get("model", ""),
                base_url=extra.get("base_url", ""),
                temperature=float(extra.get("temperature", 0.2)),
            )
        except ValueError:
            return None

    def _ask_analyst(self, member: CouncilMember, snapshot: dict[str, Any],
                     context: dict[str, Any], recovery: bool,
                     extra_rules: str) -> MemberOpinion:
        gateway = self._gateway(member)
        if gateway is None:
            return MemberOpinion(member, False, error="Model yapılandırması geçersiz.")

        result = gateway.decide(snapshot, context, recovery_mode=recovery,
                                extra_rules=extra_rules)
        if not result.ok or result.decision is None:
            return MemberOpinion(member, False, error=result.error,
                                 latency_ms=result.latency_ms)

        d = result.decision
        return MemberOpinion(
            member=member, ok=True, action=d.action, confidence=d.confidence,
            stop_loss=d.stop_loss, take_profit=d.take_profit,
            reasoning=d.reasoning, risk_note=d.risk_note,
            latency_ms=result.latency_ms,
        )

    # ----------------------------------------------------------------- #
    #  İKİNCİ TUR — analistler birbirini okur (çok ajanlı iç iletişim)
    # ----------------------------------------------------------------- #

    def _debate_round(self, opinions: list[MemberOpinion], snapshot: dict[str, Any],
                      context: dict[str, Any], recovery: bool,
                      extra_rules: str) -> list[MemberOpinion]:
        """
        Analistlere diğerlerinin görüşünü gösterip son sözlerini sorar.

        Neden gerekli? Paralel ve birbirinden habersiz çalışan modeller aynı
        körlüğü paylaşabilir; biri kritik bir ayrıntıyı görmüşse (örneğin bir
        haber ya da bir seviye) diğerleri bunu asla öğrenemez. İkinci turda
        her analist masadaki TÜM gerekçeleri görür ve fikrini
        değiştirebilir — ya da savunabilir.

        Kurallar:
          * Yalnızca `strict` modda çalışır (maliyeti iki katına çıkarır).
          * Fikir değiştirmek zorunlu değildir; ısrar da bir bilgidir.
          * Revizyon başarısız olursa ilk tur görüşü korunur — tur asla
            kararı kaybettirmez.
        """
        valid = [o for o in opinions if o.ok]
        if len(valid) < 2:
            return opinions

        table = "\n".join(
            f"- {o.member.model}: {o.action} (güven {o.confidence:.2f}) — "
            f"{(o.reasoning or '')[:220]}"
            for o in valid
        )
        briefing = (
            "MASADAKİ GÖRÜŞLER (diğer analistler):\n" + table +
            "\n\nBu görüşleri değerlendir. Gördükleri bir şeyi kaçırdıysan "
            "kararını DEĞİŞTİR; kendi gerekçen daha güçlüyse SAVUN. "
            "Çoğunluğa uymak için fikir değiştirme — yalnızca kanıt için değiştir."
        )

        def _revise(previous: MemberOpinion) -> MemberOpinion:
            if not previous.ok:
                return previous
            revised = self._ask_analyst(previous.member, snapshot, context,
                                        recovery, extra_rules + "\n\n" + briefing)
            if not revised.ok:
                return previous                      # revizyon başarısızsa ilk görüş
            revised.changed_mind = revised.action != previous.action
            revised.first_action = previous.action
            return revised

        with ThreadPoolExecutor(max_workers=min(len(valid), 6)) as pool:
            revised = list(pool.map(_revise, opinions))
        return revised

    def _ask_critic(self, proposal: dict[str, Any], snapshot: dict[str, Any],
                    context: dict[str, Any],
                    opinions: list[MemberOpinion]) -> dict[str, Any] | None:
        if self.critic is None:
            return None
        gateway = self._gateway(self.critic)
        if gateway is None:
            return None

        payload = {
            "onerilen_islem": proposal,
            "piyasa_verisi": snapshot,
            "hesap_baglami": context,
            "analist_gerekceleri": [
                {"model": o.member.model, "action": o.action,
                 "confidence": o.confidence, "reasoning": o.reasoning}
                for o in opinions if o.ok
            ],
        }
        try:
            data = gateway._post(  # noqa: SLF001 — aynı paket içi, bilinçli kullanım
                CRITIC_PROMPT,
                json.dumps(payload, ensure_ascii=False, indent=1)[:12000],
                json_mode=True,
            )
            text = gateway._extract_text(gateway.spec.style, data)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            log.warning("Risk eleştirmeni yanıt vermedi: %s", exc)
            return {"verdict": "APPROVE", "reason": f"Eleştirmen ulaşılamadı: {exc}"[:200],
                    "model": self.critic.model, "unavailable": True}

        obj = extract_json(text)
        if obj is None:
            record_schema_failure(self.db, self.user.id, self.critic.provider,
                                  self.critic.model)
            return {"verdict": "VETO", "model": self.critic.model,
                    "reason": "Risk eleştirmeni geçerli JSON döndürmedi — "
                              "fail-safe gereği işlem iptal."}
        try:
            verdict = CritiqueVerdict.model_validate(obj)
        except ValidationError:
            record_schema_failure(self.db, self.user.id, self.critic.provider,
                                  self.critic.model)
            return {"verdict": "VETO", "model": self.critic.model,
                    "reason": "Eleştirmen yanıtı şemaya uymadı — fail-safe."}

        return {"verdict": verdict.verdict, "reason": verdict.reason,
                "biggest_risk": verdict.biggest_risk, "model": self.critic.model}

    def _ask_arbiter(self, snapshot: dict[str, Any], context: dict[str, Any],
                     opinions: list[MemberOpinion]) -> dict[str, Any] | None:
        if self.arbiter is None:
            return None
        gateway = self._gateway(self.arbiter)
        if gateway is None:
            return None

        payload = {
            "piyasa_verisi": snapshot,
            "hesap_baglami": context,
            "bolunmus_analistler": [o.to_dict() for o in opinions if o.ok],
        }
        try:
            data = gateway._post(  # noqa: SLF001
                ARBITER_PROMPT,
                json.dumps(payload, ensure_ascii=False, indent=1)[:12000],
                json_mode=True,
            )
            text = gateway._extract_text(gateway.spec.style, data)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            return {"action": "WAIT", "reasoning": f"Hakem ulaşılamadı: {exc}"[:180],
                    "model": self.arbiter.model}

        obj = extract_json(text) or {}
        action = str(obj.get("action", "WAIT")).upper()
        if action not in ("BUY", "SELL", "WAIT"):
            action = "WAIT"
        try:
            confidence = float(obj.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return {"action": action, "confidence": max(0.0, min(1.0, confidence)),
                "reasoning": str(obj.get("reasoning", ""))[:500],
                "model": self.arbiter.model}

    # -- genel API ---------------------------------------------------------- #
    def deliberate(self, snapshot: dict[str, Any], context: dict[str, Any], *,
                   recovery_mode: bool = False, extra_rules: str = "",
                   algo_action: str = "WAIT") -> CouncilVerdict:
        """
        Tüm analistleri paralel çalıştırır, oyları deterministik birleştirir,
        risk eleştirmenine sunar ve nihai kararı döndürür.
        """
        decision_id = uuid.uuid4().hex[:16]
        notes: list[str] = []

        if not self.analysts:
            return CouncilVerdict(decision_id, "WAIT", 0.0, 0.0, 0.0,
                                  "Konseyde analist model yok.", 0.0, 0, 0,
                                  notes=["Analist tanımlanmamış."])

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=min(len(self.analysts), 6)) as pool:
            opinions = list(pool.map(
                lambda m: self._ask_analyst(m, snapshot, context, recovery_mode, extra_rules),
                self.analysts,
            ))
        # Sağlamcı modda ikinci tur: analistler birbirinin gerekçesini okur.
        if self.mode == "strict" and sum(1 for o in opinions if o.ok) >= 2:
            opinions = self._debate_round(opinions, snapshot, context,
                                          recovery_mode, extra_rules)

        elapsed = int((time.perf_counter() - started) * 1000)

        valid = [o for o in opinions if o.ok]
        for opinion in opinions:
            if not opinion.ok and opinion.error and "JSON" in opinion.error.upper():
                record_schema_failure(self.db, self.user.id,
                                      opinion.member.provider, opinion.member.model)

        credit_decision(self.db, self.user.id, self.analysts,
                        {o.member.model: o.latency_ms for o in opinions})

        if not valid:
            return CouncilVerdict(
                decision_id, "WAIT", 0.0, 0.0, 0.0,
                "Hiçbir analist geçerli yanıt vermedi — fail-safe gereği işlem yok.",
                0.0, len(self.analysts), 0, opinions=opinions,
                notes=[f"Konsey {elapsed} ms içinde yanıtsız kaldı."],
            )

        if len(valid) < len(self.analysts):
            notes.append(f"{len(self.analysts) - len(valid)} analist yanıt vermedi; "
                         "kalanlarla devam edildi.")

        # --- Ağırlıklı oylama (deterministik) --- #
        tally: dict[str, float] = {"BUY": 0.0, "SELL": 0.0, "WAIT": 0.0, "CLOSE": 0.0}
        for opinion in valid:
            tally[opinion.action] = tally.get(opinion.action, 0.0) + (
                opinion.member.weight * max(opinion.confidence, 0.05)
            )

        total_weight = sum(tally.values()) or 1.0
        winner = max(tally, key=lambda k: tally[k])
        agreement = tally[winner] / total_weight * 100.0

        agreeing = [o for o in valid if o.action == winner]

        # --- Bölünmüş konsey → hakem --- #
        arbitration = None
        if winner in ("BUY", "SELL") and agreement < MIN_WEIGHTED_AGREEMENT * 100:
            arbitration = self._ask_arbiter(snapshot, context, valid)
            if arbitration and arbitration["action"] == winner:
                notes.append(f"Konsey bölündü (%{agreement:.0f}); hakem {winner} tarafını seçti.")
                agreement = max(agreement, MIN_WEIGHTED_AGREEMENT * 100)
            else:
                chosen = arbitration["action"] if arbitration else "WAIT"
                notes.append(f"Konsey bölündü (%{agreement:.0f}) ve hakem "
                             f"{'kararsız' if chosen == 'WAIT' else chosen} dedi → işlem yok.")
                return CouncilVerdict(
                    decision_id, "WAIT", 0.0, 0.0, 0.0,
                    "Modeller anlaşamadı. Bölünmüş konseyde işlem açılmaz — "
                    "belirsizlikte beklemek disiplindir.",
                    agreement, len(self.analysts), len(valid),
                    opinions=opinions, arbitration=arbitration, notes=notes,
                )

        if winner in ("WAIT", "CLOSE") or not agreeing:
            return CouncilVerdict(
                decision_id, winner, 0.0, 0.0, 0.0,
                _merge_reasons(agreeing) or "Konsey beklemeyi tercih etti.",
                agreement, len(self.analysts), len(valid),
                opinions=opinions, arbitration=arbitration, notes=notes,
            )

        # --- Algoritmik motorla çelişki kontrolü --- #
        if algo_action in ("BUY", "SELL") and algo_action != winner:
            notes.append(f"Algoritmik motor {algo_action} diyor, konsey {winner}. "
                         "Mutabakat yok → işlem yok.")
            return CouncilVerdict(
                decision_id, "WAIT", 0.0, 0.0, 0.0,
                f"Konsey {winner} dedi ancak bağımsız algoritmik motor {algo_action} "
                "gösteriyor. Çelişkide işlem açılmaz.",
                agreement, len(self.analysts), len(valid),
                opinions=opinions, arbitration=arbitration, notes=notes,
            )

        # --- Seviyeler: anlaşan üyelerin MEDYANI (sayı modelden değil, koddan) --- #
        stops = [o.stop_loss for o in agreeing if o.stop_loss > 0]
        targets = [o.take_profit for o in agreeing if o.take_profit > 0]
        if not stops or not targets:
            return CouncilVerdict(
                decision_id, "WAIT", 0.0, 0.0, 0.0,
                "Anlaşan üyeler geçerli stop/hedef vermedi — fail-safe gereği işlem yok.",
                agreement, len(self.analysts), len(valid),
                opinions=opinions, notes=notes,
            )

        stop = float(statistics.median(stops))
        target = float(statistics.median(targets))

        # Kanaat testi: üyelerin stopları birbirinden çok uzaksa konu net değildir
        if len(stops) >= 2 and stop > 0:
            dispersion = (max(stops) - min(stops)) / abs(stop)
            if dispersion > MAX_STOP_DISPERSION:
                notes.append(f"Stop seviyelerinde %{dispersion * 100:.0f} ayrışma — "
                             "kanaat zayıf.")
                return CouncilVerdict(
                    decision_id, "WAIT", 0.0, 0.0, 0.0,
                    "Modeller yön konusunda hemfikir ama seviyelerde ciddi şekilde "
                    "ayrışıyor. Bu, kurulumun net olmadığını gösterir → işlem yok.",
                    agreement, len(self.analysts), len(valid),
                    opinions=opinions, notes=notes,
                )

        confidence = statistics.median([o.confidence for o in agreeing])
        # Ağırlıklı mutabakat güveni ayarlar: tam mutabakat bonus, zayıf mutabakat ceza
        confidence = max(0.0, min(0.97, confidence * (0.7 + 0.3 * agreement / 100.0)))

        proposal = {"action": winner, "confidence": round(confidence, 3),
                    "stop_loss": stop, "take_profit": target}

        # --- Risk eleştirmeni --- #
        critique = self._ask_critic(proposal, snapshot, context, valid)
        veto = False
        veto_reason = ""
        if critique:
            if critique["verdict"] == "VETO":
                veto = True
                veto_reason = critique.get("reason", "Risk komitesi veto etti.")
                if self.critic:
                    score = _get_or_create_score(self.db, self.user.id,
                                                 self.critic.provider, self.critic.model)
                    score.vetoes += 1
                    self.db.flush()
            elif critique["verdict"] == "REDUCE":
                confidence *= 0.8
                notes.append("Risk komitesi riski azaltarak onayladı; güven düşürüldü.")

        if veto:
            return CouncilVerdict(
                decision_id, "WAIT", 0.0, 0.0, 0.0,
                f"Risk komitesi vetosu: {veto_reason}",
                agreement, len(self.analysts), len(valid), veto=True,
                veto_reason=veto_reason, opinions=opinions, critique=critique,
                arbitration=arbitration, notes=notes,
            )

        return CouncilVerdict(
            decision_id=decision_id, action=winner, confidence=confidence,
            stop_loss=stop, take_profit=target,
            reasoning=_merge_reasons(agreeing),
            agreement_pct=agreement, participating=len(self.analysts),
            responded=len(valid), opinions=opinions, critique=critique,
            arbitration=arbitration, notes=notes,
        )


def _merge_reasons(opinions: list[MemberOpinion]) -> str:
    """Anlaşan üyelerin gerekçelerini tek bir okunabilir metinde birleştirir."""
    parts = []
    for opinion in opinions[:4]:
        reason = (opinion.reasoning or "").strip()
        if reason:
            parts.append(f"{opinion.member.model}: {reason}")
    return " | ".join(parts)[:1200]


# --------------------------------------------------------------------------- #
#  Konsey kurulumu ve kayıt
# --------------------------------------------------------------------------- #


def build_council(db: Session, user: User, config: list[dict[str, Any]]) -> list[CouncilMember]:
    """
    Kullanıcı/ajan yapılandırmasından konsey üyelerini üretir.
    `config` örneği:
        [{"credential_id": 1, "model": "gemini-2.5-flash", "role": "analyst"},
         {"credential_id": 2, "model": "deepseek-chat",    "role": "analyst"},
         {"credential_id": 1, "model": "gemini-2.5-pro",   "role": "risk_critic"}]
    """
    members: list[CouncilMember] = []
    for entry in config or []:
        cred = db.get(Credential, int(entry.get("credential_id") or 0))
        if cred is None or cred.user_id != user.id:
            continue
        model = str(entry.get("model") or read_extra(cred).get("model") or "")
        if not model:
            continue
        role = str(entry.get("role") or "analyst")
        members.append(CouncilMember(
            label=str(entry.get("label") or f"{cred.provider}:{model}"),
            credential_id=cred.id, provider=cred.provider, model=model, role=role,
            weight=member_weight(db, user.id, cred.provider, model),
        ))
    return members


def save_decision(db: Session, user: User, verdict: CouncilVerdict, *,
                  run_id: str, bot_id: int | None, symbol: str, timeframe: str,
                  evidence: dict[str, Any], executed: bool = False) -> DecisionRecord:
    """Kararı kalıcı audit defterine yazar."""
    record = DecisionRecord(
        user_id=user.id, bot_id=bot_id, run_id=run_id,
        decision_id=verdict.decision_id, symbol=symbol, timeframe=timeframe,
        action=verdict.action, confidence=verdict.confidence, executed=executed,
        veto_reason=verdict.veto_reason,
        council_json=json.dumps(verdict.to_dict(), ensure_ascii=False, default=str)[:60000],
        evidence_json=json.dumps(evidence, ensure_ascii=False, default=str)[:60000],
    )
    db.add(record)
    db.flush()
    return record


def council_models(verdict: CouncilVerdict) -> list[dict[str, str]]:
    """İşlem sonucu siciline yazılacak model listesi (yalnızca anlaşanlar)."""
    return [
        {"provider": o.member.provider, "model": o.member.model}
        for o in verdict.opinions
        if o.ok and o.action == verdict.action
    ]
