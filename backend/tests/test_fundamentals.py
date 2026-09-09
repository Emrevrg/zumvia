"""
TEMEL ANALİZ KATMANI — sözleşme testleri

Korunan kurallar:

    * Kripto için "uygulanamaz": sessizce boş değil, açıkça
      `available: False` + gerekçe.
    * Ağ hatası veri yokluğu DEĞİLDİR: istisna sızmaz.
    * Veri eksikse alan None kalır; ASLA 0 ile doldurulmaz.
    * Önbellek isabeti ağa ÇIKMAZ (ikinci çağrı ölçülür).
    * Tüm ağ erişimi `yfinance` taklidiyle kapatılır — bu dosya
      internetsiz çalışmak zorundadır.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from app.layers import finance_hub as hub
from app.layers import fundamentals as fund
from app.layers.finance_hub import Instrument


@pytest.fixture(autouse=True)
def _temiz_onbellek():
    hub.clear_cache()
    yield
    hub.clear_cache()


def hisse(symbol: str = "AAPL") -> Instrument:
    return Instrument(symbol, "stock", "yahoo", symbol, "hisse")


def kripto(symbol: str = "BTC/USDT") -> Instrument:
    return Instrument(symbol, "crypto", "binance", symbol, "kripto")


TAM_BILGI = {
    "longName": "Apple Inc.", "sector": "Technology", "industry": "Consumer Electronics",
    "currency": "USD", "marketCap": 3_000_000_000_000, "trailingPE": 28.5,
    "forwardPE": 25.1, "priceToBook": 45.2, "enterpriseToEbitda": 22.0,
    "dividendYield": 0.005, "trailingEps": 6.4, "returnOnEquity": 1.47,
    "debtToEquity": 1.75, "grossMargins": 0.43, "profitMargins": 0.25,
    "payoutRatio": 0.15, "revenueGrowth": 0.08, "earningsGrowth": 0.12,
}


class SahteTicker:
    """Ağ yapmayan yfinance taklidi; her öznitelik enjekte edilir."""

    def __init__(self, *, info=None, gelir=None, bilanco=None,
                 takvim=None, tarihler=None, temettu=None, hata: str = ""):
        self._info = info
        self.quarterly_income_stmt = gelir
        self.quarterly_balance_sheet = bilanco
        self.calendar = takvim
        self.earnings_dates = tarihler
        self.dividends = temettu
        self._hata = hata

    @property
    def info(self):
        if self._hata:
            raise RuntimeError(self._hata)
        return self._info

    def get_info(self):
        if self._hata:
            raise RuntimeError(self._hata)
        return self._info


def yamala(monkeypatch, ticker: SahteTicker):
    monkeypatch.setattr(fund, "_get_ticker", lambda symbol: ticker)


def gelir_cercevesi() -> pd.DataFrame:
    donemler = ["2024-04-01", "2024-01-01", "2023-10-01", "2023-07-01", "2023-04-01"]
    return pd.DataFrame(
        {
            donemler[0]: [100.0, 44.0, 30.0, 25.0],
            donemler[1]: [90.0, 40.0, 28.0, 22.0],
            donemler[2]: [85.0, 38.0, 26.0, 20.0],
            donemler[3]: [80.0, 36.0, 24.0, 19.0],
            donemler[4]: [70.0, 30.0, 20.0, 15.0],
        },
        index=["Total Revenue", "Gross Profit", "Operating Income", "Net Income"],
    )


def bilanco_cercevesi() -> pd.DataFrame:
    donemler = ["2024-04-01", "2024-01-01"]
    return pd.DataFrame(
        {
            donemler[0]: [400.0, 280.0, 120.0, 50.0, 90.0],
            donemler[1]: [380.0, 270.0, 110.0, 40.0, 85.0],
        },
        index=["Total Assets", "Total Liab", "Total Stockholder Equity",
               "Cash And Cash Equivalents", "Total Debt"],
    )


# --------------------------------------------------------------------------- #
#  Yardımcılar
# --------------------------------------------------------------------------- #

def test_guvenli_sayi_boslugu_sifirla_doldurmaz() -> None:
    """
    0 GERÇEK bir değerdir; yokluk değildir.

    Eksik/bozuk giriş None olur. 0 giriş 0.0 kalır — ikisi karışırsa karar
    katmanı "bedava şirket" sanır.
    """
    assert fund._safe_float(None) is None
    assert fund._safe_float(float("nan")) is None
    assert fund._safe_float(float("inf")) is None
    assert fund._safe_float("ölçülemedi") is None
    assert fund._safe_float(0) == 0.0
    assert fund._safe_float(0.0) == 0.0
    assert fund._safe_float("28.5") == pytest.approx(28.5)


def test_bilgi_iki_yoldan_da_okunur(monkeypatch) -> None:
    """.info boşsa .get_info denenir; ikisi de boşsa boş sözlük."""

    class Cagrilabilir:
        info = lambda self: dict(TAM_BILGI)  # noqa: E731

    monkeypatch.setattr(fund, "_get_ticker", lambda s: Cagrilabilir())
    assert fund._get_info(Cagrilabilir())["trailingPE"] == pytest.approx(28.5)

    class Bos:
        def __init__(self):
            self.info: dict = {}

        def get_info(self):
            return {}

    assert fund._get_info(Bos()) == {}

    class Bozuk:
        @property
        def info(self):
            raise RuntimeError("ağ koptu")

        def get_info(self):
            raise RuntimeError("ağ koptu")

    assert fund._get_info(Bozuk()) == {}


def test_gercek_ticker_uretimi_ag_yapmaz() -> None:
    """Tek giriş noktası sembolü doğru taşır (nesne kurmak ağa çıkmaz)."""
    ticker = fund._get_ticker("AAPL")
    assert ticker.ticker == "AAPL"


# --------------------------------------------------------------------------- #
#  Değerleme fotoğrafı
# --------------------------------------------------------------------------- #

def test_mutlu_yol_tum_oranlari_dondurur(monkeypatch) -> None:
    yamala(monkeypatch, SahteTicker(info=dict(TAM_BILGI)))
    veri = fund.snapshot(hisse())
    assert veri["available"] is True
    assert veri["market_cap"] == pytest.approx(3_000_000_000_000)
    assert veri["trailing_pe"] == pytest.approx(28.5)
    assert veri["forward_pe"] == pytest.approx(25.1)
    assert veri["price_to_book"] == pytest.approx(45.2)
    assert veri["ev_to_ebitda"] == pytest.approx(22.0)
    assert veri["dividend_yield"] == pytest.approx(0.005)
    assert veri["eps_trailing"] == pytest.approx(6.4)
    assert veri["roe"] == pytest.approx(1.47)
    assert veri["debt_to_equity"] == pytest.approx(1.75)
    assert veri["gross_margin"] == pytest.approx(0.43)
    assert veri["net_margin"] == pytest.approx(0.25)
    assert veri["source"] == "yfinance"
    assert veri["fetched_at"]
    assert veri["age_seconds"] == 0.0


def test_eksik_alan_none_kalir_sifir_yazilmaz(monkeypatch) -> None:
    """Veri eksikse alan None; asla 0 ile doldurulmaz."""
    yamala(monkeypatch, SahteTicker(info={"longName": "Eksik A.Ş."}))
    veri = fund.snapshot(hisse("EKSK"))
    assert veri["available"] is True
    assert veri["trailing_pe"] is None
    assert veri["market_cap"] is None
    assert veri["net_margin"] is None


def test_sifir_gercek_degerdir_korunur(monkeypatch) -> None:
    """Temettü vermeyen şirketin verimi 0.0'dır, None değil."""
    bilgi = dict(TAM_BILGI, dividendYield=0.0)
    yamala(monkeypatch, SahteTicker(info=bilgi))
    veri = fund.snapshot(hisse())
    assert veri["dividend_yield"] == 0.0


