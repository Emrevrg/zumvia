"""
MODEL HATALARINI İNSAN DİLİNE ÇEVİRME
======================================

Sağlayıcılar hataları HTTP koduyla döner: 401, 404, 410, 429... Kullanıcıya
"Client error '404 Not Found'" göstermek hiçbir şey anlatmaz ve çözüm de
sunmaz. Bu modül her hatayı **ne olduğu + ne yapılacağı** biçiminde açıklar.

Gerçek bir örnek (bu projede yaşandı):
    NVIDIA NIM, `meta/llama-3.3-70b-instruct` modelini emekliye ayırdı.
    Kullanıcı "404 Not Found" gördü ve platformun bozuk olduğunu düşündü.
    Oysa yapılması gereken tek şey başka bir model seçmekti.

Kural: hata mesajı **eylem içermeli**. "Model bulunamadı" yetmez; "bu model
artık yok, şu modeller çalışıyor" gerekir.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..core.logging import get_logger

log = get_logger("zumvia.model_errors")


@dataclass(slots=True)
class ModelProblem:
    """Anlaşılır hata + önerilen eylem."""

    code: str
    message: str
    recoverable: bool = False      # başka modelle otomatik denenebilir mi?
    suggestions: list[str] | None = None

    def to_text(self) -> str:
        text = self.message
        if self.suggestions:
            text += "\n\nÇalışan alternatifler: " + ", ".join(self.suggestions[:5])
        return text


_STATUS_RE = re.compile(r"\b(4\d\d|5\d\d)\b")


def _status_of(error: Any) -> int | None:
    """Hata nesnesinden/metninden HTTP kodunu çıkarır."""
    response = getattr(error, "response", None)
    if response is not None and getattr(response, "status_code", None):
        return int(response.status_code)
    match = _STATUS_RE.search(str(error))
    return int(match.group(1)) if match else None


def explain(error: Any, *, provider: str = "", model: str = "",
            available: list[str] | None = None) -> ModelProblem:
    """
    Sağlayıcı hatasını kullanıcının anlayacağı ve çözebileceği biçime çevirir.

    `available` verilirse (canlı model listesi) öneri olarak sunulur.
    """
    status = _status_of(error)
    raw = str(error)
    label = f"'{model}'" if model else "seçili model"
    suggestions = [m for m in (available or []) if m != model][:5]

    lowered = raw.lower()

    # Sağlayıcılar kimlik hatasını AYNI kodla döndürmez. Google, geçersiz
    # anahtara 400 "Please pass a valid API key" der; 401 beklemek bu hatayı
    # "bilinmeyen" kutusuna düşürür ve kullanıcı ham HTTP metni görür —
    # oysa yapılması gereken tek şey anahtarı düzeltmektir.
    _AUTH_WORDS = ("unauthorized", "invalid api key", "api key not valid",
                   "valid api key", "api_key_invalid", "invalid authentication",
                   "incorrect api key", "authentication failed",
                   "no auth credentials", "permission denied")

    if status == 401 or any(word in lowered for word in _AUTH_WORDS):
        return ModelProblem(
            "AUTH",
            f"{provider or 'Sağlayıcı'} API anahtarınızı kabul etmedi. "
            f"Anahtarın doğru kopyalandığından ve süresinin dolmadığından emin olun "
            f"(Kasa → anahtarı düzenle).",
        )

    if status == 402 or "quota" in lowered or "insufficient" in lowered:
        return ModelProblem(
            "QUOTA",
            f"{provider or 'Sağlayıcı'} hesabınızda kullanılabilir kota/kredi yok. "
            f"Ücretsiz kotası olan bir sağlayıcı ekleyebilirsiniz (örn. Google Gemini).",
        )

    if status == 410:
        return ModelProblem(
            "RETIRED",
            f"{label} modeli sağlayıcı tarafından **emekliye ayrıldı** ve artık "
            f"çağrılamıyor. Model listesinden güncel bir model seçin.",
            recoverable=True, suggestions=suggestions,
        )

    # Sağlayıcılar "bu model yok" demenin onlarca yolunu bulmuş: kimi 404,
    # kimi 400 + açıklama döndürür. Metne bakmadan yalnızca koda güvenmek,
    # çalışmayan bir modelde ısrar etmeye yol açar.
    _MISSING_WORDS = ("does not support endpoint", "no endpoints found",
                      "not a valid model", "model not found", "unknown model",
                      "no such model", "model_not_found", "is not available")

    if status == 404 or any(word in lowered for word in _MISSING_WORDS):
        return ModelProblem(
            "NOT_FOUND",
            f"{label} modeli {provider or 'bu sağlayıcıda'} bulunamadı. "
            f"Model adı değişmiş, kaldırılmış ya da hesabınıza açık olmayabilir.",
            recoverable=True, suggestions=suggestions,
        )

    if status == 429:
        return ModelProblem(
            "RATE_LIMIT",
            f"{provider or 'Sağlayıcı'} hız sınırına ulaşıldı. Sistem otomatik "
            f"bekleyip yeniden deniyor; sık tekrarlıyorsa Kontrol merkezi → "
            f"Sınırlar bölümünden dakikalık limiti düşürün.",
            recoverable=True,
        )

    if status and status >= 500:
        return ModelProblem(
            "PROVIDER_DOWN",
            f"{provider or 'Sağlayıcı'} şu anda yanıt veremiyor (sunucu hatası). "
            f"Sistem deterministik motorla çalışmaya devam ediyor.",
            recoverable=True, suggestions=suggestions,
        )

    if "timeout" in lowered or "timed out" in lowered:
        return ModelProblem(
            "TIMEOUT",
            f"{provider or 'Sağlayıcı'} zamanında yanıt vermedi. Daha küçük/hızlı "
            f"bir model deneyebilirsiniz.",
            recoverable=True, suggestions=suggestions,
        )

    if "connect" in lowered or "ssl" in lowered or "dns" in lowered:
        return ModelProblem(
            "NETWORK",
            "Sağlayıcıya ulaşılamadı (ağ/bağlantı sorunu). İnternet bağlantınızı "
            "ve varsa vekil/güvenlik duvarı ayarlarınızı kontrol edin.",
            recoverable=True,
        )

    return ModelProblem("UNKNOWN",
                        f"Model çağrısı başarısız: {raw[:200]}",
                        recoverable=True, suggestions=suggestions)


def working_models(provider: str, api_key: str, base_url: str = "",
                   probe: int = 6) -> list[str]:
    """
    Sağlayıcıda GERÇEKTEN çağrılabilen modelleri bulur.

    Model listesinde görünmek, çağrılabilmek anlamına gelmiyor: sağlayıcılar
    emekli modelleri listede bırakabiliyor. Bu yüzden küçük bir istekle
    (max_tokens=4) denenir. Hız sınırına saygılıdır.
    """
    import httpx  # noqa: PLC0415

    from .model_discovery import discover  # noqa: PLC0415
    from .rate_limit import RateLimitTimeout, acquire  # noqa: PLC0415

    catalog = discover(provider, api_key, base_url).get("models", [])
    if not catalog or not api_key or not base_url:
        return []

    good: list[str] = []
    for name in catalog[:max(1, probe) * 4]:
        if len(good) >= probe:
            break
        try:
            with acquire(provider, est_tokens=32, max_wait=20):
                response = httpx.post(
                    base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": name, "max_tokens": 4,
                          "messages": [{"role": "user", "content": "hi"}]},
                    timeout=20,
                )
            if response.status_code == 200:
                good.append(name)
        except RateLimitTimeout:
            break
        except Exception as exc:  # noqa: BLE001 — bir modelin düşmesi yoklamayı bitirmez
            log.debug("%s/%s yoklaması başarısız: %s", provider, name, exc)
            continue

    log.info("%s: %d çalışan model bulundu", provider, len(good))
    return good
