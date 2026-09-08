"""
KANIT ÜRETİCİ — kütüphanenin görülmemiş veride toplu doğrulaması
=================================================================

"122 sistem var" bir sayıdır; kanıt değildir. Bu modül kütüphanedeki her
sistemi **görülmemiş veride** (walk-forward) sınar ve tek bir tabloda toplar:
hangisi hangi pariteye, hangi rejimde ne yaptı.

Ne olduğu ve ne OLMADIĞI
------------------------
✔ Görülmemiş veri testidir: eğitim penceresinde optimize edilmez, test
  penceresinde ölçülür. Aşırı uyum (overfit) farkı raporlanır.
✔ Karşılaştırılabilirdir: tüm sistemler aynı pariteler, aynı dönem, aynı
  komisyon ve slipaj varsayımıyla ölçülür.

✘ **Gelecek getiri vaadi değildir.** Geçmişte iyi çalışmış olmak, gelecekte
  çalışacağı anlamına gelmez.
✘ **Gerçek işlem kaydı değildir.** Emir defteri derinliği, gerçek dolum
  fiyatları ve kesintiler simüle edilir.

Bu ayrım raporun içine yazılır; kullanıcı "kanıt" ile "vaat" arasındaki farkı
tahmin etmek zorunda kalmaz.
"""
from __future__ import annotations

import json
import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.config import BASE_DIR
from ..core.logging import get_logger
from ..layers.playbooks import PLAYBOOKS, Playbook

log = get_logger("zumvia.evidence")

EVIDENCE_DIR = BASE_DIR / "reports" / "evidence"

# Kabul eşikleri — bir sistemin "kanıtlanmış" sayılması için
MIN_TRADES = 10
MIN_PROFIT_FACTOR = 1.15
MAX_OVERFIT_GAP = 0.60


@dataclass(slots=True)
class SystemEvidence:
    """Tek bir sistemin görülmemiş veri karnesi."""

    playbook_id: str
    label: str
    symbols: list[str] = field(default_factory=list)
    trades: int = 0
    profit_factor: float = 0.0
    win_rate: float = 0.0
    avg_r: float = 0.0
    max_drawdown: float = 0.0
    overfit_gap: float = 0.0
    verdict: str = ""
    passed: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "playbook_id": self.playbook_id, "label": self.label,
            "symbols": self.symbols, "trades": self.trades,
            "profit_factor": round(self.profit_factor, 3),
            "win_rate": round(self.win_rate, 2),
            "avg_r": round(self.avg_r, 3),
            "max_drawdown": round(self.max_drawdown, 2),
            "overfit_gap": round(self.overfit_gap, 3),
            "verdict": self.verdict, "passed": self.passed, "notes": self.notes,
        }


def _evaluate(playbook: Playbook, symbols: list[str], market: str,
              exchange: str, candles: int) -> SystemEvidence:
    """Bir sistemi verilen pariteler üzerinde walk-forward ile sınar."""
    from ..engine.optimizer import walk_forward  # noqa: PLC0415
    from ..layers.l1_market_data import fetch_ohlcv  # noqa: PLC0415

    evidence = SystemEvidence(playbook.id, playbook.label, list(symbols))
    factors: list[float] = []
    gaps: list[float] = []
    win_rates: list[float] = []
    r_values: list[float] = []
    drawdowns: list[float] = []

    for symbol in symbols:
        try:
            df = fetch_ohlcv(market, exchange, symbol, playbook.timeframe, limit=candles)
        except Exception as exc:  # noqa: BLE001 — veri yoksa o parite atlanır
            evidence.notes.append(f"{symbol}: veri alınamadı ({exc}).")
            continue

        try:
            report = walk_forward(
                df, symbol=symbol, timeframe=playbook.timeframe,
                strategies=list(playbook.strategies), min_agree=playbook.min_agree,
                risk_pct=playbook.risk_pct, allow_short=playbook.allow_short,
            )
        except Exception as exc:  # noqa: BLE001
            evidence.notes.append(f"{symbol}: doğrulama hatası ({exc}).")
            continue

        data = report.to_dict() if hasattr(report, "to_dict") else {}
        # Görülmemiş veri metrikleri `out_of_sample` altındadır; üst seviyeden
        # okumak sessizce sıfır verir ve her sistem "ölçülemedi" damgası yerdi.
        oos = data.get("out_of_sample") or {}
        trades = int(oos.get("trades") or 0)
        if trades <= 0:
            continue

        evidence.trades += trades
        factors.append(float(oos.get("profit_factor") or 0.0))
        gaps.append(float(data.get("overfit_gap") or 0.0))
        win_rates.append(float(oos.get("win_rate_pct") or 0.0))
        r_values.append(float(oos.get("avg_r") or oos.get("return_pct") or 0.0))
        drawdowns.append(float(oos.get("max_drawdown_pct") or 0.0))

    if not factors:
        evidence.verdict = "Yeterli veri veya işlem üretilmedi — ölçülemedi."
        return evidence

    evidence.profit_factor = statistics.median(factors)
    evidence.overfit_gap = statistics.median(gaps) if gaps else 0.0
    evidence.win_rate = statistics.mean(win_rates) if win_rates else 0.0
    evidence.avg_r = statistics.mean(r_values) if r_values else 0.0
    evidence.max_drawdown = max(drawdowns) if drawdowns else 0.0

    reasons = []
    if evidence.trades < MIN_TRADES:
        reasons.append(f"işlem sayısı yetersiz ({evidence.trades} < {MIN_TRADES})")
    if evidence.profit_factor < MIN_PROFIT_FACTOR:
        reasons.append(f"kâr faktörü düşük ({evidence.profit_factor:.2f} "
                       f"< {MIN_PROFIT_FACTOR})")
    if evidence.overfit_gap > MAX_OVERFIT_GAP:
        reasons.append(f"aşırı uyum farkı yüksek ({evidence.overfit_gap:.2f} "
                       f"> {MAX_OVERFIT_GAP})")

    evidence.passed = not reasons
    evidence.verdict = ("Görülmemiş veride geçti." if evidence.passed
                        else "Geçmedi: " + ", ".join(reasons))
    return evidence


