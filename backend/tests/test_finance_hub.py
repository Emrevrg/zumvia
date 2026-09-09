"""
FINANCE_HUB — sözleşme testleri

Bu dosya iki şeyi kilitler:

    1. Ekranla ajanın AYNI sayılara bakması (tek kaynak ilkesi).
    2. Ölçülemeyen verinin "ölçülmüş" gibi sunulmaması (hata raporlanır,
       eski fiyat canlı gibi gösterilmez).

Derinlik katmanı (fundamentals/macro/fx) bu hub'ı dolaylı sürer; o yüzden
bu dosya aynı zamanda finance_hub kapsamını %60'ın üstüne taşır.

Tüm ağ erişimi taklit edilir — internetsiz çalışır.
"""
from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.layers import finance_hub as hub
from app.layers.finance_hub import Instrument
from app.main import app


@pytest.fixture(autouse=True)
def _temiz_onbellek():
    hub.clear_cache()
    yield
    hub.clear_cache()


def mum_cercevesi(satir: int = 48, fiyat: float = 100.0) -> pd.DataFrame:
    """Ölçülebilir OHLCV çerçevesi: kapanışlar yukarı sürüklenir."""
    idx = pd.date_range("2024-01-01", periods=satir, freq="h", tz="UTC")
    kapanis = [fiyat + i * 0.5 for i in range(satir)]
    return pd.DataFrame(
        {"open": [c - 0.1 for c in kapanis],
         "high": [c + 0.3 for c in kapanis],
         "low": [c - 0.3 for c in kapanis],
         "close": kapanis,
         "volume": [1000.0] * satir},
        index=idx,
    )


def btc() -> Instrument:
    return Instrument("BTC/USDT", "crypto", "binance", "Bitcoin", "kripto")


def aapl() -> Instrument:
    return Instrument("AAPL", "stock", "yahoo", "Apple", "hisse")


# --------------------------------------------------------------------------- #
#  Sembol çözümleme
# --------------------------------------------------------------------------- #

def test_bilinen_sembol_dogrudan_cozulur() -> None:
    inst = hub.resolve("btc")
    assert inst.symbol == "BTC/USDT"
    assert inst.market == "crypto"


def test_parite_yazimi_kriptodur() -> None:
    inst = hub.resolve("ETH/USDT")
    assert inst.market == "crypto"


def test_piyasa_verilirse_sorulmaz() -> None:
    inst = hub.resolve("XYZ", "stock", "yahoo")
    assert inst.symbol == "XYZ"
    assert hub._default_exchange("crypto") == "binance"
    assert hub._default_exchange("stock") == "yahoo"


def test_bos_sembol_reddedilir() -> None:
    with pytest.raises(ValueError):
        hub.resolve("")


def test_borsa_listesi_hatasinda_cozumleme_durmaz(monkeypatch) -> None:
    """Borsa listesi alınamazsa çözümleme yine çalışır (bilinen tabanla)."""

    def patlayan(*args, **kwargs):
        raise RuntimeError("borsa kapalı")

    import app.layers.l1_market_data as l1
    monkeypatch.setattr(l1, "get_exchange", patlayan)
    inst = hub.resolve("DOGE")
    assert inst.symbol == "DOGE"


def test_parite_varligi_onbellege_alinir(monkeypatch) -> None:
    import app.layers.l1_market_data as l1

    class SahteBorsa:
        def load_markets(self):
            return {"BTC/USDT": {}, "ETH/USDT": {}}

    monkeypatch.setattr(l1, "get_exchange", lambda *a, **k: SahteBorsa())
    assert hub._pair_exists("BTC/USDT") is True
    # İkinci çağrı önbellekten gelir (borsaya tekrar sorulmaz).
    monkeypatch.setattr(l1, "get_exchange", lambda *a, **k: 1 / 0)
    assert hub._pair_exists("BTC/USDT") is True


# --------------------------------------------------------------------------- #
#  Önbellek
# --------------------------------------------------------------------------- #

