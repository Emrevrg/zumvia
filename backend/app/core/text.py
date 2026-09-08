"""
TÜRKÇE-GÜVENLİ METİN KARŞILAŞTIRMA

Python'da `"İ".lower()` sonucu `"i̇"` (i + birleşik nokta) olur; `"ince"`
araması `"İnce piyasa"` metnini BULAMAZ. Aynı tuzak JavaScript'te de vardır.
Türkçe birincil dil olan bir üründe bu, sessizce yanlış sonuç üreten bir
hata sınıfıdır — arama boş döner ve kimse sebebini anlamaz.

`fold()` bu harfleri karşılaştırma öncesi sadeleştirir.
"""
from __future__ import annotations

import unicodedata

# Türkçe'ye özgü harflerin karşılaştırma karşılıkları
_MAP = str.maketrans({
    "İ": "i", "I": "i", "ı": "i",
    "Ş": "s", "ş": "s",
    "Ğ": "g", "ğ": "g",
    "Ü": "u", "ü": "u",
    "Ö": "o", "ö": "o",
    "Ç": "c", "ç": "c",
})


def fold(text: str) -> str:
    """Karşılaştırma için sadeleştirilmiş biçim (aksan ve büyük/küçük duyarsız)."""
    if not text:
        return ""
    folded = text.translate(_MAP).lower()
    # Kalan birleşik işaretleri (örn. i + nokta) at
    return "".join(ch for ch in unicodedata.normalize("NFD", folded)
                   if not unicodedata.combining(ch))


def contains(haystack: str, needle: str) -> bool:
    """Türkçe-güvenli 'içeriyor mu' kontrolü."""
    return fold(needle) in fold(haystack)
