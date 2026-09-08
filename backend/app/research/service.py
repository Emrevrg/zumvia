"""
ARAŞTIRMA SERVİSİ — boru hattını platforma bağlar
==================================================

Boru hattı bilinçli olarak platformdan habersizdir: model nereden geliyor,
veri nereye yazılıyor, olaylar kime gidiyor bilmez. Bu modül o bağlantıları
kurar:

    * uzman rollerine FARKLI sağlayıcı/model atar (biri düşerse iş durmaz),
    * ölçümleri gerçek borsa/haber/geri test katmanlarından toplar,
    * ilerlemeyi WebSocket'ten yayınlar (yan panel canlı izler),
    * biten raporu diske ve veritabanına yazar.

Rol dağıtımı önemlidir: dört uzman aynı modele sorulursa dört kez aynı
önyargı elde edilir. Farklı sağlayıcılara dağıtmak hem dayanıklılık hem de
gerçek çeşitlilik sağlar.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..core.creds import read_extra, read_secrets
from ..core.logging import get_logger
from ..engine.hub import hub
from ..layers.l3_llm_gateway import PROVIDERS, ChatGateway
from ..models import Credential, CredentialKind, User
from . import collectors
from . import report as report_mod
from . import risk as risk_mod
from .pipeline import ResearchPipeline, ResearchResult
from .roles import Role

log = get_logger("zumvia.research.service")

#  Raporlar proje kökünde tutulur (kanıt raporlarıyla aynı yerde).
BASE_DIR = Path(__file__).resolve().parents[2]
REPORT_DIR = BASE_DIR / "reports" / "research"


class RoleAssigner:
    """
    Uzman rollerini kullanılabilir modellere dağıtır.

    Kural: mümkün olduğunca FARKLI sağlayıcı. Dört uzman tek modele
    sorulursa dört kez aynı önyargı alınır ve "çoklu ajan" bir yanılsamaya
    dönüşür. Ayrıca tek sağlayıcı düştüğünde tüm araştırma çöker.

    Anahtar tek taneyse elden geleni yapar: aynı sağlayıcının farklı
    modellerine dağıtır. O da yoksa hepsi aynı modeli kullanır — ama bu
    durum rapora not olarak yazılır, sessizce geçilmez.
    """

    def __init__(self, db: Session, user: User) -> None:
        self.db = db
        self.user = user
        self._pool: list[tuple[Credential, str]] = self._build_pool()
        self._cursor = 0
        self._cache: dict[str, Any] = {}
        self._avoid: dict[str, set[str]] = {}   # rol -> denenmiş modeller

    def _build_pool(self) -> list[tuple[Credential, str]]:
        creds = (self.db.query(Credential)
                 .filter(Credential.user_id == self.user.id,
                         Credential.kind == CredentialKind.LLM)
                 .order_by(Credential.id).all())

        pool: list[tuple[Credential, str]] = []
        # Önce her sağlayıcıdan BİRER model: çeşitlilik önce genişlik ister.
        for cred in creds:
            model = self._preferred_model(cred)
            if model:
                pool.append((cred, model))

        # Sonra aynı sağlayıcının diğer modelleri: derinlik.
        #
        # Çalışmadığı ÖLÇÜLMÜŞ modeller atlanır. Katalog listeleri güvenilmez
        # (OpenRouter'ın listelediği bir model "does not support endpoint"
        # döndürebilir); bir uzmanı bilerek bozuk bir modele göndermek, o
        # uzmanın görüşünü boşuna kaybetmek demektir.
        from ..layers import model_verify  # noqa: PLC0415

        for cred in creds:
            spec = PROVIDERS.get(cred.provider)
            if not spec:
                continue
            # Araştırma DÜZ SOHBET kullanır; yalnızca araç şemasıyla
            # çalışan modeller buraya alınmaz.
            usable = model_verify.plain_capable(self.db, cred.id, spec.models[1:6])
            for model in usable:
                if not any(c.id == cred.id and m == model for c, m in pool):
                    pool.append((cred, model))
        return pool

    def _preferred_model(self, cred: Credential) -> str:
        """
        Bu anahtar için en iyi model.

        Doğrulanmış (gerçekten çağrılabildiği ölçülmüş) modeller önceliklidir;
        yoksa kullanıcının seçtiği, o da yoksa katalog varsayılanı.
        """
        from ..layers import model_verify  # noqa: PLC0415

        extra = read_extra(cred)
        spec = PROVIDERS.get(cred.provider)
        candidates = [extra.get("model", ""), *(spec.models if spec else ())]
        candidates = [c for c in candidates if c]
        if not candidates:
            return ""
        usable = model_verify.plain_capable(self.db, cred.id, candidates) or candidates
        return model_verify.best_model(self.db, cred.id, usable) or usable[0]

    @property
    def diversity_note(self) -> str:
        """Kullanıcıya söylenecek dürüst durum."""
        providers = {cred.provider for cred, _ in self._pool}
        if not self._pool:
            return "Kayıtlı yapay zeka anahtarı yok; uzmanlar çalışamaz."
        if len(providers) == 1 and len(self._pool) == 1:
            return ("Tek model kullanılıyor: uzman rolleri farklı bakış açılarıyla "
                    "çalışır ama hepsi aynı modelin önyargısını taşır. İkinci bir "
                    "sağlayıcı eklerseniz çeşitlilik gerçek olur.")
        if len(providers) == 1:
            return (f"{len(self._pool)} model, tek sağlayıcı ({providers.pop()}). "
                    f"Çeşitlilik var, ama sağlayıcı düşerse tüm uzmanlar durur.")
        return f"{len(self._pool)} model, {len(providers)} farklı sağlayıcı."

    def _pick(self, role: Role) -> tuple[Credential, str]:
        """
        Bu rol için sıradaki modeli seçer.

        Sıra round-robin'dir ama UYUM SİCİLİ gözetilir: bir model bu rolde
        yönergeye uymamışsa öne alınmaz. Böylece sistem hangi modele ne
        verebileceğini zamanla öğrenir — küçük bir modeli inatla sentez
        rolüne göndermeye devam etmez.

        Sicil boşsa hiçbir model dışlanmaz: bilinmemek, kötü olmakla aynı
        şey değildir.
        """
        from . import compliance  # noqa: PLC0415

        avoided = self._avoid.setdefault(role.id, set())
        size = len(self._pool)

        best: tuple[Credential, str] | None = None
        best_score = -1.0
        for step in range(size):
            candidate = self._pool[(self._cursor + step) % size]
            if candidate[1] in avoided:
                continue
            score = compliance.reliability(candidate[1], role.id)
            if score > best_score:
                best, best_score = candidate, score
            if score >= 1.0:
                break                       # kusursuz sicil; aramayı uzatma

        chosen = best or self._pool[self._cursor % size]
        self._cursor += 1
        avoided.add(chosen[1])
        return chosen

    def gateway_for(self, role: Role, fresh: bool = False) -> Any:
        """
        Bir role sıradaki modeli atar (round-robin).

        `fresh` verilirse önbellek atlanır ve havuzdaki BİR SONRAKİ model
        kullanılır. Bir model boş yanıt döndürdüğünde aynısıyla tekrar
        denemenin faydası yoktur; başkasına geçmek gerekir.
        """
        if not self._pool:
            return None
        if fresh:
            self._cache.pop(role.id, None)
            self._avoid.setdefault(role.id, set())
        elif role.id in self._cache:
            return self._cache[role.id]

        cred, model = self._pick(role)

        secrets = read_secrets(self.user, cred)
        extra = read_extra(cred)
        api_key = secrets.get("api_key", "")
        if not api_key:
            return None

        spec = PROVIDERS.get(cred.provider)
        gateway = ChatGateway(
            provider=cred.provider,
            api_key=api_key,
            model=model,
            base_url=extra.get("base_url", "") or (spec.base_url if spec else ""),
            temperature=float(extra.get("temperature", 0.2)),
        )
        self._cache[role.id] = gateway
        log.info("rol %s → %s / %s", role.id, cred.provider, model)
        return gateway


def run_research(db: Session, user: User, question: str, *,
                 session_id: int | None = None,
                 timeframe: str = "4h",
                 with_backtest: bool = True,
                 cancelled: Any = None) -> ResearchResult:
    """
    Bir araştırmayı baştan sona çalıştırır ve raporu kaydeder.

    Bloklayıcıdır; çağıran arka planda çalıştırmalıdır. İlerleme WebSocket
    üzerinden akar, bu yüzden kullanıcı beklerken ne olduğunu görür.
    """
    assigner = RoleAssigner(db, user)

    def emit(event: dict[str, Any]) -> None:
        hub.publish(user.id, {**event, "session_id": session_id})

    def collect(q: str, ledger) -> dict[str, Any]:
        return collectors.collect(q, ledger, db=db, user_id=user.id,
                                  timeframe=timeframe,
                                  with_backtest=with_backtest, emit=emit)

    emit({"type": "research_start", "question": question,
          "models": assigner.diversity_note})

    pipeline = ResearchPipeline(
        gateway_for=assigner.gateway_for,
        collect=collect,
        measure_risk=risk_mod.measure,
        emit=emit,
        cancelled=cancelled or (lambda: False),
    )
    result = pipeline.run(question)

    if len(assigner._pool) <= 1:                    # noqa: SLF001 — kendi sınıfı
        result.warnings.append(assigner.diversity_note)

    markdown = report_mod.render(result)
    path = _save(user.id, result, markdown)

    emit({"type": "research_done", "trust_score": round(result.trust_score, 3),
          "facts": len(result.ledger), "duration_ms": result.duration_ms,
          "path": str(path) if path else "", "warnings": result.warnings})
    return result


def _save(user_id: int, result: ResearchResult, markdown: str) -> Path | None:
    """
    Raporu diske yazar.

    Yerel dosya bilinçli bir tercih: kullanıcının araştırması cihazından
    çıkmaz, dışarıya gönderilmez ve platform kapansa bile elinde kalır.
    """
    # Disk kritikse rapor YAZILMAZ — ama bu sessizce geçilmez.
    #
    # Rapor vazgeçilebilir; pozisyon kaydı değil. Sıkışınca lüks kesilir,
    # zorunlu olan korunur. Kullanıcı raporun neden yazılmadığını görür.
    from ..core import storage  # noqa: PLC0415

    disk = storage.ensure_space_for_optional_write()
    if not disk.can_write_optional:
        log.warning("rapor yazılmadı: %s", disk.message)
        result.warnings.append(
            f"Araştırma tamamlandı ama rapor dosyası YAZILAMADI: {disk.message} "
            f"Sonuçlar ekranda duruyor; kalıcı kayıt için yer açın.")
        return None

    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        slug = "".join(c if c.isalnum() else "-" for c in result.question[:40]).strip("-")
        base = REPORT_DIR / f"{user_id}-{stamp}-{slug or 'arastirma'}"

        base.with_suffix(".md").write_text(markdown, encoding="utf-8")
        base.with_suffix(".json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        log.info("araştırma raporu yazıldı: %s", base.with_suffix('.md'))
        return base.with_suffix(".md")
    except Exception:  # noqa: BLE001 — rapor yazılamasa da araştırma geçerlidir
        log.exception("rapor kaydedilemedi")
        return None