def test_kripto_icin_uygulanamaz_yolu() -> None:
    """Kriptonun bilançosu yoktur; açıkça uygulanamaz döner."""
    veri = fund.snapshot(kripto())
    assert veri["available"] is False
    assert veri["reason"]


def test_ag_hatasi_istisna_sizdirmaz(monkeypatch) -> None:
    """Ağ hatası veri yokluğu DEĞİLDİR: available=False + gerekçe."""
    yamala(monkeypatch, SahteTicker(info={}, hata="bağlantı koptu"))
    veri = fund.snapshot(hisse())
    assert veri["available"] is False
    assert veri["reason"]


def test_bos_bilgi_bulunamadi_doner(monkeypatch) -> None:
    yamala(monkeypatch, SahteTicker(info={}))
    veri = fund.snapshot(hisse("YOK"))
    assert veri["available"] is False
    assert veri["reason"]


def test_onbellek_isabeti_aga_cikmaz(monkeypatch) -> None:
    """İkinci çağrı ağa çıkmıyor: sayaç birde kalır."""
    cagrilar = {"adet": 0}

    def sayan(symbol):
        cagrilar["adet"] += 1
        return SahteTicker(info=dict(TAM_BILGI))

    monkeypatch.setattr(fund, "_get_ticker", sayan)
    ilk = fund.snapshot(hisse())
    ikinci = fund.snapshot(hisse())
    assert cagrilar["adet"] == 1
    assert ikinci["trailing_pe"] == ilk["trailing_pe"]
    assert ikinci["age_seconds"] >= 0.0


