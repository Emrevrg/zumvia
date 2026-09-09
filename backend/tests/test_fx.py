"""
DÖVİZ ÇEVRİM KATMANI — sözleşme testleri

Korunan kurallar:

    * `rate()` 60 sn önbelleklidir; ikinci çağrı ağa çıkmaz.
    * Çevrilemeyen satır sessizce atlanmaz: `unconverted` içinde
      gerekçesiyle raporlanır.
    * Kur alınamazsa istisna sızar ama tur ölmez (satıra yazılır).
    * Bu dosya internetsiz çalışır — yfinance taklit edilir.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.layers import finance_hub as hub
from app.layers import fx


@pytest.fixture(autouse=True)
def _temiz_onbellek():
    hub.clear_cache()
    yield
    hub.clear_cache()


class SahteHizli:
    def __init__(self, fiyat):
        self.last_price = fiyat


class SahteTicker:
    """Ağ yapmayan ticker taklidi: hızlı fiyat ya da geçmiş verir."""

    def __init__(self, *, hizli=None, gecmis=None, hata: str = ""):
        self.fast_info = SahteHizli(hizli) if hizli is not None else None
        self._gecmis = gecmis
        self._hata = hata

    def history(self, *args, **kwargs):
        if self._hata:
            raise RuntimeError(self._hata)
        return self._gecmis


def gecmis_cerceve(son: float) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    return pd.DataFrame({"Close": [son - 1.0, son - 0.5, son],
                         "Volume": [1.0, 1.0, 1.0]}, index=idx)


# --------------------------------------------------------------------------- #
#  Kur okuma
# --------------------------------------------------------------------------- #

def test_ayni_para_aga_cikmaz() -> None:
    assert fx.rate("USD", "USD") == pytest.approx(1.0)
    assert fx.rate("usd", "Usd") == pytest.approx(1.0)


def test_bos_para_kodu_reddedilir() -> None:
    with pytest.raises(ValueError):
        fx.rate("", "TRY")
    with pytest.raises(ValueError):
        fx.normalize_portfolio([{"amount": 1, "currency": "USD"}], "")


def test_dogrudan_parite_mutlu_yol(monkeypatch) -> None:
    """Doğrudan parite varsa ters yöne bakılmaz."""
    monkeypatch.setattr(fx, "_ticker_close", lambda ticker: 32.5)
    kur = fx.rate("USD", "TRY")
    assert kur == pytest.approx(32.5)


def test_ters_parite_cevrilip_kullanilir(monkeypatch) -> None:
    """TRYUSD=X yoktur; USDTRY=X'in tersi alınır."""
    degerler = {"USDTRY=X": 40.0}

    def sahte_kapanis(ticker):
        sembol = getattr(ticker, "_sembol", "")
        return degerler.get(sembol)

    class Sembollu:
        def __init__(self, sembol):
            self._sembol = sembol

    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", Sembollu)
    monkeypatch.setattr(fx, "_ticker_close", sahte_kapanis)
    kur = fx._fetch_rate_uncached("TRY", "USD")
    assert kur == pytest.approx(1 / 40.0)


def test_iki_yon_de_bossa_hata_verir(monkeypatch) -> None:
    import yfinance as yf

    monkeypatch.setattr(yf, "Ticker", lambda s: SahteTicker(hizli=None,
                                                            gecmis=None))
    monkeypatch.setattr(fx, "_ticker_close", lambda ticker: None)
    with pytest.raises(RuntimeError):
        fx._fetch_rate_uncached("USD", "XYZ")


def test_ters_parite_hatasi_gerekceye_yansir(monkeypatch) -> None:
    import yfinance as yf

    def patlayan(sembol):
        raise RuntimeError("ağ koptu")

    monkeypatch.setattr(yf, "Ticker", patlayan)
    with pytest.raises(RuntimeError):
        fx._fetch_rate_uncached("USD", "TRY")


def test_onbellek_isabeti_aga_cikmaz(monkeypatch) -> None:
    cagrilar = {"adet": 0}

    def sayan(base, quote):
        cagrilar["adet"] += 1
        return 32.0

    monkeypatch.setattr(fx, "_fetch_rate_uncached", sayan)
    assert fx.rate("USD", "TRY") == pytest.approx(32.0)
    assert fx.rate("USD", "TRY") == pytest.approx(32.0)
    assert cagrilar["adet"] == 1


# --------------------------------------------------------------------------- #
#  Kapanış okuma
# --------------------------------------------------------------------------- #

