"""
TÜREV ÖLÇÜMLER VE İDDİA SINIFLARI

Doğrulayıcı ilk sürümde şunu yapıyordu: uzman "stop fiyattan %2,7 aşağı"
yazdığında, defterde 2.7 diye bir sayı olmadığı için iddiayı DAYANAKSIZ
ilan ediyordu. Oysa fiyat da stop da defterdeydi; aradaki yüzde tek bir
bölmeydi. Sistem hesaplamadığı şey için uzmanı suçluyordu.

Gerçek raporlar üzerinde ölçüldü: 43 dayanaksız iddianın 23'ü bu yüzdendi.

Bu dosya iki şeyi birden korur ve ikisi de gereklidir:
  * hesaplanabilir olan hesaplanır (dayanaksızlık azalır),
  * uydurma sayı YİNE yakalanır (gevşeme, körleşme değildir).
"""
from __future__ import annotations

import pytest

from app.research import derive
from app.research.ledger import Ledger
from app.research.verify import Verdict, audit, extract_claims


@pytest.fixture()
def ledger() -> Ledger:
    """Gerçek bir BTC araştırmasından alınmış ham ölçümler + türevleri."""
    led = Ledger()
    for key, value in [
        ("BTC/USDT.fiyat", 78427.40),
        ("BTC/USDT.indicators.rsi_14", 56.0),
        ("BTC/USDT.indicators.adx_14", 41.3),
        ("BTC/USDT.indicators.atr_14", 1070.34),
        ("BTC/USDT.indicators.ema_50", 75310.23),
        ("BTC/USDT.indicators.bb_lower", 76945.16),
        ("BTC/USDT.indicators.bb_upper", 80285.79),
        ("BTC/USDT.indicators.stoch_k", 21.84),
        ("BTC/USDT.indicators.change_24_candle_pct", 1.755),
        ("BTC/USDT.algo.stop_loss", 76286.74),
        ("BTC/USDT.algo.take_profit", 83243.92),
        ("BTC/USDT.algo.confidence", 0.948),
        ("BTC/USDT.algo.agree", 3),
        ("BTC/USDT.algo.total", 8),
        ("BTC/USDT.backtest.profit_factor", 1.24),
        ("portfoy.sermaye", 10000.0),
    ]:
        led.add(key, value, "test", "olcum")
    derive.enrich(led, {"symbols": ["BTC/USDT"]})
    return led


def _verdicts(text: str, led: Ledger) -> list[str]:
    return [str(c.verdict) for c in audit(text, led).claims]


# --------------------------------------------------------------------------- #
#  Türev üretimi
# --------------------------------------------------------------------------- #

def test_stop_and_target_distances_are_computed(ledger) -> None:
    """
    Uzmanların en sık türettiği üç sayı artık ÖLÇÜLMÜŞ olur.

    (78427.40 - 76286.74) / 78427.40 * 100 = 2.729
    """
    assert ledger.get("BTC/USDT.stop_mesafe_pct").numeric == pytest.approx(2.729, abs=0.01)
    assert ledger.get("BTC/USDT.hedef_mesafe_pct").numeric == pytest.approx(6.14, abs=0.01)
    assert ledger.get("BTC/USDT.risk_odul").numeric == pytest.approx(2.25, abs=0.01)


def test_percentage_form_of_a_ratio_is_written(ledger) -> None:
    """
    "%94.8" ile defterdeki 0.948 aynı şeydir ama eşleşmezdi.

    Yüzde karşılığını da yazmak bu sahte uyuşmazlığı kökten bitirir.
    """
    assert ledger.get("BTC/USDT.algo.confidence_pct").numeric == pytest.approx(94.8)
    assert ledger.get("BTC/USDT.algo.uyum_pct").numeric == pytest.approx(37.5)


def test_every_derived_fact_carries_its_formula(ledger) -> None:
    """
    Kaynağı gösterilemeyen bir sayı, hesaplanmış olsa bile güvenilmezdir.

    Türev olgular defterin en kolay şüphe çekecek kısmıdır; nasıl
    üretildikleri yanlarında yazmazsa denetim zinciri kopar.
    """
    derived = [f for f in ledger.all() if f.source == derive.SOURCE]
    assert derived, "hiç türev üretilmedi"
    for fact in derived:
        assert fact.method, f"{fact.key} formülsüz yazılmış"


