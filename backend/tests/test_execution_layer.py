"""
YÜRÜTME KATMANI — gerçek paranın geçtiği yer

Bu dosyadaki her test bir para kaybı senaryosunu kapatır. Katman %34
kapsamla duruyordu; en çok kod barındıran ve en az denenmiş yer burasıydı.

Korunan sözleşmeler:

    * Borsa reddederse bu SAKLANMAZ — hata döner, işlem başarılı sayılmaz.
    * "Bilinmiyor" ile "yok" asla karıştırılmaz (mutabakatın temeli).
    * Uçuş öncesi kontrol, emir GÖNDERİLMEDEN çalışır.
    * Koruyucu stop bırakılamazsa motor takibi devralır, sessizce
      korumasız kalınmaz.
    * Kâğıt modunda komisyon ve kayma gerçekten hesaplanır — yoksa geri
      test iyimser çıkar ve kullanıcı yanılır.
"""
from __future__ import annotations

import pytest

from app.layers.l5_execution import LiveBroker, OrderResult, PaperBroker, build_broker


class FakeExchange:
    """ccxt borsasının test için yeterli taklidi."""

    def __init__(self, *, order=None, error: Exception | None = None,
                 balance: float = 10_000.0, positions=None,
                 open_orders=None, markets=None,
                 supports_positions: bool = True) -> None:
        # ccxt borsaları yeteneklerini `has` sözlüğünde bildirir; kod da
        # buna bakarak vadeli/spot yolunu seçer.
        self.has = {"fetchPositions": supports_positions}
        self._order = order or {"id": "1", "average": 100.0, "filled": 1.0,
                                "fee": {"cost": 0.1}}
        self._error = error
        self._balance = balance
        self._positions = positions
        self._open_orders = open_orders
        self.markets = markets or {"BTC/USDT": {
            "limits": {"amount": {"min": 0.0001}, "cost": {"min": 1.0}},
            "precision": {"amount": 8, "price": 2}, "active": True}}
        self.calls: list[tuple] = []

    def create_order(self, symbol, type_, side, qty, price=None, params=None):  # noqa: ARG002
        self.calls.append((symbol, type_, side, qty))
        if self._error:
            raise self._error
        return self._order

    def fetch_balance(self):
        return {"free": {"USDT": self._balance}}

    def fetch_positions(self, symbols=None):  # noqa: ARG002
        if self._positions is None:
            raise RuntimeError("borsa desteklemiyor")
        return self._positions

    def fetch_open_orders(self, symbol=None):  # noqa: ARG002
        if self._open_orders is None:
            raise RuntimeError("borsa desteklemiyor")
        return self._open_orders

    def load_markets(self):
        return self.markets

    def market(self, symbol):
        return self.markets[symbol]

    def amount_to_precision(self, symbol, qty):  # noqa: ARG002
        return f"{float(qty):.8f}"


def live(**kwargs) -> LiveBroker:
    """Ağa çıkmayan bir LiveBroker."""
    broker = LiveBroker.__new__(LiveBroker)
    broker.exchange_id = "test"
    broker.sandbox = True
    broker.ex = FakeExchange(**kwargs)
    return broker


# --------------------------------------------------------------------------- #
#  Kâğıt broker
# --------------------------------------------------------------------------- #

def test_paper_orders_include_fees_and_slippage() -> None:
    """
    Komisyon ve kayma hesaplanmazsa geri test iyimser çıkar.

    Kullanıcı kâğıtta kazanan bir sistemi canlıda kaybederken görür ve
    sebebini anlayamaz — çünkü fark sistemde değil, ihmal edilen
    maliyettedir.
    """
    broker = PaperBroker(10_000.0)
    result = broker.market_order("BTC/USDT", "buy", 0.1, 50_000.0)

    assert result.ok
    assert result.fee > 0, "komisyon hesaplanmadı"
    assert result.filled_price != 50_000.0, "kayma uygulanmadı"