def run(*, market: str = "crypto", exchange: str = "binance",
        symbols: list[str] | None = None, candles: int = 1500,
        playbook_ids: list[str] | None = None,
        workers: int = 4) -> dict[str, Any]:
    """
    Kütüphaneyi (ya da seçilen sistemleri) görülmemiş veride sınar.

    Yalnızca ÇEKİRDEK aileler test edilir: sürümler aynı tezin farklı vade ve
    risk ayarlarıdır; hepsini ayrı ayrı test etmek aynı kanıtı 9 kez saymak
    olurdu.
    """
    universe = symbols or ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]

    if playbook_ids:
        selected = [PLAYBOOKS[pid] for pid in playbook_ids if pid in PLAYBOOKS]
    else:
        selected = [pb for pb in PLAYBOOKS.values()
                    if not pb.family or pb.family == pb.id]

    started = datetime.now(UTC)
    log.info("kanıt üretimi başladı: %d sistem × %d parite", len(selected), len(universe))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(
            lambda pb: _evaluate(pb, universe, market, exchange, candles),
            selected,
        ))

    measured = [r for r in results if r.trades > 0]
    passed = [r for r in measured if r.passed]

    summary = {
        "generated_at": started.isoformat(),
        "market": market, "exchange": exchange,
        "symbols": universe, "candles": candles,
        "systems_tested": len(selected),
        "systems_measured": len(measured),
        "systems_passed": len(passed),
        "pass_rate_pct": round(len(passed) / len(measured) * 100, 1) if measured else 0.0,
        "median_profit_factor": round(
            statistics.median([r.profit_factor for r in measured]), 3) if measured else 0.0,
        "thresholds": {
            "min_trades": MIN_TRADES,
            "min_profit_factor": MIN_PROFIT_FACTOR,
            "max_overfit_gap": MAX_OVERFIT_GAP,
        },
        "systems": [r.to_dict() for r in sorted(
            results, key=lambda r: (-r.profit_factor, r.playbook_id))],
        "kapsam": (
            "Bu rapor GÖRÜLMEMİŞ veride ölçümdür (walk-forward). "
            "Gelecek getiri vaadi DEĞİLDİR ve gerçek işlem kaydı değildir; "
            "dolum fiyatları ve kesintiler simüle edilir."
        ),
    }

    _persist(summary)
    log.info("kanıt üretimi bitti: %d/%d sistem geçti",
             len(passed), len(measured))
    return summary


def _persist(summary: dict[str, Any]) -> Path | None:
    """Raporu diske yazar (JSON). Yazılamazsa sessizce geçilir."""
    try:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = EVIDENCE_DIR / f"evidence-{stamp}.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        return path
    except OSError as exc:
        log.info("kanıt raporu yazılamadı: %s", exc)
        return None


def latest() -> dict[str, Any] | None:
    """En son üretilen kanıt raporu."""
    try:
        files = sorted(EVIDENCE_DIR.glob("evidence-*.json"))
    except OSError:
        return None
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
