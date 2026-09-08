"""
SAĞLAYICI HIZ SINIRLARI — kimsenin kapısını kırmadan çalışmak
==============================================================

Her sağlayıcının kendi kuralı var: NVIDIA NIM dakikada 40 istek, Gemini'nin
ücretsiz katmanı 15, Groq 30, bazıları da dakikalık token tavanı koyuyor.
Bu sınırları aşmak iki şey yapar:

  * 429 yer, istek boşa gider (ve ajan turu yarıda kalır),
  * Tekrarlanırsa hesap geçici olarak askıya alınır.

Bu modül, isteği **göndermeden önce** sıraya sokar. Kuyruk mantığı token
bucket'tır: her sağlayıcı için dakikada N jeton üretilir, istek bir jeton
harcar, jeton yoksa jeton üretilene kadar beklenir.

TASARIM KURALLARI
-----------------
* **Süreç geneli tekildir.** Aynı sağlayıcıya konseyden ve bot motorundan
  eş zamanlı gidilse bile tek sayaç geçerlidir (thread-safe).
* **Bilinmeyen sağlayıcı engellenmez.** Limiti bilinmiyorsa temkinli bir
  varsayılan uygulanır; sistem asla "bilmiyorum" diye durmaz.
* **429 gelirse öğrenir.** Sağlayıcı `Retry-After` verirse ona uyulur;
  vermezse üstel geri çekilme uygulanır.
* **Sonsuz beklemez.** Tavan süre aşılırsa istek reddedilir ve çağıran
  fail-safe'ine düşer (algoritmik motor devralır).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..core.logging import get_logger

log = get_logger("zumvia.ratelimit")

# Bir isteğin kuyrukta bekleyebileceği azami süre. Aşılırsa çağıran
# fail-safe'ine düşer; ticaret motoru asla kilitlenmez.
MAX_WAIT_SECONDS = 45.0

# Limiti bilinmeyen sağlayıcı için temkinli varsayılan
DEFAULT_RPM = 30


@dataclass(frozen=True, slots=True)
class ProviderLimit:
    """Bir sağlayıcının yayınlanmış hız sınırları."""

    rpm: int                      # dakikada istek
    tpm: int = 0                  # dakikada token (0 = sınır bilinmiyor/yok)
    concurrent: int = 0           # eş zamanlı istek (0 = sınırsız)
    note: str = ""


# Sağlayıcıların ücretsiz/başlangıç katmanlarındaki yayınlanmış sınırlar.
# Kullanıcının katmanı daha yüksekse ayarlardan artırılabilir; buradaki
# değerler "en kötü ihtimalle güvenli" olacak şekilde seçilmiştir.
PROVIDER_LIMITS: dict[str, ProviderLimit] = {
    "nvidia":      ProviderLimit(40, 0, 4, "NVIDIA NIM ücretsiz katman: 40 istek/dk"),
    "gemini":      ProviderLimit(15, 1_000_000, 4, "Google AI Studio ücretsiz: 15 istek/dk"),
    "groq":        ProviderLimit(30, 6_000, 4, "Groq ücretsiz katman"),
    "cerebras":    ProviderLimit(30, 60_000, 4, "Cerebras ücretsiz katman"),
    "sambanova":   ProviderLimit(20, 0, 3),
    "openrouter":  ProviderLimit(20, 0, 4, "OpenRouter ücretsiz modellerde 20/dk"),
    "huggingface": ProviderLimit(10, 0, 2, "HF Inference ücretsiz katman dar"),
    "mistral":     ProviderLimit(60, 0, 6),
    "deepseek":    ProviderLimit(60, 0, 6),
    "moonshot":    ProviderLimit(60, 0, 6),
    "qwen":        ProviderLimit(60, 0, 6),
    "zhipu":       ProviderLimit(60, 0, 6),
    "minimax":     ProviderLimit(60, 0, 6),
    "siliconflow": ProviderLimit(60, 0, 6),
    "together":    ProviderLimit(60, 0, 8),
    "fireworks":   ProviderLimit(60, 0, 8),
    "deepinfra":   ProviderLimit(60, 0, 8),
    "novita":      ProviderLimit(60, 0, 6),
    "nebius":      ProviderLimit(60, 0, 6),
    "perplexity":  ProviderLimit(50, 0, 4),
    "cohere":      ProviderLimit(20, 0, 4, "Cohere deneme anahtarı: 20/dk"),
    "anthropic":   ProviderLimit(50, 40_000, 8),
    "openai":      ProviderLimit(60, 60_000, 8),
    "xai":         ProviderLimit(60, 0, 8),
    "azure":       ProviderLimit(60, 0, 8),
    "bedrock":     ProviderLimit(60, 0, 8),
    "vertex":      ProviderLimit(60, 0, 8),
    "ibm":         ProviderLimit(30, 0, 4),
    "upstage":     ProviderLimit(30, 0, 4),
    "kluster":     ProviderLimit(30, 0, 4),
    "aimlapi":     ProviderLimit(30, 0, 4),
    "replicate":   ProviderLimit(30, 0, 4),
    "anyscale":    ProviderLimit(30, 0, 4),
    "octoai":      ProviderLimit(30, 0, 4),
    "stepfun":     ProviderLimit(30, 0, 4),
    "yi":          ProviderLimit(30, 0, 4),
    "baidu":       ProviderLimit(30, 0, 4),
    "tencent":     ProviderLimit(30, 0, 4),
    "bytedance":   ProviderLimit(30, 0, 4),
    # Yerel sunucular: ağ sınırı yok, yalnızca donanım. Eş zamanlılık düşük.
    "ollama":      ProviderLimit(10_000, 0, 2, "Yerel — sınır donanımınız"),
    "vllm":        ProviderLimit(10_000, 0, 4, "Yerel — sınır donanımınız"),
    "lmstudio":    ProviderLimit(10_000, 0, 2, "Yerel — sınır donanımınız"),
    "custom":      ProviderLimit(DEFAULT_RPM, 0, 4),
}


@dataclass
class _Bucket:
    """Tek bir sağlayıcının sayaçları."""

    limit: ProviderLimit
    requests: deque = field(default_factory=deque)     # son 60 sn'deki istek anları
    tokens: deque = field(default_factory=deque)       # (an, token) çiftleri
    active: int = 0                                    # şu an uçuşta olan istek
    blocked_until: float = 0.0                         # 429 sonrası bekleme
    lock: threading.Condition = field(default_factory=threading.Condition)

    # ÖĞRENİLEN sınırlar. Tablodaki değerler "sağlayıcının söylediği"dir;
    # bunlar "sağlayıcının gerçekte kabul ettiği"dir. 429 geldikçe daralır,
    # kesintisiz başarı sürdükçe yavaşça gevşer.
    learned_rpm: int = 0                               # 0 = henüz öğrenilmedi
    learned_concurrent: int = 0
    strikes: int = 0                                   # üst üste 429 sayısı
    clean_streak: int = 0                              # 429'suz başarılı istek

    # istatistik
    served: int = 0
    waited_seconds: float = 0.0
    throttled: int = 0
    rejected: int = 0


_buckets: dict[str, _Bucket] = {}
_registry_lock = threading.Lock()


def _bucket(provider: str) -> _Bucket:
    key = (provider or "custom").lower()
    with _registry_lock:
        found = _buckets.get(key)
        if found is None:
            found = _Bucket(PROVIDER_LIMITS.get(key, ProviderLimit(DEFAULT_RPM)))
            _buckets[key] = found
        return found


def _prune(bucket: _Bucket, now: float) -> None:
    while bucket.requests and now - bucket.requests[0] >= 60.0:
        bucket.requests.popleft()
    while bucket.tokens and now - bucket.tokens[0][0] >= 60.0:
        bucket.tokens.popleft()


#  Sağlayıcının gerçek davranışı, belgesindeki sayıdan daha güvenilirdir.
#  Bu iki eşik 429 sonrası daralmayı ve sonrasındaki gevşemeyi yönetir.
MIN_LEARNED_RPM = 4              # bunun altına inilmez: sistem tümden durmasın
CLEAN_STREAK_TO_RELAX = 25       # kaç temiz istekten sonra bir kademe gevşesin


def _effective_rpm(bucket: _Bucket) -> int:
    """Tablodaki ve öğrenilen sınırdan SIKI olanı geçerlidir."""
    table = bucket.limit.rpm
    learned = bucket.learned_rpm
    if not learned:
        return table
    return min(table, learned) if table else learned


def _effective_concurrent(bucket: _Bucket) -> int:
    table = bucket.limit.concurrent
    learned = bucket.learned_concurrent
    if not learned:
        return table
    return min(table, learned) if table else learned


def _wait_needed(bucket: _Bucket, now: float, est_tokens: int) -> float:
    """Bu isteğin geçebilmesi için kaç saniye beklenmeli?"""
    if now < bucket.blocked_until:
        return bucket.blocked_until - now

    limit = bucket.limit
    waits = [0.0]

    rpm = _effective_rpm(bucket)
    if rpm and len(bucket.requests) >= rpm:
        waits.append(60.0 - (now - bucket.requests[0]))

    if limit.tpm and est_tokens > 0:
        used = sum(t for _, t in bucket.tokens)
        if used + est_tokens > limit.tpm and bucket.tokens:
            waits.append(60.0 - (now - bucket.tokens[0][0]))

    concurrent = _effective_concurrent(bucket)
    if concurrent and bucket.active >= concurrent:
        waits.append(0.25)                       # kısa nabız; slot boşalınca uyanır

    return max(waits)


class Slot:
    """
    Bir istek için ayrılmış geçiş hakkı.

    `with acquire("nvidia") as slot:` şeklinde kullanılır. Blok bitince slot
    otomatik serbest bırakılır; istisna fırlasa bile sayaç sızmaz.
    """

    def __init__(self, bucket: _Bucket, provider: str, waited: float) -> None:
        self._bucket = bucket
        self.provider = provider
        self.waited = waited

    def record_tokens(self, count: int) -> None:
        """Yanıt geldikten sonra gerçek token tüketimini bildirir."""
        if count <= 0 or not self._bucket.limit.tpm:
            return
        with self._bucket.lock:
            self._bucket.tokens.append((time.monotonic(), count))

    def penalize(self, retry_after: float | None = None) -> None:
        """
        429 alındı: beklenir VE sağlayıcının gerçek tavanı öğrenilir.

        Yalnızca beklemek yetmez — aynı hızla devam edilirse bir sonraki
        istek de 429 alır ve görev ölür. Bu yüzden her redde eş zamanlılık
        ve dakikalık tavan bir kademe daraltılır. Daralma kalıcı değildir:
        kesintisiz başarı sürdükçe kademe kademe geri açılır.
        """
        bucket = self._bucket
        with bucket.lock:
            wait = retry_after if retry_after and retry_after > 0 else 5.0
            # Üst üste redlerde bekleme üstel büyür (5, 10, 20, 40 … ≤120 sn).
            bucket.strikes += 1
            bucket.clean_streak = 0
            if retry_after is None:
                wait = min(5.0 * (2 ** (bucket.strikes - 1)), 120.0)

            bucket.blocked_until = max(bucket.blocked_until,
                                       time.monotonic() + min(wait, 120.0))
            bucket.throttled += 1

            # Gözlemlenen tavanı daralt.
            observed_concurrent = max(1, bucket.active)
            current_concurrent = _effective_concurrent(bucket) or observed_concurrent
            bucket.learned_concurrent = max(1, min(current_concurrent - 1,
                                                   observed_concurrent))

            # Her redde tavan %30 daraltılır. Ayrıca bu dakikada gerçekten
            # ÇOK istek geçtiyse (gözlem anlamlıysa) o sayı da tavan kabul
            # edilir. Tek bir istekten sonra gelen 429 ise dakikalık tavanın
            # düşük olduğunu KANITLAMAZ — bu genelde eş zamanlılık ya da
            # ani yığılma sınırıdır, onu ayrıca daralttık. Gözlemi burada
            # koşulsuz tavan saymak, sistemi tek redde dakikada 4 isteğe
            # düşürürdü.
            observed_rpm = len(bucket.requests)
            current_rpm = _effective_rpm(bucket) or DEFAULT_RPM
            target = int(current_rpm * 0.7)
            if observed_rpm >= MIN_LEARNED_RPM:
                target = min(target, observed_rpm)
            bucket.learned_rpm = max(MIN_LEARNED_RPM, target)

            bucket.lock.notify_all()

        log.warning("%s hız sınırı (429): %.1f sn bekleniyor. Öğrenilen tavan: "
                    "%s istek/dk, %s eş zamanlı.", self.provider, wait,
                    bucket.learned_rpm, bucket.learned_concurrent)

    def succeed(self) -> None:
        """
        İstek 429 almadan tamamlandı.

        Uzun süre temiz gidiliyorsa öğrenilen tavan bir kademe gevşetilir;
        aksi hâlde tek bir geçici red, sistemi kalıcı olarak yavaşlatırdı.
        """
        bucket = self._bucket
        with bucket.lock:
            bucket.strikes = 0
            bucket.clean_streak += 1
            if (bucket.learned_rpm
                    and bucket.clean_streak >= CLEAN_STREAK_TO_RELAX):
                bucket.clean_streak = 0
                bucket.learned_rpm = int(bucket.learned_rpm * 1.25) + 1
                if bucket.learned_concurrent:
                    bucket.learned_concurrent += 1
                table_rpm = bucket.limit.rpm
                if table_rpm and bucket.learned_rpm >= table_rpm:
                    bucket.learned_rpm = 0        # tabloya döndük, öğrenmeyi bırak
                    bucket.learned_concurrent = 0
                log.info("%s tavanı gevşetildi: %s istek/dk", self.provider,
                         bucket.learned_rpm or bucket.limit.rpm)

    def __enter__(self) -> Slot:
        return self

    def __exit__(self, *exc: Any) -> bool:
        with self._bucket.lock:
            self._bucket.active = max(0, self._bucket.active - 1)
            self._bucket.lock.notify_all()
        return False


class RateLimitTimeout(RuntimeError):
    """Kuyrukta çok uzun beklendi; çağıran fail-safe'ine düşmeli."""