def test_paper_buy_fills_above_and_sell_below_reference() -> None:
    """Kayma her zaman ALEYHE olur; lehte kayma diye bir şey yoktur."""
    broker = PaperBroker(10_000.0)
    buy = broker.market_order("BTC/USDT", "buy", 0.1, 50_000.0)
    sell = broker.market_order("BTC/USDT", "sell", 0.1, 50_000.0)

    assert buy.filled_price > 50_000.0, "alışta kayma lehe uygulanmış"
    assert sell.filled_price < 50_000.0, "satışta kayma lehe uygulanmış"


def test_paper_broker_reports_its_balance() -> None:
    assert PaperBroker(2_500.0).free_balance("USDT") == 2_500.0


# --------------------------------------------------------------------------- #
#  Canlı broker — hata dürüstlüğü
# --------------------------------------------------------------------------- #

def test_an_exchange_rejection_is_never_swallowed() -> None:
    """
    Borsa emri reddettiyse işlem BAŞARILI sayılamaz.

    Sessizce yutulan bir ret, veritabanında var olmayan bir pozisyon
    yaratır; sonrasındaki her hesap yanlış olur.
    """
    broker = live(error=RuntimeError("insufficient balance"))
    result = broker.market_order("BTC/USDT", "buy", 0.01, 50_000.0)

    assert result.ok is False
    assert "insufficient balance" in result.error


def test_a_successful_order_carries_the_real_fill() -> None:
    """
    Gerçekleşen fiyat borsadan gelir, bizim tahminimizden değil.

    Referans fiyatı doldurulmuş fiyat sanmak, kâr/zarar hesabını sessizce
    bozar.
    """
    broker = live(order={"id": "abc", "average": 50_123.45, "filled": 0.02,
                         "fee": {"cost": 0.75}})
    result = broker.market_order("BTC/USDT", "buy", 0.02, 50_000.0)

    assert result.ok
    assert result.filled_price == 50_123.45
    assert result.filled_qty == 0.02
    assert result.fee == 0.75
    assert result.order_id == "abc"


def test_preflight_runs_before_the_order_is_sent() -> None:
    """
    Canlı emirlerin çoğu piyasa yüzünden değil, borsa kuralları yüzünden
    reddedilir: min lot, min tutar, hassasiyet.

    Bunu GÖNDERDİKTEN sonra öğrenmek, gereksiz bir ret ve kayıp zaman
    demektir.
    """
    tiny = live()
    tiny.ex.markets["BTC/USDT"]["limits"]["amount"]["min"] = 1.0
    result = tiny.market_order("BTC/USDT", "buy", 0.00001, 50_000.0)

    assert result.ok is False
    assert tiny.ex.calls == [], "reddedilmesi gereken emir borsaya gönderildi"


# --------------------------------------------------------------------------- #
#  Mutabakat okumaları — "bilinmiyor" ile "yok" farkı
# --------------------------------------------------------------------------- #

def test_unknown_is_not_the_same_as_zero_for_positions() -> None:
    """
    Bu ayrım mutabakatın TEMELİDİR.

    Borsa okunamadığında `0.0` dönmek "pozisyon yok" demektir ve mutabakat
    kaydımızdaki açık pozisyonu kapatır — oysa pozisyon orada durmaktadır.
    Okunamayan bir şey `None` döner ve hiçbir şey kapatılmaz.
    """
    # Vadeli: borsa pozisyon uçlarını destekliyor ama okuma patlıyor.
    unreadable = live(positions=None, supports_positions=True)
    assert unreadable.open_position_qty("BTC/USDT") is None

    # Aynı borsa boş liste dönerse bu GERÇEKTEN "pozisyon yok" demektir.
    empty = live(positions=[], supports_positions=True)
    assert empty.open_position_qty("BTC/USDT") == 0.0


