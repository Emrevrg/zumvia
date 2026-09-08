"""
OLGU DEFTERİ — araştırmadaki her sayının kaynağı
=================================================

Bir finansal rapordaki en tehlikeli şey, nereden geldiği bilinmeyen bir
sayıdır. Dil modelleri akıcı ve kendinden emin biçimde sayı uydurur; "BTC
44.500 dolarda" cümlesi doğru da olabilir, tamamen uydurma da. Kullanıcı
farkı göremez — parasını kaybedene kadar.

Bu modül farkı görülebilir kılar. Araştırma sırasında ölçülen HER değer
buraya, nasıl elde edildiğiyle birlikte yazılır:

    Fact(key="BTC/USDT.last", value=44500.0, source="binance",
         method="ticker", at=..., unit="USD")

Sonrasında modelin ürettiği metindeki her sayı bu deftere karşı sınanır.
Defterde karşılığı olan iddia DESTEKLENİR; çelişen iddia ÇELİŞKİLİ; hiçbir
karşılığı olmayan iddia DAYANAKSIZ olarak işaretlenir.

Defter değiştirilemezdir (append-only): bir olgu yazıldıktan sonra üzerine
yazılmaz, yenisi eklenir. Böylece "rakam sonradan değiştirildi mi" sorusu
her zaman cevaplanabilir.
"""
from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..core.logging import get_logger

log = get_logger("zumvia.research.ledger")

#  Sayısal karşılaştırmada kabul edilen sapma. Model "44.5 bin" derken defter
#  44.512 diyorsa bu bir yalan değil, yuvarlamadır. Eşik yüzdeseldir çünkü
#  fiyatlar 0.0001 ile 100.000 arasında değişir.
DEFAULT_TOLERANCE = 0.01          # %1


@dataclass(frozen=True, slots=True)
class Fact:
    """
    Ölçülmüş tek bir değer ve nereden geldiği.

    `method`, değerin NASIL elde edildiğini söyler ("ticker", "rsi14",
    "backtest.profit_factor"). Bu alan olmadan defter, kaynağı belirsiz
    sayılar listesine dönüşür ve amacını kaybeder.
    """

    key: str
    value: float | str | bool | None
    source: str                    # binance, rss:reuters, engine, backtest…
    method: str = ""
    unit: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(UTC))
    detail: str = ""

    @property
    def numeric(self) -> float | None:
        """Sayısal karşılaştırmaya uygun değer (değilse `None`)."""
        if isinstance(self.value, bool) or self.value is None:
            return None
        if isinstance(self.value, (int, float)):
            return float(self.value)
        try:
            return float(str(self.value).replace(",", "."))
        except (TypeError, ValueError):
            return None

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "source": self.source,
                "method": self.method, "unit": self.unit,
                "at": self.at.isoformat(), "detail": self.detail}

    def cite(self) -> str:
        """Rapor dipnotu biçimi."""
        where = self.source + (f"/{self.method}" if self.method else "")
        stamp = self.at.strftime("%H:%M")
        return f"[{where} · {stamp}]"


class Ledger:
    """
    Bir araştırmanın tüm ölçülmüş olguları.

    Ekleme-only'dur ve iş parçacığı güvenlidir: uzman ajanlar paralel
    çalışırken hepsi aynı deftere yazar.
    """

    def __init__(self) -> None:
        self._facts: list[Fact] = []
        self._lock = threading.Lock()

    # -- yazma -------------------------------------------------------------- #

    def add(self, key: str, value: Any, source: str, method: str = "",
            unit: str = "", detail: str = "") -> Fact:
        """Deftere bir olgu yazar ve yazılan kaydı döndürür."""
        fact = Fact(key=key, value=value, source=source, method=method,
                    unit=unit, detail=detail)
        with self._lock:
            self._facts.append(fact)
        return fact

    def add_many(self, source: str, values: dict[str, Any], prefix: str = "",
                 method: str = "") -> list[Fact]:
        """
        Bir sözlüğün sayısal alanlarını topluca deftere yazar.

        Araç sonuçları iç içe sözlükler döndürür; burada düzleştirilerek
        `fiyat.son`, `gostergeler.rsi14` gibi anahtarlara çevrilir.
        """
        written: list[Fact] = []
        for key, value in _flatten(values, prefix).items():
            if isinstance(value, (int, float, str, bool)) or value is None:
                written.append(self.add(key, value, source, method))
        return written

    # -- okuma -------------------------------------------------------------- #

    def all(self) -> list[Fact]:
        with self._lock:
            return list(self._facts)

    def get(self, key: str) -> Fact | None:
        """Bir anahtarın EN SON yazılmış değeri."""
        with self._lock:
            for fact in reversed(self._facts):
                if fact.key == key:
                    return fact
        return None

    def numbers(self) -> list[Fact]:
        """Yalnızca sayısal olgular (iddia doğrulaması bunları kullanır)."""
        return [f for f in self.all() if f.numeric is not None]

    def sources(self) -> list[str]:
        """Bu araştırmada gerçekten kullanılmış kaynaklar."""
        return sorted({f.source for f in self.all()})

    # -- doğrulama ---------------------------------------------------------- #

    def supports(self, value: float, tolerance: float = DEFAULT_TOLERANCE
                 ) -> Fact | None:
        """
        Verilen sayıyı destekleyen bir olgu var mı?

        Yüzdesel tolerans kullanılır: model yuvarlayabilir, defter yuvarlamaz.
        Sıfıra yakın değerlerde mutlak eşiğe düşülür, aksi hâlde yüzde
        hesabı anlamsızlaşır.
        """
        best: Fact | None = None
        best_gap = math.inf
        for fact in self.numbers():
            reference = fact.numeric
            if reference is None:
                continue
            scale = max(abs(reference), 1e-9)
            gap = abs(reference - value) / scale if abs(reference) > 1e-6 \
                else abs(reference - value)
            if gap <= tolerance and gap < best_gap:
                best, best_gap = fact, gap
        return best

    def to_dict(self) -> dict[str, Any]:
        return {"count": len(self._facts), "sources": self.sources(),
                "facts": [f.to_dict() for f in self.all()]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    def __len__(self) -> int:
        with self._lock:
            return len(self._facts)


# --------------------------------------------------------------------------- #
#  Yardımcı
# --------------------------------------------------------------------------- #

_SKIP_KEYS = {"ok", "error", "hint", "note", "notes", "warning", "warnings",
              "disclaimer", "message"}


def _flatten(data: Any, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """
    İç içe sözlüğü `a.b.c` anahtarlarına düzleştirir.

    Derinlik sınırlıdır: araç çıktıları bazen uzun listeler içerir (mum
    verisi gibi) ve bunları tek tek deftere yazmak defteri işe yaramaz hâle
    getirir. Amaç ÖZET değerleri saklamaktır, ham seriyi değil.
    """
    if depth > 3:
        return {}

    out: dict[str, Any] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            if key in _SKIP_KEYS:
                continue
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                out.update(_flatten(value, name, depth + 1))
            elif isinstance(value, list):
                # Listelerden yalnızca uzunluk saklanır; ham seri değil.
                out[f"{name}.adet"] = len(value)
            else:
                out[name] = value
    return out
