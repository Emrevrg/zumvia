"""
KATMAN 5 — İCRA MOTORU (Paper & Canlı)
=======================================
İki icra sağlayıcısı aynı arayüzü uygular:

  * `PaperBroker` — Sanal bakiye. Gerçek canlı fiyatlarla çalışır, sıfır risk.
                    Komisyon ve slipaj SİMÜLE EDİLİR ki sonuçlar gerçekçi olsun.
  * `LiveBroker`  — ccxt üzerinden gerçek emir iletimi.

Canlı modda tasarım kararı:
  Giriş emri piyasa emri olarak iletilir. Stop-loss / take-profit seviyeleri
  motor tarafından sunucu tarafında izlenir; borsa destekliyorsa ayrıca
  koruyucu stop emri de bırakılır. Böylece borsa farkları platformu kırmaz.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..core.logging import get_logger
from .l1_market_data import MarketDataError, get_exchange

log = get_logger("zumvia.layer5")

# Gerçekçi simülasyon varsayılanları
DEFAULT_TAKER_FEE_PCT = 0.075     # %0.075 (Binance taker, BNB indirimsiz ~0.1)
DEFAULT_SLIPPAGE_PCT = 0.02       # %0.02 piyasa emri kayması


@dataclass(slots=True)
class OrderResult:
    ok: bool
    filled_price: float = 0.0
    filled_qty: float = 0.0
    fee: float = 0.0
    order_id: str = ""
    error: str = ""
    raw: dict[str, Any] | None = None


class Broker(Protocol):
    """İcra sağlayıcı arayüzü."""

    def market_order(self, symbol: str, side: str, qty: float,
                     reference_price: float) -> OrderResult: ...


    def free_balance(self, currency: str) -> float: ...


# --------------------------------------------------------------------------- #
#  Paper (sanal) icra
# --------------------------------------------------------------------------- #


class PaperBroker:
    """
    Sanal icra. Kâr/zarar gerçek fiyat hareketinden hesaplanır; sadece emirler
    sanaldır. Komisyon ve slipaj uygulanır — aksi halde geri test yanıltıcı olur.
    """

    def __init__(self, balance: float, fee_pct: float = DEFAULT_TAKER_FEE_PCT,
                 slippage_pct: float = DEFAULT_SLIPPAGE_PCT) -> None:
        self.balance = float(balance)
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct

    def market_order(self, symbol: str, side: str, qty: float,
                     reference_price: float) -> OrderResult:
        if qty <= 0 or reference_price <= 0:
            return OrderResult(False, error="Geçersiz miktar veya fiyat.")

        # Alışta fiyat yukarı, satışta aşağı kayar (aleyhte slipaj — muhafazakâr)
        slip = self.slippage_pct / 100.0
        price = reference_price * (1 + slip) if side.lower() == "buy" else reference_price * (1 - slip)
        fee = price * qty * self.fee_pct / 100.0
        return OrderResult(True, filled_price=price, filled_qty=qty, fee=fee,
                           order_id=f"paper-{side}-{int(reference_price * 1e6)}")

    def free_balance(self, currency: str) -> float:  # noqa: ARG002
        return self.balance


# --------------------------------------------------------------------------- #
#  Canlı icra
# --------------------------------------------------------------------------- #


class LiveBroker:
    """ccxt tabanlı gerçek emir iletimi."""

    def __init__(self, exchange_id: str, api_key: str, secret: str,
                 password: str = "", sandbox: bool = False) -> None:
        self.exchange_id = exchange_id
        self.sandbox = sandbox
        self.ex = get_exchange(exchange_id, api_key, secret, password, sandbox)

    def market_order(self, symbol: str, side: str, qty: float,
                     reference_price: float) -> OrderResult:
        # UÇUŞ ÖNCESİ KONTROL: canlı emirlerin çoğu piyasa yüzünden değil,
        # borsa kuralları (min lot, min tutar, hassasiyet, bakiye) yüzünden
        # reddedilir. Bunları göndermeden önce biliriz.
        from .preflight import check as preflight_check  # noqa: PLC0415

        try:
            quote_ccy = symbol.rsplit("/", maxsplit=1)[-1]
            balance = self.free_balance(quote_ccy) if side.lower() == "buy" else None
        except Exception:  # noqa: BLE001 — bakiye okunamazsa kontrol atlanır
            balance = None

        verdict = preflight_check(self.ex, symbol, side, qty, reference_price,
                                  free_balance=balance)
        for warning in verdict.warnings:
            log.info("uçuş öncesi uyarı (%s): %s", symbol, warning)

        if not verdict.ok:
            return OrderResult(False, error=f"{verdict.reason} [{verdict.code}]")

        qty = verdict.qty
        try:
            order = self.ex.create_order(symbol, "market", side.lower(), qty)
        except Exception as exc:  # noqa: BLE001
            log.exception("Canlı emir hatası %s %s: %s", symbol, side, exc)
            return OrderResult(False, error=f"Borsa emri reddetti: {exc}")

        filled = float(order.get("average") or order.get("price") or reference_price or 0.0)
        amount = float(order.get("filled") or order.get("amount") or qty)
        fee_obj = order.get("fee") or {}
        fee = float(fee_obj.get("cost") or 0.0)
        return OrderResult(True, filled_price=filled, filled_qty=amount, fee=fee,
                           order_id=str(order.get("id", "")), raw=order)

    def place_protective_stop(self, symbol: str, side: str, qty: float,
                              stop_price: float) -> OrderResult:
        """
        Borsa destekliyorsa koruyucu stop emri bırakır.
        Desteklenmiyorsa sessizce atlanır — motor stop takibini kendisi yapar.
        """
        exit_side = "sell" if side.lower() == "buy" else "buy"
        try:
            order = self.ex.create_order(
                symbol, "stop_market", exit_side, qty, None,
                {"stopPrice": stop_price, "reduceOnly": True},
            )
            return OrderResult(True, order_id=str(order.get("id", "")), raw=order)
        except Exception as exc:  # noqa: BLE001
            log.info("Koruyucu stop emri bırakılamadı (%s): %s — motor takip edecek.",
                     symbol, exc)
            return OrderResult(False, error=str(exc)[:200])


    # ------------------------------------------------------------------ #
    #  Mutabakat için okuma uçları
    # ------------------------------------------------------------------ #

    def open_position_qty(self, symbol: str) -> float | None:
        """
        Borsadaki AÇIK pozisyon miktarı (yoksa 0.0, öğrenilemezse None).

        `None` ile `0.0` arasındaki fark kritiktir: "pozisyon yok" ile
        "öğrenemedim" karıştırılırsa, mutabakat gerçek pozisyonu yanlışlıkla
        kapatılmış sayar. Şüphede hiçbir şey yapılmaz.
        """
        try:
            if getattr(self.ex, "has", {}).get("fetchPositions"):
                for row in self.ex.fetch_positions([symbol]):
                    contracts = row.get("contracts") or row.get("contractSize") or 0
                    if row.get("symbol") == symbol and contracts:
                        return abs(float(contracts))
                return 0.0
        except Exception as exc:  # noqa: BLE001
            log.info("pozisyon okunamadı (%s): %s", symbol, exc)
            return None

        # Spot borsalarda "pozisyon" yoktur; taban varlık bakiyesine bakılır.
        try:
            base = symbol.split("/", maxsplit=1)[0]
            return float(self.free_balance(base))
        except Exception as exc:  # noqa: BLE001
            log.info("bakiye okunamadı (%s): %s", symbol, exc)
            return None

    def has_open_stop(self, symbol: str) -> bool | None:
        """Borsada bekleyen koruyucu stop emri var mı? (None = öğrenilemedi)"""
        try:
            orders = self.ex.fetch_open_orders(symbol)
        except Exception as exc:  # noqa: BLE001
            log.info("açık emirler okunamadı (%s): %s", symbol, exc)
            return None

        for order in orders:
            kind = str(order.get("type", "")).lower()
            params = order.get("info", {}) or {}
            if "stop" in kind or params.get("stopPrice") or order.get("stopPrice"):
                return True
        return False

    def free_balance(self, currency: str) -> float:
        try:
            bal = self.ex.fetch_balance()
            return float((bal.get("free") or {}).get(currency, 0.0))
        except Exception as exc:  # noqa: BLE001
            raise MarketDataError(f"Bakiye alınamadı: {exc}") from exc

    def test_connection(self) -> tuple[bool, str]:
        """API anahtarı geçerli mi ve okuma yetkisi var mı?"""
        try:
            bal = self.ex.fetch_balance()
            total = bal.get("total") or {}
            nonzero = {k: v for k, v in total.items() if v and float(v) > 0}
            return True, f"Bağlantı başarılı. Bakiyeli varlık: {len(nonzero)}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:300]


def build_broker(mode: str, balance: float, exchange_id: str = "",
                 api_key: str = "", secret: str = "", password: str = "",
                 sandbox: bool = False) -> Broker:
    """Mod'a göre uygun icra sağlayıcısını üretir."""
    if mode == "live":
        if not api_key or not secret:
            raise ValueError("Canlı mod için borsa API anahtarı zorunludur.")
        return LiveBroker(exchange_id, api_key, secret, password, sandbox)
    return PaperBroker(balance)