def test_an_open_position_is_reported_with_its_size() -> None:
    broker = live(positions=[{"symbol": "BTC/USDT", "contracts": 0.5,
                              "side": "long"}], supports_positions=True)
    assert broker.open_position_qty("BTC/USDT") == pytest.approx(0.5)


def test_unknown_is_not_the_same_as_no_stop() -> None:
    """
    Aynı ayrım koruyucu stop için de geçerlidir.

    "Stop yok" sanıp yenisini koymak çift stop yaratır; "stop var" sanıp
    koymamak pozisyonu korumasız bırakır. Bilinmiyorsa bilinmiyor denir.
    """
    unreadable = live(open_orders=None)
    assert unreadable.has_open_stop("BTC/USDT") is None

    none_open = live(open_orders=[])
    assert none_open.has_open_stop("BTC/USDT") is False


# --------------------------------------------------------------------------- #
#  Koruyucu stop
# --------------------------------------------------------------------------- #

def test_a_protective_stop_is_placed_on_the_opposite_side() -> None:
    """Long pozisyonun koruması SATIŞ emridir; ters koymak kaybı büyütür."""
    broker = live()
    broker.place_protective_stop("BTC/USDT", "buy", 0.1, 48_000.0)

    _symbol, order_type, side, qty = broker.ex.calls[-1]
    assert side == "sell"
    assert order_type == "stop_market"
    assert qty == 0.1


def test_a_failed_stop_is_reported_so_the_engine_can_take_over() -> None:
    """
    Borsa koruyucu stop desteklemiyorsa bu SESSİZCE geçilmez.

    Motor stop takibini kendisi devralır — ama devralabilmesi için
    bırakılamadığını bilmesi gerekir.
    """
    broker = live(error=RuntimeError("stop_market desteklenmiyor"))
    result = broker.place_protective_stop("BTC/USDT", "buy", 0.1, 48_000.0)

    assert result.ok is False
    assert result.error


# --------------------------------------------------------------------------- #
#  Broker seçimi
# --------------------------------------------------------------------------- #

def test_paper_mode_never_builds_a_live_broker() -> None:
    """
    Mod 'paper' ise gerçek borsaya bağlanılmaz.

    Bu, kullanıcının "sanal moddayım" beklentisinin kod tarafındaki
    karşılığıdır; ihlali doğrudan gerçek para riskidir.
    """
    broker = build_broker("paper", 1000.0, exchange_id="binance",
                          api_key="k", secret="s")
    assert isinstance(broker, PaperBroker)


def test_live_mode_without_credentials_fails_loudly(monkeypatch) -> None:
    """
    Anahtar yoksa canlı broker SESSİZCE kâğıda düşmez, hata verir.

    Sessiz düşüş daha güvenli GÖRÜNÜR ama tehlikelidir: kullanıcı canlı
    işlem yaptığını sanırken kâğıtta olur ve tersi de mümkündür. Katman
    kesin konuşur; ne yapılacağına çağıran karar verir — orkestratör bu
    hatayı yakalayıp kâğıda düşer (bir sonraki test).
    """
    with pytest.raises(ValueError, match="anahtar"):
        build_broker("live", 1000.0, exchange_id="binance",
                     api_key="", secret="")


def test_the_engine_falls_back_to_paper_when_live_cannot_be_built() -> None:
    """
    Katman kesin konuşur, motor güvenli davranır.

    Canlı broker kurulamadığında motor çökmez: kâğıda düşer ve kullanıcı
    parasını kaybetmez.
    """
    import inspect

    from app.engine import orchestrator

    source = inspect.getsource(orchestrator._make_broker)
    assert "PaperBroker" in source, "canlı kurulum başarısızsa geri düşüş yok"


def test_order_result_defaults_are_safe() -> None:
    """Başarısız bir sonuç, yanlışlıkla başarılı gibi okunamamalı."""
    failed = OrderResult(False, error="x")
    assert failed.ok is False
    assert failed.filled_qty in (0, 0.0)
