"""
MODEL DOĞRULAMA — "listede var" ile "gerçekten çalışıyor" farkı
================================================================

Sağlayıcıların `/models` uç noktası dürüst değildir. NVIDIA NIM'in listesinde
38 model görünürken hesabın çağırabildiği yalnızca birkaçıdır; gerisi 404
(hesaba kapalı), 410 (emekliye ayrılmış) ya da ayrı abonelik ister. Kullanıcı
listeden birini seçer, görev ilk turda ölür ve suç platformda görünür.

Bu modül farkı gerçek bir çağrıyla ölçer:

  * en küçük istek gönderilir (4 token) — kotadan neredeyse hiç yemez,
  * araç çağırma yeteneği ayrıca sınanır: finans ajanı araç çağıramayan bir
    modelle çalışamaz, o model "çalışıyor" sayılsa bile işe yaramaz,
  * sonuç kalıcı yazılır; her açılışta tekrar sorgulanmaz,
  * hız sınırı katmanına uyulur — doğrulama, sağlayıcıyı kızdırmamalıdır.

Doğrulanmamış model YASAK değildir: kullanıcı elle yazdığını kullanabilir.
Sistem yalnızca ne bildiğini ve ne bilmediğini dürüstçe gösterir.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..models import ModelCheck
from .l3_llm_gateway import PROVIDERS
from .rate_limit import RateLimitTimeout, acquire

log = get_logger("zumvia.model_verify")

TIMEOUT = 25.0
PROBE_TOKENS = 4                 # yanıtın içeriği önemsiz; sadece kabul edilsin
FRESH_FOR = timedelta(hours=24)  # bu süre içindeki sonuç yeniden sorgulanmaz

#  Araç çağırma yoklaması için en küçük şema. Finans ajanı araç çağıramayan
#  bir modelle çalışamaz; "cevap veriyor" yetmez.
_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Bağlantı yoklaması.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


@dataclass(slots=True)
class Verdict:
    """Tek bir modelin doğrulama sonucu."""

    model: str
    ok: bool
    code: str = ""
    detail: str = ""
    supports_tools: bool = False
    works_plain: bool = True          # araçsız (düz sohbet) çağrıda çalışıyor mu
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model, "ok": self.ok, "code": self.code,
                "detail": self.detail, "supports_tools": self.supports_tools,
                "works_plain": self.works_plain, "latency_ms": self.latency_ms}


@dataclass(slots=True)
class Report:
    """Bir anahtarın tüm modelleri için doğrulama özeti."""

    provider: str
    verdicts: list[Verdict] = field(default_factory=list)
    skipped: int = 0

    @property
    def working(self) -> list[str]:
        return [v.model for v in self.verdicts if v.ok]

    @property
    def tool_capable(self) -> list[str]:
        return [v.model for v in self.verdicts if v.ok and v.supports_tools]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "checked": len(self.verdicts),
            "skipped": self.skipped,
            "working": self.working,
            "tool_capable": self.tool_capable,
            "results": [v.to_dict() for v in self.verdicts],
        }


# --------------------------------------------------------------------------- #
#  Tek model yoklaması
# --------------------------------------------------------------------------- #

def _classify(response: httpx.Response) -> tuple[str, str]:
    """HTTP yanıtını (kod, açıklama) çiftine çevirir."""
    from .model_errors import explain  # noqa: PLC0415

    body = response.text[:300]
    problem = explain(f"HTTP {response.status_code} {body}")
    return problem.code, body


def probe(provider: str, api_key: str, base_url: str, model: str,
          check_tools: bool = True) -> Verdict:
    """
    Modeli gerçekten çağırarak çalışıp çalışmadığını ölçer.

    İKİ biçim de sınanır, çünkü sistem ikisini de kullanır:

        ARAÇLI      ajan döngüsü (araç çağırmalı sohbet)
        ARAÇSIZ     araştırma boru hattı (düz sohbet)

    Bir modelin birinde çalışıp diğerinde çalışmaması gerçek bir durumdur:
    `qwen/qwen-2.5-72b-instruct` OpenRouter üzerinden yalnızca araç şemasıyla
    kabul edilir, araçsız çağrıda 400 döndürür. Yalnızca bir biçimi
    doğrulamak, "doğrulanmış" damgası vurulmuş bir modelin kullanıldığı yerde
    patlaması demektir.
    """
    spec = PROVIDERS.get(provider)
    url = (base_url or (spec.base_url if spec else "")).rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": PROBE_TOKENS,
        "messages": [{"role": "user", "content": "ping"}],
    }
    if check_tools:
        payload["tools"] = [_PROBE_TOOL]

    started = time.perf_counter()
    try:
        with acquire(provider, est_tokens=64, max_wait=30):
            with httpx.Client(timeout=TIMEOUT) as client:
                response = client.post(url, headers=headers, json=payload)
    except RateLimitTimeout as exc:
        return Verdict(model, False, "RATE_LIMIT", str(exc)[:300])
    except Exception as exc:  # noqa: BLE001 — ağ arızası modelin suçu değil
        return Verdict(model, False, "NETWORK", str(exc)[:300])

    latency = int((time.perf_counter() - started) * 1000)

    if response.status_code == 200:
        if not check_tools:
            # Araçsız yoklama başarılı: bu çağrı zinciri araçlı denemeden
            # sonra gelmiş olabilir, kararı çağıran birleştirir.
            return Verdict(model, True, "", "", supports_tools=False,
                           works_plain=True, latency_ms=latency)

        # Araçlı çalışıyor. Araçsız da çalışıyor mu? Araştırma boru hattı
        # düz sohbet kullandığı için bu ayrıca ölçülmeli.
        plain = probe(provider, api_key, base_url, model, check_tools=False)
        return Verdict(model, True, "", "" if plain.ok else
                       "Model YALNIZCA araç şemasıyla çağrılabiliyor; düz "
                       "sohbette hata veriyor.",
                       supports_tools=True, works_plain=plain.ok,
                       latency_ms=latency)

    # 400 + araç şeması → model muhtemelen araç desteklemiyor; araçsız dene.
    if response.status_code == 400 and check_tools:
        plain = probe(provider, api_key, base_url, model, check_tools=False)
        if plain.ok:
            plain.supports_tools = False
            plain.works_plain = True
            plain.detail = "Model araç çağırmayı desteklemiyor."
        return plain

    code, detail = _classify(response)
    return Verdict(model, False, code, detail, works_plain=False,
                   latency_ms=latency)


# --------------------------------------------------------------------------- #
#  Kalıcı sicil
# --------------------------------------------------------------------------- #

def cached(db: Session, credential_id: int) -> dict[str, dict[str, Any]]:
    """Bu anahtar için daha önce öğrenilenler."""
    rows = db.query(ModelCheck).filter(ModelCheck.credential_id == credential_id).all()
    return {
        row.model: {
            "ok": row.ok, "code": row.code, "detail": row.detail,
            "supports_tools": row.supports_tools, "works_plain": row.works_plain,
            "latency_ms": row.latency_ms,
            "checked_at": row.checked_at.isoformat() if row.checked_at else None,
            "fresh": _is_fresh(row.checked_at),
        }
        for row in rows
    }


def _is_fresh(checked_at: datetime | None) -> bool:
    if checked_at is None:
        return False
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - checked_at < FRESH_FOR


def remember(db: Session, credential_id: int, verdict: Verdict) -> None:
    """Sonucu kalıcı yazar (aynı model için üzerine yazılır)."""
    row = (db.query(ModelCheck)
           .filter(ModelCheck.credential_id == credential_id,
                   ModelCheck.model == verdict.model).first())
    if row is None:
        row = ModelCheck(credential_id=credential_id, model=verdict.model)
        db.add(row)
    row.ok = verdict.ok
    row.code = verdict.code[:32]
    row.detail = verdict.detail[:400]
    row.supports_tools = verdict.supports_tools
    row.works_plain = verdict.works_plain
    row.latency_ms = verdict.latency_ms
    row.checked_at = datetime.now(UTC)
    db.commit()


def forget(db: Session, credential_id: int) -> int:
    """Bu anahtarın tüm doğrulama geçmişini siler (yeniden tarama için)."""
    count = (db.query(ModelCheck)
             .filter(ModelCheck.credential_id == credential_id)
             .delete(synchronize_session=False))
    db.commit()
    return count


# --------------------------------------------------------------------------- #
#  Toplu doğrulama
# --------------------------------------------------------------------------- #

def verify_all(db: Session, credential_id: int, provider: str, api_key: str,
               base_url: str, models: Iterable[str],
               force: bool = False,
               on_progress: Callable[[int, int, Verdict], None] | None = None,
               ) -> Report:
    """
    Verilen modellerin hepsini sırayla yoklar ve sonuçları kalıcı yazar.

    Sıralı yapılır, paralel değil: amaç sağlayıcıyı hız sınırına sokmadan
    doğru bilgi almaktır. `force` verilmezse taze sonuçlar atlanır, böylece
    ikinci tarama neredeyse anında biter.

    `on_progress`, her model bittiğinde çağrılır — arayüz ilerlemeyi canlı
    gösterebilsin diye.
    """
    known = cached(db, credential_id)
    todo = list(models)
    report = Report(provider=provider)

    for index, model in enumerate(todo, start=1):
        previous = known.get(model)
        if not force and previous and previous["fresh"]:
            report.skipped += 1
            verdict = Verdict(model, previous["ok"], previous["code"],
                              previous["detail"], previous["supports_tools"],
                              previous.get("works_plain", True),
                              previous["latency_ms"])
            report.verdicts.append(verdict)
            if on_progress:
                on_progress(index, len(todo), verdict)
            continue

        verdict = probe(provider, api_key, base_url, model)
        remember(db, credential_id, verdict)
        report.verdicts.append(verdict)
        if on_progress:
            on_progress(index, len(todo), verdict)

    log.info("%s doğrulaması bitti: %d/%d çalışıyor (%d araç yetenekli)",
             provider, len(report.working), len(report.verdicts),
             len(report.tool_capable))
    return report


def best_model(db: Session, credential_id: int, candidates: Iterable[str]) -> str:
    """
    Doğrulanmışlar arasından en iyi varsayılanı seçer.

    Öncelik sırası: araç çağırabilen ve çalışan → çalışan → hiç bilinmeyen.
    Ajan araç çağıramayan bir modelle iş yapamayacağı için bu sıra önemlidir.
    """
    known = cached(db, credential_id)
    names = list(candidates)

    for name in names:
        row = known.get(name)
        if row and row["ok"] and row["supports_tools"]:
            return name
    for name in names:
        row = known.get(name)
        if row and row["ok"]:
            return name
    for name in names:
        if name not in known:
            return name
    return names[0] if names else ""


def plain_capable(db: Session, credential_id: int, candidates: Iterable[str]) -> list[str]:
    """
    DÜZ SOHBETTE çalıştığı bilinen modeller.

    Araştırma boru hattı araç göndermez; yalnızca araçla çalışan bir modeli
    oraya vermek, o uzmanın görüşünü baştan kaybetmektir. Hiç denenmemiş
    modeller listeye dahildir — bilinmemek, bozuk olmakla aynı şey değildir.
    """
    known = cached(db, credential_id)
    out: list[str] = []
    for name in candidates:
        row = known.get(name)
        if row is None:
            out.append(name)                     # denenmemiş: şansı var
        elif row["ok"] and row.get("works_plain", True):
            out.append(name)
    return out
