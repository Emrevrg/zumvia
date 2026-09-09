"""
PAPER KAPILARI — dürüst ret yolları (hızlı birim testleri)
==========================================================
`test_paper_standard.py` mutlu yolu ve ana retleri kilitler; burada
savunma dalları kapatılır: bozuk kotasyon, geçersiz bakiye, sağlayıcısız
koşum, reddeden risk kalkanı, bloke eden solvency. İlke aynıdır: ölçüm
yoksa EMİR YOK, gerekçe yazılır, istisna sızmaz.
"""
from __future__ import annotations

import math
import time
import types
from datetime import UTC, datetime

import pandas as pd
import pytest

from app.engine import data_quality as kalite
from app.engine import paper_evaluation as paper
from app.engine.paper_evaluation import _dry_run_order, run_paper_evaluation
from app.layers.l1_market_data import _demo_ohlcv

NOW = datetime.now(UTC)


def taze_df(satir: int = 300) -> pd.DataFrame:
    """Bayatlamayan demo mumu (son bar ~şimdi)."""
    return _demo_ohlcv("BTC/USDT", "1h", satir)


def zengin_df() -> pd.DataFrame:
    """Yeterlilik kapısını geçen büyük çerçeve (deneyle doğrulandı)."""
    return _demo_ohlcv("BTC/USDT", "1h", 5000)


def alinti(df: pd.DataFrame) -> dict:
    fiyat = float(df["close"].iloc[-1])
    return {"price": fiyat, "bid": fiyat * 0.9999, "ask": fiyat * 1.0001,
            "ts": time.time()}


# --------------------------------------------------------------------------- #
#  Kotasyon kapısı
# --------------------------------------------------------------------------- #

def test_sayi_olmayan_fiyat_reddedilir() -> None:
    sonuc = kalite.check_quote("ölçülemedi")
    assert sonuc["available"] is False
    assert sonuc["code"] == "QUOTE_NON_NUMERIC"


def test_sifir_ve_negatif_fiyat_reddedilir() -> None:
    assert kalite.check_quote(0)["code"] == "QUOTE_INVALID"
    assert kalite.check_quote(-5.0)["code"] == "QUOTE_INVALID"
    assert kalite.check_quote(float("nan"))["code"] == "QUOTE_INVALID"


def test_bayat_kotasyon_reddedilir() -> None:
    sonuc = kalite.check_quote(100.0, ts=time.time() - 600.0)
    assert sonuc["available"] is False
    assert sonuc["code"] == "QUOTE_STALE"


def test_zamansiz_kotasyon_bayata_bakilmaz() -> None:
    """Zaman damgası çözülemezse bayatlık hükmü verilmez, fiyat hükmü verilir."""
    sonuc = kalite.check_quote(100.0, ts="bozuk-zaman")
    assert sonuc["available"] is True
    assert sonuc["code"] == "OK"


def test_capraz_spread_reddedilir() -> None:
    sonuc = kalite.check_quote(100.0, bid=101.0, ask=99.0)
    assert sonuc["available"] is False
    assert sonuc["code"] == "CROSSED_SPREAD"


def test_bozuk_spread_fiyati_dusurmez() -> None:
    sonuc = kalite.check_quote(100.0, bid="bozuk", ask=100.5)
    assert sonuc["available"] is True
    assert sonuc["details"]["spread_pct"] is None


def test_gecerli_kotasyon_spread_hesaplar() -> None:
    sonuc = kalite.check_quote(100.0, bid=99.9, ask=100.1, ts=time.time())
    assert sonuc["available"] is True
    assert sonuc["details"]["spread_pct"] == pytest.approx(0.2)


# --------------------------------------------------------------------------- #
#  Mum kapısı
# --------------------------------------------------------------------------- #

def test_naive_zaman_utc_sayilir() -> None:
    """tz-naive `now` UTC varsayılır; taze veri geçer."""
    sonuc = kalite.check_candles(taze_df(), "1h",
                                 now=datetime.now().replace(tzinfo=None))
    assert sonuc["available"] is True


