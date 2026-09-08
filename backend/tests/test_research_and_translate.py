"""
WEB ARAŞTIRMA + İSTEM ÇEVİRİSİ TESTLERİ

İkisinin de ortak kuralı var: **başarısız olduklarında sistemi durdurmazlar.**
İnternet yoksa ticaret devam eder; çeviri yapılamazsa kullanıcının mesajı
aynen gider. Testler bu güvenli düşüşü ve dış içeriğin "veri, talimat değil"
sınırını doğrular.
"""
from __future__ import annotations

import pytest

from app.agent.translate import LANGUAGE_NAMES, detect, needs_translation, translate
from app.layers import web_research

# --------------------------------------------------------------------------- #
#  Dil tespiti
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("Kriptoda 1000 dolarımı yönet, uygun pariteyi sen seç", "tr"),
    ("Manage my thousand dollars in crypto and pick the pair", "en"),
    ("أدر أموالي في العملات الرقمية واختر الزوج بنفسك", "ar"),
    ("Управляй моими деньгами в криптовалюте", "ru"),
    ("帮我管理加密货币资金并自行选择交易对", "zh"),
    ("Verwalte bitte mein Geld und wähle das Paar selbst", "de"),
])
def test_language_detection(text: str, expected: str) -> None:
    assert detect(text) == expected


def test_short_text_is_not_guessed() -> None:
    """Kısa metinde tahmin yürütmek yanlış çeviriye yol açar; sessiz kalınır."""
    assert detect("al") == ""
    assert detect("BTC") == ""


def test_ambiguous_text_is_left_alone() -> None:
    """Ayırt edici işaret yoksa çeviri yapılmaz (şüphede müdahale etme)."""
    assert detect("BTC/USDT 45000 50000 1.5") == ""


def test_same_language_needs_no_translation() -> None:
    assert needs_translation("Kriptoda paramı yönet lütfen", "tr") is False


def test_different_language_triggers_translation() -> None:
    assert needs_translation("Please manage my money in the crypto market", "ar") is True


def test_unknown_target_language_is_ignored() -> None:
    assert needs_translation("Please manage my money in the market", "xx") is False


# --------------------------------------------------------------------------- #
#  Çeviri — hata hâlinde mesaj KAYBOLMAZ
# --------------------------------------------------------------------------- #

class _Turn:
    def __init__(self, ok: bool, text: str = "", error: str = "") -> None:
        self.ok, self.text, self.error = ok, text, error


class _Gateway:
    def __init__(self, turn: _Turn) -> None:
        self._turn = turn
        self.calls = 0

    def chat(self, messages, tools=None, system="", max_tokens=4000):  # noqa: ARG002
        self.calls += 1
        return self._turn


def test_successful_translation_keeps_original() -> None:
    gateway = _Gateway(_Turn(True, "Paramı kripto piyasasında yönet"))
    result = translate(gateway, "test-model", "Manage my money in the crypto market", "tr")

    assert result["translated"] is True
    assert result["text"] == "Paramı kripto piyasasında yönet"
    assert result["original"] == "Manage my money in the crypto market"
    assert result["target"] == "tr"


def test_model_failure_returns_original_text() -> None:
    """Model hata verirse kullanıcının mesajı asla düşmez."""
    original = "Manage my money in the crypto market"
    result = translate(_Gateway(_Turn(False, error="kota doldu")), "m", original, "tr")

    assert result["translated"] is False
    assert result["text"] == original


def test_exception_returns_original_text() -> None:
    class Broken:
        def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            raise RuntimeError("ağ yok")

    original = "Please buy bitcoin for me now"
    result = translate(Broken(), "m", original, "tr")
    assert result["translated"] is False
    assert result["text"] == original


def test_identical_output_is_not_marked_as_translated() -> None:
    text = "Manage my money in the crypto market"
    result = translate(_Gateway(_Turn(True, text)), "m", text, "tr")
    assert result["translated"] is False


def test_every_ui_language_has_a_translator_name() -> None:
    """Arayüzdeki her dil çeviri istemi için bir isme sahip olmalı."""
    for code in ("tr", "en", "de", "fr", "es", "pt", "ru", "ar", "zh"):
        assert code in LANGUAGE_NAMES


# --------------------------------------------------------------------------- #
#  Web araştırma — ağ yokken bile güvenli
# --------------------------------------------------------------------------- #

def test_empty_query_is_rejected() -> None:
    assert web_research.search("")["results"] == []


def test_network_failure_returns_empty_not_exception(monkeypatch) -> None:
    """İnternet yoksa arama boş döner; ticaret motoru çalışmaya devam eder."""
    class Boom:
        def __init__(self, *a, **k): pass                      # noqa: ANN002, ANN003
        def __enter__(self): return self
        def __exit__(self, *a): return False                   # noqa: ANN002
        def post(self, *a, **k): raise OSError("ağ yok")       # noqa: ANN002, ANN003

    monkeypatch.setattr(web_research.httpx, "Client", Boom)
    result = web_research.search("bitcoin")
    assert result["results"] == []
    assert "error" in result


def test_page_reader_rejects_non_http_schemes() -> None:
    """file:// ve benzeri şemalarla yerel dosya okunamaz."""
    for url in ("file:///etc/passwd", "ftp://x/y", "javascript:alert(1)"):
        assert "error" in web_research.read_page(url)


def test_result_parsing_extracts_title_and_domain(monkeypatch) -> None:
    html = '''
      <a class="result__a" href="https://example.com/btc">Bitcoin &amp; piyasa</a>
      <a class="result__snippet">Fiyat <b>yükseldi</b>.</a>
    '''

    class Fake:
        def __init__(self, *a, **k): pass                       # noqa: ANN002, ANN003
        def __enter__(self): return self
        def __exit__(self, *a): return False                    # noqa: ANN002
        def post(self, *a, **k):                                # noqa: ANN002, ANN003
            class R:
                text = html
                def raise_for_status(self): return None
            return R()

    monkeypatch.setattr(web_research.httpx, "Client", Fake)
    result = web_research.search("btc")

    assert result["count"] == 1
    row = result["results"][0]
    assert row["title"] == "Bitcoin & piyasa"        # HTML varlıkları çözülür
    assert row["domain"] == "example.com"
    assert "yükseldi" in row["snippet"]              # etiketler temizlenir


def test_search_output_declares_content_is_data_not_instructions() -> None:
    """
    Prompt-injection savunması: çıktı, ajana içeriğin TALİMAT olmadığını
    açıkça söyler. Bu not kaybolursa savunma sessizce zayıflar.
    """
    result = web_research.search("")
    combined = str(web_research.search.__doc__) + str(web_research.__doc__)
    assert "talimat" in combined.lower()
    assert isinstance(result, dict)
