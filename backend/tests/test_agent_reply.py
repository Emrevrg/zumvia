"""
AJANIN SÖYLEDİĞİ SÖZ — tur asla sessiz bitmez

İki gerçek hata bu dosyayı doğurdu:

1. Araç çağrılarını sohbet geçmişine `[Araç çağrıları: x, y]` diye
   iliştiriyoruz. Model bu kalıbı ÖRNEK sandı ve taklit etti: kullanıcıya
   tek satırlık `[Araç çağrıları: recommend_playbook]` döndü. Ekranda
   anlamsız bir köşeli parantez — gerçek bir modelle, gerçek bir soruda
   yaşandı.

2. Kapanış cümlesi yalnızca ADIM SINIRINA ulaşıldığında devreye giriyordu.
   Model araç çağırıp hiç konuşmadan durursa ekranda hiçbir cevap kalmıyordu:
   kullanıcı bir şey sordu, sistem çalıştı, cevap görünmedi.

Kullanıcıya dönen her tur, ya gerçek bir cümle ya da neden cümle olmadığını
açıklayan dürüst bir not içermelidir.
"""
from __future__ import annotations

import pytest

from app.agent.controller import _spoken_text

# --------------------------------------------------------------------------- #
#  İşaret taklidi
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("taklit", [
    "[Araç çağrıları: recommend_playbook]",
    "  [Araç çağrıları: get_portfolio, list_playbooks]  ",
    "[araç çağrıları: x]",
    "[Araç çağrıları:]",
])
def test_a_bare_tool_marker_is_not_speech(taklit: str) -> None:
    """
    Yalnızca iç işaretten ibaret çıktı KONUŞMA DEĞİLDİR.

    Boş sayılır ki tur sonundaki dürüst kapanış cümlesi devreye girsin.
    """
    assert _spoken_text(taklit) == ""


def test_real_text_survives() -> None:
    """Gerçek cümle olduğu gibi korunur."""
    assert _spoken_text("BTC/USDT şu an 79.853 dolar.") == "BTC/USDT şu an 79.853 dolar."


def test_a_marker_glued_to_the_end_is_trimmed_not_the_sentence() -> None:
    """
    Cümlenin sonuna yapışmış işaret atılır, cümle korunur.

    Modelin söylediği şeyi işaret yüzünden çöpe atmak, hatayı düzeltmek
    değil yerini değiştirmektir.
    """
    metin = "Portföyü okudum, üç bot var.\n[Araç çağrıları: get_portfolio]"
    assert _spoken_text(metin) == "Portföyü okudum, üç bot var."


def test_empty_stays_empty() -> None:
    for bos in ("", "   ", "\n\n", None):
        assert _spoken_text(bos) == ""


def test_a_marker_shaped_sentence_is_still_speech() -> None:
    """
    Köşeli parantezle başlayan her şey işaret değildir.

    Fazla hevesli bir filtre, modelin gerçek cümlesini yutar.
    """
    assert _spoken_text("[Not] Piyasa bugün sakin.") != ""
    assert _spoken_text("[Araç çağrıları: x] ama devamında şunu buldum.") != ""


# --------------------------------------------------------------------------- #
#  Sessiz tur olmaz
# --------------------------------------------------------------------------- #

def test_the_loop_always_records_a_closing_sentence() -> None:
    """
    `final_text` boşsa kapanış cümlesi HER DURUMDA yazılır.

    Eskiden koşul `steps >= max_steps and not final_text` idi; model erken
    ve sessiz bittiğinde ekranda hiçbir cevap kalmıyordu.
    """
    import inspect

    from app.agent import controller

    src = inspect.getsource(controller.run_api_agent)
    assert "if not final_text:" in src, (
        "kapanış cümlesi hâlâ yalnızca adım sınırına bağlı — "
        "sessiz tur mümkün")
    # İki ayrı gerekçe: sınıra takıldı / kesin sonuç üretemedi
    assert "adım sınırına" in src
    assert "kesin bir sonuç" in src