def test_eksik_kolon_reddedilir() -> None:
    df = taze_df().drop(columns=["volume"])
    sonuc = kalite.check_candles(df, "1h", NOW)
    assert sonuc["code"] == "MISSING_COLUMNS"


def test_zamansiz_indeks_reddedilir() -> None:
    df = pd.DataFrame({c: [1.0, 2.0] for c in kalite.REQUIRED_COLS})
    sonuc = kalite.check_candles(df, "1h", NOW)
    assert sonuc["code"] == "TZ_NAIVE"


def test_cevrilemez_zaman_reddedilir(monkeypatch) -> None:
    """tz var ama UTC'ye çevrilemezse bütünlük doğrulanamaz."""
    monkeypatch.setattr(
        pd.DatetimeIndex, "tz_convert",
        lambda self, *a, **k: (_ for _ in ()).throw(
            RuntimeError("çevrilemez")))
    sonuc = kalite.check_candles(taze_df(10), "1h", NOW)
    assert sonuc["code"] == "TZ_UNCONVERTIBLE"


def test_tekrarli_zaman_reddedilir() -> None:
    df = pd.concat([taze_df(10).head(3), taze_df(10)])
    sonuc = kalite.check_candles(df, "1h", NOW)
    assert sonuc["code"] == "DUPLICATE_TS"


def test_sirasiz_mum_reddedilir() -> None:
    df = taze_df(10).iloc[::-1]
    sonuc = kalite.check_candles(df, "1h", NOW)
    assert sonuc["code"] == "UNSORTED"


def test_sayi_olmayan_ohlc_reddedilir() -> None:
    df = taze_df(10).copy()
    df["open"] = "bozuk"
    assert kalite.check_candles(df, "1h", NOW)["code"] == "NON_NUMERIC"


def test_sonsuz_deger_reddedilir() -> None:
    df = taze_df(10).copy()
    df.loc[df.index[3], "close"] = math.inf
    assert kalite.check_candles(df, "1h", NOW)["code"] == "NON_FINITE"


def test_sifir_fiyat_reddedilir() -> None:
    df = taze_df(10).copy()
    df.loc[df.index[3], "low"] = 0.0
    assert kalite.check_candles(df, "1h", NOW)["code"] == "NON_POSITIVE_PRICE"


def test_tutarsiz_ohlc_reddedilir() -> None:
    df = taze_df(10).copy()
    df.loc[df.index[3], "high"] = 1.0
    df.loc[df.index[3], "low"] = 999999.0
    assert kalite.check_candles(df, "1h", NOW)["code"] == "OHLC_INCONSISTENT"


def test_ani_sicrama_reddedilir() -> None:
    """Tek barda %50 sıçrama: doğrulanamayan fiyatla işlem yok."""
    idx = pd.date_range(NOW - pd.Timedelta(hours=10), periods=10, freq="h", tz="UTC")
    kapanis = [100.0] * 5 + [150.0] * 5
    df = pd.DataFrame(
        {"open": kapanis, "high": [c * 1.01 for c in kapanis],
         "low": [c * 0.99 for c in kapanis], "close": kapanis,
         "volume": [1000.0] * 10},
        index=idx,
    )
    sonuc = kalite.check_candles(df, "1h", NOW)
    assert sonuc["code"] == "PRICE_SPIKE"
    assert sonuc["details"]["max_bar_jump_pct"] == pytest.approx(50.0)


# --------------------------------------------------------------------------- #
#  Değerlendirme giriş kapıları (hızlı retler — ölçüm koşmaz)
# --------------------------------------------------------------------------- #

def test_gecersiz_bakiye_bastan_reddedilir() -> None:
    for bozuk in (0.0, -10.0, float("nan")):
        rapor = run_paper_evaluation(starting_balance=bozuk, now=NOW)
        assert rapor["available"] is False
        assert rapor["steps"] == []


