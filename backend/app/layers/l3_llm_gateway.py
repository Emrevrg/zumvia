"""
KATMAN 3 — EVRENSEL YAPAY ZEKA AĞ GEÇİDİ (Universal LLM Gateway)
=================================================================
Hangi modeli takarsanız takın, sistem onu **profesyonel bir niceliksel fon
yöneticisi** gibi çalıştırır. Model değişse de davranış sözleşmesi değişmez.

Desteklenen sağlayıcılar (hepsi OpenAI `/chat/completions` şemasıyla konuşur):
  gemini · deepseek · qwen · anthropic · openai · groq · openrouter
  ollama (yerel) · vllm (yerel) · custom (herhangi bir OpenAI uyumlu uç nokta)

Sözleşme:
  1. Modele YALNIZCA Katman 2'nin ürettiği kesin sayılar verilir.
  2. Modelden hesap yapması İSTENMEZ; yorum ve senteze zorlanır.
  3. Yanıt Pydantic şeması ile doğrulanır. Şema tutmazsa → işlem İPTAL (fail-safe).
  4. Modelin çıktısı tavsiyedir; nihai yetki Katman 4 (Risk Kalkanı) koddadır.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from ..core.logging import get_logger
from . import rate_limit

log = get_logger("zumvia.layer3")


# --------------------------------------------------------------------------- #
#  Sağlayıcı kataloğu
# --------------------------------------------------------------------------- #



def _retry_after(response: Any) -> float | None:
    """Sağlayıcı `Retry-After` verdiyse ona uyulur (saniye)."""
    raw = response.headers.get("retry-after") if hasattr(response, "headers") else None
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _used_tokens(data: dict[str, Any]) -> int:
    """Yanıttaki gerçek token tüketimi (varsa)."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return 0
    for key in ("total_tokens", "total_token_count"):
        if isinstance(usage.get(key), int):
            return usage[key]
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    try:
        return int(prompt) + int(completion)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class Provider:
    id: str
    label: str
    base_url: str
    models: tuple[str, ...]
    needs_key: bool = True
    note: str = ""
    style: Literal["openai", "anthropic"] = "openai"


