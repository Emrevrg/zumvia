"""
PORTFÖY SEVİYESİ RİSK YÖNETİMİ
===============================
Katman 4 tek bir işlemi korur. Bu modül **tüm portföyü** korur.

Tek tek her işlem kurallara uysa bile portföy hâlâ patlayabilir:
  * 3 ayrı bot aynı anda %1.5 riskle long açarsa toplam risk %4.5 olur.
  * BTC, ETH ve SOL aynı anda long ise bu üç ayrı işlem değil, **tek bir bahistir**;
    piyasa döndüğünde üçü birden stop olur.
  * Spread'i geniş, likiditesi düşük bir pariteye girmek görünmeyen bir maliyettir.

Bu modül üç profesyonel korumayı uygular:
  1. **Portföy ısısı (heat)** — tüm açık pozisyonlardaki toplam risk tavanı.
  2. **Korelasyon kalkanı** — aynı yönde, birbirine bağlı varlıklarda küme riski.
  3. **Likidite/spread filtresi** — işlem maliyeti kabul edilebilir mi?

Tümü deterministiktir; hiçbir kararı dil modeli vermez.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

# Veri yoksa kullanılan sezgisel küme haritası (kripto majörleri birlikte hareket eder)
STATIC_CLUSTERS: dict[str, str] = {
    "BTC": "crypto_major", "ETH": "crypto_major", "SOL": "crypto_major",
    "BNB": "crypto_major", "XRP": "crypto_major", "ADA": "crypto_alt",
    "AVAX": "crypto_alt", "DOT": "crypto_alt", "LINK": "crypto_alt",
    "MATIC": "crypto_alt", "DOGE": "crypto_meme", "SHIB": "crypto_meme",
    "PEPE": "crypto_meme", "AAPL": "us_tech", "MSFT": "us_tech",
    "NVDA": "us_tech", "GOOGL": "us_tech", "AMZN": "us_tech",
    "META": "us_tech", "TSLA": "us_tech", "QQQ": "us_index", "SPY": "us_index",
}

# Bu eşiğin üstündeki korelasyon "aynı bahis" sayılır
CORRELATION_THRESHOLD = 0.70


@dataclass(slots=True)
class PortfolioVerdict:
    allowed: bool
    reason: str = ""
    code: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason,
                "code": self.code, "details": self.details}


def base_asset(symbol: str) -> str:
    """`BTC/USDT` → `BTC`, `THYAO.IS` → `THYAO`, `GC=F` → `GC`"""
    return (symbol.split("/", maxsplit=1)[0].split(".", maxsplit=1)[0].split("=", maxsplit=1)[0]).upper().strip()


def cluster_of(symbol: str) -> str:
    return STATIC_CLUSTERS.get(base_asset(symbol), f"other:{base_asset(symbol)}")


# --------------------------------------------------------------------------- #
#  1. Portföy ısısı
# --------------------------------------------------------------------------- #


def open_risk_amount(position) -> float:
    """
    Bir pozisyonun HÂLÂ risk altındaki tutarı.
    Stop başabaşa çekildiyse veya kâra geçtiyse risk sıfırlanmış sayılır —
    bu, iz süren stopun portföy ısısını gerçekten düşürdüğünü yansıtır.
    """
    entry = float(position.entry_price)
    stop = float(position.stop_loss)
    qty = float(position.qty)
    is_long = getattr(position.side, "value", position.side) == "long"

    per_unit = (entry - stop) if is_long else (stop - entry)
    if per_unit <= 0:          # stop kâr bölgesinde → risksiz işlem
        return 0.0
    return per_unit * qty


def portfolio_heat(positions: Iterable, equity: float) -> float:
    """Açık tüm pozisyonlardaki toplam riskin özkaynağa oranı (%)."""
    if equity <= 0:
        return 0.0
    total = sum(open_risk_amount(p) for p in positions)
    return round(total / equity * 100.0, 4)


# --------------------------------------------------------------------------- #
#  2. Korelasyon kalkanı
# --------------------------------------------------------------------------- #


def returns_correlation(a: pd.Series, b: pd.Series, lookback: int = 120) -> float | None:
    """
    İki fiyat serisinin getiri korelasyonu (Pearson).
    Ortak zaman damgası yoksa None döner — çağıran statik kümeye düşer.
    """
    try:
        ra = a.pct_change().dropna().tail(lookback)
        rb = b.pct_change().dropna().tail(lookback)
        joined = pd.concat([ra, rb], axis=1, join="inner").dropna()
        if len(joined) < 30:
            return None
        value = float(np.corrcoef(joined.iloc[:, 0], joined.iloc[:, 1])[0, 1])
        return value if np.isfinite(value) else None
    except Exception:  # noqa: BLE001 — korelasyon hesaplanamazsa akış durmaz
        return None


def correlated_exposure(new_symbol: str, new_side: str,
                        open_positions: Iterable,
                        price_series: dict[str, pd.Series] | None = None) -> dict[str, Any]:
    """
    Yeni işlem, mevcut açık pozisyonlarla aynı bahsin parçası mı?

    Önce gerçek getiri korelasyonuna bakılır; veri yoksa statik küme haritası
    kullanılır. Yalnızca **aynı yöndeki** pozisyonlar küme riski sayılır
    (ters yönlü pozisyon riski azaltır, artırmaz).
    """
    series = price_series or {}
    matches: list[dict[str, Any]] = []

    for position in open_positions:
        side = getattr(position.side, "value", position.side)
        if side != new_side:
            continue

        symbol = position.symbol
        correlation = None
        if new_symbol in series and symbol in series:
            correlation = returns_correlation(series[new_symbol], series[symbol])

        if correlation is not None:
            if abs(correlation) >= CORRELATION_THRESHOLD and correlation > 0:
                matches.append({"symbol": symbol, "correlation": round(correlation, 3),
                                "source": "returns"})
        elif cluster_of(symbol) == cluster_of(new_symbol):
            matches.append({"symbol": symbol, "cluster": cluster_of(symbol),
                            "source": "cluster"})

    return {"count": len(matches), "matches": matches}


# --------------------------------------------------------------------------- #
#  3. Bütünleşik portföy kapısı
# --------------------------------------------------------------------------- #


def check_portfolio_limits(*, equity: float, new_risk_amount: float,
                           open_positions: Iterable, new_symbol: str, new_side: str,
                           max_heat_pct: float = 3.0,
                           max_correlated: int = 1,
                           spread_pct: float | None = None,
                           max_spread_pct: float = 0.15,
                           price_series: dict[str, pd.Series] | None = None,
                           ) -> PortfolioVerdict:
    """
    Bir işlem Katman 4'ü geçtikten SONRA çalışan portföy kapısı.
    Reddederse işlem açılmaz — bu bir arıza değil, küme riskinden korunmadır.
    """
    positions = list(open_positions)

    # --- Likidite / spread ---
    if spread_pct is not None and spread_pct > max_spread_pct:
        return PortfolioVerdict(
            False,
            f"Spread çok geniş (%{spread_pct:.3f} > %{max_spread_pct:.3f}) — "
            "işlem maliyeti beklenen kârı yer.",
            "WIDE_SPREAD",
            {"spread_pct": spread_pct, "limit": max_spread_pct},
        )

    # --- Portföy ısısı ---
    current_heat = portfolio_heat(positions, equity)
    new_heat = current_heat + (new_risk_amount / equity * 100.0 if equity > 0 else 0.0)
    if new_heat > max_heat_pct + 1e-9:
        return PortfolioVerdict(
            False,
            f"Portföy ısısı tavanı aşılıyor: mevcut %{current_heat:.2f} + yeni işlem "
            f"= %{new_heat:.2f} (tavan %{max_heat_pct:.2f}). Önce mevcut riskin "
            "azalmasını bekle.",
            "PORTFOLIO_HEAT",
            {"current_heat_pct": current_heat, "projected_heat_pct": round(new_heat, 3),
             "limit_pct": max_heat_pct},
        )

    # --- Korelasyon kümesi ---
    exposure = correlated_exposure(new_symbol, new_side, positions, price_series)
    if exposure["count"] >= max_correlated:
        names = ", ".join(m["symbol"] for m in exposure["matches"])
        return PortfolioVerdict(
            False,
            f"Küme riski: {new_symbol} zaten açık olan {names} ile aynı yönde ve "
            "yüksek korelasyonlu. Bu ayrı bir işlem değil, aynı bahsin büyütülmesidir.",
            "CORRELATED_CLUSTER",
            exposure,
        )

    return PortfolioVerdict(
        True,
        f"Portföy uygun: ısı %{current_heat:.2f} → %{new_heat:.2f}, "
        f"korelasyonlu pozisyon {exposure['count']}",
        "OK",
        {"current_heat_pct": current_heat, "projected_heat_pct": round(new_heat, 3),
         "correlated": exposure["count"]},
    )


def portfolio_summary(positions: Iterable, equity: float) -> dict[str, Any]:
    """Arayüz ve ajan için portföy risk fotoğrafı."""
    items = list(positions)
    clusters: dict[str, int] = {}
    for position in items:
        key = cluster_of(position.symbol)
        clusters[key] = clusters.get(key, 0) + 1

    long_count = sum(1 for p in items
                     if getattr(p.side, "value", p.side) == "long")
    return {
        "open_positions": len(items),
        "heat_pct": portfolio_heat(items, equity),
        "risk_amount": round(sum(open_risk_amount(p) for p in items), 4),
        "risk_free_positions": sum(1 for p in items if open_risk_amount(p) == 0.0),
        "long": long_count,
        "short": len(items) - long_count,
        "clusters": clusters,
    }