def test_desteklenmeyen_varlik_olculmez() -> None:
    rapor = run_paper_evaluation(market="xxx", symbol="???", now=NOW)
    assert rapor["available"] is False
    assert rapor["status"] == "blocked"
    assert rapor["backtest"] is None


def test_saglayicisiz_kosum_aga_cikmaz() -> None:
    rapor = run_paper_evaluation(market="crypto", symbol="BTC/USDT",
                                 fetch_ohlcv=None, now=NOW)
    assert rapor["available"] is False
    assert "ağa çıkılmadı" in rapor["steps"][-1]["detail"]


def test_mum_hatasi_gerekceyle_doner() -> None:
    def patlayan(market, symbol, timeframe):
        raise RuntimeError("borsa kapalı")

    rapor = run_paper_evaluation(market="crypto", symbol="BTC/USDT",
                                 fetch_ohlcv=patlayan, now=NOW)
    assert rapor["available"] is False
    assert rapor["steps"][-1]["data"]["code"] == "FETCH_FAILED"


def test_olcum_hatasi_gerekceyle_doner(monkeypatch) -> None:
    """
    ÖLÇÜM HATASI YAMASINDA ÇİFT BAĞLAMA ZORUNLUDUR.

    Ölçüldü: `optimizer.py` `run_backtest`'i modül seviyesinde `from`-ile
    bağlar ve ilk kez `run_paper_evaluation` içinde (yamalıyken) içe aktarılır.
    Yalnızca `backtest` yamalanırsa sahte fonksiyon optimizer ad alanına
    KALICI yapışır, yama geri alınsa bile sonraki testler sahteyi koşar.
    Bu yüzden HER İKİ ad alanı da yamalanır; ikisi de test sonunda geri alınır.
    """
    import app.engine.backtest as geri_test
    import app.engine.optimizer as eniyileyici  # önce içe aktar: orijinali bağlasın

    def patlayan(*args, **kwargs):
        raise RuntimeError("motor bozuldu")

    monkeypatch.setattr(geri_test, "run_backtest", patlayan)
    monkeypatch.setattr(eniyileyici, "run_backtest", patlayan)
    rapor = run_paper_evaluation(
        market="crypto", symbol="BTC/USDT", timeframe="1h",
        fetch_ohlcv=lambda m, s, tf: taze_df(), now=NOW)
    assert rapor["available"] is False
    assert rapor["steps"][-1]["data"]["code"] == "MEASURE_FAILED"


# --------------------------------------------------------------------------- #
#  Kotasyon/emir kapıları (yeterli kanıtla koşar — yavaş testler)
# --------------------------------------------------------------------------- #

def test_kotasyon_hatasi_emri_durdurur() -> None:
    df = zengin_df()

    def patlayan_alinti(market, symbol):
        raise RuntimeError("kotasyon yok")

    rapor = run_paper_evaluation(
        market="crypto", symbol="BTC/USDT", timeframe="1h",
        fetch_ohlcv=lambda m, s, tf: df, fetch_quote=patlayan_alinti, now=NOW)
    assert rapor["available"] is False
    assert rapor["steps"][-1]["data"]["code"] == "QUOTE_FAILED"
    assert rapor["paper_order"] is None


def test_kotasyonsuz_kosum_son_kapanisi_referans_alir() -> None:
    """Canlı kotasyon yoksa emir İLETİLMEZ; yalnızca boyut hesabı yapılır."""
    df = zengin_df()
    rapor = run_paper_evaluation(
        market="crypto", symbol="BTC/USDT", timeframe="1h",
        fetch_ohlcv=lambda m, s, tf: df, fetch_quote=None, now=NOW)
    assert rapor["available"] is True
    assert any("yalnızca boyut hesabı" in s for s in rapor["limitations"])