PROVIDERS: dict[str, Provider] = {
    # --- Ücretsiz / düşük maliyetli başlangıç ---
    "gemini": Provider(
        id="gemini",
        label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        models=(
            "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro",
            "gemini-2.0-flash", "gemini-3-flash-preview",
        ),
        note="Google AI Studio ücretsiz kotası ile aylık ~$0 maliyet. Varsayılan öneri.",
    ),
    "deepseek": Provider(
        id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        models=("deepseek-chat", "deepseek-reasoner", "deepseek-v3.1"),
        note="Derin muhakeme (R1) — fiyat/performans lideri.",
    ),
    "qwen": Provider(
        id="qwen",
        label="Qwen (Alibaba DashScope)",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        models=("qwen-max", "qwen-plus", "qwen-turbo", "qwen2.5-72b-instruct", "qwen3-30b-a3b"),
    ),
    # --- Kimi / Moonshot: kullanıcının sorduğu K3 ailesi ---
    "moonshot": Provider(
        id="moonshot",
        label="Moonshot / Kimi (K2 · K3)",
        base_url="https://api.moonshot.ai/v1",
        models=(
            "kimi-k2-thinking", "kimi-k2-turbo", "kimi-k2-0711-preview",
            "kimi-k3-preview", "moonshot-v1-128k", "moonshot-v1-32k",
        ),
        note="Kimi K3 önizleme dahil — güçlü uzun bağlam, finans dokümanlarında başarılı.",
    ),
    # --- Büyük ABD sağlayıcıları ---
    "anthropic": Provider(
        id="anthropic",
        label="Anthropic Claude",
        base_url="https://api.anthropic.com/v1",
        models=(
            "claude-sonnet-4-5", "claude-opus-4-1",
            "claude-3-7-sonnet-latest", "claude-3-5-haiku-latest",
        ),
        note="Makro ve bilanço analizinde en güçlü muhakeme.",
        style="anthropic",
    ),
    "openai": Provider(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        models=("gpt-4o", "gpt-4o-mini", "o3-mini", "o4-mini", "gpt-4.1", "gpt-4.1-mini"),
    ),
    "xai": Provider(
        id="xai",
        label="xAI Grok",
        base_url="https://api.x.ai/v1",
        models=("grok-3", "grok-3-mini", "grok-2-1212"),
        note="Haber akışı + gerçek zamanlı bilgiye güçlü erişim.",
    ),
    # --- Hız odaklı / çıkarım çiftlikleri ---
    "groq": Provider(
        id="groq",
        label="Groq (ultra hızlı)",
        base_url="https://api.groq.com/openai/v1",
        models=("llama-3.3-70b-versatile", "deepseek-r1-distill-llama-70b", "qwen-2.5-32b"),
    ),
    "together": Provider(
        id="together",
        label="Together AI",
        base_url="https://api.together.xyz/v1",
        models=("meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8", "deepseek-ai/DeepSeek-R1", "Qwen/Qwen2.5-72B-Instruct"),
    ),
    "fireworks": Provider(
        id="fireworks",
        label="Fireworks AI",
        base_url="https://api.fireworks.ai/inference/v1",
        models=("accounts/fireworks/models/llama4-maverick-instruct-basic", "accounts/fireworks/models/deepseek-r1"),
    ),
    "mistral": Provider(
        id="mistral",
        label="Mistral AI",
        base_url="https://api.mistral.ai/v1",
        models=("mistral-large-latest", "mistral-small-latest", "codestral-latest", "mistral-nemo"),
    ),
    "perplexity": Provider(
        id="perplexity",
        label="Perplexity",
        base_url="https://api.perplexity.ai",
        models=("sonar-pro", "sonar-reasoning-pro", "sonar"),
        note="Arama destekli modeller — haber doğrulamada güçlü.",
    ),
    "cohere": Provider(
        id="cohere",
        label="Cohere",
        base_url="https://api.cohere.ai/compatibility/v1",
        models=("command-r-plus-08-2024", "command-r-08-2024", "command-a-03-2025"),
    ),
    # --- Toplayıcılar ---
    "openrouter": Provider(
        id="openrouter",
        label="OpenRouter (400+ model)",
        base_url="https://openrouter.ai/api/v1",
        models=(
            "deepseek/deepseek-chat", "google/gemini-2.5-flash",
            "qwen/qwen-2.5-72b-instruct", "moonshotai/kimi-k2",
            "anthropic/claude-sonnet-4", "openai/gpt-4o",
        ),
        note="Tek anahtarla yüzlerce model — asıl dayanıklılık katmanı.",
    ),
    "nebius": Provider(
        id="nebius",
        label="Nebius AI Studio",
        base_url="https://api.studio.nebius.com/v1",
        models=("meta-llama/Meta-Llama-3.1-70B-Instruct", "Qwen/Qwen2.5-72B-Instruct"),
    ),
    # --- Çin / alternatif ---
    "zhipu": Provider(
        id="zhipu",
        label="Zhipu AI / GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        models=("glm-4-plus", "glm-4-flash", "glm-4-air", "glm-4.5", "glm-4.5-air"),
    ),
    "minimax": Provider(
        id="minimax",
        label="MiniMax",
        base_url="https://api.minimax.chat/v1",
        models=("MiniMax-Text-01", "abab6.5s-chat", "minimax-m2"),
    ),
    "siliconflow": Provider(
        id="siliconflow",
        label="SiliconFlow (Çin hızlı)",
        base_url="https://api.siliconflow.cn/v1",
        models=("deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct", "moonshotai/Kimi-K2-Instruct", "THUDM/glm-4-9b-chat"),
        note="Çin içi ultra ucuz çıkarım.",
    ),
    "baidu": Provider(
        id="baidu",
        label="Baidu Qianfan",
        base_url="https://qianfan.baidubce.com/v2",
        models=("ernie-4.0-8k", "ernie-3.5-8k", "ernie-speed-8k"),
    ),
    "tencent": Provider(
        id="tencent",
        label="Tencent Hunyuan",
        base_url="https://api.hunyuan.cloud.tencent.com/v1",
        models=("hunyuan-turbos-latest", "hunyuan-large", "hunyuan-standard"),
    ),
    "bytedance": Provider(
        id="bytedance",
        label="ByteDance Doubao / Volcengine",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        models=("doubao-seed-1.6-thinking", "doubao-1.5-pro-32k", "deepseek-v3-250324"),
    ),
    "stepfun": Provider(
        id="stepfun",
        label="StepFun",
        base_url="https://api.stepfun.com/v1",
        models=("step-2-16k", "step-1-8k", "step-1o-turbo"),
    ),
    "yi": Provider(
        id="yi",
        label="01.AI Yi",
        base_url="https://api.lingyiwanwu.com/v1",
        models=("yi-large", "yi-medium", "yi-spark"),
    ),
    # --- Küresel çıkarım ağı ---
    "azure": Provider(
        id="azure",
        label="Azure OpenAI",
        base_url="https://YOUR_RESOURCE.openai.azure.com/openai/deployments/YOUR_DEPLOYMENT",
        models=("gpt-4o", "gpt-4o-mini", "o3-mini"),
        note="base_url’i Azure portalından kopyalayın.",
    ),
    "bedrock": Provider(
        id="bedrock",
        label="AWS Bedrock (Claude/GPT via proxy)",
        base_url="https://bedrock-runtime.REGION.amazonaws.com",
        models=("anthropic.claude-3-5-sonnet-20241022-v2:0", "openai.gpt-4o"),
        note="Bedrock OpenAI-proxy üzerinden.",
    ),
    "vertex": Provider(
        id="vertex",
        label="Google Vertex AI",
        base_url="https://LOCATION-aiplatform.googleapis.com/v1/projects/PROJECT/locations/LOCATION",
        models=("gemini-2.5-pro", "gemini-2.5-flash", "claude-sonnet-4@vertex"),
        note="GCP Vertex endpoint’inizi girin.",
    ),
    "nvidia": Provider(
        id="nvidia",
        label="NVIDIA NIM",
        base_url="https://integrate.api.nvidia.com/v1",
        # NOT: Bu liste yalnızca BAŞLANGIÇ önerisidir. NVIDIA modelleri sık
        # emekliye ayırıyor (410 Gone) ve listede görünen her model her hesapta
        # çağrılamıyor. Gerçek liste kullanıcının anahtarıyla canlı çekilir
        # (bkz. layers/model_discovery.py) ve model_errors.working_models()
        # hangilerinin GERÇEKTEN çağrılabildiğini sınar.
        # 2026-08-26'da kullanıcının anahtarıyla 69 modelin tamamı tek tek
        # çağrılarak doğrulandı: 14'ü yanıt verdi, 12'si araç çağırabildi.
        # Aşağıdaki liste yalnızca ARAÇ ÇAĞIRABİLENLERDEN oluşur — finans
        # ajanı araç çağıramayan bir modelle iş yapamaz.
        models=("nvidia/nemotron-3-super-120b-a12b", "moonshotai/kimi-k3",
                "minimaxai/minimax-m3", "nvidia/nemotron-3.5-lightning-30b-a3b",
                "nvidia/nemotron-3-nano-30b-a3b", "openai/gpt-oss-20b",
                "stepfun-ai/step-3.7-flash", "meta/muse-glimmer-30b"),
        note="NVIDIA API Catalog — GPU hızlandırmalı. Model listesi sık değişir; "
             "'Test et' ile doğrulayın.",
    ),
    "huggingface": Provider(
        id="huggingface",
        label="HuggingFace Inference",
        base_url="https://api-inference.huggingface.co/v1",
        models=("meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"),
    ),
    "replicate": Provider(
        id="replicate",
        label="Replicate",
        base_url="https://api.replicate.com/v1/openai",
        models=("meta/meta-llama-3-70b-instruct", "deepseek-ai/deepseek-r1"),
    ),
    "anyscale": Provider(
        id="anyscale",
        label="Anyscale Endpoints",
        base_url="https://api.endpoints.anyscale.com/v1",
        models=("meta-llama/Llama-3.3-70B-Instruct", "mistralai/Mistral-7B-Instruct-v0.3"),
    ),
    "deepinfra": Provider(
        id="deepinfra",
        label="DeepInfra",
        base_url="https://api.deepinfra.com/v1/openai",
        models=("meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-R1"),
    ),
    "octoai": Provider(
        id="octoai",
        label="OctoAI",
        base_url="https://text.octoai.run/v1",
        models=("meta-llama-3.1-405b-instruct", "deepseek-ai/deepseek-r1"),
    ),
    "novita": Provider(
        id="novita",
        label="Novita AI",
        base_url="https://api.novita.ai/v3/openai",
        models=("meta-llama/llama-3.3-70b-instruct", "deepseek/deepseek-r1-distill-llama-70b"),
    ),
    "aimlapi": Provider(
        id="aimlapi",
        label="AI/ML API",
        base_url="https://api.aimlapi.com/v1",
        models=("openai/gpt-4o", "anthropic/claude-3.5-sonnet", "deepseek/deepseek-chat", "x-ai/grok-2"),
        note="100+ model toplayıcı.",
    ),
    "upstage": Provider(
        id="upstage",
        label="Upstage Solar",
        base_url="https://api.upstage.ai/v1/solar",
        models=("solar-pro", "solar-mini"),
    ),
    "kluster": Provider(
        id="kluster",
        label="Kluster.ai",
        base_url="https://api.kluster.ai/v1",
        models=("kluster/llama-3.3-70b", "kluster/deepseek-r1"),
    ),
    "sambanova": Provider(
        id="sambanova",
        label="SambaNova Cloud",
        base_url="https://api.sambanova.ai/v1",
        models=("Meta-Llama-3.3-70B-Instruct", "DeepSeek-R1"),
    ),
    "ibm": Provider(
        id="ibm",
        label="IBM watsonx.ai",
        base_url="https://us-south.ml.cloud.ibm.com/ml/v1",
        models=("ibm/granite-13b-chat-v2", "meta-llama/llama-3-70b-instruct"),
    ),
    "cerebras": Provider(
        id="cerebras",
        label="Cerebras",
        base_url="https://api.cerebras.ai/v1",
        models=("llama3.1-70b", "llama-3.3-70b"),
        note="Wafer-Scale ultra hızlı.",
    ),
    # --- Yerel ---
    "ollama": Provider(
        id="ollama",
        label="Ollama (yerel · ücretsiz)",
        base_url="http://localhost:11434/v1",
        models=("qwen2.5:14b", "llama3.1:8b", "deepseek-r1:14b", "kimi-k2:14b"),
        needs_key=False,
        note="Kendi bilgisayarınızda çalışır, veri dışarı çıkmaz, maliyet $0.",
    ),
    "vllm": Provider(
        id="vllm",
        label="vLLM / LM Studio (yerel)",
        base_url="http://localhost:8000/v1",
        models=("local-model",),
        needs_key=False,
    ),
    "lmstudio": Provider(
        id="lmstudio",
        label="LM Studio (yerel)",
        base_url="http://localhost:1234/v1",
        models=("local-model",),
        needs_key=False,
    ),
    "custom": Provider(
        id="custom",
        label="Özel OpenAI-uyumlu uç nokta",
        base_url="",
        models=(),
        needs_key=False,
        note="base_url + model adını kendiniz girersiniz (Azure, Bedrock-gateway, vb.).",
    ),
}


