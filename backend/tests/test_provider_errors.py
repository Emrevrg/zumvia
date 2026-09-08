"""
SAĞLAYICI HATALARI — kullanıcı ne olduğunu ve ne yapacağını bilmeli

Yaşanmış arıza: Google, geçersiz API anahtarına 401 değil **400** döndürür ve
gerçek açıklamayı yanıt GÖVDESİNDE yazar ("Please pass a valid API key").
Sistem gövdeyi atıyor, yalnızca "Client error '400 Bad Request'" saklıyordu.

Sonuç iki katmanlı bir başarısızlıktı:
  1. Kullanıcı ham HTTP metni görüyor, ne yapacağını bilemiyordu.
  2. Hata "bilinmeyen" sayıldığı için otomatik sağlayıcı devri de çalışmıyordu
     — oysa bu, devrin en açık gerekçesi.
"""
from __future__ import annotations

import httpx
import pytest

from app.layers.l3_llm_gateway import _http_detail
from app.layers.model_errors import explain


def _error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError(
        f"Client error '{status} Bad Request' for url '{request.url}'",
        request=request, response=response)


# --------------------------------------------------------------------------- #
#  Gövde korunmalı
# --------------------------------------------------------------------------- #

def test_provider_explanation_survives_into_the_error_text() -> None:
    """Asıl bilgi gövdededir; atılırsa geri getirilemez."""
    detail = _http_detail(_error(400, '{"error":{"message":"Please pass a valid API key"}}'))
    assert "400" in detail
    assert "valid API key" in detail


def test_error_without_a_body_still_reads_sensibly() -> None:
    detail = _http_detail(RuntimeError("bağlantı koptu"))
    assert "bağlantı koptu" in detail


def test_detail_is_bounded() -> None:
    """Devasa bir gövde, kaydı ve arayüzü boğmamalı."""
    detail = _http_detail(_error(500, "x" * 10_000))
    assert len(detail) <= 600


# --------------------------------------------------------------------------- #
#  Doğru sınıflandırma
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("status,body", [
    (400, '{"error":{"message":"Please pass a valid API key"}}'),   # Google
    (401, "Unauthorized"),                                          # çoğu sağlayıcı
    (403, "API key not valid"),                                     # bazı vekiller
    (400, '{"error":{"code":"api_key_invalid"}}'),
    (400, "Incorrect API key provided"),                            # OpenAI uyumlu
])
def test_authentication_failures_are_recognised(status: int, body: str) -> None:
    """
    Kimlik hatası, HANGİ kodla gelirse gelsin tanınmalı.

    Yalnızca 401 beklemek, Google kullanıcılarını çözümsüz bir hata
    ekranıyla baş başa bırakıyordu.
    """
    problem = explain(_http_detail(_error(status, body)), provider="Test")
    assert problem.code == "AUTH", f"{status} {body} → {problem.code}"
    assert "anahtar" in problem.message.lower(), "kullanıcıya ne yapacağı söylenmedi"


def test_auth_failure_triggers_provider_failover() -> None:
    """
    Anahtar geçersizse başka sağlayıcıya geçilmeli.

    Bu, devrin en açık gerekçesidir: aynı anahtarla tekrar denemenin hiçbir
    faydası yoktur.
    """
    from app.agent import controller

    problem = explain(_http_detail(_error(400, "Please pass a valid API key")))
    assert problem.code in controller._PROVIDER_FAULTS


def test_a_real_bad_request_is_not_mistaken_for_an_auth_error() -> None:
    """
    Her 400 kimlik hatası değildir.

    Aşırı geniş bir eşleşme, bozuk istekleri "anahtarınız yanlış" diye
    gösterip kullanıcıyı yanlış yere yönlendirirdi.
    """
    problem = explain(_http_detail(_error(400, '{"error":"max_tokens too large"}')))
    assert problem.code != "AUTH"


def test_raw_http_code_never_reaches_the_user() -> None:
    """Kullanıcıya gösterilen metin eyleme dönüştürülebilir olmalı."""
    problem = explain(_http_detail(_error(400, "Please pass a valid API key")),
                      provider="Google Gemini")
    assert "Bad Request" not in problem.message
    assert "Kasa" in problem.message, "nereye gideceği söylenmedi"