def test_reddedilen_emir_pozisyonsuz_kapanir(monkeypatch) -> None:
    monkeypatch.setattr(paper, "_dry_run_order",
                        lambda **k: {"ok": False, "reason": "Risk kalkanı reddetti.",
                                     "code": "RISK_REJECT"})
    df = zengin_df()
    rapor = run_paper_evaluation(
        market="crypto", symbol="BTC/USDT", timeframe="1h",
        fetch_ohlcv=lambda m, s, tf: df,
        fetch_quote=lambda m, s: alinti(df), now=NOW)
    assert rapor["available"] is False
    assert rapor["status"] == "order_rejected"
    assert rapor["paper_order"] is None
    assert any("pozisyon açılmadı" in s for s in rapor["limitations"])


# --------------------------------------------------------------------------- #
#  Güven aralığı (bootstrap %95 — tohumlu, tekrarlanabilir)
# --------------------------------------------------------------------------- #

def _ornek_islemler() -> list:
    from app.engine.backtest import BacktestTrade

    # 12 işlem: 5 kazanan, 7 kaybeden (karışık, gerçekçi).
    pnller = [8.0, -5.0, 12.0, -4.0, 6.0, -7.0, -3.0, 9.0, -6.0, 4.0, -5.0, 7.0]
    return [BacktestTrade(entry_time="t", exit_time="t", side="BUY",
                          entry=100.0, exit=100.0 + p, qty=1.0, pnl=p,
                          r_multiple=p / 5.0, reason="test", strategy="test")
            for p in pnller]


def test_guven_araligi_tekrarlanabilir_ve_tutarli() -> None:
    from app.engine.backtest import _metrics

    islemler = _ornek_islemler()
    ilk = _metrics(islemler, [100.0] * 13, 100.0, 100.0, 200)
    ikinci = _metrics(islemler, [100.0] * 13, 100.0, 100.0, 200)
    assert ilk["profit_factor_ci95"] == ikinci["profit_factor_ci95"]
    assert ilk["win_rate_ci95"] == ikinci["win_rate_ci95"]
    assert ilk["expectancy_R_ci95"] == ikinci["expectancy_R_ci95"]

    lo, hi = ilk["profit_factor_ci95"]
    assert 0 <= lo <= hi
    # Karışık örnekte aralık yozlaşmaz (tek noktaya çökmez).
    assert hi > lo, "aralık yozlaştı; bootstrap örneklemesi çalışmıyor"
    wlo, whi = ilk["win_rate_ci95"]
    assert 0 <= wlo <= whi <= 100
    assert "rneklem belirsizli" in ilk["ci_note"]


def test_yetersiz_ornekte_aralik_uydurulmaz() -> None:
    from app.engine.backtest import _metrics

    islemler = _ornek_islemler()[:4]
    sonuc = _metrics(islemler, [100.0] * 5, 100.0, 100.0, 50)
    assert sonuc["profit_factor_ci95"] is None
    assert sonuc["win_rate_ci95"] is None
    assert sonuc["expectancy_R_ci95"] is None
    assert "yetersiz" in sonuc["ci_note"].lower()

    bos = _metrics([], [], 100.0, 100.0, 50)
    assert bos["trade_count"] == 0
    assert bos["profit_factor_ci95"] is None


def test_guven_araligi_rapora_tasinir() -> None:
    df = _demo_ohlcv("BTC/USDT", "1h", 5000)
    rapor = run_paper_evaluation(
        market="crypto", symbol="BTC/USDT", timeframe="1h",
        fetch_ohlcv=lambda m, s, tf: df,
        fetch_quote=lambda m, s: {"price": float(df["close"].iloc[-1]),
                                  "bid": None, "ask": None, "ts": None},
        now=NOW)
    metrik = rapor["backtest"]["metrics"]
    assert "profit_factor_ci95" in metrik
    assert "%95 GA" in rapor["steps"][3]["detail"]


# --------------------------------------------------------------------------- #
#  Piyasa kapısı (market_rules — hızlı birim testleri)
# --------------------------------------------------------------------------- #

