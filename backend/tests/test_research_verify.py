"""
İDDİA DOĞRULAMA

Bu dosya, sistemin en kritik sözünü korur: **modelin ürettiği hiçbir sayı
doğrulanmadan rapora girmez.**

İki yönlü sınanır ve ikisi de aynı derecede önemlidir:

  YAKALAMA  uydurma bir sayı ÇELİŞİYOR olarak işaretlenmeli.
  SESSİZLİK doğru yazılmış bir sayı için alarm verilmemeli.

İkincisi ilkinden daha kolay ihmal edilir ve daha çok zarar verir: boşuna
alarm veren bir doğrulayıcı, kullanıcının bir süre sonra tüm uyarıları
görmezden gelmesine yol açar. Buradaki örneklerin çoğu, canlı çalıştırmada
gerçekten yanlış alarm üretmiş metinlerdir.
"""
from __future__ import annotations

import pytest

from app.research.ledger import Fact, Ledger
from app.research.verify import Verdict, audit, extract_claims


@pytest.fixture()
def ledger() -> Ledger:
    """Gerçek bir araştırmadan alınmış ölçümler."""
    led = Ledger()
    led.add("BTC/USDT.fiyat", 78564.48, "binance", "ticker", "USD")
    led.add("BTC/USDT.indicators.rsi_14", 61.4, "binance", "indicators")
    led.add("BTC/USDT.indicators.adx_14", 43.62, "binance", "indicators")
    led.add("BTC/USDT.indicators.atr_14", 1143.31, "binance", "indicators")
    led.add("BTC/USDT.indicators.volume_z_score", -0.84, "binance", "indicators")
    led.add("BTC/USDT.backtest.profit_factor", 1.24, "backtest", "sim")
    led.add("BTC/USDT.backtest.trades_count", 37, "backtest", "sim")
    led.add("portfoy.sermaye", 10000.0, "veritabani", "toplam", "USD")
    return led


def _verdicts(text: str, led: Ledger) -> list[str]:
    return [str(c.verdict) for c in audit(text, led).claims]


# --------------------------------------------------------------------------- #
#  YAKALAMA — uydurma sayılar işaretlenmeli
# --------------------------------------------------------------------------- #

def test_invented_price_is_contradicted(ledger) -> None:
    """En tehlikeli yalan: ölçülmüş fiyattan farklı bir fiyat yazmak."""
    result = audit("BTC şu an 95.000 dolardan işlem görüyor.", ledger)
    assert result.claims[0].verdict is Verdict.CONTRADICTED
    assert "78564" in result.claims[0].note.replace(".", "").replace(",", "")


def test_invented_indicator_is_contradicted(ledger) -> None:
    result = audit("RSI 28 ile aşırı satım bölgesinde.", ledger)
    assert result.claims[0].verdict is Verdict.CONTRADICTED


def test_invented_backtest_metric_is_contradicted(ledger) -> None:
    """
    Kâr faktörünü şişirmek, kullanıcıyı doğrudan paraya mal eder.

    Defter 1.24 diyorsa "3,8" yazan bir metin işaretlenmelidir — üstelik
    defter anahtarı İngilizce (`profit_factor`), metin Türkçe olduğu hâlde.
    """
    result = audit("Kâr faktörü 3,8 gibi çok güçlü bir değer.", ledger)
    assert result.claims[0].verdict is Verdict.CONTRADICTED


def test_forecast_is_a_projection_not_an_accusation(ledger) -> None:
    """
    Tahmin yalan değildir; ölçüm de değildir; dayanaksızlık da değildir.

    "120.000 doları görebilir" cümlesi geleceğe dairdir: bugünkü ölçümle
    ÇELİŞEMEZ, çünkü bugün hakkında bir iddiada bulunmuyor. "Dayanaksız"
    demek de örtük bir suçlamadır — analistin ileriye bakması meşru işidir.
    Doğru etiket ÖNGÖRÜ'dür.
    """
    result = audit("Önümüzdeki ay 120.000 doları görebilir.", ledger)
    assert result.claims[0].verdict is Verdict.PROJECTION
    assert not result.contradicted


