"""
RİSK ÖLÇÜMÜ — modelin görüşünden bağımsız, deterministik
=========================================================

Risk müdürü rolü riski YORUMLAR; buradaki sayılar riski ÖLÇER. İkisi
çelişirse kod kazanır ve çelişki rapora yazılır.

Ölçülen dört şey:

    POZİSYON BÜYÜKLÜĞÜ  kasa riski ve stop mesafesinden türetilir
    EN KÖTÜ SENARYO     aynı anda her şey ters giderse ne kaybedilir
    LİKİDİTE            bu büyüklükte giriş/çıkış fiyatı ne kadar bozar
    YOĞUNLAŞMA          portföy zaten aynı riske mi girmiş

Hiçbiri tahmin değildir; hepsi defterdeki ölçümlerden hesaplanır. Ölçüm
yoksa alan boş bırakılır — varsayılan bir sayı uydurulmaz, çünkü uydurulmuş
bir risk sayısı, risk hesabı yapmamaktan daha tehlikelidir.
"""
from __future__ import annotations

from typing import Any

from ..core.config import settings
from ..core.logging import get_logger
from .ledger import Ledger

log = get_logger("zumvia.research.risk")

#  Stop mesafesi ATR'nin kaç katı olsun. 2 ATR, gürültüye takılmadan ama
#  kaybı da büyütmeden duran yaygın bir seçimdir.
ATR_STOP_MULTIPLE = 2.0

#  Aynı anda kaç pozisyonun birlikte ters gideceği varsayımı. Korelasyon
#  yüksek olduğunda pozisyonlar bağımsız değildir; "hepsi birden" senaryosu
#  iyimser değil gerçekçidir.
SIMULTANEOUS_LOSSES = 3


def measure(ledger: Ledger, context: dict[str, Any]) -> dict[str, Any]:
    """
    Defterdeki ölçümlerden risk tablosunu çıkarır.

    Dönen sözlükteki her alan ya gerçek bir hesaplamadır ya da yoktur.
    `None` değerler "ölçülemedi" anlamına gelir ve rapora öyle geçer.
    """
    symbols: list[str] = context.get("symbols", []) or []
    equity = _value(ledger, "portfoy.sermaye")
    max_risk_pct = float(settings.hard_max_risk_pct)

    per_symbol: list[dict[str, Any]] = []
    for symbol in symbols:
        per_symbol.append(_for_symbol(ledger, symbol, equity, max_risk_pct))

    measured = [row for row in per_symbol if row.get("risk_tutari") is not None]
    worst_case = None
    if measured and equity:
        # En kötü makul senaryo: en riskli birkaç pozisyon AYNI ANDA stop olur.
        biggest = sorted((row["risk_tutari"] for row in measured), reverse=True)
        worst_case = round(sum(biggest[:SIMULTANEOUS_LOSSES]), 2)

    result: dict[str, Any] = {
        "sermaye": equity,
        "kasa_riski_pct": max_risk_pct,
        "islem_basi": per_symbol,
        "en_kotu_senaryo_tutar": worst_case,
        "en_kotu_senaryo_pct": (round(worst_case / equity * 100, 2)
                                if worst_case and equity else None),
        "acik_pozisyon": _value(ledger, "portfoy.acik_pozisyon"),
        "portfoy_isisi_pct": _value(ledger, "portfoy.heat_pct"),
        "notlar": [],
    }

    _add_notes(result, ledger, context)
    log.debug("risk ölçüldü: %s", {k: v for k, v in result.items() if k != "islem_basi"})
    return result