def test_desteklenmeyen_piyasa_reddedilir() -> None:
    from app.layers import market_rules as kurallar

    sonuc = kurallar.validate_symbol("xxx", "BTC/USDT")
    assert sonuc["supported"] is False
    assert sonuc["code"] == "UNSUPPORTED_MARKET"


def test_demo_sembol_test_icin_gecerlidir() -> None:
    from app.layers import market_rules as kurallar

    assert kurallar.validate_symbol("demo", "HERHANGI")["supported"] is True
    sonuc = kurallar.assess("demo", "HERHANGI")
    assert sonuc["supported"] is True and sonuc["tradable"] is True


def test_ayni_parali_parite_reddedilir() -> None:
    from app.layers import market_rules as kurallar

    sonuc = kurallar.validate_symbol("forex", "USD/USD")
    assert sonuc["supported"] is False


def test_bozuk_hisse_sembol_reddedilir() -> None:
    from app.layers import market_rules as kurallar

    sonuc = kurallar.validate_symbol("stock", "BOZUK SEMBOL!!")
    assert sonuc["code"] == "UNSUPPORTED_SYMBOL"


def test_naive_zaman_seansta_utc_sayilir() -> None:
    from datetime import datetime

    from app.layers import market_rules as kurallar

    # Pazartesi 15:00 UTC = NYSE açık, BIST kapalı dilimi.
    sonuc = kurallar.stock_session(datetime(2024, 1, 8, 15, 0, 0))
    assert sonuc["nyse_open"] is True
    assert "holiday_note" in sonuc


def test_acik_seansta_hisse_islem_gorur() -> None:
    from datetime import datetime

    from app.layers import market_rules as kurallar

    sonuc = kurallar.assess("stock", "AAPL", datetime(2024, 1, 8, 15, 0, 0))
    assert sonuc["tradable"] is True
    assert sonuc["venue"] == "NYSE"


# --------------------------------------------------------------------------- #
#  Dry-run emir kapısı (doğrudan birim testleri — hızlı)
# --------------------------------------------------------------------------- #

def test_gecersiz_fiyat_emir_denemez() -> None:
    sonuc = _dry_run_order(symbol="BTC/USDT", price=0.0, equity=100.0,
                           fee_pct=0.1, slippage_pct=0.05)
    assert sonuc["ok"] is False
    assert sonuc["code"] == "INVALID_INPUT"


def test_boyutlandirma_hatasi_gerekceyle_doner(monkeypatch) -> None:
    import app.layers.l4_risk as risk

    monkeypatch.setattr(risk, "validate_and_size",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("kalkan hatası")))
    sonuc = _dry_run_order(symbol="BTC/USDT", price=50000.0, equity=100.0,
                           fee_pct=0.1, slippage_pct=0.05)
    assert sonuc["code"] == "SIZE_FAILED"


def test_risk_reddi_emri_durdurur(monkeypatch) -> None:
    import app.layers.l4_risk as risk

    ret = types.SimpleNamespace(allowed=False, reason="Stop çok uzak.",
                                code="RISK_REJECT")
    monkeypatch.setattr(risk, "validate_and_size", lambda *a, **k: (ret, None))
    sonuc = _dry_run_order(symbol="BTC/USDT", price=50000.0, equity=100.0,
                           fee_pct=0.1, slippage_pct=0.05)
    assert sonuc["ok"] is False
    assert sonuc["code"] == "RISK_REJECT"


def test_solvency_bloku_emri_durdurur(monkeypatch) -> None:
    import app.layers.solvency as odeme

    monkeypatch.setattr(odeme, "check_position",
                        lambda *a, **k: types.SimpleNamespace(
                            allowed=False, reason="Borçlanma riski."))
    sonuc = _dry_run_order(symbol="BTC/USDT", price=50000.0, equity=100.0,
                           fee_pct=0.1, slippage_pct=0.05)
    assert sonuc["ok"] is False
    assert sonuc["code"] == "SOLVENCY_BLOCKED"