def test_missing_input_produces_no_derived_fact() -> None:
    """
    Hesaplanamayan şey SIFIR diye yazılmaz.

    Uydurulmuş bir risk sayısı, hesap yapmamaktan daha tehlikelidir.
    """
    led = Ledger()
    led.add("BTC/USDT.fiyat", 78427.40, "test", "olcum")   # stop/hedef yok
    derive.enrich(led, {"symbols": ["BTC/USDT"]})
    assert led.get("BTC/USDT.stop_mesafe_pct") is None
    assert led.get("BTC/USDT.risk_odul") is None


def test_derivation_survives_an_empty_ledger() -> None:
    led = Ledger()
    assert derive.enrich(led, {"symbols": ["BTC/USDT"]}) == 0


# --------------------------------------------------------------------------- #
#  Türevler doğrulamayı besler
# --------------------------------------------------------------------------- #

def test_derived_distance_is_now_supported(ledger) -> None:
    """Gerçek rapordan alınmış satır — eskiden DAYANAKSIZ'dı."""
    assert _verdicts("SL 76.286,7 (fiyattan ~%2,7 aşağı)", ledger) == \
        [str(Verdict.SUPPORTED)] * 2


def test_derived_risk_reward_is_now_supported(ledger) -> None:
    assert _verdicts("R/R oranı ~2,3.", ledger) == [str(Verdict.SUPPORTED)]


def test_percent_written_in_turkish_order_is_supported(ledger) -> None:
    """Türkçede yüzde işareti ÖNDE gelir: "%94.8"."""
    assert _verdicts("Algo güveni %94.8.", ledger) == [str(Verdict.SUPPORTED)]


# --------------------------------------------------------------------------- #
#  Ayrıştırma kusurları
# --------------------------------------------------------------------------- #

def test_space_separated_thousands_is_one_number(ledger) -> None:
    """
    "76 286.74" tek bir sayıdır.

    Boşluklu binlik ayracı tanınmayınca sayı ikiye bölünüyor ve tek bir
    doğru iddia, iki sahte dayanaksız iddiaya dönüşüyordu.
    """
    claims = extract_claims("Stop 76 286.74 seviyesinde.")
    assert len(claims) == 1
    assert claims[0].value == pytest.approx(76286.74)
    assert _verdicts("Stop 76 286.74 seviyesinde.", ledger) == [str(Verdict.SUPPORTED)]


def test_number_at_end_of_sentence_is_not_skipped() -> None:
    """
    Liste numarası SATIR BAŞINDA olur.

    Konum şartı olmadan cümle sonundaki her sayı ("R/R ~2,3.") liste
    numarası sanılıp sessizce atlanıyordu. Sessiz atlama, yanlış yargıdan
    daha sinsidir: kimse eksik olanı fark etmez.
    """
    assert len(extract_claims("R/R oranı ~2,3.")) == 1
    assert extract_claims("1) giriş yapılır.") == []


def test_each_bullet_line_is_its_own_claim() -> None:
    """
    Satır sonu da cümle sonudur.

    Madde listeleri noktayla bitmez; tek parça sayıldıklarında bir
    maddedeki sayı başka bir maddedeki kelimeyle eşleştiriliyordu.
    """
    text = "- **SL:** 76.286,7 (fiyattan ~%2,7 aşağı)\n- **TP:** 83.243,9"
    contexts = {c.context for c in extract_claims(text)}
    assert not any("TP" in c and "SL" in c for c in contexts), \
        "iki madde tek bağlamda birleşti"


def test_threshold_levels_are_not_claims() -> None:
    """">70" bir ölçüm değil, RSI'nin tanımıdır."""
    assert extract_claims("Aşırı alım sayılmaz (>70).") == []
    assert extract_claims("RSI < 30 olursa aşırı satım.") == []


def test_meta_statements_are_not_claims() -> None:
    """Modelin defter hakkında konuşması piyasa iddiası değildir."""
    assert extract_claims("Bu rapor 59 olguya dayanır.") == []
    assert extract_claims("They provide 58 data points.") == []


