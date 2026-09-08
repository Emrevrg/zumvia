"""
ARAŞTIRMA BORU HATTI

Korunan sözler:

  * Bir uzman düşerse araştırma DEVAM EDER ve eksiklik rapora yazılır.
  * Kullanıcı durdurursa boru hattı hemen durur.
  * Şüpheci, diğerlerinin görüşünü GÖRDÜKTEN sonra çalışır.
  * Hiç analiz üretilemediyse rapor "yüksek dayanak" demez.
  * Uzmanlara verilen tek sayı kaynağı ölçüm defteridir.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.layers.l3_llm_gateway import ChatTurn
from app.research import risk as risk_mod
from app.research.ledger import Ledger
from app.research.pipeline import ResearchPipeline, Stage
from app.research.report import render
from app.research.roles import PARALLEL_ROLES, Role


class FakeGateway:
    """İstenen metni döndüren sahte model."""

    def __init__(self, text: str = "Görünüm nötr.", fail: bool = False,
                 model: str = "sahte/model") -> None:
        self.text = text
        self.fail = fail
        self.model = model
        self.provider_id = "sahte"
        self.seen: list[str] = []

    def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
        self.seen.append(messages[0]["content"])
        if self.fail:
            return ChatTurn(False, error="sağlayıcı düştü")
        return ChatTurn(True, text=self.text)


def _collect_nothing(question: str, ledger: Ledger) -> dict[str, Any]:  # noqa: ARG001
    return {}


def _collect_btc(question: str, ledger: Ledger) -> dict[str, Any]:  # noqa: ARG001
    ledger.add("BTC/USDT.fiyat", 78564.48, "binance", "ticker", "USD")
    ledger.add("BTC/USDT.indicators.atr_14", 1143.31, "binance", "indicators")
    ledger.add("portfoy.sermaye", 10000.0, "veritabani", "toplam", "USD")
    return {"market": "crypto", "symbols": ["BTC/USDT"], "failed": []}


# --------------------------------------------------------------------------- #
#  Akış
# --------------------------------------------------------------------------- #

def test_all_stages_run_in_order() -> None:
    events: list[dict] = []
    pipeline = ResearchPipeline(
        gateway_for=lambda role: FakeGateway(),
        collect=_collect_btc,
        measure_risk=risk_mod.measure,
        emit=events.append,
    )
    pipeline.run("BTC nasıl?")

    order = [e["stage"] for e in events
             if e["type"] == "research_stage" and e["status"] == "running"]
    assert order == [str(s) for s in (Stage.BRIEF, Stage.DATA, Stage.ANALYSIS,
                                      Stage.VERIFICATION, Stage.RISK, Stage.REPORT)]


def test_every_expert_gets_the_ledger_and_nothing_else() -> None:
    """
    Uzmanlara verilen TEK sayı kaynağı defterdir.

    Defter verilmezse model kendi hafızasından sayı üretir ve doğrulama
    aşaması bunu yakalayamaz — çünkü karşılaştıracak ölçüm yoktur.
    """
    gateway = FakeGateway()
    pipeline = ResearchPipeline(gateway_for=lambda role: gateway,
                                collect=_collect_btc)
    pipeline.run("BTC nasıl?")

    assert gateway.seen, "hiçbir uzman çalışmadı"
    for prompt in gateway.seen:
        assert "ÖLÇÜM DEFTERİ" in prompt
        assert "78564.48" in prompt


def test_a_failing_expert_does_not_stop_the_research() -> None:
    """
    Bir uzmanın düşmesi araştırmayı bitirmez — ama gizlenmez de.

    Eksik katkıyla yazılmış bir rapor, hiç rapor olmamasından iyidir;
    eksikliği saklayan bir rapor ikisinden de kötüdür.
    """
    def gateway_for(role: Role):
        return FakeGateway(fail=(role.id == "news"))

    pipeline = ResearchPipeline(gateway_for=gateway_for, collect=_collect_btc)
    result = pipeline.run("BTC nasıl?")

    assert any(o.ok for o in result.outputs), "tüm uzmanlar düştü"
    failed = [o for o in result.outputs if not o.ok]
    assert len(failed) == 1
    assert any("Haber Analisti" in w for w in result.warnings), \
        "eksik uzman kullanıcıya bildirilmedi"


def test_missing_model_is_reported_not_crashed() -> None:
    pipeline = ResearchPipeline(gateway_for=lambda role: None,
                                collect=_collect_btc)
    result = pipeline.run("BTC nasıl?")
    assert all(not o.ok for o in result.outputs)
    assert result.report == ""
    assert not result.has_analysis


def test_data_failure_does_not_stop_the_research() -> None:
    def broken(question, ledger):  # noqa: ARG001
        raise RuntimeError("borsa kapalı")

    pipeline = ResearchPipeline(gateway_for=lambda role: FakeGateway(),
                                collect=broken)
    result = pipeline.run("BTC nasıl?")

    assert any("Veri toplama" in w for w in result.warnings)
    assert any(o.ok for o in result.outputs), "veri yokken uzmanlar da durdu"


def test_experts_run_in_parallel() -> None:
    """Dört uzman sırayla çalışsaydı araştırma dört kat uzun sürerdi."""
    import threading
    import time

    peak = {"n": 0, "now": 0}
    lock = threading.Lock()

    class Slow(FakeGateway):
        def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
            with lock:
                peak["now"] += 1
                peak["n"] = max(peak["n"], peak["now"])
            time.sleep(0.15)
            with lock:
                peak["now"] -= 1
            return ChatTurn(True, text="tamam")

    pipeline = ResearchPipeline(gateway_for=lambda role: Slow(),
                                collect=_collect_nothing)
    pipeline.run("soru")
    assert peak["n"] >= 2, f"uzmanlar paralel çalışmadı (en fazla {peak['n']})"


# --------------------------------------------------------------------------- #
#  Rol sırası
# --------------------------------------------------------------------------- #

def test_skeptic_sees_the_other_experts() -> None:
    """
    İtiraz edilecek bir tez olmadan şüphecilik yapılamaz.

    Şüpheci diğerleriyle paralel çalıştırılsaydı, eleştirecek hiçbir şey
    görmeden "genel riskler" yazardı — yani işe yaramazdı.
    """
    prompts: dict[str, str] = {}

    class Recorder(FakeGateway):
        def __init__(self, role_id: str) -> None:
            super().__init__(text=f"{role_id} görüşü")
            self.role_id = role_id

        def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
            prompts[self.role_id] = messages[0]["content"]
            return ChatTurn(True, text=f"{self.role_id} görüşü")

    pipeline = ResearchPipeline(gateway_for=lambda role: Recorder(role.id),
                                collect=_collect_nothing)
    pipeline.run("soru")

    assert "DİĞER UZMANLARIN GÖRÜŞLERİ" in prompts["skeptic"]
    assert "Piyasa Analisti" in prompts["skeptic"]
    for role in PARALLEL_ROLES:
        assert "DİĞER UZMANLARIN" not in prompts[role.id], \
            f"{role.id} paralel çalışmalıydı ama başkasının görüşünü gördü"


def test_synthesis_sees_verification_results() -> None:
    """Baş araştırmacı, hangi iddianın doğrulandığını bilmeli."""
    prompts: dict[str, str] = {}

    class Recorder(FakeGateway):
        def __init__(self, role_id: str) -> None:
            super().__init__(text="BTC 78.564 dolar.")
            self.role_id = role_id

        def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
            prompts[self.role_id] = messages[0]["content"]
            return ChatTurn(True, text="BTC 78.564 dolar.")

    pipeline = ResearchPipeline(gateway_for=lambda role: Recorder(role.id),
                                collect=_collect_btc)
    pipeline.run("soru")
    assert "doğrulama:" in prompts["synthesis"]


# --------------------------------------------------------------------------- #
#  Durdurma
# --------------------------------------------------------------------------- #

def test_cancellation_stops_the_pipeline() -> None:
    calls = {"n": 0}

    def gateway_for(role):  # noqa: ARG001
        calls["n"] += 1
        return FakeGateway()

    pipeline = ResearchPipeline(gateway_for=gateway_for,
                                collect=_collect_nothing,
                                cancelled=lambda: True)
    result = pipeline.run("soru")

    assert calls["n"] == 0, "durdurulmuşken uzman çalıştırıldı"
    assert any("durduruldu" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
#  Dürüstlük
# --------------------------------------------------------------------------- #

def test_no_analysis_never_reports_high_confidence() -> None:
    """
    Hiç uzman konuşamadıysa güven "yüksek" olamaz.

    "Kimse bir şey söylemedi" durumunu "her söylenen doğrulandı" gibi
    göstermek, raporun en üstüne yalan bir başlık yazmaktır.
    """
    pipeline = ResearchPipeline(gateway_for=lambda role: FakeGateway(fail=True),
                                collect=_collect_btc)
    result = pipeline.run("BTC nasıl?")

    assert result.trust_score == 0.0
    markdown = render(result)
    assert "ANALİZ YAPILAMADI" in markdown
    assert "Yüksek dayanak" not in markdown


def test_report_separates_measurement_from_opinion() -> None:
    """Rapor, ölçümü yorumdan ayırt edilebilir kılmalı."""
    pipeline = ResearchPipeline(
        gateway_for=lambda role: FakeGateway(
            text="BTC 78.564 dolar. Kurumsal giriş hacmi 4,2 milyar dolar."),
        collect=_collect_btc, measure_risk=risk_mod.measure)
    result = pipeline.run("BTC nasıl?")
    markdown = render(result)

    assert "## Ölçümler" in markdown
    assert "binance/ticker" in markdown, "ölçümün kaynağı gösterilmedi"
    assert "Ölçüm karşılığı yok" in markdown, "dayanaksız iddia işaretlenmedi"
    assert "yatırım tavsiyesi değildir" in markdown


def test_report_survives_a_totally_empty_research() -> None:
    pipeline = ResearchPipeline(gateway_for=lambda role: None,
                                collect=_collect_nothing)
    markdown = render(pipeline.run("soru"))
    assert "hiçbir ölçüm alınamadı" in markdown


# --------------------------------------------------------------------------- #
#  Risk — kod hesaplar, model değil
# --------------------------------------------------------------------------- #

def test_position_size_comes_from_risk_and_stop_distance() -> None:
    """
    Önce kaybedilebilecek tutar, sonra pozisyon büyüklüğü.

    Tersi (önce büyüklük, sonra stop) hesabın en yaygın ve en pahalı
    yanlışıdır: pozisyon önce seçilirse stop, kaybı sınırlamak yerine
    pozisyona uydurulur.
    """
    led = Ledger()
    led.add("BTC/USDT.fiyat", 100000.0, "binance", "ticker")
    led.add("BTC/USDT.indicators.atr_14", 1000.0, "binance", "indicators")
    led.add("portfoy.sermaye", 10000.0, "veritabani", "toplam")

    result = risk_mod.measure(led, {"symbols": ["BTC/USDT"]})
    row = result["islem_basi"][0]

    # 2 ATR stop = 2000; kasa riski %1 = 100 → 100/2000 = 0.05 adet
    assert row["stop_mesafesi"] == pytest.approx(2000.0)
    assert row["pozisyon_adet"] == pytest.approx(0.05, rel=0.01)


def test_missing_measurement_is_never_replaced_by_a_guess() -> None:
    """
    Ölçüm yoksa alan boş kalır.

    Uydurulmuş bir risk sayısı, risk hesabı yapmamaktan daha tehlikelidir:
    kullanıcı ölçülmüş sanıp ona göre pozisyon açar.
    """
    led = Ledger()
    led.add("BTC/USDT.fiyat", 100000.0, "binance", "ticker")   # ATR yok

    result = risk_mod.measure(led, {"symbols": ["BTC/USDT"]})
    row = result["islem_basi"][0]

    assert row["risk_tutari"] is None
    assert "ATR" in row["not"]
    assert result["en_kotu_senaryo_tutar"] is None


def test_worst_case_assumes_positions_fail_together() -> None:
    """
    Korelasyon yüksekken pozisyonlar bağımsız değildir.

    "Hepsi birden ters gider" senaryosu karamsarlık değil, gerçekçiliktir.
    """
    led = Ledger()
    led.add("portfoy.sermaye", 10000.0, "veritabani", "toplam")
    for symbol in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
        led.add(f"{symbol}.fiyat", 1000.0, "binance", "ticker")
        led.add(f"{symbol}.indicators.atr_14", 10.0, "binance", "indicators")

    result = risk_mod.measure(led, {"symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"]})
    per_trade = result["islem_basi"][0]["risk_tutari"]
    assert result["en_kotu_senaryo_tutar"] == pytest.approx(per_trade * 3)