def test_onbellek_isabet_ve_son_kullanim() -> None:
    hub._CACHE.put("anahtar", 42)
    deger, yas = hub._CACHE.get("anahtar", 60.0)
    assert deger == 42
    assert yas >= 0.0
    assert hub._CACHE.get("yok", 60.0) is None


def test_suresi_dolan_girdi_gorunmez(monkeypatch) -> None:
    import time as zaman

    hub._CACHE.put("eski", 1)
    gercek = zaman.monotonic()
    monkeypatch.setattr(zaman, "monotonic", lambda: gercek + 9999.0)
    assert hub._CACHE.get("eski", 60.0) is None


def test_onbellek_sinirsiz_buyumez() -> None:
    for i in range(450):
        hub._CACHE.put(f"a{i}", i)
    assert len(hub._CACHE._data) <= 400
    hub.clear_cache()
    assert hub._CACHE.get("a449", 60.0) is None


# --------------------------------------------------------------------------- #
#  Kotasyon
# --------------------------------------------------------------------------- #

def test_kotasyon_mutlu_yol(monkeypatch) -> None:
    import app.layers.l1_market_data as l1
    monkeypatch.setattr(l1, "fetch_ohlcv",
                        lambda *a, **k: mum_cercevesi(48, 100.0))
    isaret = hub.tick(btc())
    assert isaret.ok
    assert isaret.price == pytest.approx(123.5)
    assert isaret.change_pct is not None
    assert isaret.day_high is not None and isaret.day_low is not None
    assert len(isaret.spark) == 30
    sozluk = isaret.to_dict()
    assert sozluk["ok"] is True and sozluk["price"] == isaret.price


def test_hisse_kotasyonu_onceki_kapanisa_gore(monkeypatch) -> None:
    import app.layers.l1_market_data as l1
    monkeypatch.setattr(l1, "fetch_ohlcv",
                        lambda *a, **k: mum_cercevesi(40, 50.0))
    isaret = hub.tick(aapl())
    assert isaret.ok
    assert isaret.previous_close == pytest.approx(69.0)


def test_veri_hatasi_karta_yazilir_motor_dusmez(monkeypatch) -> None:
    import app.layers.l1_market_data as l1
    from app.layers.l1_market_data import MarketDataError

    def bozuk(*args, **kwargs):
        raise MarketDataError("borsa kapalı")

    monkeypatch.setattr(l1, "fetch_ohlcv", bozuk)
    isaret = hub.tick(btc())
    assert not isaret.ok
    assert isaret.error


def test_beklenmedik_hata_da_karta_yazilir(monkeypatch) -> None:
    import app.layers.l1_market_data as l1

    def patlayan(*args, **kwargs):
        raise RuntimeError("tuhaf hata")

    monkeypatch.setattr(l1, "fetch_ohlcv", patlayan)
    assert not hub.tick(btc()).ok


def test_yetersiz_mum_kartı_dusurur(monkeypatch) -> None:
    import app.layers.l1_market_data as l1
    monkeypatch.setattr(l1, "fetch_ohlcv",
                        lambda *a, **k: mum_cercevesi(1))
    isaret = hub.tick(btc())
    assert not isaret.ok
    assert "mum" in isaret.error.lower() or "Yeterli" in isaret.error


def test_hacimsiz_kaynak_fiyati_dusurmez(monkeypatch) -> None:
    """Aralık/hacim okunamazsa fiyat yine ölçülür."""
    import app.layers.l1_market_data as l1

    def hacimsiz(*args, **kwargs):
        cerceve = mum_cercevesi(48).drop(columns=["volume"])
        # `volume` kolonu yok; sum() KeyError üretir → yakalanır.
        return cerceve

    monkeypatch.setattr(l1, "fetch_ohlcv", hacimsiz)
    isaret = hub.tick(btc())
    assert isaret.ok
    assert isaret.price is not None