# --------------------------------------------------------------------------- #
#  Gevşeme körleşme DEĞİLDİR
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "BTC fiyatı şu an 95.000 dolar.",
    "RSI 28 ile aşırı satım bölgesinde.",
    "Kâr faktörü 3,8 gibi çok güçlü.",
    "Stop fiyattan %12,5 aşağıda.",
    "R/R oranı 8,0 ile mükemmel.",
    "Algo güveni %35 gibi düşük.",
    "ADX 12 ile trend zayıf.",
    "Alt bant 60.000 seviyesinde.",
    "Sermayeniz 250.000 dolar.",
])
def test_invented_numbers_are_still_caught(ledger, text: str) -> None:
    """
    Toleransları gevşetmek yalanları kaçırmamalı.

    Bu liste, türev katmanı eklendikten SONRA yazıldı: kazanılan esneklik
    körlük pahasına gelmemeli.
    """
    assert str(Verdict.CONTRADICTED) in _verdicts(text, ledger), \
        f"uydurma sayı yakalanmadı: {text}"


def test_numeric_coincidence_is_not_evidence(ledger) -> None:
    """
    Konusu uymayan bir ölçüme yakın düşmek KANIT DEĞİLDİR.

    Yaşanmış hata: "BTC 95.000 dolar" iddiası, algoritma güveninin yüzde
    karşılığına (94.8) denk geldiği için DOĞRULANDI sayılıyordu. Uydurma
    bir fiyatı "ölçümle doğrulandı" diye göstermek, bu ürünün yapabileceği
    en kötü hatadır.
    """
    result = audit("BTC fiyatı şu an 95.000 dolar.", ledger)
    claim = result.claims[0]
    assert claim.verdict is not Verdict.SUPPORTED
    assert claim.evidence is None or "confidence" not in claim.evidence.key


def test_truncation_rule_does_not_apply_below_one(ledger) -> None:
    """
    1'in altında her değer 0'a kesilir.

    Kesme kuralı bu aralıkta anlamsızdır: 0.948 ile 0.35 "aynı" görünür ve
    uydurma bir güven oranı doğrulanmış sayılırdı.
    """
    assert str(Verdict.SUPPORTED) not in _verdicts("Algo güveni 0,35.", ledger)


# --------------------------------------------------------------------------- #
#  Öngörü ayrı bir sınıftır
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "Yıl sonunda 200.000 doları görebilir.",
    "Önümüzdeki ay %40 yükselebilir.",
    "Hedef olarak 90.000 bekleniyor.",
])
def test_forward_looking_numbers_are_projections(ledger, text: str) -> None:
    """
    Geleceğe dair bir sayı bugünkü ölçümle ÇELİŞEMEZ.

    Analistin ileriye bakması meşru işidir; onu "dayanaksız" ya da
    "çelişkili" diye işaretlemek raporu değersizleştirir.
    """
    assert str(Verdict.PROJECTION) in _verdicts(text, ledger)


def test_projection_is_never_called_a_contradiction(ledger) -> None:
    result = audit("Yıl sonunda 200.000 doları görebilir.", ledger)
    assert not result.contradicted


def test_projection_scores_better_than_ignorance_but_worse_than_measurement(
        ledger) -> None:
    """
    Güven ölçeği dört sınıfı ayırt etmeli.

    Öngörüyü dayanaksızlıkla eşitlemek, ileriye bakmayı cezalandırmak
    olurdu; ölçümle eşitlemek ise beklentiyi ölçüm gibi göstermek.
    """
    measured = audit("BTC fiyatı 78.427 dolar.", ledger).trust_score
    forecast = audit("Yıl sonunda 200.000 doları görebilir.", ledger).trust_score
    unknown = audit("Kurumsal giriş hacmi 4,2 milyar dolar.", ledger).trust_score
    lying = audit("BTC fiyatı 95.000 dolar.", ledger).trust_score

    assert measured > forecast > unknown > lying