def provider_catalog() -> list[dict[str, Any]]:
    """Arayüzün model seçici bileşenine gönderilen katalog."""
    return [
        {
            "id": p.id, "label": p.label, "base_url": p.base_url,
            "models": list(p.models), "needs_key": p.needs_key, "note": p.note,
            "style": p.style,
        }
        for p in PROVIDERS.values()
    ]


# --------------------------------------------------------------------------- #
#  Provider sağlık izleme — dayanıklılık katmanı
#  Thread-safe bellek içi devre kesici: 3 ardışık hata → 5 dk soğuma.
# --------------------------------------------------------------------------- #

import threading as _th

_PROVIDER_HEALTH: dict[str, dict[str, Any]] = {}
_HEALTH_LOCK = _th.Lock()
_HEALTH_FAIL_THRESHOLD = 3
_HEALTH_COOLDOWN_S = 300  # 5 dk


def _mark_success(provider_id: str, latency_ms: int) -> None:
    with _HEALTH_LOCK:
        h = _PROVIDER_HEALTH.setdefault(provider_id, {"fails": 0, "ok": 0, "last_latency": 0, "cooldown_until": 0.0})
        h["fails"] = 0
        h["ok"] += 1
        h["last_latency"] = latency_ms


def _mark_failure(provider_id: str) -> None:
    import time as _t
    with _HEALTH_LOCK:
        h = _PROVIDER_HEALTH.setdefault(provider_id, {"fails": 0, "ok": 0, "last_latency": 0, "cooldown_until": 0.0})
        h["fails"] += 1
        if h["fails"] >= _HEALTH_FAIL_THRESHOLD:
            h["cooldown_until"] = _t.time() + _HEALTH_COOLDOWN_S