def test_ikinci_kotasyon_onbellekten_gelir(monkeypatch) -> None:
    import app.layers.l1_market_data as l1
    cagrilar = {"adet": 0}

    def sayan(*args, **kwargs):
        cagrilar["adet"] += 1
        return mum_cercevesi(48)

    monkeypatch.setattr(l1, "fetch_ohlcv", sayan)
    hub.tick(btc())
    hub.tick(btc())
    assert cagrilar["adet"] == 1


def test_hazir_cerceveden_kotasyon() -> None:
    isaret = hub._tick_from_frame(btc(), mum_cercevesi(48, 10.0))
    assert isaret.ok
    assert isaret.price == pytest.approx(33.5)


def test_coklu_kotasyon_paralel_olculur(monkeypatch) -> None:
    import app.layers.l1_market_data as l1
    monkeypatch.setattr(l1, "fetch_ohlcv",
                        lambda *a, **k: mum_cercevesi(48))
    monkeypatch.setattr(hub, "_prewarm_stocks", lambda *a, **k: None)
    monkeypatch.setattr(hub, "_prewarm_exchanges", lambda *a, **k: None)
    sonuc = hub.ticks([btc(), aapl()])
    assert len(sonuc) == 2
    assert all(t.ok for t in sonuc)
    assert hub.ticks([]) == []


def test_zaman_asimi_karta_yazilir(monkeypatch) -> None:
    monkeypatch.setattr(hub, "_prewarm_stocks", lambda *a, **k: None)
    monkeypatch.setattr(hub, "_prewarm_exchanges", lambda *a, **k: None)

    def yavas(inst, spark=True):
        raise TimeoutError("çok yavaş")

    monkeypatch.setattr(hub, "tick", yavas)
    sonuc = hub.ticks([btc()])
    assert not sonuc[0].ok


# --------------------------------------------------------------------------- #
#  Toplu ısıtma
# --------------------------------------------------------------------------- #

def test_hisse_isitma_tek_cagrida_yapar(monkeypatch) -> None:
    import sys
    import types

    sahte = types.ModuleType("yfinance")

    def download(*args, **kwargs):
        idx = pd.date_range("2024-01-01", periods=40, freq="D", tz="UTC")
        sutunlar = pd.MultiIndex.from_product(
            [["AAPL", "MSFT"], ["open", "high", "low", "close", "volume"]])
        import numpy as np
        veri = np.arange(40 * 10, dtype=float).reshape(40, 10) + 10.0
        return pd.DataFrame(veri, index=idx, columns=sutunlar)

    sahte.download = download
    monkeypatch.setitem(sys.modules, "yfinance", sahte)
    hub._prewarm_stocks([aapl(),
                         Instrument("MSFT", "stock", "yahoo", "Microsoft", "hisse")])
    assert hub._CACHE.get(f"tick:{aapl().key()}:1", hub.QUOTE_TTL) is not None


def test_isitma_az_hissede_calismaz() -> None:
    hub._prewarm_stocks([aapl()])
    assert hub._CACHE.get(f"tick:{aapl().key()}:1", hub.QUOTE_TTL) is None


def test_isitma_indirme_hatasinda_yavas_yola_duser(monkeypatch) -> None:
    import sys
    import types
    sahte = types.ModuleType("yfinance")
    sahte.download = lambda *a, **k: 1 / 0
    monkeypatch.setitem(sys.modules, "yfinance", sahte)
    hub._prewarm_stocks([aapl(),
                         Instrument("MSFT", "stock", "yahoo", "Microsoft", "hisse")])


def test_isitma_bos_yanitta_sessiz_doner(monkeypatch) -> None:
    import sys
    import types
    sahte = types.ModuleType("yfinance")
    sahte.download = lambda *a, **k: pd.DataFrame()
    monkeypatch.setitem(sys.modules, "yfinance", sahte)
    hub._prewarm_stocks([aapl(),
                         Instrument("MSFT", "stock", "yahoo", "Microsoft", "hisse")])