def test_report_marks_projections_separately(ledger) -> None:
    """Rapor, beklentiyi ölçümden ayırt edilebilir göstermeli."""
    from app.research.pipeline import ResearchResult, RoleOutput
    from app.research.report import render

    result = ResearchResult(question="BTC?", ledger=ledger)
    output = RoleOutput(role="market", label="Piyasa Analisti",
                        text="Yıl sonunda 200.000 doları görebilir.")
    output.audit = audit(output.text, ledger)
    result.outputs.append(output)

    markdown = render(result)
    assert "Öngörü (ölçüm değil)" in markdown
    assert "Ölçüm karşılığı yok" not in markdown


# --------------------------------------------------------------------------- #
#  Canlı koşudan çıkan kurallar
# --------------------------------------------------------------------------- #

def test_indicator_period_is_not_a_claim(ledger) -> None:
    """
    "RSI 14: 55.72" satırında 14 periyot, 55.72 ölçümdür.

    Canlı koşuda beş sahte çelişkinin tamamı bu kalıptandı: periyot,
    göstergenin kendi değeriyle karşılaştırılıyordu.
    """
    assert [c.raw for c in extract_claims("RSI 14: 55.72")] == ["55.72"]
    assert [c.raw for c in extract_claims("EMA 200: 68612.20")] == ["68612.20"]
    assert [c.raw for c in extract_claims("ATR 14: 1084.62")] == ["1084.62"]


def test_indicator_value_is_still_a_claim(ledger) -> None:
    """
    "RSI 28 aşırı satım" cümlesinde 28 ÖLÇÜMÜN KENDİSİDİR.

    Periyot kuralı yalnızca gösterge adına bakarak uygulanırsa doğrulayıcı,
    tam da yakalaması gereken uydurma değere kör kalır.
    """
    assert [c.raw for c in extract_claims("RSI 28 aşırı satım bölgesinde.")] == ["28"]
    assert str(Verdict.CONTRADICTED) in _verdicts("RSI 28 aşırı satım.", ledger)


def test_unicode_minus_is_read_as_a_sign() -> None:
    """
    Modeller tire yerine uzun tire yazar: "–376.5".

    İşaret okunmazsa değer +376.5 olur ve gerçek ölçümle (-376.5)
    eşleşmez — doğru yazan model yalancı çıkar.
    """
    led = Ledger()
    led.add("BTC/USDT.indicators.macd_hist", -376.513095, "binance", "indicators")
    claims = extract_claims("MACD histogramı –376.5 seviyesinde.")
    assert claims[0].value == pytest.approx(-376.5)
    assert _verdicts("MACD histogramı –376.5 seviyesinde.", led) == \
        [str(Verdict.SUPPORTED)]


def test_contradiction_uses_the_closest_measurement() -> None:
    """
    Suçlamada EN ALAKALI ölçüme bakılır, ilk rastlanana değil.

    Yaşanmış hata: "MACD histogramı –376.5" cümlesi "momentum" kavramı
    üzerinden hem RSI'ye hem MACD'ye bağlanıyordu; defterde önce RSI
    geçtiği için iddia onunla karşılaştırılıp haksız yere çelişkili ilan
    edildi.
    """
    led = Ledger()
    led.add("BTC/USDT.indicators.rsi_14", 55.72, "binance", "indicators")
    led.add("BTC/USDT.indicators.macd_hist", -376.51, "binance", "indicators")

    result = audit("MACD histogramı –300.0, momentum zayıf.", led)
    claim = result.claims[0]
    assert claim.evidence is not None
    assert "macd" in claim.evidence.key, \
        f"yanlış ölçümle karşılaştırıldı: {claim.evidence.key}"


def test_turkish_suffixed_threshold_is_not_a_claim() -> None:
    """Türkçede sayıya kesme işaretiyle ek gelir: "30'un altında"."""
    assert extract_claims("30'un altında işlem sayısı geçerli değil.") == []


