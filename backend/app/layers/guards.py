"""
ÖNLEM KATMANI (GUARDS) — sistemlerin zayıf yanını fiilen kapatır
=================================================================

Her ticaret sisteminin bir zayıf yanı vardır; bu doğanın kanunudur. Ama zayıf
yanı **bilmek** ile ona **karşı önlem almak** farklı şeylerdir. Bu modül,
kütüphanedeki her sistemin bilinen zaafını devreye girmeden önce durduran
deterministik filtreleri uygular.

Örnekler:
  * Trend takibi yatay piyasada testere yer      -> `adx_min` ile yatayda susar.
  * Ortalamaya dönüş trend başlayınca yıkılır    -> `adx_max` ile trendde susar.
  * Kırılım hacimsizken tuzağa düşer             -> `volume_z_min` ile teyit ister.
  * Gün içi sistem geniş spread'de komisyona yenilir -> `max_spread_pct`.
  * Zarardan sonra hemen aynı yere girmek        -> `cooldown_bars` ile bekler.
  * Aşırı volatilitede stop kayması              -> `atr_pct_max` ile durur.

Bu filtreler ÖNERİ değildir: `check_guards` girişten hemen önce çağrılır ve
reddederse pozisyon açılmaz. Hepsi ölçülen sayılara bakar, modele sormaz.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

# --------------------------------------------------------------------------- #
#  Tanımlı önlemler ve insan diliyle karşılıkları
# --------------------------------------------------------------------------- #

GUARD_LABELS: dict[str, str] = {
    "adx_min": "Trend gücü en az {v} olmalı (yatay piyasada susar)",
    "adx_max": "Trend gücü {v} üstündeyse işlem yok (trend başladıysa çekilir)",
    "volume_z_min": "Hacim ortalamanın en az {v} standart sapma üstünde olmalı",
    "atr_pct_min": "Volatilite %{v} altındaysa işlem yok (hareket yoksa girme)",
    "atr_pct_max": "Volatilite %{v} üstündeyse işlem yok (stop kayması riski)",
    "max_spread_pct": "Alış-satış farkı %{v} üstündeyse işlem yok",
    "cooldown_bars": "Zarardan sonra {v} bar bekler (peş peşe kayıp zinciri kırılır)",
    "max_trades_per_day": "Günde en fazla {v} işlem",
    "require_higher_tf_agreement": "Üst zaman diliminde trend aynı yönde olmalı",
    "min_candles": "En az {v} barlık geçmiş şart (yeni pariteye körlemesine girmez)",
}


#  KORUMA TABANI
#
#  `guards_json` varsayılanı boş sözlüktür. Ajanın kurduğu ya da elle
#  oluşturulmuş bir botta hiçbir koruma tanımlı olmayabilir — canlı koşuda
#  tam olarak bu görüldü: bot stop oldu ve aynı turda aynı yere yeniden
#  girdi.
#
#  Değerler bilinçli olarak düşük: amaç stratejiyi kısıtlamak değil,
#  kimsenin bilerek istemeyeceği üç kayıp kalıbını kapatmak.
BASELINE_GUARDS: dict[str, Any] = {
    "cooldown_bars": 2,        # zarardan sonra anında geri dönme
    "max_trades_per_day": 8,   # aşırı işlem, en sessiz para kaybı
    "max_spread_pct": 0.6,     # geniş makas: daha girerken kaybetmek
}


def with_baseline(guards: dict[str, Any] | None) -> dict[str, Any]:
    """
    Tanımlı korumaları taban değerlerle birleştirir.

    Açıkça verilen her değer tabanı EZER — yukarı da aşağı da. Kullanıcının
    bilinçli tercihi sistemin varsayımından üstündür; taban yalnızca
    SESSİZLİĞİ doldurur.

    `None` değeri de bilinçli bir tercihtir ("bu korumayı istemiyorum") ve
    korunur; yalnızca hiç bahsedilmemiş anahtarlar tabandan gelir.
    """
    merged = dict(BASELINE_GUARDS)
    merged.update(guards or {})
    return merged


def describe(guards: dict[str, Any]) -> list[str]:
    """
    Önlemleri kullanıcıya gösterilecek cümlelere çevirir (Türkçe).

    Ajan tarafı ve raporlar bunu kullanmaya devam eder. ARAYÜZ ise
    `describe_pairs` kullanır: hazır bir cümle çevrilemez, anahtar+değer
    çevrilebilir.
    """
    return [f"{item['key']}:{item['value']}" if False else item["text"]
            for item in describe_pairs(guards)]


def describe_pairs(guards: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Önlemleri ANAHTAR + DEĞER olarak döndürür.

    Arayüz cümleyi kendi dilinde kurar. Sunucu hazır Türkçe cümle
    gönderdiğinde İngilizce arayüzde de Türkçe kalıyordu ve bu, 122 sistemin
    her kartında tekrar ediyordu — tek bir düzeltmeyle hepsi çözülüyor.
    """
    out: list[dict[str, Any]] = []
    for key, value in (guards or {}).items():
        template = GUARD_LABELS.get(key)
        if not template:
            continue
        text = template.format(v=value) if "{v}" in template else template
        out.append({"key": key, "value": value, "text": text})
    return out