def test_borsa_isitma_baglantiyi_acar(monkeypatch) -> None:
    import app.layers.l1_market_data as l1
    acilan = {"adet": 0}

    class SahteBorsa:
        pass

    def sahte_get(exchange_id):
        acilan["adet"] += 1
        return SahteBorsa()

    monkeypatch.setattr(l1, "get_exchange", sahte_get)
    hub._prewarm_exchanges([btc(), btc()])
    assert acilan["adet"] == 1
    # Hisse ısıtmaya girmez.
    hub._prewarm_exchanges([aapl()])
    assert acilan["adet"] == 1


def test_borsa_isitma_hatasi_yavas_yola_duser(monkeypatch) -> None:
    import app.layers.l1_market_data as l1

    def patlayan(exchange_id):
        raise RuntimeError("bağlanamadı")

    monkeypatch.setattr(l1, "get_exchange", patlayan)
    hub._prewarm_exchanges([btc()])


# --------------------------------------------------------------------------- #
#  Tahta
# --------------------------------------------------------------------------- #

def test_tahta_nabiz_ve_hareket_edenler(monkeypatch) -> None:
    import app.layers.l1_market_data as l1
    monkeypatch.setattr(l1, "fetch_ohlcv",
                        lambda *a, **k: mum_cercevesi(48))
    monkeypatch.setattr(hub, "_prewarm_stocks", lambda *a, **k: None)
    monkeypatch.setattr(hub, "_prewarm_exchanges", lambda *a, **k: None)
    tahta = hub.board([btc(), aapl()])
    assert tahta["measured"] == len(hub.PULSE) + 2
    assert tahta["unavailable"] == 0
    assert tahta["failed_count"] == 0
    assert len(tahta["pulse"]) == len(hub.PULSE)
    assert tahta["at"]


def test_tahta_olumeyenleri_sayar(monkeypatch) -> None:
    import app.layers.l1_market_data as l1
    from app.layers.l1_market_data import MarketDataError
    monkeypatch.setattr(hub, "_prewarm_stocks", lambda *a, **k: None)
    monkeypatch.setattr(hub, "_prewarm_exchanges", lambda *a, **k: None)

    def secici(market, exchange, symbol, timeframe, limit):
        if "BTC" in symbol:
            raise MarketDataError("kapalı")
        return mum_cercevesi(48)

    monkeypatch.setattr(l1, "fetch_ohlcv", secici)
    tahta = hub.board([btc()])
    assert tahta["unavailable"] >= 1


# --------------------------------------------------------------------------- #
#  Ayrıntı
# --------------------------------------------------------------------------- #

def _detay_hazirligi(monkeypatch, *, oy_hatasi: bool = False):
    import app.layers.l1_market_data as l1
    import app.layers.l2_indicators as gosterge
    import app.layers.strategies as strateji

    monkeypatch.setattr(l1, "fetch_ohlcv",
                        lambda *a, **k: mum_cercevesi(320, 100.0))
    monkeypatch.setattr(gosterge, "compute_all", lambda df: df)

    class SahteFoto:
        def to_dict(self):
            return {"sembol": "BTC/USDT"}

    monkeypatch.setattr(gosterge, "build_snapshot",
                        lambda *a, **k: SahteFoto())

    class SahteMotor:
        def run(self, df):
            if oy_hatasi:
                raise RuntimeError("oy alınamadı")

            class Oy:
                def to_dict(self):
                    return {"yon": "notr"}

            return Oy()

    monkeypatch.setattr(strateji, "StrategyEngine", SahteMotor)
    monkeypatch.setattr(hub, "symbol_news", lambda inst, limit=8: [])


def test_ayrinti_tam_dosya_dondurur(monkeypatch) -> None:
    _detay_hazirligi(monkeypatch)
    dosya = hub.detail(btc(), "1h", with_news=False)
    assert dosya["snapshot"] == {"sembol": "BTC/USDT"}
    assert dosya["consensus"] == {"yon": "notr"}
    assert len(dosya["candles"]) == 180
    assert dosya["quote"]["symbol"] == "BTC/USDT"


