"""
İSTEM ÇEVİRİSİ — kullanıcının yazdığı dil ile arayüz dili farklıysa
====================================================================

Kullanıcı arayüzü Arapça'ya almış ama internetten bulduğu İngilizce bir
prompt'u yapıştırmış olabilir. Bu durumda:

  * Metin, **anlamı bozulmadan** arayüz diline çevrilir,
  * Sohbete hem çeviri hem de orijinal metin kaydedilir (şeffaflık),
  * Ajan çeviriyi işler, kullanıcı ne gönderdiğini görmeye devam eder.

Kapalıysa hiçbir şey yapılmaz; metin olduğu gibi gider. Varsayılan AÇIK.

Neden LLM ile çeviri? Sözlük tabanlı çeviri finans terimlerini bozuyor
("stop" → "durak", "short" → "kısa"). Zaten bağlı olan modelden düşük
sıcaklıkla çeviri istemek hem doğru hem de ek maliyet getirmiyor.
"""
from __future__ import annotations

import re
from typing import Any

from ..core.logging import get_logger

log = get_logger("zumvia.translate")

MAX_CHARS = 4000

LANGUAGE_NAMES = {
    "tr": "Turkish", "en": "English", "de": "German", "fr": "French",
    "es": "Spanish", "pt": "Portuguese", "ru": "Russian",
    "ar": "Arabic", "zh": "Chinese (Simplified)",
}

# Yazı sistemine göre hızlı tespit — model çağrısı gerektirmez
_SCRIPTS = [
    ("ar", re.compile(r"[؀-ۿ]")),
    ("ru", re.compile(r"[Ѐ-ӿ]")),
    ("zh", re.compile(r"[一-鿿]")),
]

# Latin alfabesi kullanan diller için ayırt edici işaretler/kelimeler
_HINTS = {
    "tr": re.compile(r"[çğışöüÇĞİŞÖÜ]|\b(ve|için|bir|olarak|yönet|paramı|lütfen)\b", re.I),
    "de": re.compile(r"\b(und|nicht|bitte|mein|das|ist|werden|verwalte)\b|[äöüß]", re.I),
    "fr": re.compile(r"\b(et|pour|mon|avec|s'il|vous|gère|marché)\b|[àâçéèêôû]", re.I),
    "es": re.compile(r"\b(y|para|mi|con|por favor|gestiona|mercado)\b|[áéíóúñ¿¡]", re.I),
    "pt": re.compile(r"\b(e|para|meu|com|por favor|gere|mercado)\b|[ãõáéíóúç]", re.I),
    "en": re.compile(r"\b(the|and|for|my|with|please|manage|market|buy|sell)\b", re.I),
}


def detect(text: str) -> str:
    """
    Metnin dilini kabaca tespit eder.

    Kesin bir dil tanıma kütüphanesi değildir ve olmasına da gerek yoktur:
    tek soruyu cevaplar — "bu metin arayüz diliyle aynı mı?" Emin olamazsa
    boş döner ve çeviri yapılmaz (şüphede müdahale etme).
    """
    sample = (text or "").strip()
    if len(sample) < 12:
        return ""

    for code, pattern in _SCRIPTS:
        hits = len(pattern.findall(sample))
        if hits >= max(3, len(sample) * 0.08):
            return code

    scores = {code: len(pattern.findall(sample)) for code, pattern in _HINTS.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= 2 else ""


def needs_translation(text: str, target: str) -> bool:
    if target not in LANGUAGE_NAMES:
        return False
    source = detect(text)
    return bool(source) and source != target


def translate(gateway: Any, model: str, text: str, target: str) -> dict[str, Any]:  # noqa: ARG001
    """
    Metni hedef dile çevirir.

    `gateway`, zaten yapılandırılmış bir `ChatGateway` olmalıdır. Çeviri
    başarısız olursa ORİJİNAL metin döner — kullanıcı mesajı asla kaybolmaz.
    """
    target_name = LANGUAGE_NAMES.get(target, "English")
    source = detect(text) or "unknown"

    system = (
        "You are a precise translator for a financial trading application. "
        f"Translate the user's message into {target_name}. "
        "Rules: preserve the exact meaning and intent; keep numbers, tickers "
        "(BTC/USDT), and product names unchanged; keep trading terms accurate; "
        "do not answer, explain, or add anything. Output ONLY the translation."
    )

    try:
        turn = gateway.chat(
            [{"role": "user", "content": text[:MAX_CHARS]}],
            system=system,
            max_tokens=1200,
        )
        if not turn.ok:
            log.info("çeviri başarısız: %s", turn.error)
            return {"translated": False, "text": text, "source": source,
                    "error": turn.error[:200]}
        translated = (turn.text or "").strip()
    except Exception as exc:  # noqa: BLE001 — çeviri hatası mesajı düşürmez
        log.info("çeviri başarısız: %s", exc)
        return {"translated": False, "text": text, "source": source, "error": str(exc)[:200]}

    if not translated or translated.strip() == text.strip():
        return {"translated": False, "text": text, "source": source}

    return {
        "translated": True,
        "text": translated,
        "original": text,
        "source": source,
        "target": target,
    }
