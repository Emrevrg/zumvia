"""
UÇUŞ ÖNCESİ KONTROL — emir gönderilmeden önce borsanın kuralları
=================================================================

Canlı emirlerin çoğu piyasa yanlış gittiği için değil, **emir borsanın
kurallarına uymadığı için** reddedilir:

  * Miktar minimum lot büyüklüğünün altında,
  * Emrin parasal karşılığı minimum notional'ın altında (Binance'te 10 USDT),
  * Fiyat/miktar hassasiyeti (precision) borsanın istediği ondalık sayıda değil,
  * Bakiye yetersiz,
  * Parite o borsada yok ya da işleme kapalı.

Bunların hepsi emir gönderilmeden **önce** bilinebilir. Sistem bu kontrolleri
yapmadan gerçek paraya geçerse, ilk canlı emir aynı zamanda ilk test olur —
ve o test canlı parayla yapılır.

Bu modül `ccxt`'nin market meta verisini kullanır: `limits`, `precision`,
`active`. Borsa bu bilgiyi vermiyorsa kontrol **atlanır ve bu açıkça
söylenir** — uydurma bir limit kabul edilmez.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.logging import get_logger

log = get_logger("zumvia.preflight")


@dataclass(slots=True)
class PreflightResult:
    """Emrin gönderilmeye uygunluğu."""

    ok: bool
    qty: float                         # borsa hassasiyetine yuvarlanmış miktar
    reason: str = ""
    code: str = ""
    checks: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "qty": self.qty, "reason": self.reason,
                "code": self.code, "checks": self.checks, "warnings": self.warnings}


def _limit(market: dict[str, Any], group: str, bound: str) -> float | None:
    try:
        value = ((market.get("limits") or {}).get(group) or {}).get(bound)
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def check(exchange: Any, symbol: str, side: str, qty: float, price: float, *,
          free_balance: float | None = None) -> PreflightResult:
    """
    Emri göndermeden önce borsanın kurallarına uygunluğunu doğrular.

    `exchange` bir ccxt örneğidir. Market bilgisi okunamıyorsa emir
    engellenmez; yalnızca "doğrulanamadı" uyarısı eklenir — borsa meta verisi
    vermiyor diye ticareti durdurmak doğru olmaz.
    """
    checks: dict[str, Any] = {}
    warnings: list[str] = []

    if qty <= 0 or price <= 0:
        return PreflightResult(False, 0.0, "Miktar veya fiyat sıfır.", "INVALID_INPUT")

    market: dict[str, Any] | None = None
    try:
        markets = getattr(exchange, "markets", None)
        if not markets:
            exchange.load_markets()
            markets = getattr(exchange, "markets", {}) or {}
        market = markets.get(symbol)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Borsa bilgisi okunamadı ({exc}); kurallar doğrulanamadı.")

    if market is None:
        warnings.append(f"{symbol} borsa listesinde bulunamadı; kurallar doğrulanamadı.")
        return PreflightResult(True, qty, "Kurallar doğrulanamadı — dikkatli devam.",
                               "UNVERIFIED", checks, warnings)

    # ------------------------------------------------------------- 1. aktif mi
    if market.get("active") is False:
        return PreflightResult(False, 0.0,
                               f"{symbol} bu borsada işleme kapalı.",
                               "MARKET_INACTIVE", checks, warnings)

    # ------------------------------------------------- 2. hassasiyet (precision)
    adjusted = qty
    try:
        adjusted = float(exchange.amount_to_precision(symbol, qty))
        checks["precision_qty"] = adjusted
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Miktar hassasiyeti uygulanamadı ({exc}).")

    if adjusted <= 0:
        return PreflightResult(False, 0.0,
                               "Borsa hassasiyetine yuvarlandığında miktar sıfırlandı; "
                               "pozisyon minimum işlem büyüklüğünün altında.",
                               "QTY_ROUNDS_TO_ZERO", checks, warnings)

    # --------------------------------------------------------- 3. minimum lot
    min_amount = _limit(market, "amount", "min")
    if min_amount is not None:
        checks["min_amount"] = min_amount
        if adjusted < min_amount:
            return PreflightResult(
                False, adjusted,
                f"Miktar ({adjusted:.8g}) borsanın minimum lot büyüklüğünün "
                f"({min_amount:.8g}) altında.",
                "BELOW_MIN_AMOUNT", checks, warnings)

    max_amount = _limit(market, "amount", "max")
    if max_amount is not None and adjusted > max_amount:
        checks["max_amount"] = max_amount
        warnings.append(f"Miktar borsanın tek emir tavanını ({max_amount:.8g}) aşıyor; "
                        f"emir küçültüldü.")
        adjusted = max_amount

    # ---------------------------------------------------- 4. minimum notional
    notional = adjusted * price
    checks["notional"] = round(notional, 8)
    min_cost = _limit(market, "cost", "min")
    if min_cost is not None:
        checks["min_cost"] = min_cost
        if notional < min_cost:
            return PreflightResult(
                False, adjusted,
                f"Emrin parasal karşılığı ({notional:.2f}) borsanın minimum "
                f"tutarının ({min_cost:.2f}) altında.",
                "BELOW_MIN_NOTIONAL", checks, warnings)

    # ------------------------------------------------------------- 5. bakiye
    if free_balance is not None:
        checks["free_balance"] = free_balance
        needed = notional if side.lower() == "buy" else adjusted
        if needed > free_balance * 1.0001:
            return PreflightResult(
                False, adjusted,
                f"Bakiye yetersiz: gereken {needed:.4f}, kullanılabilir "
                f"{free_balance:.4f}.",
                "INSUFFICIENT_BALANCE", checks, warnings)

    return PreflightResult(True, adjusted,
                           "Borsa kurallarına uygun.", "OK", checks, warnings)


def dry_run(exchange: Any, symbol: str, side: str, qty: float,
            price: float) -> dict[str, Any]:
    """
    Emri göndermeden tam bir rapor üretir.

    Gerçek paraya geçmeden önce kullanıcıya "bu emir borsada kabul edilir mi?"
    sorusunun cevabını verir. Hiçbir emir gönderilmez.
    """
    balance = None
    try:
        base = symbol.split("/", maxsplit=1)[0]
        quote = symbol.rsplit("/", maxsplit=1)[-1]
        bal = exchange.fetch_balance()
        free = bal.get("free") or {}
        balance = float(free.get(quote if side.lower() == "buy" else base, 0.0))
    except Exception as exc:  # noqa: BLE001
        log.info("dry-run bakiyesi okunamadı: %s", exc)

    result = check(exchange, symbol, side, qty, price, free_balance=balance)
    return {
        "symbol": symbol, "side": side,
        "requested_qty": qty, "price": price,
        **result.to_dict(),
        "gonderilmedi": True,
        "not": "Bu bir PROVA'dır: hiçbir emir borsaya iletilmedi.",
    }
