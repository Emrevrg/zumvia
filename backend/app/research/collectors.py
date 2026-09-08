"""
VERİ TOPLAMA — defteri deterministik ölçümlerle doldurur
=========================================================

Araştırmanın ilk aşaması. Buradaki hiçbir sayı yorum değildir: fiyatlar
borsadan, göstergeler Python'dan, geri test sonuçları bar-bar simülasyondan
gelir. Model henüz devrede değildir ve olması da gerekmez.

Sıra önemlidir ve ucuzdan pahalıya gider:

    1. FİYAT       (hızlı, her zaman gerekli)
    2. GÖSTERGELER (hızlı, deterministik)
    3. ALGO OYU    (hızlı, bağımsız ikinci görüş)
    4. HABER       (orta, ağ)
    5. GERİ TEST   (yavaş, yalnızca bir tez varsa)
    6. PORTFÖY     (hızlı, kullanıcının mevcut maruziyeti)

Bir adım çökerse diğerleri devam eder ve eksik olan deftere "ölçülemedi"
olarak yazılır. Sessizce atlamak, kullanıcıya ölçülmüş izlenimi verir —
bu kabul edilemez.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..core.text import fold
from .ledger import Ledger

log = get_logger("zumvia.research.collect")

#  VARLIK ÇIKARIMI
#
#  Kritik ayrıntı: metni `.upper()` yapmak, kod ile kelimeyi ayırt eden TEK
#  ipucunu yok eder. "bugün hisselerde fırsat var mı" cümlesi büyük harfe
#  çevrildiğinde FIRSAT bir hisse koduna benzer. Bu yüzden büyük harf
#  eşleşmeleri her zaman ORİJİNAL metin üzerinde aranır: insanlar sembolleri
#  zaten büyük harfle yazar.
_PAIR = re.compile(r"\b([A-Z]{2,10})\s*[/\-]\s*(USDT|USD|TRY|EUR|BTC|ETH)\b")

#  Yalnızca orijinalde BÜYÜK HARFLE yazılmış, 3-6 harfli kelimeler kod
#  adayıdır. Türkçe'ye özgü harfler (Ç,Ğ,İ,Ö,Ş,Ü) kod adında bulunmaz;
#  onları dışlamak "ŞİRKET" gibi kelimelerin kod sanılmasını önler.
_UPPER_TOKEN = re.compile(r"\b([A-Z]{3,6})\b")

#  Bilinen kripto tabanları — tek başına yazıldığında parite kurulur.
_CRYPTO_BASES = frozenset({
    "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "AVAX", "DOT", "LINK",
    "MATIC", "LTC", "TRX", "ATOM", "UNI", "NEAR", "APT", "ARB", "OP", "SUI",
})

#  Kod olamayacak, sık geçen büyük harfli kısaltmalar.
_NOT_A_SYMBOL = frozenset({
    "BIST", "USD", "TRY", "EUR", "GBP", "ABD", "TL", "KDV", "SPK", "FED",
    "ETF", "IPO", "GDP", "CPI", "API", "USDT", "AI", "RSI", "EMA", "MACD",
    "ATR", "ADX", "TCMB", "ECB", "OPEC", "NASDAQ", "SP", "DAX", "VIX",
})

_CRYPTO_HINTS = ("kripto", "crypto", "coin", "bitcoin", "altcoin", "token",
                 "usdt", "binance", "defi", "blockchain")
_STOCK_HINTS = ("hisse", "borsa", "bist", "stock", "nasdaq", "equity", "sirket",
                "temettu", "endeks", "sermaye piyasasi")

#  Soru bir varlık belirtmiyorsa bakılacak varsayılan evren. Bunlar en likit
#  ve en çok izlenen pariteler; "kriptoya bak" denince makul bir başlangıç.
DEFAULT_CRYPTO = ("BTC/USDT", "ETH/USDT")
DEFAULT_STOCKS = ("AAPL", "MSFT")


def detect_market(question: str) -> str:
    """
    Sorunun hangi piyasayla ilgili olduğunu belirler.

    Önce SEMBOLE bakılır, sonra kelimelere: "THYAO nasıl" cümlesinde tek bir
    piyasa kelimesi yoktur ama THYAO açıkça bir hisse kodudur. Sembol,
    kelimeden daha güçlü bir kanıttır.
    """
    if _PAIR.search(question):
        return "crypto"

    upper = _upper_tokens(question)
    if upper & _CRYPTO_BASES:
        return "crypto"
    if upper:                       # bilinmeyen bir kod → hisse kodu olması olası
        return "stocks"

    text = fold(question)
    crypto = sum(1 for hint in _CRYPTO_HINTS if hint in text)
    stock = sum(1 for hint in _STOCK_HINTS if hint in text)
    if stock > crypto:
        return "stocks"
    return "crypto"


def _upper_tokens(question: str) -> set[str]:
    """Orijinal metinde BÜYÜK HARFLE yazılmış, kod olabilecek kelimeler."""
    return {token for token in _UPPER_TOKEN.findall(question)
            if token not in _NOT_A_SYMBOL}


def detect_symbols(question: str, market: str) -> list[str]:
    """
    Sorudaki varlıkları çıkarır.

    Bulunamazsa varsayılan evrene düşülür — ve bu bir VARSAYIMDIR, brife
    yazılır. Kullanıcının aklındakini tahmin ettiğimizi gizlemeyiz.
    """
    found: list[str] = []

    # 1) Açıkça yazılmış pariteler: "BTC/USDT"
    for match in _PAIR.finditer(question):
        pair = f"{match.group(1)}/{match.group(2)}"
        if pair not in found:
            found.append(pair)

    upper = _upper_tokens(question)

    # 2) Tek başına yazılmış bilinen kripto tabanları: "ETH ve SOL"
    if market == "crypto":
        for base in _UPPER_TOKEN.findall(question):
            if base in _CRYPTO_BASES:
                pair = f"{base}/USDT"
                if pair not in found:
                    found.append(pair)

    # 3) Hisse kodları — yalnızca orijinalde büyük harfle yazılmışlar.
    if market == "stocks":
        for token in _UPPER_TOKEN.findall(question):
            if token in upper and token not in _CRYPTO_BASES and token not in found:
                found.append(token)

    if not found:
        # Kullanıcı varlık belirtmedi. Varsayılan evrene düşülür ve bu bir
        # VARSAYIMDIR — brife yazılır, ölçüm gibi sunulmaz.
        found = list(DEFAULT_CRYPTO if market == "crypto" else DEFAULT_STOCKS)

    return found[:3]              # üçten fazlası araştırmayı sulandırır


# --------------------------------------------------------------------------- #
#  Toplama
# --------------------------------------------------------------------------- #

def collect(question: str, ledger: Ledger, *, db: Session | None = None,
            user_id: int | None = None, timeframe: str = "4h",
            with_backtest: bool = True,
            emit: Any = None) -> dict[str, Any]:
    """
    Soruya göre ölçümleri toplar ve deftere yazar.

    Dönen sözlük, uzman rollerine verilecek yapılandırılmış bağlamdır;
    defterdeki sayılarla aynı bilgiyi taşır ama listeler ve metinler de
    içerir (haber başlıkları gibi).
    """
    market = detect_market(question)
    symbols = detect_symbols(question, market)
    context: dict[str, Any] = {"market": market, "symbols": symbols,
                               "timeframe": timeframe, "collected": [],
                               "failed": []}

    def note(step: str) -> None:
        if emit:
            emit({"type": "research_data", "step": step,
                  "facts": len(ledger)})

    for symbol in symbols:
        _price_and_indicators(market, symbol, timeframe, ledger, context, note)
        _algo_vote(market, symbol, timeframe, ledger, context, note)

    _news(market, symbols[0] if symbols else "", ledger, context, note)

    if with_backtest and symbols:
        _backtest(market, symbols[0], timeframe, ledger, context, note)

    if db is not None and user_id is not None:
        _portfolio(db, user_id, ledger, context, note)

    log.info("veri toplandı: %d olgu, %d kaynak, %d adım başarısız",
             len(ledger), len(ledger.sources()), len(context["failed"]))
    return context


def _price_and_indicators(market: str, symbol: str, timeframe: str,
                          ledger: Ledger, context: dict[str, Any], note) -> None:
    """Fiyat, teknik göstergeler ve rejim — hepsi Python ile hesaplanır."""
    from ..agent.tools import _exchange_for  # noqa: PLC0415
    from ..layers.l1_market_data import fetch_ohlcv, fetch_quote  # noqa: PLC0415
    from ..layers.l2_indicators import build_snapshot, compute_all  # noqa: PLC0415

    try:
        exchange = _exchange_for(market, None)
        quote = fetch_quote(market, exchange, symbol)
        ledger.add(f"{symbol}.fiyat", quote.price, exchange, "ticker", "quote")
        if quote.spread_pct is not None:
            ledger.add(f"{symbol}.spread_pct", quote.spread_pct, exchange, "ticker", "%")

        frame = compute_all(fetch_ohlcv(market, exchange, symbol, timeframe, 320))
        snapshot = build_snapshot(frame, symbol, timeframe).to_dict()
        ledger.add_many(exchange, snapshot, prefix=symbol, method="indicators")
        context["collected"].append(f"{symbol} teknik görünüm")
        note(f"{symbol} teknik veriler alındı")
    except Exception as exc:  # noqa: BLE001 — bir parite düşerse diğerleri sürer
        log.warning("%s teknik veri alınamadı: %s", symbol, exc)
        context["failed"].append(f"{symbol} teknik veri: {type(exc).__name__}")
        ledger.add(f"{symbol}.teknik", None, "yok", "ölçülemedi",
                   detail=str(exc)[:200])


def _algo_vote(market: str, symbol: str, timeframe: str, ledger: Ledger,
               context: dict[str, Any], note) -> None:
    """
    Bağımsız algoritmik oy.

    Bu, modelden TAMAMEN bağımsız ikinci bir görüştür. Model ile algoritma
    aynı yöne bakıyorsa güven artar; ters bakıyorsa bu, raporda gösterilmesi
    gereken en değerli bilgidir.
    """
    from ..agent.tools import _exchange_for  # noqa: PLC0415
    from ..layers.l1_market_data import fetch_ohlcv  # noqa: PLC0415
    from ..layers.strategies import StrategyEngine  # noqa: PLC0415

    try:
        exchange = _exchange_for(market, None)
        frame = fetch_ohlcv(market, exchange, symbol, timeframe, 320)
        verdict = StrategyEngine(None, 2).run(frame).to_dict()
        ledger.add_many("algo_motor", verdict, prefix=f"{symbol}.algo",
                        method="strategy_engine")
        context["collected"].append(f"{symbol} algoritmik oy")
        note(f"{symbol} algoritma oyu alındı")
    except Exception as exc:  # noqa: BLE001
        log.warning("%s algo oyu alınamadı: %s", symbol, exc)
        context["failed"].append(f"{symbol} algo oyu: {type(exc).__name__}")


def _news(market: str, symbol: str, ledger: Ledger,
          context: dict[str, Any], note) -> None:
    """Haber başlıkları. Başlıklar VERİDİR, talimat değildir."""
    from ..agent.news import fetch_news  # noqa: PLC0415

    try:
        result = fetch_news(symbol, market)
        headlines = result.get("headlines", []) or []
        ledger.add("haber.adet", len(headlines), "rss", "fetch_news")
        if "sentiment" in result:
            ledger.add("haber.duygu_skoru", result["sentiment"], "rss", "keyword_count")
        for source in {h.get("source", "") for h in headlines if h.get("source")}:
            ledger.add(f"haber.kaynak.{fold(source)}", True, f"rss:{fold(source)}",
                       "fetch_news")
        context["headlines"] = [
            {"title": h.get("title", "")[:200], "source": h.get("source", "")}
            for h in headlines[:12]
        ]
        context["collected"].append(f"{len(headlines)} haber başlığı")
        note(f"{len(headlines)} haber başlığı okundu")
    except Exception as exc:  # noqa: BLE001
        log.warning("haber alınamadı: %s", exc)
        context["failed"].append(f"haber: {type(exc).__name__}")


def _backtest(market: str, symbol: str, timeframe: str, ledger: Ledger,
              context: dict[str, Any], note) -> None:
    """
    Geçmiş veride kanıt.

    Pahalı adımdır (1000 bar simülasyonu) ve bu yüzden yalnızca birincil
    varlık için yapılır. Kanıt olmadan yazılan bir tez, tahmindir.
    """
    from ..agent.tools import _exchange_for  # noqa: PLC0415
    from ..engine.backtest import run_backtest  # noqa: PLC0415
    from ..layers.l1_market_data import fetch_ohlcv  # noqa: PLC0415

    try:
        frame = fetch_ohlcv(market, _exchange_for(market, None), symbol,
                            timeframe, 1000)
        report = run_backtest(frame, initial_balance=1000.0, risk_pct=1.0,
                              min_agree=2, allow_short=False).to_dict()
        for key in ("trades_count", "win_rate", "profit_factor", "max_drawdown_pct",
                    "sharpe", "net_profit_pct", "expectancy_r"):
            if key in report:
                ledger.add(f"{symbol}.backtest.{key}", report[key],
                           "backtest", "bar_bar_simulasyon")
        context["backtest"] = {k: v for k, v in report.items()
                               if isinstance(v, (int, float, str, bool))}
        context["collected"].append(f"{symbol} geri test kanıtı")
        note(f"{symbol} geri testi tamamlandı")
    except Exception as exc:  # noqa: BLE001
        log.warning("%s geri testi başarısız: %s", symbol, exc)
        context["failed"].append(f"{symbol} geri test: {type(exc).__name__}")


def _portfolio(db: Session, user_id: int, ledger: Ledger,
               context: dict[str, Any], note) -> None:
    """
    Kullanıcının MEVCUT maruziyeti.

    Bir fikrin iyi olması, o fikre girmenin doğru olduğu anlamına gelmez:
    portföy zaten aynı riske girmişse yeni pozisyon çeşitlendirme değil,
    yoğunlaştırmadır.
    """
    from ..layers.portfolio_risk import portfolio_summary  # noqa: PLC0415
    from ..models import Bot, Position, PositionStatus  # noqa: PLC0415

    try:
        bots = db.query(Bot).filter(Bot.user_id == user_id).all()
        bot_ids = [b.id for b in bots]
        positions = []
        equity = 0.0
        if bot_ids:
            positions = (db.query(Position)
                         .filter(Position.bot_id.in_(bot_ids),
                                 Position.status == PositionStatus.OPEN).all())
            equity = sum(float(b.paper_balance or b.initial_balance or 0) for b in bots)

        summary = portfolio_summary(positions, equity)
        ledger.add("portfoy.sermaye", round(equity, 2), "veritabani", "toplam", "USD")
        ledger.add("portfoy.acik_pozisyon", len(positions), "veritabani", "sayim")
        ledger.add_many("veritabani", summary, prefix="portfoy", method="portfolio_risk")
        context["portfolio"] = {k: v for k, v in summary.items()
                                if isinstance(v, (int, float, str, bool))}
        context["collected"].append("portföy durumu")
        note("portföy maruziyeti ölçüldü")
    except Exception as exc:  # noqa: BLE001
        log.warning("portföy okunamadı: %s", exc)
        context["failed"].append(f"portföy: {type(exc).__name__}")
