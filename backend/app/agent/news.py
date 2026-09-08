"""
HABER & DUYGU ARACI (anahtar gerektirmez)
==========================================
Ücretsiz RSS kaynaklarından başlık toplar ve deterministik anahtar-kelime
skorlamasıyla kaba bir duygu ölçüsü üretir. Skor sayısaldır (LLM üretmez);
başlıklar ise komuta ajanına ham metin olarak verilir, yorumu o yapar.

Kaynaklar anahtar istemez ve hepsi halka açık RSS'tir.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ..core.logging import get_logger

log = get_logger("zumvia.news")

CRYPTO_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://cryptoslate.com/feed/",
]
MACRO_FEEDS = [
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://finance.yahoo.com/news/rssindex",
]
SYMBOL_FEED = ("https://feeds.finance.yahoo.com/rss/2.0/headline"
               "?s={symbol}&region=US&lang=en-US")

# Deterministik duygu sözlüğü (ağırlıklı)
POSITIVE = {
    "surge": 3, "rally": 3, "soar": 3, "jump": 2, "gain": 2, "rise": 2, "bullish": 3,
    "record high": 4, "all-time high": 4, "breakout": 3, "adoption": 2, "approval": 3,
    "approved": 3, "inflow": 2, "upgrade": 2, "beat": 2, "profit": 2, "growth": 2,
    "partnership": 2, "etf": 1, "buy": 1, "accumulate": 2, "recovery": 2, "rebound": 3,
}
NEGATIVE = {
    "crash": -4, "plunge": -4, "plummet": -4, "slump": -3, "tumble": -3, "drop": -2,
    "fall": -2, "bearish": -3, "selloff": -3, "sell-off": -3, "liquidation": -3,
    "hack": -4, "exploit": -4, "stolen": -4, "lawsuit": -2, "ban": -3, "crackdown": -3,
    "investigation": -2, "outflow": -2, "downgrade": -2, "miss": -2, "loss": -2,
    "bankruptcy": -4, "fraud": -4, "warning": -2, "fear": -2, "collapse": -4,
    "halt": -2, "delay": -1, "rejected": -3, "default": -3,
}


@dataclass(slots=True)
class Headline:
    title: str
    source: str
    published: str
    link: str
    score: int

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "source": self.source,
                "published": self.published, "score": self.score, "link": self.link}


def _score_text(text: str) -> int:
    lowered = text.lower()
    score = 0
    for word, weight in POSITIVE.items():
        if word in lowered:
            score += weight
    for word, weight in NEGATIVE.items():
        if word in lowered:
            score += weight
    return max(-10, min(10, score))


def _fetch_feed(url: str, limit: int = 12) -> list[Headline]:
    try:
        with httpx.Client(timeout=12.0, follow_redirects=True,
                          headers={"User-Agent": "ZumviaQuant/1.0"}) as client:
            resp = client.get(url)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:  # noqa: BLE001 — haber alınamaması ticareti durdurmaz
        log.info("RSS alınamadı (%s): %s", url, str(exc)[:120])
        return []

    source = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    out: list[Headline] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        out.append(Headline(
            title=title[:220],
            source=source,
            published=(item.findtext("pubDate") or "")[:31],
            link=(item.findtext("link") or "")[:300],
            score=_score_text(title),
        ))
        if len(out) >= limit:
            break
    return out


def fetch_news(symbol: str = "", market: str = "crypto",
               limit: int = 18) -> dict[str, Any]:
    """
    Sembol ve piyasaya göre haber başlıkları + toplam duygu skoru döner.
    Skor: -10 (çok olumsuz) … +10 (çok olumlu), deterministik hesaplanır.
    """
    feeds = list(CRYPTO_FEEDS if market == "crypto" else MACRO_FEEDS)
    base = symbol.split("/", maxsplit=1)[0].replace("=F", "").strip().upper()
    if market == "stock" and base:
        feeds.insert(0, SYMBOL_FEED.format(symbol=base))

    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(_fetch_feed, feeds))

    headlines: list[Headline] = [h for batch in batches for h in batch]

    # Sembole özgü başlıkları öne al
    if base:
        keys = {base, base.lower(), {"BTC": "bitcoin", "ETH": "ethereum",
                                     "SOL": "solana", "XRP": "ripple"}.get(base, base.lower())}
        headlines.sort(key=lambda h: not any(k in h.title.lower() for k in keys))

    headlines = headlines[:limit]
    if not headlines:
        return {"available": False, "reason": "Haber kaynaklarına ulaşılamadı.",
                "sentiment_score": 0, "headlines": []}

    scores = [h.score for h in headlines]
    total = sum(scores)
    avg = total / len(scores)
    label = ("ÇOK_OLUMLU" if avg >= 1.5 else "OLUMLU" if avg >= 0.5
             else "ÇOK_OLUMSUZ" if avg <= -1.5 else "OLUMSUZ" if avg <= -0.5 else "NÖTR")

    return {
        "available": True,
        "fetched_at": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "count": len(headlines),
        "sentiment_score": round(avg, 2),
        "sentiment_label": label,
        "positive_count": sum(1 for s in scores if s > 0),
        "negative_count": sum(1 for s in scores if s < 0),
        "headlines": [h.to_dict() for h in headlines],
        "note": ("Skor deterministik anahtar-kelime sayımıdır, tahmin değildir. "
                 "Başlıkları kendiniz yorumlayın."),
    }
