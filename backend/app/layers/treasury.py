"""
SERMAYE HARİTASI — "param şu an tam olarak nerede?"
====================================================

Portföy özeti "ne kadar kazandın" sorusunu cevaplar. Bu modül daha temel
bir soruyu cevaplar ve aslında ilk sorulması gereken odur:

    Toplam ne kadar koydum?
    Şu an ne kadarı nerede?
    Ne kadarı BOŞTA, ne kadarı PİYASADA, ne kadarı RİSKTE?

Üçü farklı şeylerdir ve karıştırıldığında kullanıcı yanılır:

    BOŞTA      Nakit. Hiçbir pozisyonda değil, kaybedilemez.
    PİYASADA   Pozisyonların içindeki para (notional). Dalgalanır.
    RİSKTE     Stop'lara kadar olan mesafenin toplamı. Her şey ters
               giderse GERÇEKTEN kaybedilecek olan budur.

"Piyasada 8.000 dolarım var" cümlesi korkutucudur; "riskte 240 dolarım var"
cümlesi gerçektir. İkisini ayırmak, kullanıcının paniğe kapılmadan doğru
kararı vermesini sağlar.

Ayrıca DAĞILIM gösterilir: hangi piyasada, hangi enstrümanda, hangi botta.
Tek bir varlıkta yoğunlaşma, sayı olarak görülmeden fark edilmez.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..models import Bot, BotStatus, Position, PositionStatus, TradingMode, User

log = get_logger("zumvia.treasury")

# Tek bir enstrümanda bu orandan fazlası varsa kullanıcı uyarılır.
CONCENTRATION_WARN_PCT = 40.0


@dataclass(slots=True)
class Slice:
    """Sermayenin bir dilimi."""

    label: str
    amount: float
    share_pct: float = 0.0
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"etiket": self.label, "tutar": round(self.amount, 2),
                "pay_pct": round(self.share_pct, 2), "aciklama": self.detail,
                **self.meta}


def _share(amount: float, total: float) -> float:
    return (amount / total * 100.0) if total else 0.0


def _live_price(bot: Bot) -> float | None:
    """
    Anlık fiyat. Alınamazsa `None` — sıfır DEĞİL.

    Sıfır dönmek, pozisyonun değerini sıfır göstermek demektir ve kullanıcı
    paranın buharlaştığını sanır. "Ölçülemedi" ile "sıfır" asla aynı şey
    değildir.
    """
    from .finance_hub import Instrument, tick  # noqa: PLC0415

    try:
        result = tick(Instrument(bot.symbol, bot.market, bot.exchange,
                                 bot.symbol, "kripto"), spark=False)
        return result.price if result.ok else None
    except Exception as exc:  # noqa: BLE001 — harita, fiyat alınamadı diye çökmez
        log.debug("%s fiyatı alınamadı: %s", bot.symbol, exc)
        return None


# --------------------------------------------------------------------------- #
#  Harita
# --------------------------------------------------------------------------- #

def snapshot(db: Session, user: User, *, live_prices: bool = True) -> dict[str, Any]:
    """
    Sermayenin tam dökümü.

    `live_prices=False` testler ve hızlı çağrılar için: anlık fiyat
    çekilmez, pozisyonlar giriş fiyatından değerlenir.
    """
    bots = db.query(Bot).filter(Bot.user_id == user.id).all()
    bot_ids = [b.id for b in bots]

    positions: list[Position] = []
    if bot_ids:
        positions = (db.query(Position)
                     .filter(Position.bot_id.in_(bot_ids),
                             Position.status == PositionStatus.OPEN).all())

    by_bot: dict[int, list[Position]] = defaultdict(list)
    for pos in positions:
        by_bot[pos.bot_id].append(pos)

    # ---------------------------------------------------------------- toplam
    total_deposited = sum(float(b.initial_balance or 0.0) for b in bots)
    # Hesap bakiyesi. Bu motorda bakiye pozisyon AÇILIRKEN düşmez; yalnızca
    # kapanışta kâr/zarar kadar değişir. Yani "boşta duran para" bakiyenin
    # kendisi DEĞİL, bakiyeden pozisyonlara bağlanan kısmı çıkarılmış hâlidir.
    balance = sum(float(b.paper_balance or 0.0) for b in bots)

    in_market = 0.0
    at_risk = 0.0
    unrealized = 0.0
    unpriced = 0

    per_bot: list[dict[str, Any]] = []
    by_symbol: dict[str, float] = defaultdict(float)
    by_market: dict[str, float] = defaultdict(float)
    by_mode: dict[str, float] = defaultdict(float)

    for bot in bots:
        rows = by_bot.get(bot.id, [])
        price = _live_price(bot) if (rows and live_prices) else None

        bot_notional = 0.0
        bot_risk = 0.0
        bot_unrealized = 0.0
        bot_unpriced = 0

        for pos in rows:
            entry = float(pos.entry_price or 0.0)
            qty = float(pos.qty or 0.0)
            mark = price if price else entry
            if price is None:
                bot_unpriced += 1

            notional = mark * qty
            bot_notional += notional

            # Riskteki tutar: giriş ile stop arasındaki mesafe. Stop yoksa
            # (olmamalı ama) pozisyonun tamamı risk sayılır — bilinmeyeni
            # küçük göstermek, en tehlikeli varsayımdır.
            stop = float(pos.stop_loss or 0.0)
            bot_risk += abs(entry - stop) * qty if stop > 0 else notional

            direction = 1.0 if pos.side.value == "long" else -1.0
            bot_unrealized += (mark - entry) * qty * direction

        in_market += bot_notional
        at_risk += bot_risk
        unrealized += bot_unrealized
        unpriced += bot_unpriced

        if bot_notional:
            by_symbol[bot.symbol] += bot_notional
            by_market[bot.market] += bot_notional
            by_mode[bot.mode.value] += bot_notional

        per_bot.append({
            "bot_id": bot.id, "ad": bot.name, "sembol": bot.symbol,
            "piyasa": bot.market, "mod": bot.mode.value,
            "durum": bot.status.value,
            "nakit": round(max(0.0, float(bot.paper_balance or 0.0) - bot_notional), 2),
            "piyasada": round(bot_notional, 2),
            "riskte": round(bot_risk, 2),
            "acik_kar_zarar": round(bot_unrealized, 2),
            "pozisyon": len(rows),
            "fiyat_olculemedi": bot_unpriced,
            "canli_mi": bot.mode == TradingMode.LIVE,
        })

    # BOŞTA olan para = bakiye − pozisyonlara bağlanan tutar.
    #
    # Bu çıkarma olmadan aynı para İKİ KEZ sayılıyordu: ekranda "1.000 boşta"
    # ve "213 piyasada" yazıyor, toplasan 1.213 ediyordu — oysa hesapta 1.000
    # vardı. Kullanıcının en temel sorusuna ("param nerede?") yanlış cevap
    # veren bir tablo, hiç olmamasından kötüdür.
    cash = max(0.0, balance - in_market)
    equity = balance + unrealized
    total = balance or 1.0

    # ------------------------------------------------------------ yoğunlaşma
    warnings: list[str] = []
    for symbol, amount in sorted(by_symbol.items(), key=lambda kv: -kv[1]):
        share = _share(amount, in_market) if in_market else 0.0
        if share > CONCENTRATION_WARN_PCT:
            warnings.append(
                f"Piyasadaki paranın %{share:.0f}'i tek bir enstrümanda "
                f"({symbol}). Tek hikâye ters giderse hepsi birlikte gider.")
        break

    if unpriced:
        warnings.append(
            f"{unpriced} pozisyonun anlık fiyatı ölçülemedi; bunlar giriş "
            f"fiyatından değerlendi. Gösterilen değer güncel olmayabilir.")

    live_amount = by_mode.get("live", 0.0)
    if live_amount > 0:
        warnings.append(
            f"{live_amount:,.2f} birim GERÇEK PARA piyasada. Bu tutar "
            f"gerçekten kaybedilebilir.")

    return {
        "ozet": {
            "toplam_yatirilan": round(total_deposited, 2),
            "guncel_ozkaynak": round(equity, 2),
            "net_kar_zarar": round(equity - total_deposited, 2),
            "getiri_pct": round(_share(equity - total_deposited, total_deposited), 2),
            "nakit": round(cash, 2),
            "piyasada": round(in_market, 2),
            "riskte": round(at_risk, 2),
            "acik_kar_zarar": round(unrealized, 2),
            "riskte_pct": round(_share(at_risk, equity), 2),
        },
        "dagilim": {
            "durum": [
                Slice("Boşta (nakit)", cash, _share(cash, total),
                      "Hiçbir pozisyonda değil; kaybedilemez.").to_dict(),
                Slice("Piyasada", in_market, _share(in_market, total),
                      "Pozisyonların içinde; dalgalanır.").to_dict(),
            ],
            "riskteki_tutar": {
                "tutar": round(at_risk, 2),
                "aciklama": ("Tüm stop'lar aynı anda çalışsa kaybedilecek "
                             "toplam. 'Piyasada' olan tutarla karıştırılmamalı "
                             "— piyasadaki para dalgalanır, riskteki para "
                             "kaybedilir."),
                "ozkaynak_pct": round(_share(at_risk, equity), 2),
            },
            "enstrumana_gore": [
                Slice(symbol, amount, _share(amount, in_market)).to_dict()
                for symbol, amount in sorted(by_symbol.items(), key=lambda kv: -kv[1])
            ],
            "piyasaya_gore": [
                Slice(market, amount, _share(amount, in_market)).to_dict()
                for market, amount in sorted(by_market.items(), key=lambda kv: -kv[1])
            ],
            "moda_gore": [
                Slice("Gerçek para" if mode == "live" else "Sanal",
                      amount, _share(amount, in_market)).to_dict()
                for mode, amount in sorted(by_mode.items(), key=lambda kv: -kv[1])
            ],
        },
        "botlar": sorted(per_bot, key=lambda r: -r["piyasada"]),
        "sayim": {
            "bot": len(bots),
            "calisan": sum(1 for b in bots if b.status == BotStatus.RUNNING),
            "kilitli": sum(1 for b in bots if b.status == BotStatus.LOCKED),
            "acik_pozisyon": len(positions),
        },
        "uyarilar": warnings,
        "kullaniciya_soyle": _headline(total_deposited, equity, cash,
                                       in_market, at_risk),
    }


def _headline(deposited: float, equity: float, cash: float,
              in_market: float, at_risk: float) -> str:
    """Tek cümlelik özet. Tabloya bakmadan anlaşılmalı."""
    if deposited <= 0:
        return "Henüz sermaye tanımlanmamış."

    change = equity - deposited
    verb = "kazançta" if change > 0 else "zararda" if change < 0 else "başabaşta"
    return (
        f"Toplam {deposited:,.0f} koydunuz, şu an {equity:,.0f} "
        f"({abs(change):,.0f} {verb}). Bunun {cash:,.0f} kadarı boşta, "
        f"{in_market:,.0f} kadarı piyasada. Her şey ters giderse "
        f"kaybedeceğiniz tutar {at_risk:,.0f} — piyasadaki tutarın tamamı "
        f"değil, stop'lara kadar olan mesafe kadarı."
    )
