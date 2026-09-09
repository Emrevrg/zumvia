"""
MAKRO REJİM KATMANI — sözleşme testleri

Korunan kurallar:

    * `regime()` saf çıktı üretir: emir/DB yazımı YOKTUR.
    * Getiri eğrisi tersine döndüğünde bayrak DOĞRU verilir.
    * Ağ hatası istisna sızdırmaz; `available: False` + gerekçe.
    * Eşikler adlandırılmış sabittir (gövdede sihirli sayı yok).
    * Bu dosya internetsiz çalışır — yfinance taklit edilir.
"""
from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

from app.layers import finance_hub as hub
from app.layers import macro


@pytest.fixture(autouse=True)
def _temiz_onbellek():
    hub.clear_cache()
    yield
    hub.clear_cache()


def alinti(fiyat=None, degisim=None) -> dict:
    return {"price": fiyat, "change_pct": degisim}


def normal_piyasa() -> dict:
    """Sakin piyasa: eğri normal, VIX düşük, dolar zayıf."""
    return {
        "^IRX": alinti(2.0, 0.0),
        "^FVX": alinti(3.0, 0.0),
        "^TNX": alinti(4.0, 0.0),
        "^TYX": alinti(4.5, 0.0),
        "DX-Y.NYB": alinti(97.0, -0.1),
        "^VIX": alinti(14.0, -1.0),
        "GC=F": alinti(2000.0, 0.2),
        "BZ=F": alinti(80.0, 0.5),
        "HG=F": alinti(4.0, 0.3),
    }


def notr_piyasa() -> dict:
    """
    Nötr zemin: hiçbir gösterge oy vermez.

    Tek sinyal testleri buradan başlar; yoksa taban oylar sonucu bulandırır
    (örn. zayıf doların risk_on oyu, test edilen risk_off sinyalini ezer).
    """
    return {
        "^IRX": alinti(2.0, 0.0),
        "^FVX": alinti(3.0, 0.0),
        "^TNX": alinti(4.0, 0.0),
        "^TYX": alinti(4.5, 0.0),
        "DX-Y.NYB": alinti(100.0, 0.0),
        "^VIX": alinti(20.0, 0.0),
        "GC=F": alinti(2000.0, 0.2),
        "BZ=F": alinti(80.0, 0.5),
        "HG=F": alinti(4.0, 0.3),
    }


# --------------------------------------------------------------------------- #
#  Saf sınıflandırma
# --------------------------------------------------------------------------- #

def test_tersine_donen_egri_dogru_bayrak_verir() -> None:
    """
    KRİTİK SENARYO: 10y orta vadenin ALTINA inerse eğri tersinedir.

    Bu bayrak ileride bot kararlarını etkileyecek; yanlış bayrak yanlış
    karar demektir.
    """
    alintilar = notr_piyasa()
    alintilar["^TNX"] = alinti(2.5, 0.0)   # 10y %2.5
    alintilar["^FVX"] = alinti(4.0, 0.0)   # orta vade %4.0 → eğim −1.5
    karar = macro.classify(alintilar)
    assert karar["indicators"]["curve_inverted"] is True
    assert karar["indicators"]["curve_slope_10y_mid"] == pytest.approx(-1.5)
    assert any("tersine" in g for g in karar["reasons"])
    # Nötr zeminde tek sinyal rejimi tek başına belirler.
    assert karar["regime"] == "risk_off"


def test_normal_egri_ters_bayragi_kaldirmaz() -> None:
    karar = macro.classify(normal_piyasa())
    assert karar["indicators"]["curve_inverted"] is False
    assert karar["indicators"]["curve_slope_10y_mid"] == pytest.approx(1.0)
    assert karar["regime"] == "risk_on"


def test_yuksek_vix_risk_off_uretir() -> None:
    alintilar = notr_piyasa()
    alintilar["^VIX"] = alinti(32.0, 5.0)
    karar = macro.classify(alintilar)
    assert karar["regime"] == "risk_off"
    assert any("VIX" in g for g in karar["reasons"])


def test_guclu_dolar_risk_off_oyu_verir() -> None:
    alintilar = normal_piyasa()
    alintilar["^VIX"] = alinti(20.0, 0.0)  # nötr bölgede tut
    alintilar["DX-Y.NYB"] = alinti(107.0, 0.5)
    alintilar["^IRX"] = alinti(5.5, 0.0)   # sıkı para
    alintilar["BZ=F"] = alinti(90.0, 4.0)  # petrol şoku
    alintilar["HG=F"] = alinti(3.5, -3.0)  # bakır çöküşü
    alintilar["GC=F"] = alinti(2100.0, 2.5)  # güvenli liman akını
    karar = macro.classify(alintilar)
    assert karar["regime"] == "risk_off"
    gerekceler = " ".join(karar["reasons"])
    assert "Dolar" in gerekceler
    assert "sıkı" in gerekceler
    assert "Petrol" in gerekceler or "petrol" in gerekceler
    assert "Bakır" in gerekceler or "bakır" in gerekceler
    assert "Altın" in gerekceler or "altın" in gerekceler


def test_karisik_gostergeler_belirsiz_doner() -> None:
    """Oy çıkmazsa rejim 'belirsiz'dir; iki yöne de savrulmaz."""
    alintilar = {s: alinti(None, None) for s in macro.MACRO_TICKERS}
    karar = macro.classify(alintilar)
    assert karar["regime"] == "belirsiz"
    assert karar["reasons"]