# --------------------------------------------------------------------------- #
#  Adım sınırı ve oyalanma
# --------------------------------------------------------------------------- #

def test_the_step_limit_fits_a_whole_deployment() -> None:
    """
    Bir portföy kurulumu tek turda BİTEBİLMELİ.

    ÖLÇÜLDÜ: sınır 16'ydı. Gerçek bir konuşmada ajan yedi aracı ölçüme
    harcadı, kanıt toplamaya birkaç tane daha, ve iş bitmeden sınıra çarptı.
    Kullanıcı beş kez "artık söyle" demek zorunda kaldı.

    Zincir: portföy → güvenlik → tarama → anlık görüntü → strateji motoru →
    öneri → geri test → doğrulama → kurulum → denetim = on adım. Üstüne
    kullanıcıya cevap yazmak için de yer kalmalı.
    """
    from app.agent.controller import MAX_STEPS

    assert MAX_STEPS >= 30, (
        f"adım sınırı {MAX_STEPS} — tam bir kurulum zinciri sığmaz")


def test_the_agent_prompt_forbids_stalling() -> None:
    """
    UYGULA modu prompt'u "bu turda bitir" demeli.

    Prompt yalnızca süreç anlatırsa model süreci uygular: ölçer, ölçer ve
    kullanıcı tekrar sormak zorunda kalır.
    """
    from app.agent.work_mode import AGENT, prompt_block

    metin = prompt_block(AGENT)
    assert "OYALANMA" in metin
    assert "Bu turda bitir" in metin
    assert "Aynı soruyu iki kez sorma" in metin
    assert "YAPTIĞINI SÖYLE" in metin


def test_english_flavoured_tool_markers_are_also_filtered() -> None:
    """
    Model iç işaretin KENDİ karışımını uydurabilir.

    Gerçekte görüldü: kalıbı `[Araç çağrıları: …]` diye yazıyoruz ama model
    `[Araç calls: run_backtest, …]` üretti ve bu kullanıcının ekranına düştü.
    Tek yazıma göre filtrelemek yetmiyor.
    """
    for taklit in (
        "[Araç calls: run_backtest, validate_strategy]",
        "[Tool calls: get_portfolio]",
        "[arac cagrilari: x]",
        "[ARAÇ ÇAĞRILARI: deploy_playbook]",
    ):
        assert _spoken_text(taklit) == "", f"filtre kaçırdı: {taklit}"


def test_a_successful_change_is_always_reported() -> None:
    """
    Durumu değiştiren bir araç çalıştıysa tur SESSİZ bitemez.

    ÖLÇÜLDÜ: `deploy_playbook` başarıyla çalıştı, bot kuruldu — ama tur
    özetsiz bitti ve kullanıcı "bir özet üretemedim" cümlesini gördü. İş
    olmuştu, kullanıcı olmadı sandı. Bu, hiç çalışmamaktan daha kötüdür.
    """
    import inspect

    from app.agent import controller

    src = inspect.getsource(controller.run_api_agent)
    assert "changed_by" in src, "değiştiren araçlar izlenmiyor"
    assert "Şunları yaptım" in src, "yapılanlar kullanıcıya bildirilmiyor"


def test_action_intent_and_safe_no_action_are_distinct() -> None:
    """Erken kapanış yakalanırken güvenli 'işlem yok' kararı zorlanmamalı."""
    from app.agent.controller import _expects_change, _is_terminal_outcome

    assert _expects_change("Portföyü kur, çalıştır ve tamamla")
    assert _expects_change("Manage and deploy this portfolio")
    assert not _expects_change("Portföyün mevcut durumu nedir?")
    assert _is_terminal_outcome("Net kurulum yok; işlem açmıyorum ve bekliyorum.")
    assert _is_terminal_outcome("API anahtarı yok, bu yüzden yapılamıyor.")
    assert not _is_terminal_outcome("Bakıyorum, birazdan tamamlayacağım.")
