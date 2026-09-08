"""
YÖNERGE UYUMU — küçük modeller de eksiksiz uysun

Yaşanmış durum: 30B'lik bir model, "sürecini anlatma" kuralı yönergede
açıkça yazdığı hâlde raporun yarısını yönergeyi tekrarlayarak doldurdu.

Küçük bir model için talimat bir ÖNERİDİR, kısıt değil. Kısıtı yönergeye
yazarak değil, çıktıyı ölçüp düzelterek uygularız:

    1. TEMİZLE   süreç satırlarını at              (bedava)
    2. UYAR      aynı modele düzeltme talimatı ver (1 çağrı)
    3. DEĞİŞTİR  başka modele geç                  (1 çağrı)

Bu dosya üç şeyi birden korur: ihlal yakalanmalı, düzeltme çalışmalı ve
sağlam çıktı BOŞUNA yeniden denenmemeli — her deneme kota yakar.
"""
from __future__ import annotations

import pytest

from app.layers.l3_llm_gateway import ChatTurn
from app.research import compliance
from app.research.ledger import Ledger
from app.research.pipeline import ResearchPipeline, strip_thinking

GOOD = """- Yön: yükseliş. Fiyat EMA200 üstünde, ADX 41 güçlü trend gösteriyor.
- Kritik seviye: destek 76.945, direnç 80.285 civarında.
- Momentum: RSI 56 ile nötr bölgede, aşırı alım yok.
- Zayıf taraf: MACD histogramı negatif, hacim ortalamanın altında."""

THINKING = """Wait, the prompt says: "Şunları yaz: 1. Yön 2. Seviyeler".
I need to output 4 specific items as per the system prompt structure.
The user wants Turkish, madde madde. So likely we need bullet points.
Must not use numbers unless present in the ledger.
- Yön: yükseliş."""

ENGLISH = """- Direction: bullish, price is above the 200 EMA and ADX shows
  a strong trend which should continue for the next several sessions.
- Support and resistance levels are clearly defined by the bands.
- Momentum is neutral because the relative strength index has not
  reached the overbought threshold that would suggest exhaustion."""


def _check(text: str, language: str = "tr") -> compliance.Verdict:
    return compliance.check(text, language=language, strip_thinking=strip_thinking)


@pytest.fixture(autouse=True)
def _clean_record():
    compliance.reset()
    yield
    compliance.reset()


# --------------------------------------------------------------------------- #
#  Ölçüm
# --------------------------------------------------------------------------- #

def test_good_output_passes_untouched() -> None:
    """
    Sağlam çıktı için hiçbir şey yapılmamalı.

    Boşuna yeniden deneme, kotayı yakar ve araştırmayı yavaşlatır.
    """
    verdict = _check(GOOD)
    assert verdict.ok
    assert verdict.problems == []
    assert verdict.cleaned == GOOD


def test_process_narration_is_caught() -> None:
    """Yönergeyi tekrarlayan çıktı kullanılamaz sayılmalı."""
    verdict = _check(THINKING)
    assert not verdict.ok
    assert any("anlatım" in p or "tekrar" in p for p in verdict.problems)
    assert verdict.thinking_ratio > compliance.MAX_THINKING_RATIO


def test_wrong_language_is_caught() -> None:
    """
    Türkçe istenirken İngilizce yazmak da bir yönerge ihlalidir.

    Kullanıcı raporu okuyamıyorsa rapor yoktur.
    """
    verdict = _check(ENGLISH)
    assert not verdict.ok
    assert any("Türkçe" in p for p in verdict.problems)


def test_technical_english_terms_do_not_trigger_language_alarm() -> None:
    """
    "stop loss", "take profit" finansta Türkçe metinde de İngilizce yazılır.

    Bunları dil kayması saymak, doğru yazan modeli boşuna yeniden
    çalıştırırdı.
    """
    text = ("- Stop loss 76.945 seviyesinde, take profit 83.243.\n"
            "- Risk/ödül oranı 2,25 ile makul görünüyor.\n"
            "- Momentum zayıf: MACD histogram negatif, volume düşük.\n"
            "- Bu tablonun zayıf tarafı hacim teyidinin olmaması.")
    assert _check(text).ok


def test_empty_answer_is_caught() -> None:
    verdict = _check("")
    assert not verdict.ok
    assert "boş" in verdict.problems[0].lower()


def test_too_short_answer_is_caught() -> None:
    verdict = _check("Yükseliş var.")
    assert not verdict.ok
    assert any("kısa" in p for p in verdict.problems)


def test_correction_instruction_is_concrete() -> None:
    """
    Modele "daha iyi yap" demek işe yaramaz; NE yanlış olduğu söylenmeli.
    """
    verdict = _check(THINKING)
    instruction = verdict.instruction()
    assert "KULLANILAMAZ" in instruction
    assert any(problem in instruction for problem in verdict.problems)
    assert "süreç anlatma" in instruction


