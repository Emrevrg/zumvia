"""
ÖDEME GÜCÜ — kullanıcı borçlanamaz

Risk kalkanı stop'un ÇALIŞACAĞINI varsayar ve çoğu zaman haklıdır. Tam
olarak bu yüzden tehlikelidir: hesabı bitiren, stop'un çalışmadığı nadir
gündür — hafta sonu boşluğu, likidite kopuşu, işlem durdurma.

Uzun pozisyonda o gün kötüdür ama sınırlıdır (fiyat en fazla sıfıra iner).
Kısa pozisyonda kayıp TEORİK OLARAK SINIRSIZDIR ve bakiyenin altına inebilir.
Bakiyenin altına inen bir hesap borçtur.

Bu dosya iki şeyi koruyor: en kötü senaryonun ölçülüyor olmasını ve
bakiyenin sıfırın altına inememesini.
"""
from __future__ import annotations

import pytest

from app.layers import solvency
from app.layers.solvency import (
    ACCOUNT_FLOOR_PCT,
    GAP_STRESS_PCT,
    MAX_PORTFOLIO_STRESS_PCT,
    MAX_STRESS_LOSS_PCT,
    account_floor_breached,
    check_position,
    clamp_balance,
    worst_case_loss,
)


class FakePosition:
    def __init__(self, side: str, entry: float, qty: float) -> None:
        self.side = side
        self.entry_price = entry
        self.qty = qty


# --------------------------------------------------------------------------- #
#  En kötü senaryo matematiği
# --------------------------------------------------------------------------- #

def test_a_long_can_lose_at_most_what_it_is_worth() -> None:
    """Uzun pozisyonda fiyat en fazla sıfıra iner; kayıp sınırlıdır."""
    loss = worst_case_loss("long", entry=100.0, qty=10.0, gap_pct=100.0)
    assert loss == pytest.approx(1000.0)

    beyond = worst_case_loss("long", entry=100.0, qty=10.0, gap_pct=500.0)
    assert beyond == pytest.approx(1000.0), "uzun pozisyon değerinden fazlasını kaybetti"


def test_a_short_loss_is_not_capped_by_the_position_value() -> None:
    """
    Kısa pozisyonun asıl tehlikesi budur ve simetri varsayımı bunu gizler.

    Fiyat üçe katlanırsa kısa pozisyon, pozisyon değerinin İKİ KATINI
    kaybettirir — yani kasadan fazlasını.
    """
    doubled = worst_case_loss("short", entry=100.0, qty=10.0, gap_pct=200.0)
    assert doubled == pytest.approx(2000.0)
    assert doubled > 100.0 * 10.0, "kısa pozisyon değeriyle sınırlanmış"


def test_the_stress_assumption_is_not_wishful() -> None:
    """
    %25 keyfi bir sayı değil.

    Kriptoda tek günde bundan büyük hareketler oldu; hisse tarafında kâr
    açıklaması sonrası %20+ boşluklar olağandır. Daha küçük bir varsayım,
    gerçekten olan şeyi "olmaz" saymaktır.
    """
    assert GAP_STRESS_PCT >= 20.0


# --------------------------------------------------------------------------- #
#  Pozisyon kapısı
# --------------------------------------------------------------------------- #

def test_a_normal_position_passes() -> None:
    verdict = check_position(equity=10_000.0, side="long", entry=100.0, qty=10.0)
    assert verdict.allowed is True
    assert verdict.stress_loss_pct > 0


def test_a_position_that_could_wipe_the_account_is_refused() -> None:
    """
    Kasanın tamamını götürebilecek bir pozisyon açılmaz.

    Risk kalkanı bunu "sadece %1 risk aldık" diye geçirebilir; çünkü %1,
    stop çalıştığı varsayımıyla hesaplanır.
    """
    verdict = check_position(equity=10_000.0, side="short", entry=100.0, qty=200.0)

    assert verdict.allowed is False
    assert verdict.code == "STRESS_TOO_LARGE"
    assert verdict.stress_loss_pct > MAX_STRESS_LOSS_PCT


def test_the_refusal_explains_the_assumption(monkeypatch) -> None:
    """
    "Reddedildi" tek başına kullanıcıyı sistemin keyfi davrandığına inandırır.

    Hangi varsayımla reddedildiği yazılmalı: %25 ters hareket, %35 tavan.
    """
    verdict = check_position(equity=1_000.0, side="short", entry=100.0, qty=50.0)

    assert verdict.allowed is False
    assert "%25" in verdict.reason or "25" in verdict.reason
    assert "Tavan" in verdict.reason or "tavan" in verdict.reason