# --------------------------------------------------------------------------- #
#  Gelir tablosu
# --------------------------------------------------------------------------- #

def test_gelir_tablosu_buyume_yuzdeleri(monkeypatch) -> None:
    yamala(monkeypatch, SahteTicker(info=dict(TAM_BILGI), gelir=gelir_cercevesi()))
    veri = fund.income_statement(hisse(), periods=5)
    assert veri["available"] is True
    donemler = {r["period"]: r for r in veri["periods"]}
    yeni = donemler["2024-04-01"]
    assert yeni["revenue"] == pytest.approx(100.0)
    assert yeni["net_income"] == pytest.approx(25.0)
    # QoQ: (100-90)/90 = %11.11
    assert yeni["revenue_qoq_pct"] == pytest.approx(11.11, abs=0.01)
    # YoY: (100-70)/70 = %42.86
    assert yeni["revenue_yoy_pct"] == pytest.approx(42.86, abs=0.01)
    assert yeni["net_yoy_pct"] == pytest.approx(66.67, abs=0.01)
    # En eski dönemin büyümesi tanımsızdır (önceki yok).
    eski = donemler["2023-04-01"]
    assert eski["revenue_qoq_pct"] is None
    assert eski["revenue_yoy_pct"] is None


def test_gelir_tablosu_kripto_ag_hatasi_bos(monkeypatch) -> None:
    assert fund.income_statement(kripto())["available"] is False

    yamala(monkeypatch, SahteTicker(info={}, hata="ağ koptu"))
    assert fund.income_statement(hisse())["available"] is False

    yamala(monkeypatch, SahteTicker(info=dict(TAM_BILGI), gelir=None))
    assert fund.income_statement(hisse())["available"] is False

    yamala(monkeypatch, SahteTicker(info=dict(TAM_BILGI),
                                    gelir=pd.DataFrame()))
    assert fund.income_statement(hisse())["available"] is False


def test_buyume_baz_sifirsa_tanimsizdir() -> None:
    """Baz sıfırsa oran yazılmaz; sonsuz YAZILMAZ."""
    assert fund._growth(10.0, 0) is None
    assert fund._growth(None, 5.0) is None
    assert fund._growth(5.0, None) is None
    assert fund._growth(10.0, 5.0) == pytest.approx(100.0)


def test_cerceve_bozuk_girdiyle_bos_doner() -> None:
    assert fund._frame_to_periods(None, fund._INCOME_ROWS, 4) == []
    assert fund._frame_to_periods(pd.DataFrame(), fund._INCOME_ROWS, 4) == []

    class Bozuk:
        empty = False

        @property
        def columns(self):
            raise RuntimeError("bozuk")

    assert fund._frame_to_periods(Bozuk(), fund._INCOME_ROWS, 4) == []


# --------------------------------------------------------------------------- #
#  Bilanço
# --------------------------------------------------------------------------- #