def acquire(provider: str, *, est_tokens: int = 0,
            max_wait: float = MAX_WAIT_SECONDS) -> Slot:
    """
    Sağlayıcıya istek göndermek için sıraya girer.

    Sınır doluysa uyur; `max_wait` aşılırsa `RateLimitTimeout` fırlatır.
    Beklemek, 429 yiyip isteği kaybetmekten her zaman daha ucuzdur.
    """
    bucket = _bucket(provider)
    deadline = time.monotonic() + max_wait
    waited_total = 0.0

    with bucket.lock:
        while True:
            now = time.monotonic()
            _prune(bucket, now)
            wait = _wait_needed(bucket, now, est_tokens)

            if wait <= 0:
                bucket.requests.append(now)
                bucket.active += 1
                bucket.served += 1
                bucket.waited_seconds += waited_total
                return Slot(bucket, provider, waited_total)

            if now + wait > deadline:
                bucket.rejected += 1
                raise RateLimitTimeout(
                    f"{provider}: hız sınırı kuyruğu {max_wait:.0f} sn içinde açılmadı "
                    f"(dakikada {bucket.limit.rpm} istek sınırı). "
                    f"Sistem algoritmik motorla devam ediyor."
                )

            waited_total += wait
            bucket.lock.wait(timeout=min(wait, 1.0))