def test_positions_are_stressed_together_not_one_by_one() -> None:
    """
    Tek tek güvenli işlemler, toplamda güvenli olmayabilir.

    Piyasa düştüğünde her şey birlikte düşer — bu senaryo nadir değil,
    olağandır. Her pozisyonu izole sınamak, korelasyonu yok saymaktır.
    """
    # Her biri 8.000 birimlik üç açık pozisyon: tek tek %20 stres (geçer),
    # birlikte %60'ın üstü (geçmez).
    existing = [FakePosition("long", 100.0, 80.0) for _ in range(3)]

    alone = check_position(equity=10_000.0, side="long", entry=100.0, qty=80.0)
    together = check_position(equity=10_000.0, side="long", entry=100.0, qty=80.0,
                              open_positions=existing)

    assert alone.allowed is True
    assert together.allowed is False
    assert together.code == "PORTFOLIO_STRESS"


def test_a_broken_position_record_does_not_break_the_check() -> None:
    """Bozuk bir kayıt yüzünden kapının kapanması, kapının olmamasıdır."""
    class Broken:
        side = None
        entry_price = "abc"
        qty = None

    verdict = check_position(equity=10_000.0, side="long", entry=100.0, qty=5.0,
                             open_positions=[Broken()])
    assert verdict.allowed is True


def test_zero_equity_blocks_everything() -> None:
    verdict = check_position(equity=0.0, side="long", entry=100.0, qty=1.0)
    assert verdict.allowed is False
    assert verdict.code == "NO_EQUITY"


def test_the_portfolio_ceiling_is_above_the_single_ceiling() -> None:
    """
    Portföy tavanı tek pozisyon tavanından düşük olsaydı, tek başına geçen
    bir pozisyon hiçbir zaman açılamazdı — kapı kendi kendini kilitlerdi.
    """
    assert MAX_PORTFOLIO_STRESS_PCT > MAX_STRESS_LOSS_PCT


# --------------------------------------------------------------------------- #
#  Sıfır tabanı
# --------------------------------------------------------------------------- #

def test_a_balance_never_goes_below_zero() -> None:
    """
    Negatif kâğıt bakiyesi "borçla ticarete devam" gösterir.

    Kullanıcı gerçekte imkânsız olan bir toparlanma görür ve sisteme
    olmadığı kadar güvenir.
    """
    value, clamped = clamp_balance(-450.0)
    assert value == 0.0
    assert clamped is True


def test_a_positive_balance_is_untouched() -> None:
    value, clamped = clamp_balance(1234.56)
    assert value == pytest.approx(1234.56)
    assert clamped is False


def test_hitting_the_floor_is_reported_not_silently_fixed() -> None:
    """
    Kırpma SESSİZ olamaz: bakiye tabana çarptıysa hesap teorik olarak
    silinmiştir ve kullanıcının bunu bilmesi gerekir.
    """
    _, clamped = clamp_balance(-0.01)
    assert clamped is True, "taban çarpması bildirilmiyor"


# --------------------------------------------------------------------------- #
#  Kasa tabanı
# --------------------------------------------------------------------------- #

def test_the_account_floor_stops_the_melt() -> None:
    """
    Devre kesici GÜNLÜK kaybı sınırlar, kasa tabanı TOPLAM erimeyi.

    Her gün sınırın hemen altında kaybeden bir sistem, devre kesiciyi hiç
    tetiklemeden hesabı bitirebilir.
    """
    assert account_floor_breached(10_000.0, 2_000.0) is True
    assert account_floor_breached(10_000.0, 8_000.0) is False


def test_the_floor_message_states_the_recovery_math() -> None:
    """
    %75 kaybeden bir hesabın başabaşa dönmesi için %300 kazanması gerekir.

    Bu matematiği söylemek, "biraz daha deneyelim" cümlesini imkânsız kılar.
    """
    # %75 kaybetmiş bir hesap: 10.000 → 2.500. Başabaşa dönmek için %300.
    message = solvency.floor_message(10_000.0, 2_500.0)
    assert "300" in message, message
    assert "toparlanma değil" in message


def test_the_floor_is_low_enough_to_not_fire_on_a_normal_drawdown() -> None:
    """
    Taban çok yüksek olursa normal bir düşüşte tetiklenir ve sistemi
    çalışamaz hale getirir. %25, "artık toparlanamaz" seviyesidir.
    """
    assert 10.0 <= ACCOUNT_FLOOR_PCT <= 40.0
    assert account_floor_breached(10_000.0, 7_500.0) is False


def test_a_zero_initial_balance_does_not_trip_the_floor() -> None:
    """Sıfıra bölme yok: kurulum hatası, kilitlenmeye dönüşmemeli."""
    assert account_floor_breached(0.0, 0.0) is False