def test_bilanco_net_borc_hesabi(monkeypatch) -> None:
    """Net borç = toplam borç − nakit; Python hesaplar, model değil."""
    yamala(monkeypatch, SahteTicker(info=dict(TAM_BILGI), bilanco=bilanco_cercevesi()))
    veri = fund.balance_sheet(hisse(), periods=2)
    assert veri["available"] is True
    yeni = veri["periods"][0]
    assert yeni["assets"] == pytest.approx(400.0)
    assert yeni["liabilities"] == pytest.approx(280.0)
    assert yeni["equity"] == pytest.approx(120.0)
    assert yeni["cash"] == pytest.approx(50.0)
    assert yeni["net_debt"] == pytest.approx(40.0)


def test_bilanco_nakit_yoksa_net_borc_none(monkeypatch) -> None:
    cerceve = pd.DataFrame(
        {"2024-04-01": [400.0, 280.0, 120.0, None, 90.0]},
        index=["Total Assets", "Total Liab", "Total Stockholder Equity",
               "Cash And Cash Equivalents", "Total Debt"],
    )
    yamala(monkeypatch, SahteTicker(info=dict(TAM_BILGI), bilanco=cerceve))
    veri = fund.balance_sheet(hisse("NAKT"))
    assert veri["available"] is True
    assert veri["periods"][0]["net_debt"] is None


def test_bilanco_kripto_ag_hatasi_bos(monkeypatch) -> None:
    assert fund.balance_sheet(kripto())["available"] is False
    yamala(monkeypatch, SahteTicker(info={}, hata="ağ koptu"))
    assert fund.balance_sheet(hisse())["available"] is False
    yamala(monkeypatch, SahteTicker(info=dict(TAM_BILGI), bilanco=None))
    assert fund.balance_sheet(hisse())["available"] is False


# --------------------------------------------------------------------------- #
#  Bilanço takvimi
# --------------------------------------------------------------------------- #

def test_bilanco_takvimi_surpriz_yuzdesi(monkeypatch) -> None:
    tarihler = pd.DataFrame(
        {"Reported EPS": [1.10, 0.90], "Estimated EPS": [1.00, 1.00]},
        index=["2024-04-01", "2024-01-01"],
    )
    takvim = {"Earnings Date": ["2024-07-25"], "EPS Estimate": 1.20}
    yamala(monkeypatch, SahteTicker(info=dict(TAM_BILGI), takvim=takvim,
                                    tarihler=tarihler))
    veri = fund.earnings_calendar(hisse())
    assert veri["available"] is True
    assert veri["next_earnings_date"] == "2024-07-25"
    assert veri["expected_eps"] == pytest.approx(1.20)
    assert veri["last_surprises"][0]["surprise_pct"] == pytest.approx(10.0)
    assert veri["last_surprises"][1]["surprise_pct"] == pytest.approx(-10.0)


def test_takvim_dataframe_bicimi_de_okunur(monkeypatch) -> None:
    takvim = pd.DataFrame(
        {"deger": ["2024-07-25", 1.30]},
        index=["Earnings Date", "EPS Estimate"],
    )
    yamala(monkeypatch, SahteTicker(info=dict(TAM_BILGI), takvim=takvim,
                                    tarihler=pd.DataFrame()))
    veri = fund.earnings_calendar(hisse("DFRM"))
    assert veri["available"] is True
    assert veri["next_earnings_date"] == "2024-07-25"


def test_takvim_kripto_ag_hatasi(monkeypatch) -> None:
    assert fund.earnings_calendar(kripto())["available"] is False

    def kopuk(symbol):
        raise RuntimeError("ağ koptu")

    monkeypatch.setattr(fund, "_get_ticker", kopuk)
    assert fund.earnings_calendar(hisse())["available"] is False


def test_surpriz_baz_sifirsa_tanimsizdir() -> None:
    assert fund._surprise_pct(1.0, 0) is None
    assert fund._surprise_pct(None, 1.0) is None
    assert fund._surprise_pct(1.1, 1.0) == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
#  Temettü
# --------------------------------------------------------------------------- #

def test_temettu_son_odemeler_ve_oranlar(monkeypatch) -> None:
    # Artan yıllık ödemeler: hem son-8 listesi hem artış serisi dolar.
    idx = pd.date_range("2016-01-01", periods=10, freq="YE")
    seri = pd.Series([1.00 + 0.10 * i for i in range(10)], index=idx)
    yamala(monkeypatch, SahteTicker(info=dict(TAM_BILGI), temettu=seri))
    veri = fund.dividends(hisse())
    assert veri["available"] is True
    assert len(veri["last_payments"]) == 8
    assert veri["yield"] == pytest.approx(0.005)
    assert veri["payout_ratio"] == pytest.approx(0.15)
    assert veri["increase_streak_years"] >= 1


