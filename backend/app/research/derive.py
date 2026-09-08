"""
TÜREV ÖLÇÜMLER — hesaplanabilir olanı hesapla, sonra yargıla
=============================================================

Doğrulayıcı ilk sürümde şunu yapıyordu: uzman "stop 76.286, fiyattan %2.7
aşağı" yazdığında, defterde 2.7 diye bir sayı olmadığı için iddiayı
DAYANAKSIZ ilan ediyordu.

Oysa 2.7 uydurma değildi. Defterde fiyat da vardı, stop da vardı; aradaki
yüzde tek bir bölme işlemiyle bulunur. Sistem hesaplamamıştı ve sonra
hesaplamadığı şey için uzmanı suçluyordu.

Bu modül o boşluğu kapatır: ham ölçümlerden ÇIKARILABİLEN her standart
büyüklüğü kod ile hesaplar ve deftere yazar. Sonuç, uzmanın iddiasını
doğrulamak değil — bağımsız olarak AYNI SAYIYI ÜRETMEKTİR. Uzman farklı bir
sayı yazmışsa artık gerçek bir çelişki vardır ve yakalanır.

Her türev olgu, nasıl hesaplandığını yanında taşır:

    BTC/USDT.stop_mesafe_pct = 2.73   [türev/(fiyat-stop)/fiyat*100]

Bu şart: kaynağı gösterilemeyen bir sayı, ham veriden geldiği için değil,
gösterilemediği için güvenilmezdir.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..core.logging import get_logger
from .ledger import Ledger

log = get_logger("zumvia.research.derive")

SOURCE = "türev"          # bu olguların "kaynağı": kendi hesabımız


def _num(ledger: Ledger, key: str) -> float | None:
    fact = ledger.get(key)
    return fact.numeric if fact else None


def _find(ledger: Ledger, symbol: str, *fragments: str) -> float | None:
    """Bir varlığın ölçümünü esnek adla arar (`atr`, `atr_14`, `indicators.atr_14`)."""
    prefix = symbol.lower() + "."
    found: float | None = None
    for fact in ledger.numbers():
        key = fact.key.lower()
        if not key.startswith(prefix):
            continue
        tail = key[len(prefix):]
        if any(f in tail for f in fragments):
            found = fact.numeric
    return found


def _write(ledger: Ledger, key: str, value: float | None, formula: str,
           unit: str = "") -> bool:
    """
    Türev olguyu deftere yazar.

    `None` ya da anlamsız (sonsuz/NaN) değerler YAZILMAZ: hesaplanamayan bir
    şeyi sıfır diye yazmak, ölçüm uydurmaktır.
    """
    if value is None:
        return False
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False
    if value != value or value in (float("inf"), float("-inf")):
        return False

    ledger.add(key, round(value, 6), SOURCE, method=formula, unit=unit)
    return True


# --------------------------------------------------------------------------- #
#  Varlık başına türevler
# --------------------------------------------------------------------------- #

def _for_symbol(ledger: Ledger, symbol: str) -> int:
    """Tek bir varlık için hesaplanabilen her şeyi çıkarır."""
    written = 0
    price = _num(ledger, f"{symbol}.fiyat") or _num(ledger, f"{symbol}.price")
    if not price:
        return 0

    stop = _find(ledger, symbol, "stop_loss", "stop")
    target = _find(ledger, symbol, "take_profit", "target")

    # --- işlem kurgusu: uzmanların en sık türettiği üç sayı ---------------- #
    if stop:
        written += _write(ledger, f"{symbol}.stop_mesafe_pct",
                          (price - stop) / price * 100,
                          "(fiyat-stop)/fiyat*100", "%")
        written += _write(ledger, f"{symbol}.stop_mesafe",
                          abs(price - stop), "|fiyat-stop|")
    if target:
        written += _write(ledger, f"{symbol}.hedef_mesafe_pct",
                          (target - price) / price * 100,
                          "(hedef-fiyat)/fiyat*100", "%")
        written += _write(ledger, f"{symbol}.hedef_mesafe",
                          abs(target - price), "|hedef-fiyat|")

    if stop and target and abs(price - stop) > 1e-9:
        # Risk/ödül, bir kurgunun tek en önemli sayısıdır: 1:2'nin altındaki
        # her kurulum, isabet oranı yüksek olsa bile uzun vadede kaybettirir.
        written += _write(ledger, f"{symbol}.risk_odul",
                          abs(target - price) / abs(price - stop),
                          "hedef_mesafe/stop_mesafe")

    # --- ortalamalara uzaklık ---------------------------------------------- #
    for span in (20, 50, 200):
        ema = _num(ledger, f"{symbol}.indicators.ema_{span}")
        if ema:
            written += _write(ledger, f"{symbol}.ema{span}_uzaklik_pct",
                              (price - ema) / ema * 100,
                              f"(fiyat-ema{span})/ema{span}*100", "%")

    supertrend = _num(ledger, f"{symbol}.indicators.supertrend")
    if supertrend:
        written += _write(ledger, f"{symbol}.supertrend_uzaklik_pct",
                          (price - supertrend) / price * 100,
                          "(fiyat-supertrend)/fiyat*100", "%")

    # --- Bollinger konumu --------------------------------------------------- #
    upper = _num(ledger, f"{symbol}.indicators.bb_upper")
    lower = _num(ledger, f"{symbol}.indicators.bb_lower")
    mid = _num(ledger, f"{symbol}.indicators.bb_mid")
    if upper:
        written += _write(ledger, f"{symbol}.bb_ust_uzaklik_pct",
                          (upper - price) / price * 100,
                          "(bb_ust-fiyat)/fiyat*100", "%")
    if lower:
        written += _write(ledger, f"{symbol}.bb_alt_uzaklik_pct",
                          (price - lower) / price * 100,
                          "(fiyat-bb_alt)/fiyat*100", "%")
    if mid:
        written += _write(ledger, f"{symbol}.bb_orta_uzaklik_pct",
                          (price - mid) / mid * 100,
                          "(fiyat-bb_orta)/bb_orta*100", "%")

    # --- yön gücü ----------------------------------------------------------- #
    plus_di = _num(ledger, f"{symbol}.indicators.plus_di")
    minus_di = _num(ledger, f"{symbol}.indicators.minus_di")
    if plus_di is not None and minus_di is not None:
        written += _write(ledger, f"{symbol}.di_farki", plus_di - minus_di,
                          "plus_di-minus_di")

    macd = _num(ledger, f"{symbol}.indicators.macd")
    signal = _num(ledger, f"{symbol}.indicators.macd_signal")
    if macd is not None and signal is not None:
        written += _write(ledger, f"{symbol}.macd_fark", macd - signal,
                          "macd-macd_signal")

    # --- ATR'nin para karşılığı --------------------------------------------- #
    atr = _find(ledger, symbol, "atr_14", "atr")
    if atr:
        written += _write(ledger, f"{symbol}.atr_2x", atr * 2, "atr*2")
        if not _num(ledger, f"{symbol}.indicators.atr_percent"):
            written += _write(ledger, f"{symbol}.atr_pct", atr / price * 100,
                              "atr/fiyat*100", "%")

    # --- algoritma oyunun yüzdesi ------------------------------------------- #
    #  Uzmanlar "güven %94.8" yazar; defterde 0.948 durur. Aynı şeydir ama
    #  eşleşmez — yüzde karşılığını da yazmak bu sahte uyuşmazlığı bitirir.
    confidence = _num(ledger, f"{symbol}.algo.confidence")
    if confidence is not None and 0 <= confidence <= 1:
        written += _write(ledger, f"{symbol}.algo.confidence_pct",
                          confidence * 100, "confidence*100", "%")

    agree = _num(ledger, f"{symbol}.algo.agree")
    total = _num(ledger, f"{symbol}.algo.total")
    if agree is not None and total:
        written += _write(ledger, f"{symbol}.algo.uyum_pct", agree / total * 100,
                          "agree/total*100", "%")
        # "8 stratejiden 3'ü AL diyor" cümlesinin doğal devamı "5'i demiyor"
        # olur. Uzmanlar bu çıkarımı hep yapar; sistem yapmazsa kendi
        # yapmadığı hesabı çelişki ilan eder.
        written += _write(ledger, f"{symbol}.algo.karsit", total - agree,
                          "total-agree")
        written += _write(ledger, f"{symbol}.algo.karsit_pct",
                          (total - agree) / total * 100,
                          "(total-agree)/total*100", "%")

    return written


# --------------------------------------------------------------------------- #
#  Portföy türevleri
# --------------------------------------------------------------------------- #

def _for_portfolio(ledger: Ledger, symbols: Iterable[str]) -> int:
    """Sermaye ve kurgudan çıkan pozisyon büyüklükleri."""
    from ..core.config import settings  # noqa: PLC0415

    written = 0
    equity = _num(ledger, "portfoy.sermaye")
    if not equity:
        return 0

    max_risk_pct = float(settings.hard_max_risk_pct)
    risk_amount = equity * max_risk_pct / 100.0
    written += _write(ledger, "portfoy.islem_basi_risk", risk_amount,
                      f"sermaye*{max_risk_pct}/100", "USD")

    for symbol in symbols:
        stop_distance = _num(ledger, f"{symbol}.stop_mesafe")
        price = _num(ledger, f"{symbol}.fiyat") or _num(ledger, f"{symbol}.price")
        if not stop_distance or not price:
            continue
        size = risk_amount / stop_distance
        written += _write(ledger, f"{symbol}.pozisyon_adet", size,
                          "islem_basi_risk/stop_mesafe")
        written += _write(ledger, f"{symbol}.pozisyon_tutar", size * price,
                          "pozisyon_adet*fiyat", "USD")
        written += _write(ledger, f"{symbol}.pozisyon_sermaye_pct",
                          size * price / equity * 100,
                          "pozisyon_tutar/sermaye*100", "%")
    return written


# --------------------------------------------------------------------------- #
#  Genel giriş
# --------------------------------------------------------------------------- #

def enrich(ledger: Ledger, context: dict[str, Any]) -> int:
    """
    Ham ölçümlerden çıkarılabilecek her şeyi hesaplayıp deftere ekler.

    Analiz aşamasından ÖNCE çağrılır: uzmanlar bu sayıları kendileri
    hesaplamak zorunda kalmasın, hesaplasalar bile doğrulanabilsinler.

    Dönen değer, eklenen türev olgu sayısıdır.
    """
    symbols = context.get("symbols") or []
    written = 0
    for symbol in symbols:
        written += _for_symbol(ledger, symbol)
    written += _for_portfolio(ledger, symbols)

    log.info("%d türev ölçüm eklendi (%d varlık)", written, len(symbols))
    return written