def test_indicator_period_is_recognised_in_every_writing_style() -> None:
    """
    Modeller aynı şeyi beş türlü yazar; kural yazıma bağlı olamaz.

    Canlı koşuda on sahte çelişkinin tamamı buydu. Ayrım, GÖSTERGEYE ÖZGÜ
    standart periyotlardan gelir: RSI'nin periyodu 14'tür, 28 değil.
    """
    assert [c.raw for c in extract_claims("| RSI 14 | 59.21 |")] == ["59.21"]
    assert [c.raw for c in extract_claims("EMA-20 = 2453,20")] == ["2453,20"]
    assert extract_claims("golden_cross True (EMA-20 > EMA-50).") == []
    # 12, ADX'in standart periyodu DEĞİLDİR → değerdir, iddiadır.
    assert [c.raw for c in extract_claims("ADX 12 ile trend zayıf.")] == ["12"]


def test_prices_in_the_year_range_are_not_dropped() -> None:
    """
    2000–2099 arası fiyatlar sessizce doğrulama dışı kalıyordu.

    Yıl filtresi sayının ÇEVRESİNE bakıyordu; "2074.94" fiyatındaki "2074"
    kısmını yıl sanıp iddiayı tamamen atıyordu. ETH, BNB, SOL gibi
    varlıklar tam bu aralıkta işlem görür — yani en çok işlem gören
    fiyatlar hiç denetlenmiyordu.

    Sessiz kör nokta, yanlış yargıdan daha tehlikelidir: kimse eksik olanı
    fark etmez.
    """
    assert [c.raw for c in extract_claims("ETH fiyatı 2074.94 dolar.")] == ["2074.94"]
    assert [c.raw for c in extract_claims("Fiyat 2471,97 USDT.")] == ["2471,97"]
    # Gerçek yıl hâlâ elenir: ondalığı olmayan dört haneli tam sayı.
    assert extract_claims("2024 yılında başlamış.") == []


def test_every_dash_variant_is_read_as_a_sign() -> None:
    """
    Modeller altı farklı tire karakteri kullanır.

    Canlı koşuda "**‑10,66**" (bölünmez tire) değeri +10,66 okundu ve
    ölçümle (-10,66) eşleşmedi: doğru yazan model yalancı çıktı.
    """
    led = Ledger()
    led.add("ETH/USDT.indicators.macd_hist", -10.66, "binance", "indicators")
    for dash in ("-", "‐", "‑", "‒", "–", "−"):
        text = f"MACD histogramı {dash}10,66 seviyesinde."
        assert _verdicts(text, led) == [str(Verdict.SUPPORTED)], \
            f"tire {dash!r} eksi olarak okunmadı"


def test_opposing_strategy_count_is_derived() -> None:
    """
    "8 stratejiden 3'ü AL diyor" cümlesinin doğal devamı "5'i demiyor".

    Uzmanlar bu çıkarımı hep yapar; sistem yapmazsa kendi yapmadığı hesabı
    çelişki ilan eder.
    """
    led = Ledger()
    led.add("ETH/USDT.fiyat", 2471.97, "binance", "ticker")
    led.add("ETH/USDT.algo.agree", 3, "algo_motor", "engine")
    led.add("ETH/USDT.algo.total", 8, "algo_motor", "engine")
    derive.enrich(led, {"symbols": ["ETH/USDT"]})

    assert led.get("ETH/USDT.algo.karsit").numeric == 5
    verdicts = _verdicts("3/8 strateji AL, 5 strateji karşıt veya nötr.", led)
    assert verdicts == [str(Verdict.SUPPORTED)] * 3


def test_thinking_lines_are_dropped_but_content_survives() -> None:
    """
    Süreç anlatımı atılır, bulgu kalır.

    Süzgecin emniyet kuralı da korunmalı: her satır atılacaksa metin
    OLDUĞU GİBİ bırakılır — boş bir bölüm, gürültülü bölümden kötüdür.
    """
    from app.research.pipeline import strip_thinking

    text = ("- Answer 5 specific risk management questions (from the prompt)\n"
            "I'll use the stop distance if position was 1 ETH maybe?\n"
            "- Stop mesafesi %3.47\n"
            "The user wants 4 things.")
    cleaned = strip_thinking(text)
    assert "Stop mesafesi" in cleaned
    assert "Answer 5 specific" not in cleaned
    assert "The user wants" not in cleaned

    only_thinking = "Wait, the prompt says: do this."
    assert strip_thinking(only_thinking) == only_thinking
