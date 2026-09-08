"""
ÖDEME GÜCÜ — kullanıcı BORÇLANAMAZ
===================================

Bu katman tek bir soruyu sorar: "en kötü ihtimalle kullanıcı ne kaybeder?"
Ve cevap asla "sahip olduğundan fazlasını" olamaz.

Neden ayrı bir katman? Çünkü risk kalkanı (`l4_risk`) NORMAL piyasayı
varsayar: stop konur, stop çalışır, kayıp riske edilen kadar olur. Bu
varsayım çoğu zaman doğrudur ve tam olarak bu yüzden tehlikelidir — stop'un
ÇALIŞMADIĞI durumlar nadirdir ama hesabı bitiren de onlardır.

Stop üç durumda çalışmaz:

    BOŞLUK      Piyasa kapalıyken haber çıkar, açılış stop'un çok altındadır.
    KOPUŞ       Likidite biter; emriniz stop fiyatından çok uzakta dolar.
    KESİNTİ     Borsa/işlem durdurulur; siz çıkamazken fiyat gider.

Uzun (long) pozisyonda bu kötüdür ama SINIRLIDIR: fiyat en fazla sıfıra
iner, kayıp pozisyonun değeri kadardır. Kısa (short) pozisyonda ise kayıp
TEORİK OLARAK SINIRSIZDIR: fiyat üçe katlanırsa kayıp pozisyonun iki katıdır
ve bakiyenin altına düşer. Bakiyenin altına düşen bir hesap, borçtur.

Bu yüzden burada iki sert kural vardır:

    1. STRES TESTİ  Her pozisyon, stop hiç çalışmamış gibi sınanır. Sert
                    ama gerçekçi bir ters hareket varsayılır; sonuç kasanın
                    belli bir yüzdesini aşıyorsa pozisyon AÇILMAZ.

    2. SIFIR TABANI Kâğıt bakiyesi sıfırın altına inemez. İnebilseydi geri
                    testler ve simülasyon "borçla ticaret" gösterirdi ve
                    kullanıcı gerçekte olmayan bir toparlanmayı görürdü.

Bu katman kâr ettirmez. Sadece kullanıcının, kaybedebileceğinden fazlasını
kaybetmesini imkânsız kılar.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.logging import get_logger

log = get_logger("zumvia.solvency")

# Stop'un hiç çalışmadığı varsayılan ters hareket.
#
# %25 keyfi değil: kriptoda tek günde bundan büyük hareketler oldu (Mayıs
# 2021, Kasım 2022, Mart 2020). Hisse tarafında kâr açıklaması sonrası %20+
# boşluklar olağandır. Daha küçük bir sayı seçmek, gerçekten olan şeyi
# "olmaz" saymaktır.
GAP_STRESS_PCT = 25.0

# Stres senaryosunda tek pozisyonun götürebileceği kasa yüzdesi tavanı.
MAX_STRESS_LOSS_PCT = 35.0

# Aynı senaryoda TÜM açık pozisyonların birlikte götürebileceği tavan.
# Tek tek güvenli işlemler, toplamda güvenli olmayabilir.
MAX_PORTFOLIO_STRESS_PCT = 60.0

# Kasa bu seviyenin altına inerse sistem kendini durdurur ve her şeyi kapatır.
# Buradan sonrası toparlanma değil, erimedir.
ACCOUNT_FLOOR_PCT = 25.0


@dataclass(slots=True)
class SolvencyVerdict:
    allowed: bool
    code: str = "OK"
    reason: str = ""
    stress_loss: float = 0.0
    stress_loss_pct: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "code": self.code, "reason": self.reason,
                "stres_kaybi": round(self.stress_loss, 2),
                "stres_kaybi_pct": round(self.stress_loss_pct, 2),
                **self.details}


# --------------------------------------------------------------------------- #
#  En kötü senaryo
# --------------------------------------------------------------------------- #

def worst_case_loss(side: str, entry: float, qty: float,
                    gap_pct: float = GAP_STRESS_PCT) -> float:
    """
    Stop hiç çalışmasaydı bu pozisyon ne kaybettirirdi?

    Uzun ve kısa pozisyonun matematiği SİMETRİK DEĞİLDİR ve bunu görmezden
    gelmek, kısa pozisyonun asıl tehlikesini gizler:

        UZUN   Fiyat en fazla sıfıra iner. Kayıp ≤ pozisyonun değeri.
        KISA   Fiyatın üst sınırı yoktur. %25 yukarı hareket, pozisyon
               değerinin %25'i kadar kayıptır — ve durmak zorunda değildir.
    """
    if entry <= 0 or qty <= 0:
        return 0.0

    notional = entry * qty
    if (side or "").lower() in ("long", "buy"):
        # Aşağı hareket pozisyon değeriyle sınırlıdır.
        return notional * min(gap_pct, 100.0) / 100.0
    # Yukarı hareketin sınırı yok; stres yüzdesi kadarını alırız.
    return notional * gap_pct / 100.0


def check_position(equity: float, side: str, entry: float, qty: float,
                   open_positions: list[Any] | None = None,
                   *, gap_pct: float = GAP_STRESS_PCT) -> SolvencyVerdict:
    """
    Bu pozisyon açılırsa, stop hiç çalışmazsa kullanıcı borçlanır mı?

    `open_positions` verilirse mevcut pozisyonlar da strese sokulur: hepsi
    aynı anda ters gidebilir ve genellikle de birlikte giderler — piyasa
    düştüğünde her şey düşer.
    """
    if equity <= 0:
        return SolvencyVerdict(
            allowed=False, code="NO_EQUITY",
            reason="Kasa sıfır ya da negatif; yeni pozisyon açılamaz.")

    loss = worst_case_loss(side, entry, qty, gap_pct)
    loss_pct = loss / equity * 100.0

    if loss_pct > MAX_STRESS_LOSS_PCT:
        return SolvencyVerdict(
            allowed=False, code="STRESS_TOO_LARGE",
            reason=(f"Stop hiç çalışmazsa bu pozisyon kasanın %{loss_pct:.1f}'ini "
                    f"götürür (%{gap_pct:.0f}'lik ters hareket varsayımıyla). "
                    f"Tavan %{MAX_STRESS_LOSS_PCT:.0f}. Pozisyon küçültülmeli."),
            stress_loss=loss, stress_loss_pct=loss_pct,
            details={"tavan_pct": MAX_STRESS_LOSS_PCT, "varsayim_pct": gap_pct})

    total = loss
    for pos in open_positions or []:
        try:
            total += worst_case_loss(
                pos.side.value if hasattr(pos.side, "value") else str(pos.side),
                float(pos.entry_price), float(pos.qty), gap_pct)
        except Exception as exc:  # noqa: BLE001 — bozuk kayıt toplamı bozmasın
            log.debug("stres hesabı atlandı: %s", exc)

    total_pct = total / equity * 100.0
    if total_pct > MAX_PORTFOLIO_STRESS_PCT:
        return SolvencyVerdict(
            allowed=False, code="PORTFOLIO_STRESS",
            reason=(f"Tüm pozisyonlar aynı anda ters giderse kasanın "
                    f"%{total_pct:.1f}'i gider. Tavan "
                    f"%{MAX_PORTFOLIO_STRESS_PCT:.0f}. Piyasa düştüğünde her "
                    f"şey birlikte düşer; bu senaryo nadir değildir."),
            stress_loss=total, stress_loss_pct=total_pct,
            details={"tavan_pct": MAX_PORTFOLIO_STRESS_PCT,
                     "acik_pozisyon": len(open_positions or [])})

    return SolvencyVerdict(
        allowed=True, code="OK",
        reason=(f"Stres testi geçildi: en kötü ihtimalle kasanın "
                f"%{loss_pct:.1f}'i (portföy toplamı %{total_pct:.1f})."),
        stress_loss=loss, stress_loss_pct=loss_pct,
        details={"portfoy_stres_pct": round(total_pct, 2)})


# --------------------------------------------------------------------------- #
#  Sıfır tabanı
# --------------------------------------------------------------------------- #

def clamp_balance(balance: float) -> tuple[float, bool]:
    """
    Bakiyeyi sıfırın altına düşmekten korur.

    Dönen ikinci değer "kırpıldı mı" bilgisidir ve SESSİZ GEÇİLMEZ: bakiye
    tabana çarptıysa kullanıcının hesabı teorik olarak silinmiş demektir ve
    bunu bilmesi gerekir.

    Neden kırpıyoruz: negatif bir kâğıt bakiyesi, geri testlerde ve
    simülasyonda "borçla ticarete devam" gösterir. Kullanıcı gerçekte
    imkânsız olan bir toparlanma görür ve sisteme olmadığı kadar güvenir.
    """
    if balance >= 0:
        return balance, False
    return 0.0, True


def account_floor_breached(initial_balance: float, equity: float) -> bool:
    """
    Kasa, tabanın altına indi mi?

    Buradan sonrası toparlanma değil erimedir: %75 kaybeden bir hesabın
    başabaşa dönmesi için %300 kazanması gerekir. Sistemin "biraz daha
    deneyelim" demesi, kalanı da götürmesidir.
    """
    if initial_balance <= 0:
        return False
    return equity < initial_balance * ACCOUNT_FLOOR_PCT / 100.0


def floor_message(initial_balance: float, equity: float) -> str:
    lost_pct = (1 - equity / initial_balance) * 100.0 if initial_balance else 0.0
    need_pct = ((initial_balance / equity - 1) * 100.0) if equity > 0 else float("inf")
    need = f"%{need_pct:.0f}" if need_pct != float("inf") else "sonsuz"
    return (f"KASA TABANI · Başlangıç sermayesinin %{100 - lost_pct:.0f}'i kaldı "
            f"(kayıp %{lost_pct:.0f}). Başabaşa dönmek için {need} kazanç "
            f"gerekir. Sistem yeni risk almayı durduruyor — bu noktadan sonra "
            f"devam etmek toparlanma değil, kalanı da kaybetmektir.")
