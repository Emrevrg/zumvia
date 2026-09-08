"""
Oturum çalıştırma kilidi — aynı görevin iki kez birden çalışmasını engeller.

Neden gerekli:

Bir ajan turu dakikalarca sürebilir. Bu sırada üç ayrı yerden yeni tur
başlatılabilir:

  * kullanıcı yeni mesaj gönderir,
  * kullanıcı "şimdi çalıştır" der,
  * otonom kalp atışı zamanlayıcısı tetikler.

Kilit olmadan bunlar üst üste biner. İki tur aynı geçmişi okur, aynı kararı
verir ve AYNI İŞLEMİ İKİ KEZ AÇAR. Para söz konusu olduğu için bu kabul
edilemez; bu yüzden tur başlatmak kilide bağlıdır.

Kilit süreç içidir ve bilinçli olarak öyledir: süreç çökerse kilit de gider,
yeniden başlatmada takılı kalmış "çalışıyor" durumu iş başlatmayı engellemez.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_GUARD = threading.Lock()
_ACTIVE: dict[int, float] = {}          # oturum kimliği -> başlangıç zamanı
_CANCELLED: set[int] = set()            # durdurulması istenen oturumlar


class SessionBusy(RuntimeError):
    """Oturumda zaten bir tur çalışıyor."""

    def __init__(self, session_id: int, running_for: float) -> None:
        self.session_id = session_id
        self.running_for = running_for
        super().__init__(f"oturum {session_id} zaten çalışıyor ({running_for:.0f} sn)")


@contextmanager
def hold(session_id: int) -> Iterator[None]:
    """
    Oturumu kilitler; zaten çalışıyorsa `SessionBusy` yükseltir.

    Bloklamaz: bekleyen bir kuyruk oluşturmak yerine çağıranın kararı
    kendisinin vermesini sağlar (kullanıcıya haber vermek mi, sessizce
    atlamak mı).
    """
    import time  # noqa: PLC0415

    with _GUARD:
        started = _ACTIVE.get(session_id)
        if started is not None:
            raise SessionBusy(session_id, time.monotonic() - started)
        _ACTIVE[session_id] = time.monotonic()
    try:
        yield
    finally:
        with _GUARD:
            _ACTIVE.pop(session_id, None)
            _CANCELLED.discard(session_id)


def is_running(session_id: int) -> bool:
    """Bu süreçte oturumun turu sürüyor mu."""
    with _GUARD:
        return session_id in _ACTIVE


def active_sessions() -> list[int]:
    """Şu anda çalışan oturumlar (tanılama ve arayüz için)."""
    with _GUARD:
        return sorted(_ACTIVE)


def release_all() -> None:
    """Yalnızca testler ve düzgün kapanış için."""
    with _GUARD:
        _ACTIVE.clear()


# --------------------------------------------------------------------------- #
#  Durdurma
# --------------------------------------------------------------------------- #
#  "Durdur" düğmesinin gerçekten durdurması gerekir. Yalnızca veritabanındaki
#  durum alanını değiştirmek yetmez: döngü çalışmaya devam eder ve bir sonraki
#  adımda o alanın üzerine yazar. Bu yüzden durdurma isteği burada, sürecin
#  belleğinde tutulur ve döngü her adımda buraya bakar.
#
#  Durdurma yalnızca AJAN TURUNU keser. Açık pozisyonlar, çalışan botlar ve
#  koruyucu stop emirleri etkilenmez — onlar kendi katmanlarında yaşar.


def cancel(session_id: int) -> bool:
    """Turu durdurmayı ister. Çalışan tur yoksa `False` döner."""
    with _GUARD:
        if session_id not in _ACTIVE:
            return False
        _CANCELLED.add(session_id)
        return True


def cancelled(session_id: int) -> bool:
    """Döngünün her adımda sorduğu soru: durdurulmam istendi mi."""
    with _GUARD:
        return session_id in _CANCELLED
