"""
KATMAN 4 — KIRILMAZ PYTHON RİSK & GÜVENLİK KALKANI
===================================================
Bu katman sistemin **son sözü**dür. Yapay zeka ne derse desin, klasik strateji
ne sinyal verirse versin, buradaki kurallar geçilemez.

Uygulanan koruma zinciri:
  1. Zorunlu stop-loss  — SL yoksa veya mantıksızsa (alışta SL >= giriş) işlem YOK.
  2. Dinamik lot        — Pozisyon = (Kasa × Risk%) / |Giriş − SL|
  3. Risk/Ödül ≥ 1:2    — Asimetri yoksa işlem YOK.
  4. Güven eşiği        — confidence < eşik ise işlem YOK.
  5. Günlük devre kesici— Gün içi kayıp %3'e ulaşırsa tüm pozisyonlar kapanır,
                          bot 24 saat kilitlenir.
  6. Toplam drawdown    — Zirveden %X düşüşte bot durur.
  7. Eşzamanlı pozisyon ve günlük işlem sayısı tavanı.
  8. Toparlanma modu    — Zararda risk KÜÇÜLÜR, eşikler YÜKSELİR (asla martingale değil).
  9. Notional tavanı    — Kasanın tamamı tek pozisyona asla girmez.

Bu modül saf fonksiyoneldir: yan etkisi yoktur, kolayca test edilir.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..core.config import settings
from .recovery import phase_for

# Toplam notional, kasanın bu katından fazla olamaz (kaldıraçsız hesapta 1.0)
MAX_NOTIONAL_MULTIPLE = 1.0
# Stop mesafesi fiyatın en az/çok bu kadarı olmalı (gürültü ve aşırı risk koruması)
MIN_STOP_DISTANCE_PCT = 0.10
MAX_STOP_DISTANCE_PCT = 12.0


@dataclass(slots=True)
class RiskVerdict:
    """Risk kalkanının kararı."""
    allowed: bool
    reason: str = ""
    code: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason,
                "code": self.code, "details": self.details}


@dataclass(slots=True)
class SizedOrder:
    """Risk kalkanından geçmiş, boyutlandırılmış emir."""
    side: str                # "long" | "short"
    entry: float
    stop_loss: float
    take_profit: float
    qty: float
    notional: float
    risk_amount: float
    rr_ratio: float
    risk_pct_used: float


def today_key(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
#  1. Ön koşullar — bot yeni işlem açabilir mi?
# --------------------------------------------------------------------------- #


def check_preconditions(bot, equity: float, open_count: int,
                        now: datetime | None = None) -> RiskVerdict:
    """İşlem açmadan ÖNCE çalışan kapı. Tek bir kural bile ihlalse kapı kapalı."""
    now = now or datetime.now(UTC)

    # Kilit (devre kesici)
    if bot.locked_until:
        locked_until = bot.locked_until
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=UTC)
        if locked_until > now:
            kalan = int((locked_until - now).total_seconds() // 60)
            return RiskVerdict(
                False,
                f"Bot kilitli: {bot.lock_reason or 'devre kesici'} "
                f"(kalan {kalan // 60}s {kalan % 60}dk)",
                "LOCKED",
                {"locked_until": locked_until.isoformat()},
            )

    if equity <= 0:
        return RiskVerdict(False, "Kasa bakiyesi sıfır veya negatif.", "NO_EQUITY")

    # Eşzamanlı pozisyon tavanı (toparlanma aşamasında daralır)
    position_cap = effective_max_positions(bot, equity)
    if open_count >= position_cap:
        return RiskVerdict(
            False,
            f"Eşzamanlı pozisyon tavanı dolu ({open_count}/{position_cap}).",
            "MAX_POSITIONS",
            {"phase": current_phase(bot, equity).id},
        )

    # Günlük işlem sayısı (aşırı işlem = komisyon erimesi)
    # Kullanıcı risk bütçesini sıfırlamışsa sistem yalnızca izler.
    if user_risk_budget(bot) <= 0:
        return RiskVerdict(
            False,
            "Risk bütçeniz %0 — sistem yeni pozisyon açmıyor, yalnızca mevcut "
            "pozisyonları yönetiyor. Kontrol merkezinden yükseltebilirsiniz.",
            "RISK_BUDGET_ZERO",
        )

    if bot.day_key == today_key(now) and bot.day_trades >= bot.max_trades_per_day:
        return RiskVerdict(
            False,
            f"Günlük işlem tavanı doldu ({bot.day_trades}/{bot.max_trades_per_day}).",
            "MAX_TRADES",
        )

    # Günlük zarar limiti (devre kesici eşiği)
    daily_limit = min(bot.daily_loss_limit_pct, settings.hard_daily_loss_limit_pct)
    if bot.day_key == today_key(now) and bot.day_start_equity > 0:
        change = (equity - bot.day_start_equity) / bot.day_start_equity * 100.0
        if change <= -daily_limit:
            return RiskVerdict(
                False,
                f"Günlük zarar limiti aşıldı ({change:.2f}% / -{daily_limit:.2f}%).",
                "DAILY_LOSS",
                {"daily_change_pct": round(change, 3)},
            )

    # Toplam drawdown tavanı
    if bot.peak_equity > 0:
        dd = (bot.peak_equity - equity) / bot.peak_equity * 100.0
        if dd >= bot.max_drawdown_pct:
            return RiskVerdict(
                False,
                f"Maksimum drawdown aşıldı (%{dd:.2f} / %{bot.max_drawdown_pct:.2f}).",
                "MAX_DRAWDOWN",
                {"drawdown_pct": round(dd, 3)},
            )

    return RiskVerdict(True, "Ön koşullar uygun.", "OK")


# --------------------------------------------------------------------------- #
#  2. Karar doğrulama + boyutlandırma
# --------------------------------------------------------------------------- #


def current_phase(bot, equity: float | None = None):
    """Botun içinde bulunduğu toparlanma aşaması (drawdown + üst üste zarar)."""
    if equity is None:
        equity = getattr(bot, "paper_balance", None)
    if equity is None:
        equity = getattr(bot, "peak_equity", 0.0)
    value = float(equity or 0.0)
    peak = float(bot.peak_equity or 0.0)
    drawdown = ((peak - value) / peak * 100.0) if peak > 0 else 0.0
    return phase_for(drawdown, int(getattr(bot, "consecutive_losses", 0)))


def effective_risk_pct(bot, equity: float | None = None) -> float:
    """
    Kullanılacak gerçek risk yüzdesi.

    Toparlanma aşaması derinleştikçe risk KÜÇÜLÜR. Kayıptan sonra risk artırmak
    (martingale) hesabı sıfırlayan tek davranıştır; sistem buna izin vermez.
    Bkz. `layers/recovery.py` — aşama tablosu ve gerekçesi.
    """
    base = min(bot.risk_pct, settings.hard_max_risk_pct, user_risk_budget(bot))
    if base <= 0:
        return 0.0                       # bütçe 0 => yeni pozisyon yok
    phase = current_phase(bot, equity)
    return max(0.05, round(base * phase.risk_multiplier, 4))


def user_risk_budget(bot) -> float:
    """
    Kullanıcının tüm sistem için koyduğu risk tavanı.

    Botun kendi ayarı ne olursa olsun bunun üstüne çıkamaz. İlişki yüklenmemişse
    (oturum dışı nesne) tavan uygulanmaz — bu durumda `hard_max_risk_pct` zaten
    sınırı korur.
    """
    owner = getattr(bot, "user", None)
    budget = getattr(owner, "risk_budget_pct", None)
    return float(budget) if budget is not None else settings.hard_max_risk_pct


def effective_min_confidence(bot, equity: float | None = None) -> float:
    """Toparlanma aşamasında güven eşiği yükselir → daha seçici davranış."""
    base = max(bot.min_confidence, settings.hard_min_confidence)
    phase = current_phase(bot, equity)
    return round(min(0.97, base + phase.confidence_bonus), 4)


def effective_min_agree(bot, equity: float | None = None) -> int:
    """Toparlanma aşamasında daha fazla stratejinin mutabakatı istenir."""
    phase = current_phase(bot, equity)
    return max(1, int(getattr(bot, "min_agree", 2)) + phase.min_agree_bonus)


def effective_max_positions(bot, equity: float | None = None) -> int:
    """Toparlanma aşamasında eşzamanlı pozisyon sayısı kısılır."""
    phase = current_phase(bot, equity)
    limit = int(bot.max_open_positions)
    if phase.max_positions is not None:
        limit = min(limit, phase.max_positions)
    return max(1, limit)


def validate_and_size(bot, action: str, confidence: float, entry: float,
                      stop_loss: float, take_profit: float, equity: float,
                      atr_value: float = 0.0) -> tuple[RiskVerdict, SizedOrder | None]:
    """
    Bir işlem önerisini baştan sona denetler ve geçerse lot hesaplar.
    Dönen SizedOrder yalnızca `verdict.allowed == True` iken doludur.
    """
    action = (action or "WAIT").upper()
    if action not in ("BUY", "SELL"):
        return RiskVerdict(False, "Giriş kararı değil.", "NOT_ENTRY"), None

    if action == "SELL" and not bot.allow_short:
        return RiskVerdict(False, "Short işlem bu bot için kapalı.", "SHORT_DISABLED"), None

    # --- Güven eşiği ---
    min_conf = effective_min_confidence(bot, equity)
    if confidence < min_conf:
        return RiskVerdict(
            False,
            f"Güven eşiği altında ({confidence:.2f} < {min_conf:.2f}).",
            "LOW_CONFIDENCE",
            {"confidence": round(confidence, 3), "required": min_conf},
        ), None

    if entry <= 0:
        return RiskVerdict(False, "Geçersiz giriş fiyatı.", "BAD_ENTRY"), None

    side = "long" if action == "BUY" else "short"

    # --- Zorunlu ve mantıklı stop-loss ---
    if stop_loss <= 0:
        # Yapay zeka SL vermediyse ATR'den güvenli varsayılan üret; yoksa iptal.
        if atr_value > 0:
            stop_loss = entry - 1.8 * atr_value if side == "long" else entry + 1.8 * atr_value
        else:
            return RiskVerdict(False, "Stop-loss yok — işlem iptal.", "NO_STOP"), None

    if side == "long" and stop_loss >= entry:
        return RiskVerdict(
            False, f"Mantıksız stop-loss (alışta SL {stop_loss} >= giriş {entry}).",
            "ILLOGICAL_STOP",
        ), None
    if side == "short" and stop_loss <= entry:
        return RiskVerdict(
            False, f"Mantıksız stop-loss (satışta SL {stop_loss} <= giriş {entry}).",
            "ILLOGICAL_STOP",
        ), None

    stop_distance = abs(entry - stop_loss)
    stop_pct = stop_distance / entry * 100.0
    if stop_pct < MIN_STOP_DISTANCE_PCT:
        return RiskVerdict(
            False,
            f"Stop çok yakın (%{stop_pct:.3f}) — piyasa gürültüsüne takılır.",
            "STOP_TOO_TIGHT",
        ), None
    if stop_pct > MAX_STOP_DISTANCE_PCT:
        return RiskVerdict(
            False,
            f"Stop çok uzak (%{stop_pct:.2f}) — tek işlemde aşırı maruziyet.",
            "STOP_TOO_WIDE",
        ), None

    # --- Take-profit ve Risk/Ödül ---
    min_rr = max(bot.min_rr, settings.hard_min_rr_ratio)
    if take_profit <= 0:
        take_profit = (entry + min_rr * stop_distance if side == "long"
                       else entry - min_rr * stop_distance)

    if side == "long" and take_profit <= entry:
        return RiskVerdict(False, "Alışta take-profit girişin altında.", "ILLOGICAL_TP"), None
    if side == "short" and take_profit >= entry:
        return RiskVerdict(False, "Satışta take-profit girişin üstünde.", "ILLOGICAL_TP"), None

    reward = abs(take_profit - entry)
    rr = reward / stop_distance if stop_distance else 0.0
    if rr < min_rr:
        return RiskVerdict(
            False,
            f"Risk/Ödül yetersiz ({rr:.2f} < {min_rr:.2f}) — asimetri yok.",
            "LOW_RR",
            {"rr": round(rr, 3), "required": min_rr},
        ), None

    # --- Dinamik lot hesabı ---
    risk_pct = effective_risk_pct(bot, equity)
    risk_amount = equity * risk_pct / 100.0
    qty = risk_amount / stop_distance
    notional = qty * entry

    # Kasanın tamamı tek pozisyona giremez → notional tavanı
    max_notional = equity * MAX_NOTIONAL_MULTIPLE
    if notional > max_notional:
        qty = max_notional / entry
        notional = qty * entry
        risk_amount = qty * stop_distance

    if qty <= 0 or notional < 1e-8:
        return RiskVerdict(False, "Hesaplanan pozisyon büyüklüğü sıfır.", "ZERO_SIZE"), None

    order = SizedOrder(
        side=side,
        entry=round(entry, 10),
        stop_loss=round(stop_loss, 10),
        take_profit=round(take_profit, 10),
        qty=qty,
        notional=notional,
        risk_amount=risk_amount,
        rr_ratio=round(rr, 3),
        risk_pct_used=risk_pct,
    )
    return RiskVerdict(
        True,
        f"Onaylandı: risk %{risk_pct:.2f} ({risk_amount:.2f}), R/R {rr:.2f}",
        "APPROVED",
        {"rr": order.rr_ratio, "risk_amount": round(risk_amount, 4),
         "qty": qty, "notional": round(notional, 4)},
    ), order


# --------------------------------------------------------------------------- #
#  3. Açık pozisyon yönetimi (breakeven + trailing stop)
# --------------------------------------------------------------------------- #


def manage_open_position(bot, position, price: float,
                         atr_value: float = 0.0) -> tuple[float | None, str]:
    """
    Açık pozisyonun stop seviyesini günceller.
    Dönen: (yeni_stop | None, açıklama)

    - Fiyat `breakeven_at_r` kadar lehte gittiğinde stop girişe çekilir (risksiz işlem).
    - Ardından ATR tabanlı iz süren stop devreye girer; stop ASLA geriye alınmaz.
    """
    entry = position.entry_price
    initial_stop = position.initial_stop or position.stop_loss
    risk = abs(entry - initial_stop)
    if risk <= 0 or price <= 0:
        return None, ""

    is_long = position.side.value == "long" if hasattr(position.side, "value") else position.side == "long"
    r_now = ((price - entry) / risk) if is_long else ((entry - price) / risk)
    new_stop = position.stop_loss
    note = ""

    # 1) Başabaş koruması
    if bot.breakeven_at_r > 0 and r_now >= bot.breakeven_at_r:
        be = entry * (1.0005 if is_long else 0.9995)  # komisyonu da kurtaracak minik tampon
        if (is_long and be > new_stop) or ((not is_long) and be < new_stop):
            new_stop = be
            note = f"Stop başabaşa çekildi ({r_now:.2f}R kâr)"

    # 2) İz süren stop
    if bot.trailing_stop and atr_value > 0 and r_now >= max(bot.breakeven_at_r, 1.0):
        trail = price - 2.0 * atr_value if is_long else price + 2.0 * atr_value
        if (is_long and trail > new_stop) or ((not is_long) and trail < new_stop):
            new_stop = trail
            note = f"İz süren stop güncellendi ({r_now:.2f}R)"

    if abs(new_stop - position.stop_loss) < 1e-12:
        return None, ""
    return round(new_stop, 10), note


def check_exit(position, high: float, low: float, price: float) -> tuple[str, float] | None:
    """
    Mum içi SL/TP tetiklenmesini kontrol eder.
    Aynı mumda ikisi de tetiklenmişse **stop öncelikli** kabul edilir (muhafazakâr).
    """
    is_long = position.side.value == "long" if hasattr(position.side, "value") else position.side == "long"
    if is_long:
        if low <= position.stop_loss:
            return "STOP_LOSS", position.stop_loss
        if high >= position.take_profit:
            return "TAKE_PROFIT", position.take_profit
    else:
        if high >= position.stop_loss:
            return "STOP_LOSS", position.stop_loss
        if low <= position.take_profit:
            return "TAKE_PROFIT", position.take_profit
    return None


# --------------------------------------------------------------------------- #
#  4. Devre kesici ve toparlanma durumu
# --------------------------------------------------------------------------- #


def should_trip_circuit_breaker(bot, equity: float,
                                now: datetime | None = None) -> tuple[bool, str]:
    """Günlük zarar limiti veya toplam drawdown aşıldı mı?"""
    now = now or datetime.now(UTC)
    limit = min(bot.daily_loss_limit_pct, settings.hard_daily_loss_limit_pct)

    if bot.day_key == today_key(now) and bot.day_start_equity > 0:
        change = (equity - bot.day_start_equity) / bot.day_start_equity * 100.0
        if change <= -limit:
            return True, (
                f"GÜNLÜK DEVRE KESİCİ: Bakiye bugün %{abs(change):.2f} düştü "
                f"(limit %{limit:.2f}). Tüm pozisyonlar kapatıldı, bot "
                f"{settings.circuit_breaker_lock_hours} saat kilitlendi."
            )

    if bot.peak_equity > 0:
        dd = (bot.peak_equity - equity) / bot.peak_equity * 100.0
        if dd >= bot.max_drawdown_pct:
            return True, (
                f"MAKSİMUM DRAWDOWN: Zirveden %{dd:.2f} düşüş "
                f"(limit %{bot.max_drawdown_pct:.2f}). Bot durduruldu."
            )
    return False, ""


def lock_until(hours: int | None = None, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + timedelta(
        hours=hours if hours is not None else settings.circuit_breaker_lock_hours
    )


def update_recovery_state(bot, equity: float) -> tuple[bool, str]:
    """
    Toparlanma modunu değerlendirir.

    Kural: Zirveden %5+ düşüşte VEYA üst üste 2 zararda mod açılır.
    Modda risk yarıya iner, güven eşiği yükselir, yalnızca A+ kurulum alınır.
    Bakiye zirvenin %2 yakınına dönünce mod kapanır.
    """
    if bot.peak_equity <= 0:
        return bot.recovery_mode, ""

    dd = (bot.peak_equity - equity) / bot.peak_equity * 100.0
    was = bot.recovery_mode

    if not was and (dd >= 5.0 or bot.consecutive_losses >= 2):
        bot.recovery_mode = True
        return True, (
            f"TOPARLANMA MODU AÇILDI (drawdown %{dd:.2f}, "
            f"üst üste {bot.consecutive_losses} zarar). "
            "Risk yarıya indirildi, güven eşiği yükseltildi, yalnızca A+ kurulumlar alınacak."
        )

    if was and dd <= 2.0 and bot.consecutive_losses == 0:
        bot.recovery_mode = False
        return False, "TOPARLANMA TAMAMLANDI: Bakiye zirveye yaklaştı, normal risk profiline dönüldü."

    return was, ""


# --------------------------------------------------------------------------- #
#  5. Kismi kar alma (scale-out)
# --------------------------------------------------------------------------- #


def check_partial_take_profit(bot, position, price: float) -> tuple[float, str] | None:
    """
    Pozisyonun bir kismini erken kapatma karari.

    Neden: Kurumsal masalarin standart uygulamasi. Fiyat lehe `partial_tp_at_r`
    kadar gittiginde pozisyonun bir kismi kapatilir, kalan kisim iz suren stop
    ile calisir. Etkisi:
      * Kazanma orani yukselir (kismi kar cogu zaman realize edilir),
      * Kalan kisim risksiz hale gelir (stop basabasa cekilir),
      * Buyuk trendlerde kalan pozisyon kosmaya devam eder.

    Doner: (kapatilacak_oran, aciklama) veya None.
    """
    if not getattr(bot, "partial_tp_enabled", False):
        return None
    if getattr(position, "partial_taken", False):
        return None

    entry = float(position.entry_price)
    initial_stop = float(position.initial_stop or position.stop_loss)
    risk = abs(entry - initial_stop)
    if risk <= 0 or price <= 0:
        return None

    is_long = getattr(position.side, "value", position.side) == "long"
    r_now = ((price - entry) if is_long else (entry - price)) / risk

    target_r = max(0.5, float(getattr(bot, "partial_tp_at_r", 1.5)))
    if r_now < target_r:
        return None

    fraction = min(0.9, max(0.1, float(getattr(bot, "partial_tp_fraction", 0.5))))
    return fraction, (f"Kismi kar alindi: fiyat {r_now:.2f}R lehte, pozisyonun "
                      f"%{fraction * 100:.0f}'i kapatildi, kalan kisim risksiz devam ediyor")