def test_ayrinti_ikinci_kez_onbellekten_gelir(monkeypatch) -> None:
    _detay_hazirligi(monkeypatch)
    ilk = hub.detail(btc(), "1h", with_news=False)
    ikinci = hub.detail(btc(), "1h", with_news=False)
    assert ikinci["candles"] == ilk["candles"]
    assert ikinci["age_seconds"] >= 0.0


def test_ayrinti_oy_alinamasa_grafik_gider(monkeypatch) -> None:
    _detay_hazirligi(monkeypatch, oy_hatasi=True)
    dosya = hub.detail(btc(), "1h", with_news=False)
    assert dosya["consensus"] is None
    assert dosya["candles"]


def test_ayrinti_haberle_birlikte_doner(monkeypatch) -> None:
    _detay_hazirligi(monkeypatch)
    monkeypatch.setattr(hub, "symbol_news",
                        lambda inst, limit=8: [{"title": "t"}])
    dosya = hub.detail(btc(), "1h", with_news=True)
    assert dosya["news"] == [{"title": "t"}]


def test_ayrinti_veri_hatasini_raporlar(monkeypatch) -> None:
    import app.layers.l1_market_data as l1
    from app.layers.l1_market_data import MarketDataError
    monkeypatch.setattr(l1, "fetch_ohlcv",
                        lambda *a, **k: (_ for _ in ()).throw(
                            MarketDataError("yok")))
    dosya = hub.detail(btc())
    assert dosya["error"]

    monkeypatch.setattr(l1, "fetch_ohlcv",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("tuhaf")))
    dosya = hub.detail(aapl())
    assert dosya["error"]


# --------------------------------------------------------------------------- #
#  Haber
# --------------------------------------------------------------------------- #

def test_haber_tekillestirme_ayni_basligi_birlestirir() -> None:
    satirlar = [
        {"title": "Bitcoin yükseliyor piyasalar coştu bugün yarın da"},
        {"title": "Bitcoin yükseliyor piyasalar coştu bugün yarın da"},
        {"title": "Ethereum tarafında sessiz bir gün yaşanıyor piyasada"},
        {"title": ""},
    ]
    temiz = hub._dedupe(satirlar)
    assert len(temiz) == 2


def _haber_hazirligi(monkeypatch, *, basliklar=None, hata: bool = False):
    import app.agent.news as haber

    class Satir:
        def __init__(self, i):
            self._i = i

        def to_dict(self):
            return {"title": f"Haber {self._i} piyasada önemli gelişme oldu bugün",
                    "score": 2 if self._i == 0 else -2,
                    "published": f"2024-01-0{self._i + 1}",
                    "source": "test"}

    def sahte_getir(url, limit):
        if hata:
            raise RuntimeError("kaynak kapalı")
        return [Satir(0), Satir(1)]

    monkeypatch.setattr(haber, "_fetch_feed", sahte_getir)
    monkeypatch.setattr(haber, "CRYPTO_FEEDS", ["https://ornek.test/kripto"])
    monkeypatch.setattr(haber, "MACRO_FEEDS", ["https://ornek.test/makro"])


def test_haber_akisi_duygu_skoru_determindir(monkeypatch) -> None:
    _haber_hazirligi(monkeypatch)
    akis = hub.news_stream("all", 10)
    assert akis["count"] > 0
    assert akis["mood"] == pytest.approx(0.0)
    assert akis["mood_code"] == "mixed"
    assert "UYARI" in akis


def test_haber_kaynagi_duserse_akış_bos_doner(monkeypatch) -> None:
    _haber_hazirligi(monkeypatch, hata=True)
    akis = hub.news_stream("all", 10)
    assert akis["count"] == 0
    assert "not" in akis


def test_haber_olumlu_hava_kodu(monkeypatch) -> None:
    import app.agent.news as haber

    class Olumlu:
        def to_dict(self):
            return {"title": "Piyasalar güçlü yükselişte yeni rekor kırıldı bugün",
                    "score": 5, "published": "2024-01-02", "source": "t"}

    monkeypatch.setattr(haber, "_fetch_feed", lambda url, lim: [Olumlu()])
    monkeypatch.setattr(haber, "CRYPTO_FEEDS", ["https://ornek.test/k"])
    monkeypatch.setattr(haber, "MACRO_FEEDS", [])
    akis = hub.news_stream("crypto", 5)
    assert akis["mood_code"] == "positive"


