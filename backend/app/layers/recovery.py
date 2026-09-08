"""
TOPARLANMA MOTORU (Recovery Engine)
====================================
"Zararı düzgünce telafi etmek" bu platformda bir duygu değil, **ölçülebilir bir
plandır**. Amatör yaklaşım kaybı hızlı kapatmak için riski büyütmektir
(martingale) — matematiksel olarak hesabı sıfırlayan tek davranış budur.

Profesyonel yaklaşım tersidir:

    Kayıp büyüdükçe → risk KÜÇÜLÜR, seçicilik ARTAR.

Neden? Drawdown asimetriktir:

    %10 kayıp → başabaş için %11.1 kazanç gerekir
    %20 kayıp → %25.0
    %30 kayıp → %42.9
    %50 kayıp → %100.0

Yani kayıp derinleştikçe geri dönüş katlanarak zorlaşır. Bu yüzden tek doğru
strateji, **kaybı derinleştirmemektir**. Bu modül drawdown'ı ölçer, hangi
aşamada olunduğunu belirler, kuralları sıkılaştırır ve kullanıcıya
"başabaşa dönüş için ne gerekiyor" sorusunun net sayısal cevabını verir.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
#  Aşamalar
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RecoveryPhase:
    id: str
    label: str
    min_drawdown_pct: float
    risk_multiplier: float          # işlem riski bu katsayıyla çarpılır
    confidence_bonus: float         # güven eşiğine eklenir
    min_agree_bonus: int            # konsensüs eşiğine eklenir
    max_positions: int | None       # eşzamanlı pozisyon tavanı (None = değişmez)
    counter_trend_allowed: bool
    description: str


PHASES: tuple[RecoveryPhase, ...] = (
    RecoveryPhase(
        id="normal", label="Normal", min_drawdown_pct=0.0,
        risk_multiplier=1.0, confidence_bonus=0.0, min_agree_bonus=0,
        max_positions=None, counter_trend_allowed=True,
        description="Hesap sağlıklı. Standart risk profili uygulanıyor.",
    ),
    RecoveryPhase(
        id="defensive", label="Savunma", min_drawdown_pct=5.0,
        risk_multiplier=0.5, confidence_bonus=0.08, min_agree_bonus=1,
        max_positions=2, counter_trend_allowed=False,
        description=("Zirveden %5+ düşüş. Risk yarıya indirildi, güven eşiği "
                     "yükseltildi, trende ters işlem kapatıldı."),
    ),
    RecoveryPhase(
        id="preservation", label="Sermaye Koruma", min_drawdown_pct=10.0,
        risk_multiplier=0.33, confidence_bonus=0.12, min_agree_bonus=2,
        max_positions=1, counter_trend_allowed=False,
        description=("Zirveden %10+ düşüş. Risk üçte bire indirildi, aynı anda "
                     "yalnızca 1 pozisyon, sadece A+ kurulumlar."),
    ),
    RecoveryPhase(
        id="lockdown", label="Kilit", min_drawdown_pct=18.0,
        risk_multiplier=0.2, confidence_bonus=0.18, min_agree_bonus=3,
        max_positions=1, counter_trend_allowed=False,
        description=("Zirveden %18+ düşüş. Minimum risk. Strateji gözden "
                     "geçirilmeli — bu parite/zaman dilimi çalışmıyor olabilir."),
    ),
)


def phase_for(drawdown_pct: float, consecutive_losses: int = 0) -> RecoveryPhase:
    """Drawdown ve üst üste zarar sayısına göre aşama seçer."""
    effective = drawdown_pct
    # Üst üste zararlar drawdown küçük olsa bile seçiciliği artırır
    if consecutive_losses >= 4:
        effective = max(effective, 10.0)
    elif consecutive_losses >= 2:
        effective = max(effective, 5.0)

    chosen = PHASES[0]
    for phase in PHASES:
        if effective >= phase.min_drawdown_pct:
            chosen = phase
    return chosen


# --------------------------------------------------------------------------- #
#  Plan
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RecoveryPlan:
    phase: RecoveryPhase
    equity: float
    peak_equity: float
    drawdown_pct: float
    required_gain_pct: float        # başabaşa dönmek için gereken kazanç
    effective_risk_pct: float
    required_r_multiple: float      # kaç R kazanç gerekiyor
    estimated_trades: int           # gerçekçi beklentiyle kaç işlem
    progress_pct: float             # dipten bu yana toparlanmanın yüzdesi
    rules: list[str] = field(default_factory=list)
    headline: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.id,
            "phase_label": self.phase.label,
            "phase_description": self.phase.description,
            "equity": round(self.equity, 2),
            "peak_equity": round(self.peak_equity, 2),
            "drawdown_pct": round(self.drawdown_pct, 3),
            "required_gain_pct": round(self.required_gain_pct, 3),
            "effective_risk_pct": round(self.effective_risk_pct, 4),
            "required_r_multiple": round(self.required_r_multiple, 2),
            "estimated_trades": self.estimated_trades,
            "progress_pct": round(self.progress_pct, 2),
            "rules": self.rules,
            "headline": self.headline,
        }


def build_recovery_plan(bot, equity: float, *, expectancy_r: float = 0.25,
                        trough_equity: float | None = None) -> RecoveryPlan:
    """
    Toparlanma planını hesaplar.

    `expectancy_r`: işlem başına beklenen R kazancı. Geçmiş performanstan
    ölçülebilir; ölçülemiyorsa muhafazakâr 0.25R varsayılır (yani iyi bir
    sistemin gerçekçi beklentisi).
    """
    from ..core.config import settings  # noqa: PLC0415 — döngüsel içe aktarma olmasın

    peak = max(float(bot.peak_equity or 0.0), float(equity))
    drawdown = ((peak - equity) / peak * 100.0) if peak > 0 else 0.0
    phase = phase_for(drawdown, int(getattr(bot, "consecutive_losses", 0)))

    base_risk = min(float(bot.risk_pct), settings.hard_max_risk_pct)
    effective_risk = max(0.05, round(base_risk * phase.risk_multiplier, 4))

    # Başabaş için gereken kazanç: kayıp asimetrisi
    required_gain = ((peak - equity) / equity * 100.0) if equity > 0 else 0.0

    # Kaç R gerekiyor? Her işlemde riske edilen tutar = equity * effective_risk%
    risk_per_trade = equity * effective_risk / 100.0
    required_r = (peak - equity) / risk_per_trade if risk_per_trade > 0 else 0.0
    estimated = int(max(0, round(required_r / max(expectancy_r, 0.05))))

    # İlerleme: en dipten bu yana ne kadar toparlandık?
    trough = float(trough_equity) if trough_equity else equity
    progress = 0.0
    if peak > trough:
        progress = max(0.0, min(100.0, (equity - trough) / (peak - trough) * 100.0))

    rules: list[str] = []
    if phase.id == "normal":
        rules.append("Standart risk profili — özel kısıtlama yok.")
    else:
        rules.append(f"İşlem riski %{base_risk:.2f} → %{effective_risk:.2f} "
                     f"({phase.risk_multiplier:.0%} katsayı)")
        rules.append(f"Güven eşiği +{phase.confidence_bonus:.2f} yükseltildi")
        rules.append(f"Konsensüs eşiği +{phase.min_agree_bonus} strateji")
        if phase.max_positions is not None:
            rules.append(f"Aynı anda en fazla {phase.max_positions} pozisyon")
        if not phase.counter_trend_allowed:
            rules.append("Trende ters işlem yasak")
        rules.append("Riski artırarak telafi (martingale) kod seviyesinde engelli")

    if drawdown < 0.01:
        headline = "Hesap zirvede — toparlanma gerekmiyor."
    elif phase.id == "normal":
        headline = (f"Küçük geri çekilme (%{drawdown:.2f}). Başabaş için "
                    f"%{required_gain:.2f} kazanç yeterli.")
    else:
        headline = (f"{phase.label} aşaması: zirveden %{drawdown:.2f} geride. "
                    f"Başabaş için %{required_gain:.2f} kazanç "
                    f"(≈ {required_r:.1f}R, tahmini {estimated} disiplinli işlem). "
                    f"Risk %{effective_risk:.2f}'e indirildi — kaybı kovalamıyoruz.")

    return RecoveryPlan(
        phase=phase, equity=float(equity), peak_equity=peak,
        drawdown_pct=drawdown, required_gain_pct=required_gain,
        effective_risk_pct=effective_risk, required_r_multiple=required_r,
        estimated_trades=estimated, progress_pct=progress,
        rules=rules, headline=headline,
    )


def measure_expectancy(closed_positions) -> float:
    """
    Geçmiş kapanmış işlemlerden gerçek beklenti (ortalama R).
    Yetersiz örnek varsa muhafazakâr varsayılan döner.
    """
    values = [float(p.r_multiple) for p in closed_positions if p.r_multiple]
    if len(values) < 8:
        return 0.25
    average = sum(values) / len(values)
    # Aşırı iyimserliği kırp: geçmiş performans geleceği garanti etmez
    return max(0.05, min(average, 1.0))