def test_measurable_but_unmeasured_stays_unsupported(ledger) -> None:
    """
    Ölçülebilirdi ama ölçülmedi: bu hâlâ DAYANAKSIZ'dır.

    Öngörü sınıfı, ölçülmemiş her sayıya kaçış kapısı olmamalı; yalnızca
    geleceğe dair ifadeleri kapsar.
    """
    result = audit("Kurumsal giriş hacmi 4,2 milyar dolar.", ledger)
    assert result.claims[0].verdict is Verdict.UNSUPPORTED


def test_trust_score_punishes_contradiction_more_than_ignorance(ledger) -> None:
    lying = audit("BTC fiyatı 95.000 dolar.", ledger)
    guessing = audit("Kurumsal giriş hacmi 4,2 milyar dolar.", ledger)
    assert lying.trust_score < guessing.trust_score


# --------------------------------------------------------------------------- #
#  SESSİZLİK — doğru yazımlar için alarm verilmemeli
# --------------------------------------------------------------------------- #

def test_turkish_thousands_separator_is_not_a_lie(ledger) -> None:
    """
    Türkçede "78.564" yetmiş sekiz bin demektir.

    Bu, canlı çalıştırmada 8 sahte çelişki üreten gerçek hatadır: doğru
    yazan analist yalancı ilan ediliyordu.
    """
    assert _verdicts("BTC fiyatı 78.564 dolar.", ledger) == [str(Verdict.SUPPORTED)]


def test_both_readings_of_an_ambiguous_number_are_tried() -> None:
    """
    "1.234" hem bin iki yüz otuz dört hem de 1,234 olabilir.

    Hangisinin kastedildiği metinden anlaşılmaz; ölçümle uyuşan okuma
    varsa iddia desteklenmiş sayılır. Şüphe, iddia sahibinin lehinedir.
    """
    led = Ledger()
    led.add("x.deger", 1.234, "test", "olcum")
    assert _verdicts("Değer 1.234 olarak ölçüldü.", led) == [str(Verdict.SUPPORTED)]

    led2 = Ledger()
    led2.add("x.deger", 1234.0, "test", "olcum")
    assert _verdicts("Değer 1.234 olarak ölçüldü.", led2) == [str(Verdict.SUPPORTED)]


def test_rounding_and_truncation_are_not_lies(ledger) -> None:
    """
    "ADX 43" yazmak 43.62 için meşrudur — hem yuvarlama hem kesme.

    İnsanlar prozada ondalık yazmaz; bunu çelişki saymak doğrulayıcıyı
    gürültü kaynağına çevirir.
    """
    assert _verdicts("ADX 43 seviyesinde.", ledger) == [str(Verdict.SUPPORTED)]
    assert _verdicts("ADX 44 seviyesinde.", ledger) == [str(Verdict.SUPPORTED)]


def test_unit_descriptors_are_not_claims() -> None:
    """"24 saatlik hacim" cümlesindeki 24 bir iddia değil, birimdir."""
    assert extract_claims("24 saatlik hacim yüksek.") == []
    assert extract_claims("14 günlük RSI hesaplandı.") == []


def test_list_numbering_is_not_a_claim() -> None:
    """Modeller madde numarası yazar; bunlar metnin iskeletidir."""
    assert extract_claims("Point 1: giriş. Point 2: çıkış.") == []
    assert extract_claims("3. madde en önemlisidir.") == []