def snapshot() -> dict[str, Any]:
    """Arayüz ve tanılama için sayaç fotoğrafı."""
    now = time.monotonic()
    rows = {}
    with _registry_lock:
        items = list(_buckets.items())

    for provider, bucket in items:
        with bucket.lock:
            _prune(bucket, now)
            rows[provider] = {
                "limit_rpm": bucket.limit.rpm,
                # Sağlayıcının belgesi değil, gözlemlenen gerçek tavan:
                "effective_rpm": _effective_rpm(bucket),
                "effective_concurrent": _effective_concurrent(bucket),
                "learned": bool(bucket.learned_rpm or bucket.learned_concurrent),
                "used_last_minute": len(bucket.requests),
                "active": bucket.active,
                "served": bucket.served,
                "throttled": bucket.throttled,
                "rejected": bucket.rejected,
                "avg_wait_ms": round(
                    bucket.waited_seconds / bucket.served * 1000, 1
                ) if bucket.served else 0.0,
                "cooling_down": max(0.0, round(bucket.blocked_until - now, 1)),
                "note": bucket.limit.note,
            }
    return rows


def configure(provider: str, *, rpm: int | None = None, tpm: int | None = None,
              concurrent: int | None = None) -> dict[str, Any]:
    """
    Kullanıcının katmanı daha yüksekse sınırı yükseltir (ya da düşürür).

    Ücretli katmanlarda sağlayıcılar sınırı 10 kat artırabiliyor; kullanıcı
    parasını verdiği kapasiteyi kullanabilmelidir.
    """
    key = (provider or "custom").lower()
    bucket = _bucket(key)
    current = bucket.limit
    updated = ProviderLimit(
        rpm=max(1, rpm if rpm is not None else current.rpm),
        tpm=max(0, tpm if tpm is not None else current.tpm),
        concurrent=max(0, concurrent if concurrent is not None else current.concurrent),
        note=current.note,
    )
    with bucket.lock:
        bucket.limit = updated
        bucket.lock.notify_all()
    with _registry_lock:
        PROVIDER_LIMITS[key] = updated
    log.info("%s hız sınırı güncellendi: %s istek/dk", key, updated.rpm)
    return {"provider": key, "rpm": updated.rpm, "tpm": updated.tpm,
            "concurrent": updated.concurrent}