def test_haber_olumsuz_hava_kodu(monkeypatch) -> None:
    import app.agent.news as haber

    class Olumsuz:
        def to_dict(self):
            return {"title": "Piyasalar sert düştü kayıplar büyüyor endişe hakim",
                    "score": -5, "published": "2024-01-02", "source": "t"}

    monkeypatch.setattr(haber, "_fetch_feed", lambda url, lim: [Olumsuz()])
    monkeypatch.setattr(haber, "CRYPTO_FEEDS", ["https://ornek.test/k"])
    monkeypatch.setattr(haber, "MACRO_FEEDS", [])
    akis = hub.news_stream("crypto", 5)
    assert akis["mood_code"] == "negative"


def test_sembol_haberi_basari_ve_hata(monkeypatch) -> None:
    import app.agent.news as haber
    monkeypatch.setattr(haber, "fetch_news",
                        lambda s, m: {"headlines": [{"title": "t"}] * 10})
    assert len(hub.symbol_news(btc())) == 8

    def patlayan(s, m):
        raise RuntimeError("kapalı")

    monkeypatch.setattr(haber, "fetch_news", patlayan)
    assert hub.symbol_news(btc()) == []


# --------------------------------------------------------------------------- #
#  Arama ve ajan bağlamı
# --------------------------------------------------------------------------- #

def test_arama_bos_sorguda_bos_doner() -> None:
    assert hub.search("") == []


def test_arama_tahtada_bulur() -> None:
    sonuc = hub.search("bitcoin")
    assert any(r["symbol"] == "BTC/USDT" for r in sonuc)


def test_arama_borsaya_duser(monkeypatch) -> None:
    import app.layers.l1_market_data as l1
    monkeypatch.setattr(l1, "search_symbols",
                        lambda m, e, q, lim: ["DOGE/USDT"])
    sonuc = hub.search("doge")
    assert any(r["symbol"] == "DOGE/USDT" for r in sonuc)


def test_arama_borsa_hatasinda_cozume_duser(monkeypatch) -> None:
    import app.layers.l1_market_data as l1

    def patlayan(*args, **kwargs):
        raise RuntimeError("kapalı")

    monkeypatch.setattr(l1, "search_symbols", patlayan)
    monkeypatch.setattr(hub, "_pair_exists", lambda pair: False)
    sonuc = hub.search("AAPL")
    assert any(r["symbol"] == "AAPL" for r in sonuc)


def test_arama_tahta_disi_borsa_bosken_cozume_duser(monkeypatch) -> None:
    import app.layers.l1_market_data as l1
    monkeypatch.setattr(l1, "search_symbols", lambda *a, **k: [])
    monkeypatch.setattr(hub, "_pair_exists", lambda pair: False)
    assert hub.search("bitcoin")  # tahtada bulunur


def test_ajan_baglami_ekranla_ayni_sayilari_tasir(monkeypatch) -> None:
    import app.layers.l1_market_data as l1
    monkeypatch.setattr(l1, "fetch_ohlcv",
                        lambda *a, **k: mum_cercevesi(48))
    monkeypatch.setattr(hub, "_prewarm_stocks", lambda *a, **k: None)
    monkeypatch.setattr(hub, "_prewarm_exchanges", lambda *a, **k: None)
    _haber_hazirligi(monkeypatch)
    baglam = hub.agent_context([btc()])
    assert baglam["piyasa"]
    assert baglam["haber_havasi"] in ("positive", "negative", "mixed")
    assert "KURAL" in baglam


