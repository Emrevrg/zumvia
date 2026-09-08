"""
İDDİA DOĞRULAMA — modelin yazdığı her sayı deftere karşı sınanır
=================================================================

Dil modelleri sayı uydurur ve bunu kendinden emin bir üslupla yapar. Finansal
bir raporda bu, kullanıcının parasını kaybetmesi demektir. Buradaki katman,
üretilen metni cümle cümle ayırır, içindeki sayısal iddiaları çıkarır ve her
birini olgu defterine karşı sınar:

    DESTEKLENDİ   defterde eşleşen ölçüm var        → rapora kaynağıyla girer
    ÇELİŞİYOR     defterde AYNI şey farklı ölçülmüş → rapora uyarıyla girer
    DAYANAKSIZ    defterde karşılığı yok            → rapora "ölçülmedi" ile

Üçüncüsü silinmez. Modelin yorumu değerli olabilir; değersiz olan, yorumu
ölçümmüş gibi göstermektir. Kullanıcı ikisini ayırt edebilmelidir.

Ayrıca kaynak iddiaları sınanır: metinde bir habere/siteye atıf varsa, o
kaynağın araştırma sırasında GERÇEKTEN okunmuş olması gerekir. Okunmamış
kaynağa atıf, uydurma atıftır.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.logging import get_logger
from ..core.text import fold
from .ledger import DEFAULT_TOLERANCE, Fact, Ledger

log = get_logger("zumvia.research.verify")


class Verdict(StrEnum):
    SUPPORTED = "desteklendi"       # ölçümle doğrulandı
    CONTRADICTED = "çelişiyor"      # ölçümle ters — okuyucuyu yanıltır
    PROJECTION = "öngörü"           # geleceğe dair; doğrulanamaz, çürütülemez
    UNSUPPORTED = "dayanaksız"      # ölçülebilirdi ama ölçülmedi


#  Metindeki sayılar. Binlik ayracı, ondalık, yüzde ve para birimi biçimleri
#  Türkçe ve İngilizce yazımda farklıdır; ikisi de yakalanır.
#  Binlik ayracı olarak BOŞLUK da kullanılır ("76 286.74", "1 234 567").
#  Dar boşluk (U+202F) ve bölünmez boşluk (U+00A0) dahil edilir: modeller
#  bunları sık üretir ve normal boşlukla ayırt edilemezler. Tanınmazsa tek
#  bir sayı ikiye bölünür ve iki sahte "dayanaksız iddia" doğar.
_THIN = "\u202f\u00a0\u2009"
_NUMBER = re.compile(
    r"(?<![\w.])"
    r"(%\s*)?"                                        # Türkçe: yüzde ÖNDE
    # İşaret: düz tire dışında uzun tire (–), em tire (—), matematik eksi
    # (−) ve kısa tire (‒) de kabul edilir. Modeller bunları sık kullanır;
    # işaret okunmazsa "–376.5" değeri +376.5 olur ve gerçek ölçümle
    # (-376.513) eşleşmez — doğru yazan model yalancı çıkar.
    r"([+\-\u2010-\u2015\u2212]?\d{1,3}(?:[.,\s\u202f\u00a0\u2009]\d{3})+(?:[.,]\d+)?"
    r"|[+\-\u2010-\u2015\u2212]?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?"
    r"|[+\-\u2010-\u2015\u2212]?\d+(?:[.,]\d+)?)"
    r"\s*(%|bin|milyon|milyar|k|m|b)?"                # ya da SONDA
    r"(?![\w])",
    re.IGNORECASE,
)

_MULTIPLIER = {"bin": 1e3, "k": 1e3, "milyon": 1e6, "m": 1e6,
               "milyar": 1e9, "b": 1e9}

#  Saat ve oran gösterimi ("14:30", "1/2"), sürüm numarası ("v3").
#  Bunlar sayının ÇEVRESİNDE aranır çünkü desen sayıyı kapsar.
_NOISE = re.compile(r"\d{1,2}[:/]\d{2}|\bv\d")


def _looks_like_a_year(raw: str) -> bool:
    """
    Sayı bir yıl mı?

    Yıl kontrolü sayının KENDİSİNE yapılır, çevresine değil. Çevreye bakan
    eski sürüm "2074.94" fiyatındaki "2074" kısmını yıl sanıp iddiayı
    tamamen atıyordu — yani 2000–2099 arası her fiyat sessizce doğrulama
    dışı kalıyordu. ETH, BNB, SOL gibi varlıklar tam bu aralıktadır.

    Yıl, ondalık kısmı olmayan dört haneli bir tam sayıdır.
    """
    token = raw.strip().lstrip("+-\u2010\u2011\u2012\u2013\u2014\u2015\u2212")
    if not token.isdigit() or len(token) != 4:
        return False
    return 1900 <= int(token) <= 2099

#  BİRİM TANIMLAYICILARI. "24 saatlik hacim" cümlesindeki 24 bir iddia değil,
#  ölçünün hangi pencerede alındığını söyleyen bir sıfattır. Bunları iddia
#  saymak boşuna alarm üretir — ve boşuna alarm, doğrulamanın kendisini
#  değersizleştirir: kullanıcı bir süre sonra uyarılara bakmaz olur.
_UNIT_WORD = re.compile(
    # Türkçe ekli hâller de birimdir: "24 mumda", "3 günde", "5 barlık".
    # `\b` ile bitirmek "mumda"yı kaçırıyordu; ek serbest bırakılır.
    r"^\s*(saat|gün|gun|hafta|ay|yıl|yil|dakika|bar|mum|periyot|"
    r"dönem|donem|kez|adet|defa|hour|day|week|month|year|minute|"
    r"candle|period)[a-zçğıöşü]{0,6}\b"
    # Kısaltılmış zaman dilimleri: "4 h periyodunda", "15 m", "1 d".
    r"|^\s*[hmdwy]\b",
    re.IGNORECASE,
)

#  Bir sayının hangi ölçümden bahsettiğini belirlemek için bakılan yerel
#  pencere (karakter). Tüm cümleye bakmak, uzun cümlelerde sayıyı alakasız
#  bir ölçümle eşleştirir.
_WINDOW = 45

#  META İFADELER: sayı, araştırmanın KENDİSİ hakkındadır ("59 olguya
#  dayanır", "58 data points", "5 maddeyi ele alacağım"). Bunlar piyasa
#  iddiası değildir; doğrulanacak bir şey de içermezler. İddia sayılmaları
#  raporu, modelin kendi süreç anlatımıyla üretilen sahte uyarılara boğar.
_META_CONTEXT = re.compile(
    r"(olgu|ölçüm|defter|data point|veri noktası|madde|nokta|başlık|"
    r"measurement|ledger|fact|field|point[s]?\b|adım|step|bölüm|section|"
    r"satır|row|entry|kayıt)",
    re.IGNORECASE,
)

#  EŞİK SAYILARI: karşılaştırma işaretinden hemen sonra gelen sayı bir
#  ölçüm değil, ölçünün karşılaştırıldığı REFERANS seviyedir — "aşırı alım
#  sayılmaz (>70)" cümlesindeki 70, RSI'nin tanımıdır. Doğrulamaya
#  çalışmak, tanımı doğrulamaya çalışmaktır.
_THRESHOLD_BEFORE = re.compile(r"[<>≥≤]=?\s*$|\b(üzeri|üstü|altı|uzeri|ustu|alti|"
                               r"above|below|over|under|threshold|eşik|esik|"
                               r"overbought|oversold|aşırı alım|aşırı satım|"
                               r"asiri alim|asiri satim)\s*$",
                               re.IGNORECASE)

#  Türkçede karşılaştırma sözcüğü sayıdan SONRA gelir: "30 işlem altı",
#  "%5 üzeri". Yalnızca öncesine bakmak bu kalıbı kaçırıyordu.
_THRESHOLD_AFTER = re.compile(
    # Türkçede sayıya kesme işaretiyle ek gelir: "30'un altında", "%5'in
    # üzerinde". Kesme hesaba katılmazsa eşik kalıbı kaçırılır.
    r"^\s*(['\u2019\u00b4`]\w{1,4})?\s*\w{0,10}\s*"
    r"(üzeri|üstü|altı|uzeri|ustu|alti|üzerinde|altında|"
    r"uzerinde|altinda|ve üzeri|ve altı)\b",
    re.IGNORECASE,
)

#  GÖSTERGE PERİYODU: "RSI 14: 55.72" satırındaki 14, göstergenin kaç barlık
#  hesaplandığını söyler — adın parçasıdır, ölçüm değil. Ölçüm 55.72'dir.
#
#  Ama dikkat: "RSI 28 aşırı satım bölgesinde" cümlesinde 28 ÖLÇÜMÜN
#  KENDİSİDİR. İkisini ayıran şey gösterge adı değil KALIPTIR — periyot her
#  zaman "ad periyot: değer" biçiminde gelir, yani sayıdan sonra iki nokta
#  ve BAŞKA bir sayı bulunur (bkz. `_PERIOD_AFTER`).
#
#  Kuralı yalnızca ada bakarak uygulamak, doğrulayıcıyı tam da yakalaması
#  gereken şeye kör bırakıyordu: uydurma "RSI 28" iddiası iddia bile
#  sayılmıyordu.
#  Gösterge adı, sayıdan hemen önce. Ayırıcı olarak boşluk, alt çizgi ve her
#  tür tire kabul edilir: modeller "EMA 20", "EMA_20", "EMA-20" ve bölünmez
#  tireli "EMA‑20" yazımlarının hepsini kullanır.
_INDICATOR_BEFORE = re.compile(
    r"\b(rsi|ema|sma|wma|ma|atr|adx|di|dmi|macd|bb|bollinger|stoch|stokastik|"
    r"cci|mfi|obv|roc|willr|supertrend|vwap|kama|tema|dema)"
    # Parantez ve köşeli parantez de ayırıcıdır: "RSI(14)", "ATR[14]".
    r"[\s_\-\u2010\u2011\u2012\u2013(\[]*$",
    re.IGNORECASE,
)

#  GÖSTERGEYE ÖZGÜ STANDART PERİYOTLAR.
#
#  Bir sayının periyot mu değer mi olduğunu yazım biçiminden anlamaya
#  çalışmak kırılgandır — modeller aynı şeyi beş türlü yazar. Sağlam ayrım
#  şudur: RSI'nin periyodu 14 olur, 28 olmaz; ADX'in periyodu 14'tür, 12
#  değil. Bu sayılar sektörde sabittir ve yazımdan bağımsızdır.
_STANDARD_PERIODS: dict[str, frozenset[int]] = {
    "rsi": frozenset({7, 9, 14, 21, 25}),
    "ema": frozenset({8, 9, 12, 20, 21, 26, 34, 50, 55, 100, 200}),
    "sma": frozenset({10, 20, 50, 100, 200}),
    "wma": frozenset({10, 20, 50, 200}),
    "ma": frozenset({10, 20, 50, 100, 200}),
    "atr": frozenset({7, 14, 20, 21}),
    "adx": frozenset({14}),
    "di": frozenset({14}),
    "dmi": frozenset({14}),
    "macd": frozenset({9, 12, 26}),
    "bb": frozenset({20}),
    "bollinger": frozenset({20}),
    "stoch": frozenset({3, 5, 14}),
    "stokastik": frozenset({3, 5, 14}),
    "cci": frozenset({14, 20}),
    "mfi": frozenset({14}),
    "roc": frozenset({9, 12, 14}),
    "willr": frozenset({14}),
    "supertrend": frozenset({7, 10, 14}),
    "kama": frozenset({10}),
    "tema": frozenset({9, 20}),
    "dema": frozenset({9, 20}),
}


def _is_indicator_period(before: str, value: float) -> bool:
    """
    Sayı, kendisinden önce gelen göstergenin PERİYODU mu?

    Periyot ölçüm değildir; göstergenin adının parçasıdır. Değerle
    karşılaştırmak, ölçümü kendi adıyla karşılaştırmak demektir.
    """
    match = _INDICATOR_BEFORE.search(before)
    if match is None or value != int(value) or value < 0:
        return False
    periods = _STANDARD_PERIODS.get(match.group(1).lower())
    return bool(periods and int(value) in periods)

#  Sıra bildiren yazımlar: "3. madde", "1)", "Point 2:", "5-point".
#  Bunlar metnin İSKELETİDİR, içeriği değil; iddia sayılmaları raporu
#  sahte çelişkilerle doldurur.
_ORDINAL_AFTER = re.compile(r"^\s*[.)°:]|^\s*-\s*(point|madde|nokta|adim|adım)")
_ORDINAL_BEFORE = re.compile(
    r"(?:point|madde|nokta|adım|adim|başlık|baslik|soru|no|nu|#|item|step)\s*$",
    re.IGNORECASE,
)

#  Kaynak atıfları. Yön dile göre değişir ve bu ayrım önemlidir:
#     Türkçe : KAYNAK önce gelir  → "Reuters'a göre …", "Bloomberg bildirdi"
#     İngilizce: kaynak sonra gelir → "according to Reuters"
#  Yönü karıştırmak, cümlenin geri kalanını kaynak adı sanmaya yol açar
#  ("kurumsal talep artıyor" diye bir haber ajansı yoktur).
_CITATION_BEFORE = re.compile(
    r"\b([A-ZÇĞİÖŞÜ][\wÇĞİÖŞÜçğıöşü.\-]{2,24})(?:'[a-zçğıöşü]{1,3})?\s+"
    r"(?:göre|bildirdi|açıkladı|duyurdu|yazdı)\b",
)
_CITATION_AFTER = re.compile(
    r"(?:according to|source:|kaynak:)\s+([A-Za-z0-9.\-]{3,30})",
    re.IGNORECASE,
)
_CITATION_DOMAIN = re.compile(r"\(?\b([a-z0-9\-]+\.(?:com|org|net|io|co|gov|edu)"
                              r"(?:\.[a-z]{2})?)\b\)?", re.IGNORECASE)


#  GELECEK/KOŞUL KİPİ. Bu kalıpları taşıyan bir cümledeki sayı, bugüne dair
#  bir ölçüm iddiası değil ileriye dönük bir beklentidir. Türkçede kip fiil
#  ekinde saklıdır ("görebilir", "çıkabilir"), İngilizcede yardımcı fiilde.
_PROJECTION = re.compile(
    r"\b("
    r"gelecek\w*|gelecekte|önümüzdeki|onumuzdeki|ilerleyen|yakında|yakinda|"
    r"beklen\w*|tahmin\w*|öngör\w*|ongor\w*|projeksiyon\w*|potansiyel\w*|"
    r"hedef\w*|olası|olasi|senaryo\w*|ihtimal\w*|umul\w*|"
    r"yıl sonu\w*|yil sonu\w*|ay sonu\w*|hafta sonu\w*|"
    r"could|would|might|may\b|expect\w*|forecast\w*|project\w*|"
    r"potential\w*|target\w*|outlook|by\s+end\s+of"
    r")\b"
    r"|\w+(ebilir|abilir|ecek|acak|ebilirdi|abilirdi)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class Claim:
    """Metinden çıkarılmış tek bir sayısal iddia."""

    text: str                      # iddianın geçtiği cümle
    value: float
    raw: str                       # metindeki ham yazım ("44,5 bin")
    readings: list[float] = field(default_factory=list)   # olası okumalar
    is_percent: bool = False       # yüzde olarak yazılmış (%94.8 ya da 94.8%)
    context: str = ""              # sayının HEMEN çevresindeki kelimeler
    verdict: Verdict = Verdict.UNSUPPORTED
    evidence: Fact | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text[:300], "value": self.value, "raw": self.raw,
                "verdict": str(self.verdict), "note": self.note,
                "evidence": self.evidence.to_dict() if self.evidence else None}


@dataclass(slots=True)
class Audit:
    """Bir metnin tam doğrulama sonucu."""

    claims: list[Claim] = field(default_factory=list)
    unknown_sources: list[str] = field(default_factory=list)

    @property
    def supported(self) -> list[Claim]:
        return [c for c in self.claims if c.verdict is Verdict.SUPPORTED]

    @property
    def contradicted(self) -> list[Claim]:
        return [c for c in self.claims if c.verdict is Verdict.CONTRADICTED]

    @property
    def unsupported(self) -> list[Claim]:
        return [c for c in self.claims if c.verdict is Verdict.UNSUPPORTED]

    @property
    def projections(self) -> list[Claim]:
        return [c for c in self.claims if c.verdict is Verdict.PROJECTION]

    @property
    def trust_score(self) -> float:
        """
        0–1 arası güven: bu metne ne kadar dayanılabilir.

        Dört iddia türü dört ayrı ağırlık alır ve bu ayrım bilinçlidir:

            DESTEKLENDİ  1.0  ölçümle doğrulandı, dayanılabilir
            ÖNGÖRÜ       0.7  meşru ileri bakış; ölçüm değil ama kusur da değil
            DAYANAKSIZ   0.5  ölçülebilirdi, ölçülmedi — belirsiz
            ÇELİŞİYOR   -1.0  ölçümle ters; okuyucuyu aktif olarak yanıltır

        Öngörüyü dayanaksızlıkla aynı kefeye koymak, analistin ileriye
        bakmasını kusur saymak olurdu; sıfır vermek ise "bilmiyorum" diyeni
        yalan söyleyenle eşitlerdi. Cezalandırılması gereken tek şey,
        ölçüme ters bir sayıyı emin bir dille yazmaktır.
        """
        if not self.claims:
            return 1.0                    # sayısal iddia yoksa yanıltma da yok
        score = (len(self.supported)
                 + 0.7 * len(self.projections)
                 + 0.5 * len(self.unsupported)
                 - 1.0 * len(self.contradicted)) / len(self.claims)
        return max(0.0, min(1.0, score))

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": len(self.claims),
            "supported": len(self.supported),
            "contradicted": len(self.contradicted),
            "unsupported": len(self.unsupported),
            "projections": len(self.projections),
            "trust_score": round(self.trust_score, 3),
            "unknown_sources": self.unknown_sources,
            "claims": [c.to_dict() for c in self.claims],
        }

    def summary(self) -> str:
        """Rapora eklenecek tek satırlık dürüst özet."""
        if not self.claims:
            return "Bu bölümde ölçülebilir sayısal iddia yok."
        parts = [f"{len(self.supported)} iddia ölçümle doğrulandı"]
        if self.contradicted:
            parts.append(f"{len(self.contradicted)} iddia ölçümle ÇELİŞİYOR")
        if self.projections:
            parts.append(f"{len(self.projections)} ifade geleceğe dair öngörü")
        if self.unsupported:
            parts.append(f"{len(self.unsupported)} iddianın ölçüm karşılığı yok")
        return ", ".join(parts) + "."


# --------------------------------------------------------------------------- #
#  Çıkarım
# --------------------------------------------------------------------------- #

#  Belirsiz ayraç: "78.556" Türkçe metinde 78 bin 556, İngilizce metinde
#  78 tam 556'dır. Hangisinin kastedildiği METİNDEN anlaşılmaz.
_AMBIGUOUS = re.compile(r"^[+-]?\d{1,3}[.,]\d{3}$")


def _readings(raw: str, suffix: str | None) -> list[float]:
    """
    Bir sayının OLASI okumalarını döndürür.

    Tek bir okuma seçip ona göre hüküm vermek, doğrulayıcının en zararlı
    hatasıydı: "78.556" ondalık okunduğunda 78 bin 556 fiyatını yazan model
    yalancı ilan ediliyordu. Belirsizlik varsa iki okuma da üretilir ve
    HERHANGİ BİRİ ölçümle uyuşuyorsa iddia desteklenmiş sayılır.

    Şüphe, iddiada bulunanın lehine yorumlanır — çünkü buradaki amaç suç
    bulmak değil, gerçekten yanlış olanı yakalamaktır.
    """
    text = raw.strip()
    # Modeller tire yerine uzun tire (–), matematik eksi (−) ya da kısa tire
    # (‒) yazar. İşaret okunmazsa "–376.5" değeri +376.5 olur ve gerçek
    # ölçümle (-376.513) eşleşmez: doğru yazan model yalancı çıkar.
    for dash in ("\u2010", "\u2011", "\u2012", "\u2013",
                 "\u2014", "\u2015", "\u2212"):
        text = text.replace(dash, "-")
    # Boşluklu binlik ayracı tek anlamlıdır: "76 286.74" → 76286.74
    for space in (" ", "\u202f", "\u00a0", "\u2009"):
        if space in text:
            text = text.replace(space, "")
    values: list[float] = []

    def push(candidate: str) -> None:
        try:
            value = float(candidate)
        except ValueError:
            return
        if value not in values:
            values.append(value)

    if _AMBIGUOUS.match(text):
        # İki okuma: binlik ayracı ve ondalık ayracı.
        push(text.replace(".", "").replace(",", ""))          # 78556
        push(text.replace(",", "."))                          # 78.556
    elif text.count(",") and text.count("."):
        # İkisi de var: sonda olan ondalıktır.
        if text.rfind(",") > text.rfind("."):
            push(text.replace(".", "").replace(",", "."))     # 1.234,56
        else:
            push(text.replace(",", ""))                       # 1,234.56
    elif text.count(",") == 1 and len(text.split(",")[-1]) != 3:
        push(text.replace(",", "."))                          # 44,5 → 44.5
    elif text.count(".") == 1 and len(text.split(".")[-1]) == 3:
        push(text.replace(".", ""))                           # 1.234 → 1234
        push(text)                                            # ya da 1.234
    else:
        push(text.replace(",", ""))

    if suffix and suffix.lower() in _MULTIPLIER:
        factor = _MULTIPLIER[suffix.lower()]
        values = [v * factor for v in values]
    return values


def _parse_number(raw: str, suffix: str | None) -> float | None:
    """En olası tek okuma (geriye dönük uyumluluk ve gösterim için)."""
    values = _readings(raw, suffix)
    return values[0] if values else None


def extract_claims(text: str) -> list[Claim]:
    """
    Metindeki sayısal iddiaları cümleleriyle birlikte çıkarır.

    Tarih, saat ve sürüm numaraları elenir: onlar iddia değil, biçimdir.
    """
    claims: list[Claim] = []
    # Satır sonu da cümle sonudur. Madde listeleri noktayla bitmez; tek
    # parça sayıldıklarında bir maddedeki sayı başka bir maddedeki kelimeyle
    # eşleşir ve yanlış ölçümle karşılaştırılır.
    for sentence in re.split(r"(?<=[.!?])\s+|[\r\n]+", text):
        clean = sentence.strip()
        if not clean:
            continue
        for match in _NUMBER.finditer(clean):
            prefix_pct, raw, suffix = match.group(1), match.group(2), match.group(3)
            span = clean[max(0, match.start() - 6):match.end() + 6]
            if _NOISE.search(span) or _looks_like_a_year(raw):
                continue
            after = clean[match.end():match.end() + 16]
            before = clean[max(0, match.start() - 14):match.start()]
            readings = _readings(raw, suffix)
            if not readings:
                continue

            if _UNIT_WORD.match(after):
                continue                      # "24 saatlik" → birim, iddia değil
            if _META_CONTEXT.search(after[:18]):
                continue                      # "59 olgu" → defterin kendisi
            if _is_indicator_period(before, readings[0] if readings else 0.0):
                continue                      # "RSI 14: 55.72" → 14 periyottur
            if _ORDINAL_BEFORE.search(before):
                continue                      # "Point 2" → liste numarası
            if _THRESHOLD_BEFORE.search(before) or _THRESHOLD_AFTER.match(after):
                continue                      # ">70", "30 işlem altı" → eşik

            # Liste numarası SATIR BAŞINDA olur ("1) giriş", "3. madde").
            # Konum şartı olmadan cümle sonundaki her sayı ("R/R ~2,3.")
            # liste numarası sanılıp sessizce atlanıyordu.
            # Markdown tablo hücresi ("| 1. |") ve madde imi de satır başıdır.
            at_line_start = not before.strip(" \t-*•|>#")
            if at_line_start and _ORDINAL_AFTER.match(after):
                continue
            # Sayının YEREL çevresi. Uzun bir cümlede birden çok konu geçer
            # ("Stop 76.285'e %2.9 mesafe … hacim teyidi yok"); tüm cümleyi
            # bağlam saymak, %2.9'u hacim ölçümüyle karşılaştırmaya yol açar.
            claims.append(Claim(
                text=clean, value=readings[0], raw=match.group(0).strip(),
                readings=readings,
                is_percent=bool(prefix_pct) or suffix == "%",
                context=clean[max(0, match.start() - _WINDOW):match.end() + _WINDOW],
            ))
    return claims


#  Kaynak adı sanılmaması gereken yaygın kelimeler.
_NOT_A_SOURCE = {"bu", "şu", "veri", "veriler", "analiz", "rapor", "model",
                 "piyasa", "fiyat", "grafik", "sonuç", "buna", "ona", "genel"}


def _cited_sources(text: str) -> list[str]:
    """
    Metinde atıf yapılan kaynak adları.

    Yalnızca gerçekten kaynak GİBİ duranlar alınır. Şüpheli her şeyi kaynak
    saymak, "bilinmeyen kaynak" listesini gürültüye boğar ve gerçek uydurma
    atıflar arada kaybolur.
    """
    found: list[str] = []
    for pattern in (_CITATION_BEFORE, _CITATION_AFTER, _CITATION_DOMAIN):
        for match in pattern.finditer(text):
            name = (match.group(1) or "").strip(" .,:;()").lower()
            if len(name) >= 3 and name not in _NOT_A_SOURCE and name not in found:
                found.append(name)
    return found


# --------------------------------------------------------------------------- #
#  Doğrulama
# --------------------------------------------------------------------------- #

def audit(text: str, ledger: Ledger,
          tolerance: float = DEFAULT_TOLERANCE) -> Audit:
    """
    Metindeki her sayısal iddiayı deftere karşı sınar.

    Yüzde iddiaları özel ele alınır: "%12 yükseldi" ifadesindeki 12 ile
    deftere yazılmış 0.12 aynı şeydir. İkisi de denenir, biri tutarsa iddia
    desteklenmiş sayılır.
    """
    result = Audit(claims=extract_claims(text))

    for claim in result.claims:
        # Bir iddianın birden çok okuması olabilir (binlik/ondalık belirsizliği,
        # yüzde/oran dönüşümü). Herhangi biri tutarsa iddia desteklenmiştir.
        candidates = list(claim.readings or [claim.value])
        if claim.is_percent:
            # "%94.8" ile defterdeki 0.948 aynı şeydir. Yüzde işaretinin önde
            # mi arkada mı olduğu dile göre değişir; anlam değişmez.
            candidates += [v / 100.0 for v in candidates]
        candidates += [v * 100.0 for v in candidates if 0 < v < 1]

        # Konusu iddiayla örtüşen ölçümler. Cömert tolerans YALNIZCA bunlara
        # uygulanır: 76 olguluk bir defterde herhangi bir sayının birine
        # yakın düşmesi rastlantıdır, kanıt değil.
        related = _related_facts(claim, ledger)

        evidence, matched = _first_match(candidates, related, tolerance,
                                         claim.raw, allow_rounding=True)
        if evidence is None and _significant_digits(claim.raw) >= 3:
            # Konusu uymayan ölçüm ancak NEREDEYSE BİREBİR eşleşirse kanıttır
            # — ve ancak sayı yeterince AYIRT EDİCİYSE.
            #
            # Küçük tam sayılarda rastlantı kaçınılmazdır: defterde "8"
            # (kaç strateji oy verdi) durduğu için "R/R oranı 8,0" iddiası
            # doğrulanmış sayılıyordu. Üç anlamlı basamak şartı, konu bağı
            # olmadan güvenmeyi ancak rastlantının makul olmadığı yerde
            # mümkün kılar.
            evidence, matched = _first_match(candidates, ledger.numbers(),
                                             EXACT_TOLERANCE, claim.raw,
                                             allow_rounding=False)

        if evidence is not None:
            claim.value = matched
            claim.verdict = Verdict.SUPPORTED
            claim.evidence = evidence
            claim.note = f"{evidence.key} = {evidence.value} {evidence.cite()}"
            continue

        # Geleceğe dair bir sayı, bugünkü ölçümle ÇELİŞEMEZ: bugün hakkında
        # bir iddiada bulunmuyor. Analistin ileriye bakması meşru işidir;
        # onu kusur gibi göstermek raporu değersizleştirir.
        if _PROJECTION.search(fold(claim.context or claim.text)):
            claim.verdict = Verdict.PROJECTION
            claim.note = ("Geleceğe dair beklenti — ölçüm değil. Doğrulanamaz, "
                          "çürütülemez; kararınızı verirken böyle okuyun.")
            continue

        near = _pick_contradiction(claim, related)
        if near is not None:
            claim.verdict = Verdict.CONTRADICTED
            claim.evidence = near
            claim.note = (f"Ölçüm {near.key} = {near.value}, metinde "
                          f"{claim.value} yazıyor {near.cite()}")
        else:
            claim.verdict = Verdict.UNSUPPORTED
            claim.note = "Bu sayının araştırmada ölçülmüş bir karşılığı yok."

    known = {s.lower() for s in ledger.sources()}
    for name in _cited_sources(text):
        if not any(name in source or source in name for source in known):
            if name not in result.unknown_sources:
                result.unknown_sources.append(name)

    log.debug("doğrulama: %d iddia, %d destekli, %d çelişkili",
              len(result.claims), len(result.supported), len(result.contradicted))
    return result


def _decimals(raw: str) -> int:
    """Metinde yazılan sayının kaç ondalık basamağı var."""
    cleaned = raw.strip().strip("%~≈ ").replace(" ", "")
    for separator in (",", "."):
        if separator in cleaned:
            tail = cleaned.rsplit(separator, 1)[1]
            # Üç haneli son grup binlik ayracıdır, ondalık değil.
            if tail.isdigit() and len(tail) != 3:
                return len(tail)
    return 0


def _rounded_match_in(value: float, facts: list[Fact], raw: str = "") -> Fact | None:
    """
    Yuvarlanmış yazım çelişki değildir.

    Bir sayıyı N ondalıkla yazmak, "değer bu basamağa yuvarlandığında budur"
    demektir. O hâlde kabul aralığı da son basamağın YARISI kadar olmalıdır:

        "ADX 43"    (0 ondalık) → 43 ± 0.5    → ölçüm 43.62 uyar
        "~%2,7"     (1 ondalık) → 2.7 ± 0.05  → ölçüm 2.729 uyar
        "R/R ~2,3"  (1 ondalık) → 2.3 ± 0.05  → ölçüm 2.25  uyar

    Sabit yüzdesel tolerans bunu yapamaz: %1'lik tolerans 2.729 ile 2.7
    arasını (%1.06) reddedip doğru yazan uzmanı dayanaksız ilan ediyordu.
    Ölçüt, yuvarlamanın kendi tanımı olmalı.

    Kesme (43.62 → "43") de meşrudur ve ayrıca sınanır: insanlar prozada
    ikisini de yapar ve hiçbiri yalan değildir.
    """
    places = _decimals(raw) if raw else (0 if value == int(value) else -1)
    if places < 0:
        return None

    half_unit = 0.5 * (10 ** -places) * 1.0001      # kayan nokta payı

    # Kesme kuralı ("43.62" → "43") yalnızca tam kısmı ANLAMLI olan
    # sayılarda geçerlidir. 1'in altında her değer 0'a kesilir; 0.948 ile
    # 0.35 bu kurala göre "aynı" görünür ve uydurma bir güven oranı
    # doğrulanmış sayılırdı.
    target = int(value) if places == 0 and abs(value) >= 1 else None

    for fact in facts:
        reference = fact.numeric
        if reference is None:
            continue
        if abs(reference - value) <= half_unit:
            return fact
        if target is not None and int(reference) == target:
            return fact                              # kesme: 43.62 → "43"
    return None


#  Ölçüm anahtarlarında geçen ama ayırt edici OLMAYAN kelimeler. Bunlarla
#  eşleşmek, "aynı şeyden bahsediyoruz" demek için yeterli değildir.
#  "haber" buradan çıkarıldı: sanılanın aksine ayırt edicidir. Zayıf sayılınca
#  `haber.adet` hiçbir iddiaya bağlanamıyor ve "18 haber var" cümlesi —
#  defterde tam olarak 18 yazdığı hâlde — dayanaksız görünüyordu.
_WEAK_KEY_WORDS = frozenset({
    "usdt", "usd", "try", "btc", "eth", "portfoy", "indicators", "algo",
    "backtest", "kaynak", "son", "pct", "adet", "value", "data",
})

#  KAVRAM KÖPRÜSÜ
#
#  Defter anahtarları teknik ve çoğu İngilizce (`profit_factor`, `atr_14`);
#  uzmanların metni Türkçe ("kâr faktörü", "volatilite"). Köprü olmadan
#  doğrulayıcı, ölçümle DOĞRUDAN çelişen bir cümleyi "dayanaksız" sayar ve
#  gerçek yalan, zayıf bir uyarının arkasında kaybolur.
#
#  Sol taraf: metinde geçebilecek kelimeler. Sağ taraf: defter anahtarında
#  aranan parça. Eşleşme iki yönlü çalışır.
_CONCEPTS: dict[str, tuple[str, ...]] = {
    "fiyat": ("fiyat", "price", "close", "last"),
    "dolar": ("fiyat", "price"),
    "dolardan": ("fiyat", "price"),
    "seviye": ("fiyat", "price"),
    "kur": ("fiyat", "price"),
    "hacim": ("volume", "hacim"),
    "makas": ("spread",),
    "spread": ("spread",),
    "oynaklik": ("atr", "volatility", "bb"),
    "volatilite": ("atr", "volatility", "bb"),
    "kar": ("profit", "pnl", "net"),
    "faktoru": ("factor", "profit"),
    "faktor": ("factor", "profit"),
    "kazanma": ("win", "rate"),
    "basari": ("win", "rate"),
    "cekilme": ("drawdown",),
    "drawdown": ("drawdown",),
    "islem": ("trades", "trade", "count"),
    "sermaye": ("sermaye", "equity", "balance"),
    "bakiye": ("sermaye", "equity", "balance"),
    "pozisyon": ("pozisyon", "position"),
    "risk": ("risk", "heat"),
    "stop": ("stop", "mesafe"),
    "loss": ("stop", "mesafe"),
    "hedef": ("hedef", "target", "take"),
    "take": ("hedef", "target", "take"),
    "profit": ("hedef", "target", "take"),
    # "6/8 strateji desteklemiyor" → karşıt strateji sayısı
    "desteklemiyor": ("karsit", "agree", "total"),
    "karsit": ("karsit",),
    "uyumsuzluk": ("karsit_pct", "karsit"),
    "uyumsuz": ("karsit_pct", "karsit"),
    "notr": ("karsit", "agree"),
    "destek": ("support", "bb", "low"),
    "direnc": ("resistance", "bb", "high"),
    "guven": ("confidence", "confidence_pct"),
    "guveni": ("confidence", "confidence_pct"),
    "guvenle": ("confidence", "confidence_pct"),
    "oran": ("odul", "ratio", "rate", "pct"),
    "orani": ("odul", "ratio", "rate", "pct"),
    "odul": ("odul", "risk_odul"),
    "riskodul": ("risk_odul", "odul"),
    "mesafe": ("mesafe", "distance"),
    "mesafesi": ("mesafe", "distance"),
    "asagi": ("stop", "mesafe"),
    "yukari": ("hedef", "mesafe"),
    "uzaklik": ("uzaklik", "mesafe"),
    "trend": ("adx", "trend", "ema"),
    "mum": ("candle", "mum"),
    "mumda": ("candle", "mum"),
    "mumluk": ("candle", "mum"),
    "degisim": ("change", "degisim"),
    "artis": ("change", "degisim"),
    "dusus": ("change", "degisim"),
    "stokastik": ("stoch",),
    "stoch": ("stoch",),
    "bant": ("bb", "band"),
    "banti": ("bb", "band"),
    "bantlari": ("bb", "band"),
    "bollinger": ("bb",),
    "supertrend": ("supertrend",),
    "ortalama": ("ema", "sma", "ortalama"),
    "skor": ("score", "skor"),
    "puan": ("score",),
    "strateji": ("agree", "total", "strateji", "signals"),
    # "3/8" bir çifttir: pay uyum sayısı, payda toplam strateji sayısı.
    # "agree" yalnızca paya bağlanırsa payda çelişkili görünür.
    "agree": ("agree", "total"),
    "uyum": ("agree", "total", "uyum"),
    "anlasan": ("agree", "total"),
    "sinyal": ("signal", "action", "signals"),
    "uzak": ("uzaklik", "mesafe", "distance"),
    "uzakta": ("uzaklik", "mesafe", "distance"),
    "uzaklikta": ("uzaklik", "mesafe", "distance"),
    "range": ("atr", "range"),
    "aralik": ("atr", "range"),
    "genislik": ("width", "bb"),
    "kasa": ("sermaye", "equity", "risk"),
    "buyukluk": ("pozisyon", "size", "adet"),
    "buyuklugu": ("pozisyon", "size", "adet"),
    "momentum": ("rsi", "macd"),
    "haber": ("haber", "news"),
    "duygu": ("duygu", "sentiment"),
}


#  Bölünürken kaybolan kısaltmalar. Sol taraf metinde geçen hâli, sağ taraf
#  kavram köprüsünün tanıdığı açık yazımı.
_ABBREVIATIONS = (
    ("r/r", "riskodul"), ("r:r", "riskodul"), ("r/ö", "riskodul"),
    ("risk/odul", "riskodul"), ("risk/ödül", "riskodul"),
    ("rr orani", "riskodul"), ("k/z", "kar zarar"),
    ("s/l", "stop"), ("t/p", "hedef"), ("%b", "percent_b"),
)


def _concept_words(sentence_words: set[str]) -> set[str]:
    """
    Cümledeki kelimelerden defter anahtarında aranacak parçaları üretir.

    Türkçe eklemeli bir dildir: "fiyat", "fiyattan", "fiyatın", "fiyatı"
    aynı kavramdır. Her ekli hâli tek tek listelemek hem bitmez hem de
    unutulan bir ek yüzünden doğru bir iddia sessizce dayanaksız kalır.
    Bu yüzden kelime, kavram anahtarlarıyla KÖK olarak eşleştirilir:
    kelime bir anahtarla başlıyorsa o kavramı taşır.

    Kök eşleşmesi yalnızca 4+ harfli anahtarlarda yapılır; kısa anahtarlar
    ("ay", "kar") rastgele kelimelerin başına denk gelip yanlış kavram
    üretirdi.
    """
    mapped: set[str] = set(sentence_words)
    for word in sentence_words:
        for target in _CONCEPTS.get(word, ()):
            mapped.add(target)
        if word in _CONCEPTS:
            continue
        for key, targets in _CONCEPTS.items():
            if len(key) >= 4 and word.startswith(key):
                mapped.update(targets)
                break
    return mapped


def _matches(key: str, word: str) -> bool:
    """İki terim aynı kavramı gösteriyor mu (katı eşleşme)."""
    if key == word:
        return True
    return (len(key) >= 4 and len(word) >= 4
            and (key.startswith(word) or word.startswith(key)))


#  Konusu uymayan bir ölçümle eşleşme için gereken sıkı tolerans. Dört
#  anlamlı basamağın rastlantıyla tutması beklenmez; bu yüzden konu bağı
#  olmadan da güvenilebilir.
EXACT_TOLERANCE = 0.0005          # %0.05


def _significant_digits(raw: str) -> int:
    """Metinde yazılan sayının kaç anlamlı basamağı var."""
    digits = "".join(ch for ch in raw if ch.isdigit()).lstrip("0")
    return len(digits)


#  İşareti ANLAM TAŞIMAYAN ölçümler: bir mesafe/uzaklık büyüklüktür.
#  Model "stop-loss (-3.43%)" yazar çünkü stop aşağıdadır; türev ölçüm
#  mesafeyi +3.43 olarak tutar. Aynı şeyi söylüyorlar.
#
#  Bu esneklik BAŞKA hiçbir ölçüme uygulanamaz: MACD histogramında,
#  Z-skorunda ve değişim yüzdesinde işaret anlamlıdır ve orada esneklik
#  göstermek gerçek bir hatayı gizlerdi.
_UNSIGNED_KEYS = ("mesafe", "uzaklik", "distance", "genislik", "width", "spread")


def _is_unsigned(fact: Fact) -> bool:
    key = fact.key.lower()
    return any(part in key for part in _UNSIGNED_KEYS)


def _first_match(values: list[float], facts: list[Fact], tolerance: float,
                 raw: str, allow_rounding: bool) -> tuple[Fact | None, float]:
    """
    Verilen okumalardan herhangi biri, verilen ölçümlerden birine uyuyor mu?

    Dönen ikinci değer, eşleşmeyi sağlayan okumadır: raporda hangi yorumun
    tuttuğunu göstermek için saklanır.
    """
    # Mesafe ölçümleri için işaretin tersi de denenir (bkz. `_UNSIGNED_KEYS`).
    unsigned = [f for f in facts if _is_unsigned(f)]
    if unsigned and any(v < 0 for v in values):
        values = [*values, *(abs(v) for v in values if v < 0)]

    for value in values:
        best: Fact | None = None
        best_gap = float("inf")
        for fact in facts:
            reference = fact.numeric
            if reference is None:
                continue
            scale = max(abs(reference), 1e-9)
            gap = (abs(reference - value) / scale if abs(reference) > 1e-6
                   else abs(reference - value))
            if gap <= tolerance and gap < best_gap:
                best, best_gap = fact, gap
        if best is not None:
            return best, value
        if allow_rounding:
            rounded = _rounded_match_in(value, facts, raw)
            if rounded is not None:
                return rounded, value
    return None, 0.0


def _pick_contradiction(claim: Claim, related: list[Fact]) -> Fact | None:
    """
    Konusu uyan ama değeri tutmayan bir ölçüm var mı?

    Çelişki ilan etmek ağır bir suçlamadır ve iki şart aranır:

      1. Ölçüm AYNI ŞEYİ ölçmeli (konu bağı zaten `related` ile kuruldu).
      2. Büyüklük mertebesi yakın olmalı — mertebe farkı varsa iki sayı
         farklı şeylerdir, çelişki değil.

    Birden çok aday varsa EN YAKIN olan seçilir. Yaşanmış hata: "MACD
    histogramı –376.5" cümlesi "momentum" kavramı üzerinden hem RSI'ye hem
    MACD'ye bağlanıyordu; defterde önce RSI geçtiği için iddia onunla
    karşılaştırılıp haksız yere çelişkili ilan edildi. Suçlamada en alakalı
    ölçüme bakılır, ilk rastlanana değil.
    """
    best: Fact | None = None
    best_gap = float("inf")
    for fact in related:
        reference = fact.numeric
        if reference is None or reference == 0:
            continue
        ratio = abs(claim.value) / abs(reference)
        if not (0.05 <= ratio <= 20.0):
            continue
        gap = abs(reference - claim.value) / max(abs(reference), 1e-9)
        if gap < best_gap:
            best, best_gap = fact, gap
    return best


def _related_facts(claim: Claim, ledger: Ledger) -> list[Fact]:
    """
    İddianın konusuyla AYNI şeyi ölçen olgular.

    Çelişki ilan etmek ağır bir suçlamadır ve yalnızca GÜÇLÜ kanıtla
    yapılmalıdır. İki koşul birden aranır:

      1. Ölçümün ayırt edici adı (rsi, adx, atr, stop_loss…) cümlede geçmeli.
      2. Büyüklükler aynı mertebede olmalı — 2.9 ile 76.285 arasında
         "çelişki" yoktur, bunlar farklı şeylerdir.

    Koşullar sağlanmazsa iddia "dayanaksız"dır: bilmediğimizi söylemek,
    yanlış suçlamada bulunmaktan iyidir.
    """
    #  Önce sayının YEREL çevresine bakılır: uzun bir cümlede birden çok konu
    #  geçer ve sayı, kendisine en yakın konuya aittir. Yerel pencere hiçbir
    #  ölçümle eşleşmezse tüm cümleye genişletilir — konu cümlenin başında
    #  adlandırılmış olabilir ("ATR 14: 42.86 … ~%1.7 aralık" gibi).
    for scope in (claim.context or claim.text, claim.text):
        related = _related_in(scope, claim, ledger)
        if related:
            return related
    return []


def _related_in(scope: str, claim: Claim, ledger: Ledger) -> list[Fact]:
    """Verilen metin parçasına göre konusu uyan ölçümler."""
    # Metin ve defter anahtarı AYNI kuralla bölünmeli. Anahtarlar rakamlar
    # atılarak bölünürken ("ema_20" → "ema") metin rakamlar korunarak
    # bölünüyordu ("EMA20" → "ema20"); aynı kavram birbirini bulamıyordu.
    # Kısaltmalar bölünürken kaybolur: "R/R: 2.15" satırında tek harfler
    # elendiği için hiçbir anlamlı kelime kalmıyor ve iddia hiçbir ölçümle
    # bağ kuramıyordu. Bu yüzden önce açık yazımlarına çevrilirler.
    text = fold(scope)
    for short, long in _ABBREVIATIONS:
        text = text.replace(short, long)

    words = {w for w in re.split(r"[\W\d_]+", text) if len(w) > 2}
    if not words:
        return []
    words = _concept_words(words)

    related: list[Fact] = []
    for fact in ledger.numbers():
        key_words = {w for w in re.split(r"[.\W_\d]+", fold(fact.key)) if len(w) > 2}
        distinctive = key_words - _WEAK_KEY_WORDS
        if not distinctive:
            continue
        # Anahtar parçası, sayının YEREL çevresindeki kavramlardan biriyle
        # örtüşmeli. Eşleşme KATI tutulur: serbest alt-dize karşılaştırması
        # ("sl" her yerde geçer) alakasız ölçümleri birbirine bağlar.
        if any(_matches(key, w) for key in distinctive for w in words):
            related.append(fact)
    return related