def test_uzun_vade_egimi_de_hesaplanir() -> None:
    karar = macro.classify(normal_piyasa())
    assert karar["indicators"]["curve_slope_30y_10y"] == pytest.approx(0.5)


def test_esikler_adlandirilmis_sabittir() -> None:
    """Eşikler modül tepesinde; gövdede sihirli sayı aranmaz."""
    assert macro.VIX_RISK_OFF == pytest.approx(25.0)
    assert macro.VIX_RISK_ON == pytest.approx(18.0)
    assert macro.DXY_STRONG == pytest.approx(105.0)
    assert macro.DXY_WEAK == pytest.approx(98.0)
    assert macro.CURVE_INVERSION == pytest.approx(0.0)


def test_guvenli_sayi_boslugu_sifirla_doldurmaz() -> None:
    assert macro._safe_float(None) is None
    assert macro._safe_float(float("nan")) is None
    assert macro._safe_float("ölçülemedi") is None
    assert macro._safe_float(0) == 0.0


# --------------------------------------------------------------------------- #
#  Veri indirme (yfinance taklidi)
# --------------------------------------------------------------------------- #

def _sahte_yfinance(kapanislar: dict, *, bos: bool = False):
    sahte = types.ModuleType("yfinance")
    if bos:
        sahte.download = lambda *a, **k: None
        return sahte

    def download(*args, **kwargs):
        cerceveler = {}
        for sembol, degerler in kapanislar.items():
            cerceveler[(sembol, "close")] = degerler
        kolonlar = pd.MultiIndex.from_tuples(
            [(s, "close") for s in kapanislar], names=["ticker", "field"])
        return pd.DataFrame(
            [[kapanislar[s][i] for s in kapanislar] for i in range(2)],
            columns=kolonlar,
        )

    sahte.download = download
    return sahte


def test_fiyat_indirme_tek_cagrida_dokuz_gosterge(monkeypatch) -> None:
    kapanislar = {s: [100.0, 102.0] for s in macro.MACRO_TICKERS}
    monkeypatch.setitem(sys.modules, "yfinance", _sahte_yfinance(kapanislar))
    alintilar = macro._fetch_prices()
    assert set(alintilar) == set(macro.MACRO_TICKERS)
    assert alintilar["^VIX"]["price"] == pytest.approx(102.0)
    assert alintilar["^VIX"]["change_pct"] == pytest.approx(2.0)


def test_bos_yanit_hata_yukseltir(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "yfinance", _sahte_yfinance({}, bos=True))
    with pytest.raises(RuntimeError):
        macro._fetch_prices()


def test_bozuk_sembol_digerlerini_dusurmez(monkeypatch) -> None:
    """Bir gösterge okunamazsa diğerleri yaşar; eksik None olur."""
    sahte = types.ModuleType("yfinance")

    def download(*args, **kwargs):
        # ^VIX kolonu bozuk tip taşır; diğerleri sağlam.
        kolonlar = pd.MultiIndex.from_tuples(
            [("^VIX", "close"), ("^TNX", "close")], names=["ticker", "field"])
        return pd.DataFrame([["bozuk", 4.0], ["bozuk", 4.1]], columns=kolonlar)

    sahte.download = download
    monkeypatch.setitem(sys.modules, "yfinance", sahte)
    alintilar = macro._fetch_prices()
    assert alintilar["^VIX"]["price"] is None
    assert alintilar["^TNX"]["price"] == pytest.approx(4.1)


# --------------------------------------------------------------------------- #
#  Rejim ucu
# --------------------------------------------------------------------------- #

def test_mutlu_yol_rejim_dondurur(monkeypatch) -> None:
    monkeypatch.setattr(macro, "_fetch_prices", lambda: normal_piyasa())
    veri = macro.regime()
    assert veri["available"] is True
    assert veri["regime"] == "risk_on"
    assert veri["reasons"]
    assert veri["source"] == "yfinance"
    assert veri["fetched_at"]
    assert "UYARI" in veri
    assert set(veri["quotes"]) == set(macro.MACRO_TICKERS)


def test_ag_hatasi_istisna_sizdirmaz(monkeypatch) -> None:
    def bozuk():
        raise RuntimeError("ağ koptu")

    monkeypatch.setattr(macro, "_fetch_prices", bozuk)
    veri = macro.regime()
    assert veri["available"] is False
    assert veri["reason"]


def test_onbellek_isabeti_aga_cikmaz(monkeypatch) -> None:
    cagrilar = {"adet": 0}

    def sayan():
        cagrilar["adet"] += 1
        return normal_piyasa()

    monkeypatch.setattr(macro, "_fetch_prices", sayan)
    ilk = macro.regime()
    ikinci = macro.regime()
    assert cagrilar["adet"] == 1
    assert ikinci["regime"] == ilk["regime"]
    assert ikinci["age_seconds"] >= 0.0


def test_eksik_gostergeyle_de_rejim_uretilir(monkeypatch) -> None:
    kismi = {"^VIX": alinti(30.0, 2.0)}
    monkeypatch.setattr(macro, "_fetch_prices", lambda: kismi)
    veri = macro.regime()
    assert veri["available"] is True
    assert veri["regime"] == "risk_off"
    assert veri["indicators"]["dxy"] is None