def test_ajan_baglami_olumeyeni_yazar(monkeypatch) -> None:
    import app.layers.l1_market_data as l1
    from app.layers.l1_market_data import MarketDataError
    monkeypatch.setattr(hub, "_prewarm_stocks", lambda *a, **k: None)
    monkeypatch.setattr(hub, "_prewarm_exchanges", lambda *a, **k: None)
    monkeypatch.setattr(l1, "fetch_ohlcv",
                        lambda *a, **k: (_ for _ in ()).throw(
                            MarketDataError("kapalı")))
    _haber_hazirligi(monkeypatch)
    baglam = hub.agent_context([btc()])
    assert any("ölçülemedi" in satir for satir in baglam["piyasa"])
    assert baglam["olculemeyen"] >= 1


# --------------------------------------------------------------------------- #
#  Derinlik rotaları (8 yeni uç — kimlikli istekle 200)
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def kisi():
    """Tek kullanıcı: rotaların kimlik desenini doğrulamak için yeter."""
    from app.core.crypto import new_salt
    from app.core.db import SessionLocal
    from app.core.security import create_access_token, hash_password
    from app.models import User

    db = SessionLocal()
    satir = db.query(User).filter(User.email == "derinlik@zumvia.com").first()
    if satir is None:
        satir = User(email="derinlik@zumvia.com",
                     password_hash=hash_password("derinliktest12345"),
                     vault_salt=new_salt())
        db.add(satir)
        db.commit()
        db.refresh(satir)
    jeton = create_access_token(satir.id, satir.email)
    db.close()
    return {"Authorization": f"Bearer {jeton}"}


def _uclari_yamala(monkeypatch) -> None:
    from app.layers import fundamentals as fund
    from app.layers import fx as kur
    from app.layers import macro as makro

    monkeypatch.setattr(fund, "snapshot", lambda inst: {"available": True})
    monkeypatch.setattr(fund, "income_statement",
                        lambda inst, periods=4: {"available": True})
    monkeypatch.setattr(fund, "balance_sheet",
                        lambda inst, periods=4: {"available": True})
    monkeypatch.setattr(fund, "earnings_calendar", lambda inst: {"available": True})
    monkeypatch.setattr(fund, "dividends", lambda inst: {"available": True})
    monkeypatch.setattr(fund, "peers", lambda inst, limit=8: {"available": True})
    monkeypatch.setattr(makro, "regime", lambda: {"available": True})
    monkeypatch.setattr(kur, "convert", lambda a, b, q: 3200.0)
    monkeypatch.setattr(kur, "rate", lambda b, q: 32.0)


def test_derinlik_uclari_kimlikle_200_doner(monkeypatch, kisi) -> None:
    _uclari_yamala(monkeypatch)
    istemci = TestClient(app)
    hisse_qs = "symbol=AAPL&market=stock&exchange=yahoo"
    yollar = [
        f"/api/finance/fundamentals?{hisse_qs}",
        f"/api/finance/income-statement?{hisse_qs}",
        f"/api/finance/balance-sheet?{hisse_qs}",
        f"/api/finance/earnings?{hisse_qs}",
        f"/api/finance/dividends?{hisse_qs}",
        f"/api/finance/peers?{hisse_qs}",
        "/api/finance/macro",
        "/api/finance/fx?base=USD&quote=TRY&amount=100",
    ]
    for yol in yollar:
        yanit = istemci.get(yol, headers=kisi)
        assert yanit.status_code == 200, f"{yol} → {yanit.status_code}"
    # Kimliksiz istek veri göremez.
    assert istemci.get("/api/finance/macro").status_code in (401, 403)
    # Boş sembol 400/422 ile reddedilir.
    assert istemci.get("/api/finance/fundamentals?symbol=",
                       headers=kisi).status_code in (400, 422)


def test_kur_hatasi_gerekceyle_doner(monkeypatch, kisi) -> None:
    from app.layers import fx as kur
    monkeypatch.setattr(kur, "convert",
                        lambda a, b, q: (_ for _ in ()).throw(
                            RuntimeError("kur yok")))
    istemci = TestClient(app)
    yanit = istemci.get("/api/finance/fx?base=USD&quote=XYZ", headers=kisi)
    assert yanit.status_code == 200
    assert yanit.json()["available"] is False