# --------------------------------------------------------------------------- #
#  Düzeltme döngüsü
# --------------------------------------------------------------------------- #

class Scripted:
    """Sırayla verilen yanıtları döndüren sahte model."""

    def __init__(self, answers: list[str], name: str = "sahte/model") -> None:
        self.answers = answers
        self.model = name
        self.provider_id = "sahte"
        self.seen: list[str] = []

    def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
        self.seen.append(messages[0]["content"])
        text = self.answers.pop(0) if self.answers else GOOD
        return ChatTurn(True, text=text)


def _collect(question: str, ledger: Ledger) -> dict:  # noqa: ARG001
    ledger.add("BTC/USDT.fiyat", 78427.40, "binance", "ticker", "USD")
    return {"symbols": ["BTC/USDT"], "failed": []}


def test_a_warning_fixes_a_stubborn_model() -> None:
    """
    İlk yanıt süreç anlatımı, ikinci yanıt düzgün → düzeltme çalışmalı.

    İkinci istemde modelin NE yanlış yaptığı yazılı olmalı; aynı istemi
    tekrar göndermek aynı sonucu verirdi.
    """
    gateway = Scripted([THINKING, GOOD])
    pipeline = ResearchPipeline(gateway_for=lambda role, fresh=False: gateway,
                                collect=_collect)
    result = pipeline.run("BTC?")
    market = next(o for o in result.outputs if o.role == "market")

    assert market.complied, "düzeltme sonrası hâlâ uyumsuz"
    assert market.attempts == 2
    assert "KULLANILAMAZ" in gateway.seen[1], "düzeltme talimatı gönderilmedi"


def test_a_different_model_is_tried_when_warning_fails() -> None:
    """
    İnatçı model iki kez de uymuyorsa BAŞKA modele geçilmeli.

    Aynı modeli üçüncü kez denemek kota yakmaktan başka bir şey değildir.
    """
    stubborn = Scripted([THINKING, THINKING], name="kucuk/model")
    capable = Scripted([GOOD], name="buyuk/model")

    def gateway_for(role, fresh=False):
        return capable if fresh else stubborn

    pipeline = ResearchPipeline(gateway_for=gateway_for, collect=_collect)
    result = pipeline.run("BTC?")
    market = next(o for o in result.outputs if o.role == "market")

    assert market.complied
    assert market.model == "buyuk/model", "modele geçilmedi"


def test_compliant_output_is_never_retried() -> None:
    """Sağlam çıktı için ek model çağrısı yapılmamalı."""
    gateway = Scripted([GOOD])
    pipeline = ResearchPipeline(gateway_for=lambda role, fresh=False: gateway,
                                collect=_collect)
    pipeline.run("BTC?")
    # Beş rol × birer çağrı (sentez dahil altı); fazlası yeniden denemedir.
    assert len(gateway.seen) == 6, f"gereksiz tekrar: {len(gateway.seen)} çağrı"


def test_persistent_non_compliance_is_reported_not_hidden() -> None:
    """
    Hiçbir model uymadıysa kullanıcı bunu BİLMELİ.

    Temizlenmiş ama eksik bir metni sağlam gibi sunmak, raporun güvenini
    sessizce çürütür.
    """
    gateway = Scripted([THINKING] * 30)
    pipeline = ResearchPipeline(gateway_for=lambda role, fresh=False: gateway,
                                collect=_collect)
    result = pipeline.run("BTC?")
    assert any("yönergeye tam uymadı" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
#  Uyum sicili
# --------------------------------------------------------------------------- #

def test_reliability_is_measured_not_guessed() -> None:
    compliance.note("kucuk/model", "market", False)
    compliance.note("kucuk/model", "market", False)
    compliance.note("kucuk/model", "market", True)
    compliance.note("buyuk/model", "market", True)

    assert compliance.reliability("kucuk/model", "market") == pytest.approx(1 / 3)
    assert compliance.reliability("buyuk/model", "market") == 1.0


def test_unknown_model_gets_the_benefit_of_the_doubt() -> None:
    """
    Bilinmemek, kötü olmakla aynı şey değildir.

    Yeni bir model şansı hak eder; sicil ancak gözlemle oluşur.
    """
    assert compliance.reliability("hic/denenmemis", "market") == 1.0


def test_reliability_is_tracked_per_role() -> None:
    """
    Bir model bir rolde iyi, başkasında kötü olabilir.

    Küçük bir model piyasa özetini yazabilir ama sentez gibi uzun ve
    yapılandırılmış bir görevi beceremeyebilir.
    """
    compliance.note("model/a", "market", True)
    compliance.note("model/a", "synthesis", False)
    assert compliance.reliability("model/a", "market") == 1.0
    assert compliance.reliability("model/a", "synthesis") == 0.0