def test_hizli_fiyat_once_okunur() -> None:
    assert fx._ticker_close(SahteTicker(hizli=10.5)) == pytest.approx(10.5)


def test_gecmis_kapanis_yedegi() -> None:
    ticker = SahteTicker(hizli=None, gecmis=gecmis_cerceve(32.5))
    assert fx._ticker_close(ticker) == pytest.approx(32.5)


def test_bos_gecmis_none_doner() -> None:
    assert fx._ticker_close(SahteTicker(hizli=None, gecmis=None)) is None
    assert fx._ticker_close(SahteTicker(hizli=None,
                                        gecmis=pd.DataFrame())) is None
    assert fx._ticker_close(SahteTicker(hizli=None, gecmis=gecmis_cerceve(-5))) is None


def test_gecmis_hatasi_none_doner() -> None:
    assert fx._ticker_close(SahteTicker(hizli=None, gecmis=None,
                                        hata="ağ koptu")) is None


# --------------------------------------------------------------------------- #
#  Çevrim
# --------------------------------------------------------------------------- #

def test_ceviri_kuru_carpar(monkeypatch) -> None:
    monkeypatch.setattr(fx, "_fetch_rate_uncached", lambda b, q: 32.0)
    assert fx.convert(100, "USD", "TRY") == pytest.approx(3200.0)


def test_gecersiz_tutar_reddedilir(monkeypatch) -> None:
    monkeypatch.setattr(fx, "_fetch_rate_uncached", lambda b, q: 32.0)
    with pytest.raises(ValueError):
        fx.convert("yüz", "USD", "TRY")
    with pytest.raises(ValueError):
        fx.convert(float("inf"), "USD", "TRY")


# --------------------------------------------------------------------------- #
#  Portföy normalizasyonu
# --------------------------------------------------------------------------- #

def test_portfoy_toplami_tek_paraya_iner(monkeypatch) -> None:
    kurlar = {("USD", "TRY"): 30.0, ("EUR", "TRY"): 33.0, ("TRY", "TRY"): 1.0}

    def sahte_uncached(base, quote):
        return kurlar[(base, quote)]

    monkeypatch.setattr(fx, "_fetch_rate_uncached", sahte_uncached)
    satirlar = [
        {"amount": 100.0, "currency": "USD"},
        {"value": 100.0, "ccy": "EUR"},
        {"market_value": 500.0, "currency_code": "TRY"},
    ]
    veri = fx.normalize_portfolio(satirlar, "TRY")
    assert veri["complete"] is True
    assert veri["unconverted"] == []
    assert veri["total"] == pytest.approx(100 * 30.0 + 100 * 33.0 + 500.0)
    assert veri["target_ccy"] == "TRY"
    assert veri["source"] == "yfinance"
    assert veri["fetched_at"]


def test_cevrilemeyen_satir_gerekceyle_raporlanir(monkeypatch) -> None:
    def secici(base, quote):
        if base == "USD":
            return 30.0
        raise RuntimeError("kur yok")

    monkeypatch.setattr(fx, "_fetch_rate_uncached", secici)
    satirlar = [
        {"amount": 100.0, "currency": "USD"},
        {"amount": 50.0, "currency": "XYZ"},
        {"currency": "USD"},                       # tutar yok
        {"amount": 10.0},                           # para kodu yok
        "düz metin satır",                          # sözlük değil
        {"amount": "bozuk", "currency": "USD"},     # sayı değil
    ]
    veri = fx.normalize_portfolio(satirlar, "TRY")
    assert veri["complete"] is False
    assert veri["converted_count"] == 1
    assert veri["unconverted_count"] == 5
    assert veri["total"] == pytest.approx(3000.0)
    assert all(r["reason"] for r in veri["unconverted"])


def test_bos_liste_sifir_toplam() -> None:
    veri = fx.normalize_portfolio([], "USD")
    assert veri["total"] == pytest.approx(0.0)
    assert veri["complete"] is True


def test_none_liste_bos_sayilir() -> None:
    veri = fx.normalize_portfolio(None, "USD")
    assert veri["total"] == pytest.approx(0.0)


def test_satir_alan_adlari_esnek_okunur() -> None:
    tutar, sorun = fx._row_amount({"notional": 25.0, "quote": "EUR"})
    assert tutar == pytest.approx(25.0)
    assert sorun == ""
    tutar, sorun = fx._row_amount({"total": 7.0, "para_birimi": "try"})
    assert tutar == pytest.approx(7.0)
