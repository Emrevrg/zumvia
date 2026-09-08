"""
MODEL KEŞFİ — sağlayıcıdan güncel model listesini canlı çeker
==============================================================

Sağlayıcılar model listelerini sık değiştirir; koda gömülü liste birkaç ayda
eskir. Bu modül, kullanıcının KENDİ anahtarıyla sağlayıcının model uç noktasını
sorgular ve gerçek listeyi döndürür.

Tasarım kuralları:
  * Anahtar yalnızca istek başlığında kullanılır, hiçbir yere yazılmaz.
  * Uç nokta yoksa/yanıt vermezse hata verilmez: koddaki bilinen liste döner
    ve `source` alanı bunu açıkça söyler.
  * Sohbet için anlamsız modeller (gömme, görüntü, ses, moderasyon) elenir —
    kullanıcıya 200 satırlık gürültü değil, kullanılabilir liste gösterilir.
  * Liste eksikse kullanıcı model adını ELLE yazabilir; doğrulama yapılmaz,
    çünkü sağlayıcı yarın yeni bir model çıkarabilir.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from ..core.logging import get_logger
from .l3_llm_gateway import PROVIDERS

log = get_logger("zumvia.models")

TIMEOUT = 12.0

# Sohbet/araç kullanımı için uygun OLMAYAN model aileleri
_EXCLUDE = re.compile(
    r"(embed|embedding|rerank|whisper|tts|audio|speech|voice|"
    r"image|vision-only|dall-e|imagen|stable-diffusion|sdxl|flux|"
    r"moderation|guard|safety|clip|ocr|video|veo|sora|upscal)",
    re.I,
)


def _looks_usable(model_id: str) -> bool:
    return bool(model_id) and not _EXCLUDE.search(model_id)


def _openai_style(base_url: str, api_key: str, extra_headers: dict[str, str] | None = None
                  ) -> list[str]:
    """`GET /models` — OpenAI uyumlu sağlayıcıların ortak uç noktası."""
    headers = {"Authorization": f"Bearer {api_key}"}
    headers.update(extra_headers or {})
    url = base_url.rstrip("/") + "/models"
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()

    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []

    out = []
    for row in rows:
        model_id = row.get("id") or row.get("name") if isinstance(row, dict) else str(row)
        if isinstance(model_id, str):
            out.append(model_id.split("/")[-1] if model_id.startswith("models/") else model_id)
    return out


def _anthropic_style(base_url: str, api_key: str) -> list[str]:
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.get(base_url.rstrip("/") + "/models", headers=headers)
        response.raise_for_status()
        payload = response.json()
    return [row["id"] for row in payload.get("data", []) if isinstance(row, dict) and "id" in row]


def discover(provider_id: str, api_key: str, base_url: str = "") -> dict[str, Any]:
    """
    Sağlayıcının güncel model listesini döndürür.

    Dönen `source`:
      * "live"     -> sağlayıcıdan çekildi
      * "builtin"  -> uç nokta yanıt vermedi, koddaki liste kullanıldı
    """
    spec = PROVIDERS.get(provider_id)
    builtin = list(spec.models) if spec else []
    url = (base_url or (spec.base_url if spec else "")).strip()

    if not url or not api_key:
        return {"models": builtin, "source": "builtin",
                "note": "Anahtar veya adres yok; bilinen liste gösteriliyor."}

    try:
        if spec and spec.style == "anthropic":
            found = _anthropic_style(url, api_key)
        else:
            found = _openai_style(url, api_key)
    except httpx.HTTPStatusError as exc:
        log.info("model listesi alınamadı (%s): HTTP %s", provider_id, exc.response.status_code)
        return {"models": builtin, "source": "builtin",
                "note": f"Sağlayıcı model listesi vermedi (HTTP {exc.response.status_code}). "
                        f"Bilinen liste gösteriliyor; model adını elle de yazabilirsiniz."}
    except Exception as exc:  # noqa: BLE001 — keşif başarısızlığı kurulumu engellemez
        log.info("model listesi alınamadı (%s): %s", provider_id, exc)
        return {"models": builtin, "source": "builtin",
                "note": "Sağlayıcıya ulaşılamadı. Bilinen liste gösteriliyor; "
                        "model adını elle de yazabilirsiniz."}

    usable = sorted({m for m in found if _looks_usable(m)})
    if not usable:
        return {"models": builtin, "source": "builtin",
                "note": "Sağlayıcı sohbete uygun model döndürmedi; bilinen liste gösteriliyor."}

    # Koddaki bilinen modeller listede yoksa da kaybolmasın (yeni/eski sürüm farkı)
    merged = usable + [m for m in builtin if m not in usable]
    return {
        "models": merged,
        "source": "live",
        "fetched": len(usable),
        "note": f"{len(usable)} model sağlayıcıdan canlı alındı.",
    }