@dataclass(slots=True)
class GuardVerdict:
    allowed: bool
    reason: str = ""
    code: str = ""
    details: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = {}


OK = GuardVerdict(True)


# --------------------------------------------------------------------------- #
#  Denetim
# --------------------------------------------------------------------------- #

def check_guards(guards: dict[str, Any], df: pd.DataFrame, *,
                 action: str = "BUY",
                 spread_pct: float | None = None,
                 last_loss_at: datetime | None = None,
                 timeframe_minutes: int = 60,
                 trades_today: int = 0) -> GuardVerdict:
    """
    Sistemin önlemlerini ölçülen veriye uygular.

    Reddederse pozisyon AÇILMAZ. Her ret, sebebini ve ölçülen değeri taşır;
    böylece kullanıcı "neden işlem açmadı?" sorusunun cevabını görebilir.
    """
    # Taban EN BAŞTA uygulanır.
    #
    # Burada eskiden `if not guards: return OK` vardı: koruma tanımlanmamış
    # bir bot, hiçbir denetimden geçmeden onay alıyordu. Canlı koşuda
    # sonucu görüldü — bot stop oldu ve aynı turda aynı yere yeniden girdi.
    # Taban uygulandıktan sonra sözlük hiçbir zaman boş olmaz.
    guards = with_baseline(guards)

    if df is None or df.empty:
        return GuardVerdict(False, "Veri yok — önlemler doğrulanamadı", "NO_DATA")

    row = df.iloc[-1]

    def _num(name: str, default: float = 0.0) -> float:
        try:
            value = float(row.get(name, default))
        except (TypeError, ValueError):
            return default
        return default if math.isnan(value) else value      # NaN koruması

    # 1) Geçmiş derinliği
    min_candles = guards.get("min_candles")
    if min_candles and len(df) < int(min_candles):
        return GuardVerdict(False,
                            f"Yetersiz geçmiş: {len(df)} bar (en az {min_candles} gerekli)",
                            "MIN_CANDLES", {"bars": len(df)})

    # 2) Rejim filtresi — sistemin doğduğu ortam dışında susar
    adx = _num("adx")
    adx_min = guards.get("adx_min")
    if adx_min is not None and adx < float(adx_min):
        return GuardVerdict(False,
                            f"Trend gücü yetersiz (ADX {adx:.1f} < {adx_min}). "
                            f"Bu sistem yatay piyasada işlem açmaz.",
                            "ADX_TOO_LOW", {"adx": round(adx, 2), "limit": adx_min})

    adx_max = guards.get("adx_max")
    if adx_max is not None and adx > float(adx_max):
        return GuardVerdict(False,
                            f"Trend başlamış (ADX {adx:.1f} > {adx_max}). "
                            f"Bant sistemi trendde devre dışı kalır.",
                            "ADX_TOO_HIGH", {"adx": round(adx, 2), "limit": adx_max})

    # 3) Hacim teyidi — teyitsiz kırılım tuzaktır
    vol_min = guards.get("volume_z_min")
    if vol_min is not None:
        vol_z = _num("volume_z")
        if vol_z < float(vol_min):
            return GuardVerdict(False,
                                f"Hacim teyidi yok (z={vol_z:.2f} < {vol_min}). "
                                f"Teyitsiz kırılım tuzak olabilir.",
                                "NO_VOLUME", {"volume_z": round(vol_z, 2)})

    # 4) Volatilite bandı
    atr_pct = _num("atr_pct")
    atr_min = guards.get("atr_pct_min")
    if atr_min is not None and atr_pct < float(atr_min):
        return GuardVerdict(False,
                            f"Volatilite çok düşük (%{atr_pct:.2f}). Hareket yokken "
                            f"komisyon kârı yer.",
                            "ATR_TOO_LOW", {"atr_pct": round(atr_pct, 3)})

    atr_max = guards.get("atr_pct_max")
    if atr_max is not None and atr_pct > float(atr_max):
        return GuardVerdict(False,
                            f"Volatilite aşırı (%{atr_pct:.2f}). Stop kayması riski "
                            f"kabul edilemez.",
                            "ATR_TOO_HIGH", {"atr_pct": round(atr_pct, 3)})

    # 5) Likidite
    max_spread = guards.get("max_spread_pct")
    if max_spread is not None and spread_pct is not None and spread_pct > float(max_spread):
        return GuardVerdict(False,
                            f"Alış-satış farkı geniş (%{spread_pct:.3f}). "
                            f"İşlem maliyeti beklenen kârı aşar.",
                            "WIDE_SPREAD", {"spread_pct": round(spread_pct, 4)})

    # 6) Zarar sonrası soğuma — peş peşe kayıp zincirini kırar
    cooldown = guards.get("cooldown_bars")
    if cooldown and last_loss_at is not None:
        moment = last_loss_at if last_loss_at.tzinfo else last_loss_at.replace(tzinfo=UTC)
        wait_until = moment + timedelta(minutes=timeframe_minutes * int(cooldown))
        now = datetime.now(UTC)
        if now < wait_until:
            remaining = int((wait_until - now).total_seconds() // 60)
            return GuardVerdict(False,
                                f"Zarar sonrası soğuma: {remaining} dakika daha bekleniyor "
                                f"({cooldown} bar).",
                                "COOLDOWN", {"remaining_minutes": remaining})

    # 7) Günlük işlem tavanı — aşırı işlem, sistemin en sessiz para kaybı
    max_trades = guards.get("max_trades_per_day")
    if max_trades is not None and trades_today >= int(max_trades):
        return GuardVerdict(False,
                            f"Günlük işlem tavanı doldu ({trades_today}/{max_trades}).",
                            "TRADE_CAP", {"trades_today": trades_today})

    # 8) Üst zaman dilimi uyumu — karşı trende girmeyi engeller
    if guards.get("require_higher_tf_agreement"):
        ema50, ema200 = _num("ema_50"), _num("ema_200")
        if ema50 and ema200:
            uptrend = ema50 > ema200
            if action == "BUY" and not uptrend:
                return GuardVerdict(False,
                                    "Ana trend aşağı (EMA50 < EMA200); trende karşı alım yok.",
                                    "AGAINST_TREND", {"ema_50": ema50, "ema_200": ema200})
            if action == "SELL" and uptrend:
                return GuardVerdict(False,
                                    "Ana trend yukarı (EMA50 > EMA200); trende karşı satış yok.",
                                    "AGAINST_TREND", {"ema_50": ema50, "ema_200": ema200})

    return OK