def test_number_is_matched_against_its_own_neighbourhood(ledger) -> None:
    """
    Uzun bir cümlede sayı, KENDİ çevresindeki ölçümle karşılaştırılır.

    Gerçek örnek: "Stop 76.285'e ~%2.9 mesafe, hedef 83.716'ya ~%6.5 —
    … hacim teyidi yok" cümlesindeki %2.9, cümlenin sonundaki "hacim"
    kelimesi yüzünden hacim ölçümüyle karşılaştırılıp sahte çelişki
    üretiyordu.
    """
    text = ("Stop 76.285'e ~%2.9 mesafe, hedef 83.716'ya ~%6.5 — risk/ödül "
            "kağıt üzerinde olumlu görünse de hacim teyidi yok.")
    result = audit(text, ledger)
    volume_hits = [c for c in result.contradicted
                   if c.evidence and "volume" in c.evidence.key]
    assert not volume_hits, "sayı, alakasız bir ölçümle karşılaştırıldı"


def test_different_magnitudes_are_not_contradictions(ledger) -> None:
    """2.9 ile 76285 arasında çelişki yoktur; bunlar farklı şeylerdir."""
    result = audit("Stop mesafesi yaklaşık %2.9 kadar.", ledger)
    assert not result.contradicted


def test_no_claims_means_full_trust(ledger) -> None:
    """Sayısal iddia yoksa yanıltma da yoktur."""
    result = audit("Genel görünüm temkinli olmayı gerektiriyor.", ledger)
    assert result.claims == []
    assert result.trust_score == 1.0


# --------------------------------------------------------------------------- #
#  Kaynak atıfları
# --------------------------------------------------------------------------- #

def test_uncited_source_is_flagged(ledger) -> None:
    """Okunmamış bir kaynağa atıf, uydurma atıftır."""
    result = audit("Reuters'a göre kurumsal talep artıyor.", ledger)
    assert "reuters" in result.unknown_sources


def test_source_actually_used_is_not_flagged() -> None:
    """Gerçekten okunmuş kaynak uyarı üretmemeli."""
    led = Ledger()
    led.add("haber.adet", 12, "rss:coindesk.com", "fetch_news")
    result = audit("coindesk.com kurumsal talebin arttığını yazıyor.", led)
    assert result.unknown_sources == []


def test_citation_direction_is_turkish_aware(ledger) -> None:
    """
    Türkçede kaynak "göre"den ÖNCE gelir.

    Yön karıştırılırsa cümlenin geri kalanı kaynak adı sanılır: "kurumsal
    talep artıyor" diye bir haber ajansı yoktur.
    """
    result = audit("Bloomberg'a göre kurumsal talep artıyor.", ledger)
    assert "bloomberg" in result.unknown_sources
    assert not any("talep" in s for s in result.unknown_sources)


# --------------------------------------------------------------------------- #
#  Defter
# --------------------------------------------------------------------------- #

def test_ledger_is_append_only() -> None:
    """
    Bir olgu yazıldıktan sonra üzerine yazılmaz.

    "Rakam sonradan değiştirildi mi" sorusu her zaman cevaplanabilmelidir.
    """
    led = Ledger()
    led.add("x", 1.0, "a")
    led.add("x", 2.0, "b")
    assert len(led.all()) == 2
    assert led.get("x").value == 2.0, "en son değer okunmalı"
    assert led.all()[0].value == 1.0, "eski değer silinmiş"


def test_ledger_citation_names_its_source() -> None:
    fact = Fact(key="btc.fiyat", value=100.0, source="binance", method="ticker")
    assert "binance" in fact.cite()
    assert "ticker" in fact.cite()


def test_flatten_keeps_summaries_not_raw_series() -> None:
    """
    İç içe araç çıktıları düzleştirilir ama ham seriler saklanmaz.

    1000 mumluk bir listeyi tek tek deftere yazmak defteri okunamaz hâle
    getirir ve doğrulamayı işe yaramaz kılar.
    """
    led = Ledger()
    led.add_many("test", {
        "fiyat": {"son": 100.0, "acilis": 98.0},
        "mumlar": [1, 2, 3, 4, 5],
        "ok": True,                      # atlanır (gürültü)
    })
    keys = {f.key for f in led.all()}
    assert "fiyat.son" in keys
    assert "mumlar.adet" in keys
    assert not any(k.startswith("mumlar.0") for k in keys)
    assert "ok" not in keys
