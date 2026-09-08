"""
YÖNERGE UYUMU — küçük modeller de eksiksiz uysun
=================================================

Büyük modeller yönergeyi okur ve uyar. Küçük modeller ise sık sık şunları
yapar ve üçü de raporu bozar:

    SÜREÇ ANLATIMI   "Wait, the prompt says…", "We need to produce…"
    DİL KAYMASI      Türkçe istenirken İngilizce yazmak
    BOŞ/GÜDÜK YANIT  tek cümle döndürüp görevi bitirmiş saymak

Bunları yönergeye "yapma" yazarak çözmek yetmez — zaten yazıyor, yine
yapıyorlar. Küçük bir model için talimat bir öneridir, kısıt değil.

Bu modül kısıtı DIŞARIDAN uygular: çıktı ölçülür, uymuyorsa üç aşamalı
düzeltme çalışır.

    1. TEMİZLE   süreç satırlarını at (ucuz, çoğu durumu çözer)
    2. UYAR      aynı modele somut düzeltme talimatıyla tekrar sor
    3. DEĞİŞTİR  hâlâ uymuyorsa BAŞKA bir modele geç

Ölçüt "kusursuz" değil "kullanılabilir"dir: mükemmeliyetçilik her turda
model çağrısı yakar. Yalnızca raporu gerçekten bozan ihlaller düzeltilir.

Ayrıca uyum sicili tutulur: hangi modelin hangi rolde uyduğu ölçülür ve rol
dağıtımı bu ölçüme göre yapılabilir. Böylece sistem zamanla hangi modele ne
verebileceğini ÖĞRENİR — tahmin etmez.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

from ..core.logging import get_logger

log = get_logger("zumvia.research.compliance")

#  Bir yanıtın işe yarar sayılması için gereken en az uzunluk. Bunun altı,
#  modelin görevi anlamadığı ya da erken kestiği anlamına gelir.
MIN_USEFUL_CHARS = 120

#  Süreç anlatımı bu oranı aşarsa çıktı temizlenerek kurtarılamaz; model
#  görevi yapmak yerine görevi tartışmıştır.
MAX_THINKING_RATIO = 0.45

#  Türkçe istenirken İngilizce yazılmışsa. Eşik yüksek tutulur: finans
#  metinlerinde "stop loss", "take profit" gibi terimler doğal olarak
#  İngilizcedir ve tek başına dil kayması sayılmaz.
MIN_LANGUAGE_RATIO = 0.55

#  Türkçeye özgü işaretler. Bir metnin Türkçe olduğunu bunlar gösterir;
#  ASCII'ye sıkışmış Türkçe için sık kullanılan kelimeler de sayılır.
_TURKISH_CHARS = set("çğıöşüÇĞİÖŞÜ")
_TURKISH_WORDS = frozenset({
    "ve", "ile", "bir", "bu", "için", "icin", "olarak", "daha", "ancak",
    "ama", "değil", "degil", "var", "yok", "göre", "gore", "üzerinde",
    "altında", "yüksek", "düşük", "dusuk", "risk", "fiyat", "seviye",
    "olabilir", "gerekir", "sonuç", "sonuc", "veri", "ölçüm", "olcum",
})

_ENGLISH_WORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "have", "has",
    "should", "would", "could", "must", "will", "they", "there", "which",
    "because", "however", "based", "given", "since", "while", "about",
})


@dataclass(slots=True)
class Verdict:
    """Bir çıktının yönergeye uyup uymadığı."""

    ok: bool
    problems: list[str] = field(default_factory=list)
    cleaned: str = ""
    thinking_ratio: float = 0.0
    language_ratio: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "problems": self.problems,
                "thinking_ratio": round(self.thinking_ratio, 3),
                "language_ratio": round(self.language_ratio, 3)}

    def instruction(self) -> str:
        """Modele verilecek somut düzeltme talimatı."""
        if not self.problems:
            return ""
        lines = ["ÖNCEKİ YANITIN KULLANILAMAZ. Sebep:"]
        lines += [f"- {problem}" for problem in self.problems]
        lines.append("")
        lines.append("Aynı görevi YENİDEN yap. Bu kez yalnızca bulguyu yaz: "
                     "süreç anlatma, yönergeyi tekrarlama, kendine not düşme. "
                     "Doğrudan maddelerle başla.")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Ölçüm
# --------------------------------------------------------------------------- #

def language_ratio(text: str) -> float:
    """
    Metnin ne kadarı Türkçe.

    Sayı ve teknik terim ağırlıklı metinlerde kelime sayımı yanıltır; bu
    yüzden hem Türkçeye özgü harflere hem de sık kullanılan kelimelere
    bakılır. Hiçbir ipucu yoksa 1.0 döner — suçlama için kanıt gerekir.
    """
    words = [w for w in re.split(r"[\W\d_]+", text.lower()) if len(w) > 1]
    if not words:
        return 1.0

    turkish = sum(1 for w in words if w in _TURKISH_WORDS)
    english = sum(1 for w in words if w in _ENGLISH_WORDS)
    turkish += sum(1 for ch in text if ch in _TURKISH_CHARS) // 3

    total = turkish + english
    if total < 4:
        return 1.0                    # yeterli kanıt yok
    return turkish / total


def check(text: str, *, language: str = "tr", strip_thinking=None) -> Verdict:
    """
    Çıktıyı ölçer ve düzeltme gerekip gerekmediğini söyler.

    `strip_thinking` dışarıdan verilir (boru hattındaki süzgeç); bu modül
    süzgecin nasıl çalıştığını bilmek zorunda değildir.
    """
    original = (text or "").strip()
    cleaned = strip_thinking(original) if strip_thinking else original

    verdict = Verdict(ok=True, cleaned=cleaned)

    if not original:
        verdict.ok = False
        verdict.problems.append("Yanıt boş.")
        return verdict

    # 1) Süreç anlatımı — temizlik sonrası ne kadarı hayatta kaldı?
    ratio = 1.0 - (len(cleaned) / len(original)) if original else 0.0
    verdict.thinking_ratio = max(0.0, ratio)
    if ratio > MAX_THINKING_RATIO:
        verdict.ok = False
        verdict.problems.append(
            f"Yanıtın %{ratio * 100:.0f}'i görev anlatımı ve yönerge tekrarı — "
            f"bulgu değil. Doğrudan sonuçları yaz.")

    # 2) Kullanılabilir uzunluk
    if len(cleaned) < MIN_USEFUL_CHARS:
        verdict.ok = False
        verdict.problems.append(
            f"Yanıt çok kısa ({len(cleaned)} karakter); istenen maddeler yok.")

    # 3) Dil
    if language == "tr":
        share = language_ratio(cleaned)
        verdict.language_ratio = share
        if share < MIN_LANGUAGE_RATIO:
            verdict.ok = False
            verdict.problems.append(
                "Yanıt Türkçe değil. Tüm rapor Türkçe yazılmalı "
                "(teknik terimler İngilizce kalabilir).")

    if not verdict.ok:
        log.info("uyumsuz çıktı: %s", "; ".join(verdict.problems))
    return verdict


# --------------------------------------------------------------------------- #
#  Uyum sicili
# --------------------------------------------------------------------------- #
#  Hangi model hangi rolde yönergeye uyuyor? Bu, tahmin edilecek değil
#  ölçülecek bir şeydir. Sicil süreç içi tutulur: kalıcı olması gerekmez,
#  çünkü sağlayıcılar modelleri sık değiştirir ve eski sicil yanıltır.

_LOCK = threading.Lock()
_RECORD: dict[str, dict[str, int]] = {}


def note(model: str, role: str, complied: bool) -> None:
    """Bir modelin bir roldeki uyumunu kaydeder."""
    if not model:
        return
    key = f"{model}|{role}"
    with _LOCK:
        row = _RECORD.setdefault(key, {"ok": 0, "fail": 0})
        row["ok" if complied else "fail"] += 1


def reliability(model: str, role: str) -> float:
    """
    0–1 arası uyum oranı. Hiç veri yoksa 1.0 döner.

    Bilinmemek, kötü olmakla aynı şey değildir: yeni bir model şansı hak
    eder. Ancak bir kez uymadığında bu ölçülür ve rol dağıtımında dikkate
    alınır.
    """
    with _LOCK:
        row = _RECORD.get(f"{model}|{role}")
    if not row:
        return 1.0
    total = row["ok"] + row["fail"]
    return row["ok"] / total if total else 1.0


def snapshot() -> dict[str, dict[str, Any]]:
    """Tanılama ve arayüz için sicil fotoğrafı."""
    with _LOCK:
        items = dict(_RECORD)
    return {
        key: {**row, "reliability": round(row["ok"] / (row["ok"] + row["fail"]), 3)}
        for key, row in items.items() if row["ok"] + row["fail"]
    }


def reset() -> None:
    """Yalnızca testler için."""
    with _LOCK:
        _RECORD.clear()


__all__ = ["Verdict", "check", "language_ratio", "note", "reliability",
           "reset", "snapshot"]