def is_provider_healthy(provider_id: str) -> bool:
    import time as _t
    with _HEALTH_LOCK:
        h = _PROVIDER_HEALTH.get(provider_id)
        if not h:
            return True
        return _t.time() >= float(h.get("cooldown_until", 0))


def provider_health_snapshot() -> dict[str, dict[str, Any]]:
    import time as _t
    now = _t.time()
    with _HEALTH_LOCK:
        return {
            pid: {
                "healthy": now >= float(v.get("cooldown_until", 0)),
                "fails": v["fails"],
                "ok": v["ok"],
                "last_latency": v["last_latency"],
                "cooldown_secs_left": max(0, int(float(v.get("cooldown_until", 0)) - now)),
            }
            for pid, v in _PROVIDER_HEALTH.items()
        }


# --------------------------------------------------------------------------- #
#  Katı yanıt şeması (Pydantic v2)
# --------------------------------------------------------------------------- #


class TradeDecision(BaseModel):
    """Modelin dönmek ZORUNDA olduğu yapı. Uymayan yanıt çöpe atılır."""

    model_config = {"extra": "ignore"}

    action: Literal["BUY", "SELL", "WAIT", "CLOSE"] = Field(
        description="BUY=long aç, SELL=short aç, CLOSE=açık pozisyonu kapat, WAIT=bekle"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    stop_loss: float = Field(default=0.0, ge=0.0)
    take_profit: float = Field(default=0.0, ge=0.0)
    reasoning: str = Field(default="", max_length=1200)
    risk_note: str = Field(default="", max_length=400)

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip().upper()
            return {"AL": "BUY", "LONG": "BUY", "SAT": "SELL", "SHORT": "SELL",
                    "BEKLE": "WAIT", "HOLD": "WAIT", "NONE": "WAIT",
                    "KAPAT": "CLOSE", "EXIT": "CLOSE"}.get(v, v)
        return v

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_conf(cls, v: Any) -> Any:
        # "%85" veya 85 gibi girdileri 0-1 aralığına indirger
        if isinstance(v, str):
            v = v.replace("%", "").strip()
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return f / 100.0 if f > 1.0 else f

    @field_validator("stop_loss", "take_profit", mode="before")
    @classmethod
    def _normalize_price(cls, v: Any) -> Any:
        if v in (None, "", "null"):
            return 0.0
        if isinstance(v, str):
            v = re.sub(r"[^0-9eE\.\-\+]", "", v) or "0"
        try:
            return abs(float(v))
        except (TypeError, ValueError):
            return 0.0

    @model_validator(mode="after")
    def _entry_needs_levels(self) -> TradeDecision:
        """Giriş kararında SL/TP zorunludur — yoksa şema reddedilir."""
        if self.action in ("BUY", "SELL") and (self.stop_loss <= 0 or self.take_profit <= 0):
            raise ValueError("BUY/SELL kararı stop_loss ve take_profit içermek zorundadır.")
        return self


@dataclass(slots=True)
class LLMResult:
    ok: bool
    decision: TradeDecision | None
    raw_text: str
    error: str = ""
    latency_ms: int = 0
    model: str = ""
    provider: str = ""
    usage: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
#  Persona — "Profesyonel Fon Yöneticisi" sistem talimatı
# --------------------------------------------------------------------------- #

PERSONA = """Sen ZUMVIA platformunun kıdemli niceliksel portföy yöneticisisin.
15 yıllık kurumsal hedge fon deneyimin var. Görevin: yönetimindeki sermayeyi
KORUYARAK ve DİSİPLİNLİ şekilde BÜYÜTMEK.

MESLEKİ İLKELERİN (ihlal edilemez):
1. Önce sermayeyi koru, sonra büyüt. Kaybetmediğin para, kazanman gereken paradır.
2. İşlem yapmamak da bir pozisyondur. Kurulum net değilse WAIT dersin.
   Piyasada her gün fırsat yoktur; zorlama işlem hesabı eritir.
3. Asla intikam işlemi (revenge trade) yapmazsın. Zarar sonrası telafi,
   daha büyük risk almakla değil, DAHA SEÇİCİ olmakla sağlanır.
4. Trendin yönünde işlem yaparsın. Düşen bıçağı yakalamaya çalışmazsın.
5. Her işlemde asimetri ararsın: kazanç potansiyeli, riskin en az 2 katı olmalı.
6. Volatiliteye saygı duyarsın. Stop-loss seviyesini ATR'ye göre belirlersin;
   fiyata çok yakın stop = gürültüye takılıp boşuna zarar.

SANA VERİLEN VERİ:
Tüm göstergeler (RSI, EMA, MACD, ATR, Supertrend, ADX, Bollinger, destek/direnç)
Python ile SIFIR HATA ile hesaplanmıştır. Bu sayılar kesindir.

MATEMATİK YASAĞI:
Kendi başına aritmetik yapma, yüzde hesaplama veya gösterge tahmin etme.
Sadece sana verilen sayıları YORUMLA ve piyasa yapısını SENTEZLE.
Stop-loss ve take-profit seviyelerini verilen ATR ve destek/direnç
seviyelerinden seç — uydurma.

ÇIKTI KURALI:
Yanıtın SADECE ve SADECE geçerli bir JSON nesnesi olacak. Açıklama, markdown,
kod bloğu işareti veya ek metin YOK.

{
  "action": "BUY" | "SELL" | "WAIT" | "CLOSE",
  "confidence": 0.0 ile 1.0 arası ondalık,
  "stop_loss": fiyat (sayı),
  "take_profit": fiyat (sayı),
  "reasoning": "kısa ve net Türkçe gerekçe (en fazla 3 cümle)",
  "risk_note": "bu işlemin ana riski (tek cümle)"
}

Kararsızsan action="WAIT", confidence düşük ver. Emin olmadığın bir işlemde
yüksek güven vermek meslek etiğine aykırıdır."""


RECOVERY_ADDENDUM = """
!! TOPARLANMA MODU AKTİF !!
Hesap son dönemde zarardadır. Bu modda:
- Sadece A+ kalitede, çok net kurulumları kabul edersin.
- Trend yönüne TERS hiçbir işlem açmazsın.
- Şüphen varsa kesinlikle WAIT dersin; sabır en yüksek getirili stratejidir.
- Kaybı hızlı kapatmak için risk artırmak KESİNLİKLE yasaktır."""


def build_system_prompt(recovery_mode: bool = False, extra_rules: str = "") -> str:
    prompt = PERSONA
    if recovery_mode:
        prompt += "\n" + RECOVERY_ADDENDUM
    if extra_rules.strip():
        prompt += f"\n\nKULLANICI STRATEJİ NOTLARI (bunlara da uy):\n{extra_rules.strip()[:1500]}"
    return prompt


def build_user_prompt(snapshot: dict[str, Any], context: dict[str, Any]) -> str:
    """Katman 2 çıktısı + hesap bağlamı → modele giden istem."""
    return (
        "AŞAĞIDAKİ VERİLER PYTHON İLE KESİN HESAPLANMIŞTIR.\n\n"
        f"=== PİYASA FOTOĞRAFI ===\n{json.dumps(snapshot, ensure_ascii=False, indent=1)}\n\n"
        f"=== HESAP VE RİSK BAĞLAMI ===\n{json.dumps(context, ensure_ascii=False, indent=1)}\n\n"
        "GÖREV: Yukarıdaki kesin verilere dayanarak bir işlem kararı üret.\n"
        "- Stop-loss seviyesini ATR ve en yakın destek/direnç seviyesinden seç.\n"
        "- Take-profit, stop mesafesinin en az 2 katı uzaklıkta olmalı.\n"
        "- Açık pozisyon varsa: pozisyonu korumak mantıklıysa WAIT, "
        "tez bozulduysa CLOSE de.\n"
        "SADECE JSON döndür."
    )


# --------------------------------------------------------------------------- #
#  JSON çıkarımı (reasoning modelleri için dayanıklı)
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def extract_json(text: str) -> dict[str, Any] | None:
    """Model çıktısından JSON nesnesini güvenle ayıklar."""
    if not text:
        return None
    cleaned = _THINK.sub("", text).strip()

    for candidate in (
        cleaned,
        *(m.strip() for m in _FENCE.findall(cleaned)),
    ):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Son çare: dengeli süslü parantez taraması
    start = cleaned.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(cleaned[start: i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = cleaned.find("{", start + 1)
    return None


# --------------------------------------------------------------------------- #
#  Ağ geçidi
# --------------------------------------------------------------------------- #


class LLMGateway:
    """Tek arayüz, çok sağlayıcı. Ağ hatalarında üstel geri çekilmeli tekrar dener."""

    def __init__(self, provider: str, api_key: str = "", model: str = "",
                 base_url: str = "", timeout: float = 90.0, temperature: float = 0.2,
                 max_retries: int = 2) -> None:
        self.provider_id = provider
        self.spec = PROVIDERS.get(provider, PROVIDERS["custom"])
        self.base_url = (base_url or self.spec.base_url).rstrip("/")
        self.model = model or (self.spec.models[0] if self.spec.models else "")
        self.api_key = api_key or ""
        self.timeout = timeout
        self.temperature = temperature
        self.max_retries = max_retries

        if not self.base_url:
            raise ValueError("base_url zorunludur (özel sağlayıcı için girilmelidir).")
        if not self.model:
            raise ValueError("Model adı zorunludur.")

    # -- düşük seviye çağrı ------------------------------------------------- #
    def _headers(self) -> dict[str, str]:
        if self.spec.style == "anthropic":
            return {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        h = {"content-type": "application/json"}
        if self.api_key:
            h["authorization"] = f"Bearer {self.api_key}"
        if self.provider_id == "openrouter":
            h["HTTP-Referer"] = "https://github.com/zumvia"
            h["X-Title"] = "ZUMVIA"
        return h

    def _payload(self, system: str, user: str, json_mode: bool) -> tuple[str, dict[str, Any]]:
        if self.spec.style == "anthropic":
            return "/messages", {
                "model": self.model,
                "max_tokens": 1500,
                "temperature": self.temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": 1500,
        }
        # o-serisi / reasoning modelleri sıcaklık kabul etmez
        if re.match(r"^(o[134]|gpt-5)", self.model):
            body.pop("temperature", None)
            body["max_completion_tokens"] = body.pop("max_tokens")
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return "/chat/completions", body

    @staticmethod
    def _extract_text(provider_style: str, data: dict[str, Any]) -> str:
        if provider_style == "anthropic":
            parts = data.get("content") or []
            return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        text = msg.get("content") or ""
        if not text:  # bazı reasoning modelleri ayrı alan kullanır
            text = msg.get("reasoning_content") or choices[0].get("text") or ""
        return text

    def _post(self, system: str, user: str, json_mode: bool) -> dict[str, Any]:
        # Devre kesici: soğumadaysa hemen reddet
        if not is_provider_healthy(self.provider_id) and self.provider_id not in ("ollama", "vllm", "lmstudio", "custom"):
            raise RuntimeError(f"Sağlayıcı {self.provider_id} geçici olarak devre dışı (ardışık hata). 5 dk içinde otomatik dönecek.")

        path, body = self._payload(system, user, json_mode)
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None

        # Kaba token tahmini: sağlayıcının dakikalık token tavanı varsa
        # kuyruk bunu da hesaba katsın (4 karakter ≈ 1 token).
        est_tokens = (len(system) + len(user)) // 4 + 512

        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                # Sağlayıcının yayınlanmış hız sınırını AŞMADAN sıraya gir.
                # Beklemek, 429 yiyip turu kaybetmekten ucuzdur.
                with rate_limit.acquire(self.provider_id, est_tokens=est_tokens) as slot:
                    with httpx.Client(timeout=self.timeout) as client:
                        resp = client.post(url, headers=self._headers(), json=body)

                    if resp.status_code == 429:
                        slot.penalize(_retry_after(resp))
                        raise httpx.HTTPStatusError(
                            "HTTP 429", request=resp.request, response=resp
                        )
                    if resp.status_code == 400 and json_mode:
                        log.info("Model json_object desteklemiyor, düz modda tekrar deneniyor.")
                        return self._post(system, user, json_mode=False)
                    if resp.status_code in (408, 500, 502, 503, 504):
                        raise httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}", request=resp.request, response=resp
                        )
                    resp.raise_for_status()
                    data = resp.json()
                    slot.record_tokens(_used_tokens(data))
                    slot.succeed()          # temiz geçti: öğrenilen tavan gevşeyebilir

                _mark_success(self.provider_id, int((time.perf_counter() - started) * 1000))
                return data
            except rate_limit.RateLimitTimeout as exc:
                # Kuyruk açılmadı: fail-safe'e düş, tekrar deneme.
                _mark_failure(self.provider_id)
                raise RuntimeError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
        _mark_failure(self.provider_id)
        raise RuntimeError(f"LLM çağrısı başarısız: {_http_detail(last_exc)}")

    # -- genel API ---------------------------------------------------------- #
    def decide(self, snapshot: dict[str, Any], context: dict[str, Any],
               recovery_mode: bool = False, extra_rules: str = "") -> LLMResult:
        """
        Piyasa fotoğrafı → doğrulanmış TradeDecision.
        Herhangi bir hata durumunda ok=False döner ve Katman 4 işlemi iptal eder.
        """
        system = build_system_prompt(recovery_mode, extra_rules)
        user = build_user_prompt(snapshot, context)
        started = time.perf_counter()

        try:
            data = self._post(system, user, json_mode=True)
        except Exception as exc:  # noqa: BLE001
            return LLMResult(False, None, "", f"Bağlantı/servis hatası: {exc}",
                             int((time.perf_counter() - started) * 1000),
                             self.model, self.provider_id)

        latency = int((time.perf_counter() - started) * 1000)
        text = self._extract_text(self.spec.style, data)
        usage = data.get("usage")

        obj = extract_json(text)
        if obj is None:
            return LLMResult(False, None, text[:2000],
                             "Model geçerli JSON döndürmedi — işlem iptal (fail-safe).",
                             latency, self.model, self.provider_id, usage)

        try:
            decision = TradeDecision.model_validate(obj)
        except ValidationError as exc:
            return LLMResult(False, None, text[:2000],
                             f"Şema doğrulaması başarısız — işlem iptal: {exc.errors()[:2]}",
                             latency, self.model, self.provider_id, usage)

        return LLMResult(True, decision, text[:2000], "", latency,
                         self.model, self.provider_id, usage)

    def ping(self) -> tuple[bool, str, int]:
        """Bağlantı testi — arayüzdeki 'Bağlantıyı Test Et' düğmesi için. (ok, mesaj, gecikme_ms)"""
        started = time.perf_counter()
        try:
            data = self._post(
                "Sen bir test aracısın.",
                'Sadece şu JSON ile yanıt ver: {"ok": true}',
                json_mode=True,
            )
            text = self._extract_text(self.spec.style, data)
            latency = int((time.perf_counter() - started) * 1000)
            return True, (text or "")[:200] or "Bağlantı başarılı.", latency
        except Exception as exc:  # noqa: BLE001
            latency = int((time.perf_counter() - started) * 1000)
            return False, str(exc)[:300], latency


# --------------------------------------------------------------------------- #
#  Arac cagirmali sohbet (Komuta Ajani icin)
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class ChatTurn:
    """Bir model turunun ham sonucu."""
    ok: bool
    text: str = ""
    tool_calls: list[dict[str, Any]] = None  # type: ignore[assignment]
    error: str = ""
    latency_ms: int = 0
    usage: dict[str, Any] | None = None
    raw_message: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.tool_calls is None:
            self.tool_calls = []


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI arac semasini Anthropic formatina cevirir."""
    out = []
    for t in tools:
        fn = t.get("function", t)
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


def _http_detail(exc: Exception) -> str:
    """
    Sağlayıcının AÇIKLAMASINI hata metnine ekler.

    `raise_for_status()` yalnızca "Client error '400 Bad Request'" der; asıl
    bilgi yanıt gövdesindedir ve orada durur: Google geçersiz anahtar için
    400 + "Please pass a valid API key" döndürür. Gövde atılırsa hata
    "bilinmeyen" sayılır, kullanıcı ham HTTP metni görür ve ne yapacağını
    bilemez. Oysa yapılması gereken tek şey anahtarı düzeltmektir.
    """
    text = str(exc)
    response = getattr(exc, "response", None)
    body = ""
    if response is not None:
        try:
            body = (response.text or "").strip()
        except Exception:  # noqa: BLE001 — gövde okunamıyorsa metin yine anlamlı
            body = ""
    if body:
        text = f"{text} | {body[:300]}"
    return text[:600]


class ChatGateway(LLMGateway):
    """
    Arac cagirmali (function calling) sohbet destegi.

    `chat()` bir tur calistirir ve modelin ya metin ya da arac cagrisi
    dondurdugunu bildirir. Ajan dongusu bu turlari zincirler.
    """

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             system: str = "", max_tokens: int = 4000) -> ChatTurn:
        started = time.perf_counter()
        try:
            if self.spec.style == "anthropic":
                body: dict[str, Any] = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "temperature": self.temperature,
                    "messages": messages,
                }
                if system:
                    body["system"] = system
                if tools:
                    body["tools"] = _anthropic_tools(tools)
                path = "/messages"
            else:
                full = ([{"role": "system", "content": system}] if system else []) + messages
                body = {
                    "model": self.model,
                    "messages": full,
                    "temperature": self.temperature,
                    "max_tokens": max_tokens,
                }
                if re.match(r"^(o[134]|gpt-5)", self.model):
                    body.pop("temperature", None)
                    body["max_completion_tokens"] = body.pop("max_tokens")
                if tools:
                    body["tools"] = tools
                    body["tool_choice"] = "auto"
                path = "/chat/completions"

            url = f"{self.base_url}{path}"
            last_exc: Exception | None = None
            data: dict[str, Any] | None = None
            # Araç çağırmalı turlar uzun olur; token tahmini buna göre yapılır.
            est_tokens = (sum(len(str(m.get("content", ""))) for m in messages)
                          + len(system)) // 4 + max_tokens

            for attempt in range(self.max_retries + 1):
                try:
                    # Sağlayıcının hız sınırını aşmadan sıraya gir.
                    with rate_limit.acquire(self.provider_id,
                                            est_tokens=est_tokens) as slot:
                        with httpx.Client(timeout=self.timeout) as client:
                            resp = client.post(url, headers=self._headers(), json=body)

                        if resp.status_code == 429:
                            slot.penalize(_retry_after(resp))
                            raise httpx.HTTPStatusError("HTTP 429",
                                                        request=resp.request, response=resp)
                        if resp.status_code in (408, 500, 502, 503, 504):
                            raise httpx.HTTPStatusError(f"HTTP {resp.status_code}",
                                                        request=resp.request, response=resp)
                        resp.raise_for_status()
                        data = resp.json()
                        slot.record_tokens(_used_tokens(data))
                        slot.succeed()      # temiz geçti: öğrenilen tavan gevşeyebilir
                    break
                except rate_limit.RateLimitTimeout as exc:
                    last_exc = exc
                    break                      # kuyruk açılmadı: fail-safe'e düş
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if attempt < self.max_retries:
                        time.sleep(1.5 * (attempt + 1))
            if data is None:
                # Gövdeyi BURADA ekle: sarmalama sonrası `.response` kaybolur
                # ve sağlayıcının açıklaması ("Please pass a valid API key")
                # bir daha geri getirilemez.
                raise RuntimeError(_http_detail(last_exc))

        except Exception as exc:  # noqa: BLE001
            return ChatTurn(False, error=_http_detail(exc),
                            latency_ms=int((time.perf_counter() - started) * 1000))

        latency = int((time.perf_counter() - started) * 1000)

        if self.spec.style == "anthropic":
            parts = data.get("content") or []
            text = "".join(p.get("text", "") for p in parts
                           if isinstance(p, dict) and p.get("type") == "text")
            calls = [{"id": p.get("id", ""), "name": p.get("name", ""),
                      "arguments": p.get("input", {})}
                     for p in parts if isinstance(p, dict) and p.get("type") == "tool_use"]
            return ChatTurn(True, text, calls, "", latency, data.get("usage"),
                            {"role": "assistant", "content": parts})

        choices = data.get("choices") or []
        if not choices:
            return ChatTurn(False, error="Model bos yanit dondu.", latency_ms=latency)
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        calls = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                parsed = {}
            calls.append({"id": call.get("id", ""), "name": fn.get("name", ""),
                          "arguments": parsed})
        return ChatTurn(True, text, calls, "", latency, data.get("usage"), message)
