"""
SERMAYE MÜHENDİSLİĞİ — büyük parayı piyasayı bozmadan çalıştırmak
=================================================================

Küçük hesapla büyük hesap aynı şekilde işlem yapamaz. 1.000 $'lık bir emir
piyasaya görünmez; 5.000.000 $'lık bir emir piyasanın kendisidir. Aynı
stratejiyi büyük sermayeyle çalıştırmak, "daha çok kâr" değil, **kendi
emrinize karşı işlem yapmak** demektir:

  * Emrin kendisi fiyatı iter (market impact) — ortalama giriş fiyatınız bozulur,
  * Emir defteri derinliği bitince kalan kısım çok daha kötü fiyattan dolar,
  * Çıkışta aynı şey ters yönde tekrarlanır ve iki kere ödersiniz.

Bu modül, sermaye büyüdükçe stratejinin **matematiğini korur**:

  1. `liquidity_profile`  — enstrümanın gerçekten ne kadar emir kaldırabildiği
  2. `impact_estimate`    — bu büyüklükte emir fiyatı ne kadar iter
  3. `plan_execution`     — emri kaç parçaya, hangi aralıkla bölmeli (TWAP)
  4. `allocate`           — sermayeyi enstrümanlar arasında likiditeye göre dağıt

Burada **kâr hedefi büyütülmez, kâr korunur.** Büyük hesapta kaybedilen para
çoğunlukla yanlış yönde değil, doğru yönde ama kötü uygulanmış işlemlerdedir.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..core.logging import get_logger

log = get_logger("zumvia.capital")

# Bir emrin, ilgili barın hacminde kaplayabileceği azami pay.
# Kurumsal masalarda "participation rate" denir; %10-20 üzeri iz bırakır.
DEFAULT_PARTICIPATION = 0.08          # %8 — temkinli kurumsal varsayılan
MAX_PARTICIPATION = 0.25              # bunun üstü artık "piyasayı sürüklemek"

# Kabul edilebilir tahmini etki. Bunun üstünde emir bölünür ya da küçültülür.
IMPACT_WARN_PCT = 0.15                # %0.15 -> uyarı, plan bölünür
IMPACT_BLOCK_PCT = 0.60               # %0.60 -> bu büyüklük bu enstrümana sığmaz

MIN_SLICES = 1
MAX_SLICES = 24


# --------------------------------------------------------------------------- #
#  1. Likidite profili
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class LiquidityProfile:
    """Enstrümanın gerçek taşıma kapasitesi."""

    symbol: str
    price: float
    bar_volume: float                 # son barların medyan hacmi (adet)
    bar_turnover: float               # medyan hacim × fiyat (para birimi)
    daily_turnover: float             # günlük tahmini işlem hacmi
    spread_pct: float | None = None
    depth_quote: float | None = None  # emir defterinden okunan derinlik (varsa)
    volatility_pct: float | None = None   # günlük oynaklık (%) — etki modeli için
    tier: str = "normal"              # thin | normal | deep
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "price": round(self.price, 8),
            "bar_turnover": round(self.bar_turnover, 2),
            "daily_turnover": round(self.daily_turnover, 2),
            "spread_pct": self.spread_pct, "depth_quote": self.depth_quote,
            "volatility_pct": self.volatility_pct,
            "tier": self.tier, "notes": self.notes,
        }


def _bars_per_day(timeframe: str) -> float:
    minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
               "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720,
               "1d": 1440, "1w": 10080}.get((timeframe or "1h").lower(), 60)
    return max(1.0, 1440.0 / minutes)


def liquidity_profile(df: pd.DataFrame, symbol: str, timeframe: str = "1h", *,
                      spread_pct: float | None = None,
                      depth_quote: float | None = None,
                      lookback: int = 60) -> LiquidityProfile:
    """
    Son barlardan enstrümanın taşıma kapasitesini ölçer.

    Ortalama yerine **medyan** kullanılır: tek bir haber barındaki devasa hacim
    ortalamayı şişirir ve sistemi "bu piyasa her şeyi kaldırır" diye yanıltır.
    """
    if df is None or df.empty:
        return LiquidityProfile(symbol, 0.0, 0.0, 0.0, 0.0, spread_pct, depth_quote,
                                None, "unknown", ["Veri yok — likidite ölçülemedi."])

    tail = df.tail(max(10, lookback))
    price = float(tail["close"].iloc[-1])
    volume = pd.to_numeric(tail.get("volume"), errors="coerce").dropna()

    bar_volume = float(volume.median()) if not volume.empty else 0.0
    bar_turnover = bar_volume * price
    daily_turnover = bar_turnover * _bars_per_day(timeframe)

    notes: list[str] = []
    if bar_volume <= 0:
        notes.append("Hacim verisi yok; likidite bilinmiyor sayılır.")
        tier = "unknown"
    elif daily_turnover < 250_000:
        tier = "thin"
        notes.append("İnce piyasa: büyük emir burada fiyatı belirgin şekilde iter.")
    elif daily_turnover > 50_000_000:
        tier = "deep"
    else:
        tier = "normal"

    # Günlük oynaklık: bar getirilerinin standart sapması, güne ölçeklenir
    returns = tail["close"].pct_change().dropna()
    volatility_pct = None
    if len(returns) >= 10:
        volatility_pct = float(returns.std() * 100.0 * math.sqrt(_bars_per_day(timeframe)))
        volatility_pct = max(0.2, min(volatility_pct, 25.0))

    return LiquidityProfile(symbol, price, bar_volume, bar_turnover, daily_turnover,
                            spread_pct, depth_quote, volatility_pct, tier, notes)


# --------------------------------------------------------------------------- #
#  2. Etki tahmini
# --------------------------------------------------------------------------- #


def impact_estimate(order_quote: float, profile: LiquidityProfile) -> dict[str, Any]:
    """
    Bu büyüklükteki bir emrin fiyatı ne kadar iteceğini tahmin eder.

    Karekök etki modeli kullanılır (Almgren/Kyle ailesi): etki, emrin hacme
    oranının KAREKÖKÜYLE büyür. Kesin bir tahmin değildir — amacı "bu emir
    piyasaya sığar mı?" sorusuna deterministik bir cevap vermektir.
    """
    if order_quote <= 0:
        return {"impact_pct": 0.0, "participation": 0.0, "verdict": "ok"}

    # Karekök yasasının doğru paydası GÜNLÜK hacimdir; bar hacmi yalnızca
    # emri kaç parçaya böleceğimizi belirler.
    reference = profile.daily_turnover or profile.bar_turnover
    if reference <= 0:
        return {"impact_pct": None, "participation": None, "verdict": "unknown",
                "reason": "Likidite bilinmiyor; emir küçük tutulmalı."}

    participation = order_quote / reference

    # impact ≈ Y · σ · √(Q/V)
    #   Y ≈ 0.6  (ampirik katsayı, kurumsal literatürde 0.5–1.0)
    #   σ       günlük oynaklık (%). Ölçülemezse temkinli bir varsayılan.
    sigma = profile.volatility_pct if profile.volatility_pct else 2.5
    half_spread = (profile.spread_pct or 0.04) / 2.0
    impact_pct = half_spread + 0.6 * sigma * math.sqrt(max(participation, 0.0))

    verdict = "ok"
    if impact_pct >= IMPACT_BLOCK_PCT:
        verdict = "too_large"
    elif impact_pct >= IMPACT_WARN_PCT:
        verdict = "split"

    return {
        "impact_pct": round(impact_pct, 4),
        "participation": round(participation, 4),
        "cost_estimate": round(order_quote * impact_pct / 100.0, 2),
        "verdict": verdict,
    }


# --------------------------------------------------------------------------- #
#  3. Yürütme planı (emir bölme)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ExecutionPlan:
    """Bir emrin nasıl gerçekleştirileceği."""

    total_quote: float
    slices: int
    slice_quote: float
    interval_seconds: int
    participation: float
    impact_pct: float | None
    style: str                        # immediate | sliced | reduced | rejected
    reason: str = ""
    reduced_from: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_quote": round(self.total_quote, 2),
            "slices": self.slices,
            "slice_quote": round(self.slice_quote, 2),
            "interval_seconds": self.interval_seconds,
            "participation": self.participation,
            "impact_pct": self.impact_pct,
            "style": self.style,
            "reason": self.reason,
            "reduced_from": (round(self.reduced_from, 2)
                             if self.reduced_from is not None else None),
        }


def plan_execution(order_quote: float, profile: LiquidityProfile, *,
                   timeframe: str = "1h",
                   participation: float = DEFAULT_PARTICIPATION) -> ExecutionPlan:
    """
    Emri piyasanın kaldırabileceği parçalara böler.

    Mantık:
      * Emir barın hacmine göre küçükse tek seferde geçer (bölmek gereksiz
        komisyon ve zaman riski demektir).
      * Büyükse, her parçası hedef katılım oranını aşmayacak şekilde bölünür.
      * O kadar büyükse ki makul sayıda parçaya sığmıyorsa, emir **küçültülür**
        — ve bu kullanıcıya açıkça söylenir. Sığmayan pozisyon açılmaz.
    """
    participation = max(0.005, min(participation, MAX_PARTICIPATION))

    if order_quote <= 0:
        return ExecutionPlan(0.0, 0, 0.0, 0, 0.0, 0.0, "rejected", "Emir büyüklüğü sıfır.")

    reference = profile.depth_quote or profile.bar_turnover
    if reference <= 0:
        # Likidite bilinmiyorsa temkinli davran: tek parça ama küçük
        return ExecutionPlan(order_quote, 1, order_quote, 0, 0.0, None, "immediate",
                             "Likidite ölçülemedi; emir olduğu gibi ama küçük tutulmalı.")

    capacity_per_slice = reference * participation
    needed = math.ceil(order_quote / capacity_per_slice)

    bar_seconds = int(1440 / _bars_per_day(timeframe) * 60)
    impact = impact_estimate(order_quote, profile)

    if needed <= 1:
        return ExecutionPlan(order_quote, 1, order_quote, 0,
                             impact.get("participation") or 0.0,
                             impact.get("impact_pct"), "immediate",
                             "Emir bar hacminin içinde kalıyor; bölmeye gerek yok.")

    if needed <= MAX_SLICES:
        slices = max(MIN_SLICES, needed)
        interval = max(5, bar_seconds // max(1, slices))
        return ExecutionPlan(order_quote, slices, order_quote / slices, interval,
                             impact.get("participation") or 0.0,
                             impact.get("impact_pct"), "sliced",
                             f"Emir {slices} parçaya bölündü; her parça bar hacminin "
                             f"en fazla %{participation * 100:.1f}'i kadar.")

    # Makul parça sayısına sığmıyor: pozisyonu küçült
    max_quote = capacity_per_slice * MAX_SLICES
    reduced_impact = impact_estimate(max_quote, profile)
    return ExecutionPlan(
        max_quote, MAX_SLICES, max_quote / MAX_SLICES,
        max(5, bar_seconds // MAX_SLICES),
        reduced_impact.get("participation") or 0.0,
        reduced_impact.get("impact_pct"), "reduced",
        (f"Bu enstrümanın likiditesi {order_quote:,.0f} birimlik emri kaldırmıyor. "
         f"Pozisyon {max_quote:,.0f} birime küçültüldü — kalan sermaye başka "
         f"enstrümanda değerlendirilmeli."),
        reduced_from=order_quote,
    )


# --------------------------------------------------------------------------- #
#  4. Sermaye dağıtımı
# --------------------------------------------------------------------------- #


def allocate(total_capital: float, candidates: list[dict[str, Any]], *,
             max_per_instrument_pct: float = 35.0,
             min_ticket: float = 50.0) -> dict[str, Any]:
    """
    Sermayeyi adaylar arasında **likidite ve güven** ağırlıklı dağıtır.

    Neden tek enstrümana yüklenmiyoruz? Çünkü büyük sermayede tek enstrüman
    iki şeyi birden kaybettirir: likidite tükendiği için giriş fiyatı bozulur
    ve tüm bahis tek bir hikâyeye bağlanır. Dağıtım, kârı azaltmaz; kârın
    gerçekleşmesini mümkün kılar.

    `candidates` öğeleri: {"symbol", "score" (0-1), "liquidity": LiquidityProfile}
    """
    if total_capital <= 0 or not candidates:
        return {"allocations": [], "unallocated": round(max(0.0, total_capital), 2),
                "note": "Dağıtılacak sermaye veya aday yok."}

    rows = []
    for item in candidates:
        profile: LiquidityProfile | None = item.get("liquidity")
        score = max(0.0, min(float(item.get("score", 0.5)), 1.0))
        capacity = 0.0
        if profile is not None:
            # Bir enstrümanın makul taşıma kapasitesi: günlük hacmin küçük bir kısmı
            capacity = profile.daily_turnover * 0.01
        rows.append({"symbol": item.get("symbol", "?"), "score": score,
                     "capacity": capacity, "profile": profile})

    weight_total = sum(r["score"] for r in rows) or 1.0
    cap_per_instrument = total_capital * max_per_instrument_pct / 100.0

    allocations = []
    assigned = 0.0
    for row in rows:
        target = total_capital * (row["score"] / weight_total)
        target = min(target, cap_per_instrument)

        limited_by = None
        if row["capacity"] > 0 and target > row["capacity"]:
            target = row["capacity"]
            limited_by = "likidite"
        elif target >= cap_per_instrument - 1e-9:
            limited_by = "yoğunlaşma tavanı"

        if target < min_ticket:
            continue

        assigned += target
        allocations.append({
            "symbol": row["symbol"],
            "amount": round(target, 2),
            "share_pct": round(target / total_capital * 100.0, 2),
            "score": round(row["score"], 3),
            "limited_by": limited_by,
            "liquidity_tier": row["profile"].tier if row["profile"] else "unknown",
        })

    allocations.sort(key=lambda a: -a["amount"])
    unallocated = max(0.0, total_capital - assigned)

    note = "Sermaye güven ve likidite ağırlıklı dağıtıldı."
    if unallocated > total_capital * 0.2:
        note = (f"Sermayenin {unallocated / total_capital * 100:.0f}%'i nakitte "
                f"bırakıldı: mevcut adayların likiditesi fazlasını kaldırmıyor. "
                f"Zorlamak, giriş fiyatını bozardı.")

    return {
        "total_capital": round(total_capital, 2),
        "allocations": allocations,
        "unallocated": round(unallocated, 2),
        "instruments": len(allocations),
        "note": note,
    }
