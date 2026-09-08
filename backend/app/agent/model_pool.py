"""
MODEL HAVUZU — çoklu işlem için model seçim stratejileri
=========================================================

Kullanıcı tek bir soru sorar: "birden çok model çalışsın mı, nasıl?"
Cevabı üç stratejiden biridir:

┌────────────────┬──────────────────────────────────────────────────────────┐
│ `single`       │ Tek sağlayıcı, tek model. En ucuz ve en hızlı.           │
│ `same_provider`│ TEK sağlayıcı, FARKLI modeller. Tek faturada çeşitlilik: │
│                │ aynı anahtarla güçlü + hızlı modeli birlikte kullanır.   │
│ `multi_provider│ FARKLI sağlayıcılar. En dayanıklısı: bir sağlayıcı çökse │
│                │ ya da kotası bitse diğerleri çalışmaya devam eder.       │
└────────────────┴──────────────────────────────────────────────────────────┘

Havuz, hız sınırlarını **bilerek** dağıtım yapar: NVIDIA NIM dakikada 40
istek kabul ediyorsa ve havuzda üç NVIDIA modeli varsa, üçü aynı kotayı
paylaşır — bu yüzden aynı sağlayıcıdan çok model seçmek eş zamanlılığı
artırmaz, yalnızca çeşitliliği artırır. Farklı sağlayıcılar ise kotalarını
paylaşmaz; gerçek paralellik oradan gelir.

Bu bilgi kullanıcıya da gösterilir: seçtiği stratejinin ne kazandırıp ne
kazandırmadığını tahmin etmesi gerekmez.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..core.creds import read_extra
from ..core.logging import get_logger
from ..layers.l3_llm_gateway import PROVIDERS
from ..layers.rate_limit import DEFAULT_RPM, PROVIDER_LIMITS
from ..models import Credential, CredentialKind, User

log = get_logger("zumvia.pool")

STRATEGIES = ("single", "same_provider", "multi_provider")
MAX_POOL = 6


@dataclass(slots=True)
class PoolMember:
    credential_id: int
    provider: str
    model: str
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {"credential_id": self.credential_id, "provider": self.provider,
                "model": self.model, "label": self.label}


def _llm_credentials(db: Session, user: User) -> list[Credential]:
    return (db.query(Credential)
            .filter(Credential.user_id == user.id,
                    Credential.kind == CredentialKind.LLM)
            .order_by(Credential.id).all())


def _models_for(cred: Credential, limit: int) -> list[str]:
    """Bir anahtarın kullanılabilir modelleri (kayıtlı model önce gelir)."""
    extra = read_extra(cred)
    chosen = (extra.get("model") or "").strip()
    catalog = list(PROVIDERS[cred.provider].models) if cred.provider in PROVIDERS else []

    ordered: list[str] = []
    if chosen:
        ordered.append(chosen)
    for name in catalog:
        if name not in ordered:
            ordered.append(name)
    return ordered[:max(1, limit)]


def build(db: Session, user: User, strategy: str = "single", *,
          size: int = 3) -> dict[str, Any]:
    """
    Stratejiye göre model havuzunu kurar ve **ne beklemesi gerektiğini** söyler.

    Havuz boş dönmez: hiç anahtar yoksa bunu açıkça bildirir; tek anahtar varsa
    `multi_provider` istense bile elindekiyle en iyisini yapar ve nedenini yazar.
    """
    strategy = strategy if strategy in STRATEGIES else "single"
    size = max(1, min(size, MAX_POOL))
    creds = _llm_credentials(db, user)

    if not creds:
        return {"strategy": strategy, "members": [], "usable": False,
                "reason": "Kayıtlı yapay zeka anahtarı yok."}

    members: list[PoolMember] = []
    notes: list[str] = []

    if strategy == "single" or (len(creds) == 1 and strategy == "multi_provider"):
        if strategy == "multi_provider":
            notes.append("Tek sağlayıcı anahtarınız var; farklı sağlayıcı stratejisi "
                         "uygulanamadı. İkinci bir sağlayıcı eklerseniz dayanıklılık "
                         "belirgin şekilde artar.")
        cred = creds[0]
        model = _models_for(cred, 1)[0]
        members.append(PoolMember(cred.id, cred.provider, model, cred.label))

    elif strategy == "same_provider":
        cred = creds[0]
        for model in _models_for(cred, size):
            members.append(PoolMember(cred.id, cred.provider, model, cred.label))
        if len(members) > 1:
            notes.append(f"Bu modellerin hepsi {cred.provider} kotasını paylaşır "
                         f"(dakikada {_rpm(cred.provider)} istek). Çeşitlilik artar, "
                         f"eş zamanlılık artmaz.")

    else:  # multi_provider
        seen: set[str] = set()
        for cred in creds:
            if cred.provider in seen:
                continue
            seen.add(cred.provider)
            model = _models_for(cred, 1)[0]
            members.append(PoolMember(cred.id, cred.provider, model, cred.label))
            if len(members) >= size:
                break
        notes.append("Farklı sağlayıcılar ayrı kotalara sahiptir: biri çökse ya da "
                     "kotası bitse diğerleri çalışmaya devam eder.")

    total_rpm = _combined_rpm(members)
    return {
        "strategy": strategy,
        "members": [m.to_dict() for m in members],
        "size": len(members),
        "usable": bool(members),
        "combined_rpm": total_rpm,
        "parallel_capacity": _parallel_capacity(members),
        "notes": notes,
    }


def _rpm(provider: str) -> int:
    spec = PROVIDER_LIMITS.get((provider or "").lower())
    return spec.rpm if spec else DEFAULT_RPM


def _combined_rpm(members: list[PoolMember]) -> int:
    """
    Havuzun toplam dakikalık kapasitesi.

    Aynı sağlayıcıdan birden çok model kotayı BÖLÜŞÜR; bu yüzden sağlayıcı
    başına bir kez sayılır. Kullanıcının "üç model seçtim, üç kat hızlıyım"
    sanmasını engelleyen ayrım budur.
    """
    return sum(_rpm(provider) for provider in {m.provider for m in members})


def _parallel_capacity(members: list[PoolMember]) -> int:
    """Aynı anda kaç bağımsız iş yürütülebilir (sağlayıcı sayısı kadar)."""
    return len({m.provider for m in members}) or 0


def describe(strategy: str) -> str:
    """Arayüzde gösterilen tek cümlelik açıklama."""
    return {
        "single": "Tek model — en hızlı ve en ucuz.",
        "same_provider": "Tek sağlayıcı, farklı modeller — tek faturada çeşitlilik.",
        "multi_provider": "Farklı sağlayıcılar — biri çökse diğerleri devam eder.",
    }.get(strategy, "")