def test_temettu_kripto_ag_hatasi(monkeypatch) -> None:
    assert fund.dividends(kripto())["available"] is False

    def kopuk(symbol):
        raise RuntimeError("ağ koptu")

    monkeypatch.setattr(fund, "_get_ticker", kopuk)
    assert fund.dividends(hisse())["available"] is False


def test_temettu_serisi_bossa_da_dayanir(monkeypatch) -> None:
    yamala(monkeypatch, SahteTicker(info=dict(TAM_BILGI), temettu=None))
    veri = fund.dividends(hisse("BOSS"))
    assert veri["available"] is True
    assert veri["last_payments"] == []


# --------------------------------------------------------------------------- #
#  Emsaller
# --------------------------------------------------------------------------- #

def test_emsaller_medyanla_kiyaslar(monkeypatch) -> None:
    bilgi = dict(TAM_BILGI, trailingPE=20.0)

    def emsal_bilgi(symbol):
        degerler = {"MSFT": 30.0, "AAPL": 28.0, "NVDA": 35.0}
        if symbol not in degerler:
            return {}
        return {"longName": symbol, "trailingPE": degerler[symbol],
                "revenueGrowth": 0.1, "earningsGrowth": 0.1}

    monkeypatch.setattr(fund, "_get_ticker", lambda s: SahteTicker(info=dict(bilgi)))
    monkeypatch.setattr(fund, "_peer_info", emsal_bilgi)
    veri = fund.peers(hisse("ZZZ"), limit=3)
    assert veri["available"] is True
    assert veri["sector"] == "Technology"
    assert len(veri["peers"]) == 3
    assert veri["median_peer_pe"] == pytest.approx(30.0)
    assert veri["cheaper_than_median"] is True
    assert veri["own_trailing_pe"] == pytest.approx(20.0)


def test_emsal_verisi_gelemeyen_satir_none_tasir(monkeypatch) -> None:
    monkeypatch.setattr(fund, "_get_ticker",
                        lambda s: SahteTicker(info=dict(TAM_BILGI)))
    monkeypatch.setattr(fund, "_peer_info", lambda s: {})
    veri = fund.peers(hisse("YYY"), limit=2)
    assert veri["available"] is True
    assert all(r["trailing_pe"] is None for r in veri["peers"])
    assert veri["median_peer_pe"] is None
    assert veri["cheaper_than_median"] is None


def test_emsal_kripto_ag_hatasi(monkeypatch) -> None:
    assert fund.peers(kripto())["available"] is False

    def kopuk(symbol):
        raise RuntimeError("ağ koptu")

    monkeypatch.setattr(fund, "_get_ticker", kopuk)
    assert fund.peers(hisse())["available"] is False


def test_emsal_bilgi_hatasi_bos_sozluk(monkeypatch) -> None:
    monkeypatch.setattr(fund, "_get_ticker", lambda s: 1 / 0)
    assert fund._peer_info("MSFT") == {}


def test_sektor_bilinmiyorsa_varsayilan_evren(monkeypatch) -> None:
    bilgi = {"longName": "Bilinmez", "trailingPE": 10.0}
    monkeypatch.setattr(fund, "_get_ticker", lambda s: SahteTicker(info=dict(bilgi)))
    monkeypatch.setattr(fund, "_peer_info",
                        lambda s: {"trailingPE": 12.0, "revenueGrowth": 0.05,
                                   "earningsGrowth": 0.05})
    veri = fund.peers(hisse("BLN"), limit=2)
    assert veri["available"] is True
    assert veri["sector"] is None
    assert len(veri["peers"]) == 2


def test_nan_marjin_none_olur(monkeypatch) -> None:
    bilgi = dict(TAM_BILGI, profitMargins=math.nan, grossMargins=float("inf"))
    yamala(monkeypatch, SahteTicker(info=bilgi))
    veri = fund.snapshot(hisse("NAN"))
    assert veri["net_margin"] is None
    assert veri["gross_margin"] is None