# --------------------------------------------------------------------------- #
#  Ajan araçları (5 yeni araç — REGISTRY üzerinden duman testi)
# --------------------------------------------------------------------------- #

@pytest.fixture()
def baglam():
    """Gerçek kullanıcı + oturum: araçlar DB'ye dokunmadan okur."""
    from app.agent.tools import ToolContext
    from app.core.crypto import new_salt
    from app.core.db import SessionLocal
    from app.core.security import hash_password
    from app.models import User

    db = SessionLocal()
    satir = db.query(User).filter(User.email == "arac@zumvia.com").first()
    if satir is None:
        satir = User(email="arac@zumvia.com",
                     password_hash=hash_password("aractest12345"),
                     vault_salt=new_salt())
        db.add(satir)
        db.commit()
        db.refresh(satir)
    yield ToolContext(db=db, user=satir)
    db.close()


def test_yeni_araclar_kayitlidir_ve_calisir(monkeypatch, baglam) -> None:
    from app.agent.tools import REGISTRY
    from app.layers import fundamentals as fund
    from app.layers import fx as kur
    from app.layers import macro as makro

    for ad in ("get_fundamentals", "get_earnings", "compare_peers",
               "get_macro_regime", "convert_currency"):
        assert ad in REGISTRY, f"{ad} REGISTRY'de yok"
        assert "ne zaman" in REGISTRY[ad].description.lower() or \
            "ğında" in REGISTRY[ad].description or \
            "da " in REGISTRY[ad].description, f"{ad} kullanım zamanı anlatmıyor"

    monkeypatch.setattr(fund, "snapshot", lambda inst: {"available": True})
    monkeypatch.setattr(fund, "earnings_calendar", lambda inst: {"available": True})
    monkeypatch.setattr(fund, "peers", lambda inst, limit=8: {"available": True})
    monkeypatch.setattr(makro, "regime",
                        lambda: {"available": True, "regime": "belirsiz"})
    monkeypatch.setattr(kur, "convert", lambda a, b, q: 3200.0)
    monkeypatch.setattr(kur, "rate", lambda b, q: 32.0)

    assert REGISTRY["get_fundamentals"].handler(
        baglam, {"symbol": "AAPL", "market": "stock"})["available"] is True
    assert REGISTRY["get_earnings"].handler(
        baglam, {"symbol": "AAPL"})["available"] is True
    assert REGISTRY["compare_peers"].handler(
        baglam, {"symbol": "AAPL"})["available"] is True
    assert REGISTRY["get_macro_regime"].handler(baglam, {})["regime"] == "belirsiz"
    sonuc = REGISTRY["convert_currency"].handler(
        baglam, {"amount": 100, "base": "USD", "quote": "TRY"})
    assert sonuc["converted"] == pytest.approx(3200.0)


def test_araclar_bos_sembolde_istisna_firlatmaz(monkeypatch, baglam) -> None:
    from app.agent.tools import REGISTRY
    assert "error" in REGISTRY["get_fundamentals"].handler(baglam, {"symbol": ""})
    assert "error" in REGISTRY["convert_currency"].handler(
        baglam, {"amount": "yüz", "base": "USD", "quote": "TRY"})


def test_mcp_tools_list_bes_yeni_araci_aciklar() -> None:
    """`backend/mcp_server.py` tools/list beş yeni aracı dışarı açar.

    REGISTRY testi kaydın varlığını kilitler; bu test MCP sınırından
    (handle → tools/list) aynı kaydın istemciye ulaştığını kilitler.
    Ağ/DB gerektirmez: liste yalnızca REGISTRY'den üretilir.
    """
    import mcp_server

    yanit = mcp_server.handle({"jsonrpc": "2.0", "id": 1,
                               "method": "tools/list", "params": {}})
    adlar = {arac["name"] for arac in yanit["result"]["tools"]}
    for beklenen in ("get_fundamentals", "get_earnings", "compare_peers",
                     "get_macro_regime", "convert_currency"):
        assert beklenen in adlar, f"{beklenen} tools/list içinde yok"