def _for_symbol(ledger: Ledger, symbol: str, equity: float | None,
                max_risk_pct: float) -> dict[str, Any]:
    """
    Tek bir varlık için pozisyon büyüklüğü ve stop mesafesi.

    Sıra şudur: önce KAYBEDİLEBİLECEK tutar belirlenir, sonra stop mesafesine
    bölünerek pozisyon büyüklüğü bulunur. Tersi (önce büyüklük, sonra stop)
    hesabın en yaygın ve en pahalı yanlışıdır.
    """
    row: dict[str, Any] = {"sembol": symbol}

    # Anahtar adları toplayıcıya göre değişir (`atr`, `atr_14`,
    # `indicators.atr_14`…). Tam ad beklemek, ölçüm ORADA OLDUĞU HÂLDE
    # "ölçülemedi" demeye yol açar — sessiz ve tehlikeli bir hata.
    price = _value(ledger, f"{symbol}.fiyat") or _find(ledger, symbol, "fiyat", "price")
    atr = _find(ledger, symbol, "atr")

    row["fiyat"] = price
    row["atr"] = atr

    if price is None or atr is None or atr <= 0:
        row["not"] = "Stop mesafesi için ATR ölçülemedi; pozisyon büyüklüğü hesaplanamaz."
        row["risk_tutari"] = None
        return row

    stop_distance = ATR_STOP_MULTIPLE * atr
    row["stop_mesafesi"] = round(stop_distance, 6)
    row["stop_pct"] = round(stop_distance / price * 100, 2) if price else None

    if equity:
        risk_amount = equity * max_risk_pct / 100.0
        row["risk_tutari"] = round(risk_amount, 2)
        row["pozisyon_adet"] = round(risk_amount / stop_distance, 6)
        row["pozisyon_tutari"] = round(risk_amount / stop_distance * price, 2)
    else:
        row["risk_tutari"] = None
        row["not"] = "Sermaye bilinmiyor; pozisyon büyüklüğü kullanıcı bakiyesine bağlı."

    return row


def _add_notes(result: dict[str, Any], ledger: Ledger,
               context: dict[str, Any]) -> None:
    """Ölçümlerden çıkan, kullanıcının bilmesi gereken uyarılar."""
    notes: list[str] = result["notlar"]

    heat = result.get("portfoy_isisi_pct")
    if isinstance(heat, (int, float)) and heat > 6:
        notes.append(
            f"Portföyde zaten %{heat:.1f} açık risk var. Yeni pozisyon bu ısıyı "
            f"artırır; çeşitlendirme değil yoğunlaştırma olabilir.")

    worst = result.get("en_kotu_senaryo_pct")
    if isinstance(worst, (int, float)) and worst > 5:
        notes.append(
            f"En kötü makul senaryoda sermayenin %{worst:.1f}'i kaybedilir. "
            f"Bu, tek günde toparlanması zor bir kayıptır.")

    if not result.get("sermaye"):
        notes.append(
            "Sermaye ölçülemedi. Pozisyon büyüklükleri örnek değil, "
            "hesaplanmamış demektir — bağlamadan önce bakiyenizi girin.")

    failed = context.get("failed") or []
    if failed:
        notes.append(
            f"Şu ölçümler alınamadı: {', '.join(str(f) for f in failed[:4])}. "
            f"Risk tablosu bu eksiklerle okunmalı.")

    spreads = [f for f in ledger.numbers() if f.key.endswith(".spread_pct")]
    wide = [f for f in spreads if (f.numeric or 0) > 0.5]
    if wide:
        names = ", ".join(f.key.split(".")[0] for f in wide[:3])
        notes.append(
            f"Geniş alış-satış makası: {names}. Giriş ve çıkışta beklenenden "
            f"fazla maliyet oluşur.")


def _value(ledger: Ledger, key: str) -> float | None:
    fact = ledger.get(key)
    return fact.numeric if fact else None


def _find(ledger: Ledger, symbol: str, *fragments: str) -> float | None:
    """
    Bir varlığın ölçümünü esnek adla arar.

    `BTC/USDT` için `BTC/USDT.indicators.atr_14`, `BTC/USDT.atr` ve
    `BTC/USDT.atr14` aynı şeyi gösterir; hepsini bulur. En SON yazılan
    eşleşme döner (defter ekleme-only'dur, sonuncusu en günceldir).
    """
    prefix = symbol.lower() + "."
    best: float | None = None
    for fact in ledger.numbers():
        key = fact.key.lower()
        if not key.startswith(prefix):
            continue
        tail = key[len(prefix):]
        if any(f in tail for f in fragments):
            best = fact.numeric
    return best
