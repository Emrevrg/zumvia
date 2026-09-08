"""
ÇALIŞMA MODLARI — SOR / PLANLA / UYGULA
========================================

Eskiden dört seçenek vardı (otomatik, tek model, konsey, sağlamcı) ve hepsi
aynı şeyi anlatıyordu: KARAR KALİTESİ. Hiçbiri kullanıcının asıl merak
ettiğini söylemiyordu — "bu ajan şimdi bir şey YAPACAK mı, yoksa sadece
konuşacak mı?"

Üç mod bu soruyu cevaplar:

    SOR      Ajan okur, ölçer, anlatır. Hiçbir şeyi DEĞİŞTİRMEZ.
             Bot kurmaz, başlatmaz, pozisyon açmaz, ayar değiştirmez.

    PLANLA   Ajan okur, ölçer ve bir PLAN yazar: ne yapacağını, hangi
             sırayla, hangi riskle. Uygulamaz. Kullanıcı planı görüp
             "uygula" derse Uygula moduna geçer.

    UYGULA   Ajan yapar. Ve yaptığı için 7/24 kendi kendine denetler —
             ayrı bir "otonom" düğmesi yoktur, çünkü kurduğu bir sistemi
             izlemeyen bir ajan işini yarım bırakmış demektir.

Sınır KODDA ZORLANIR, prompt'ta rica edilmez.

Bu ayrım önemli: bir modele "bunu yapma" demek bir dilektir. Model unutur,
yanlış anlar ya da kullanıcının cümlesini izin sanır. Sor modunda durumu
değiştiren araçlar ÇAĞRILAMAZ — model istese de.

Varsayılan SOR'dur. Bir aracın en güvenli hâli varsayılan olmalıdır:
kullanıcı yaptırmak istediğinde bunu açıkça söyler.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.logging import get_logger

log = get_logger("zumvia.work_mode")

ASK = "ask"
PLAN = "plan"
AGENT = "agent"
DEFAULT = ASK


@dataclass(frozen=True, slots=True)
class WorkMode:
    id: str
    label: str
    icon: str
    hint: str
    can_mutate: bool
    autonomous: bool

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "icon": self.icon,
                "hint": self.hint, "can_mutate": self.can_mutate,
                "autonomous": self.autonomous}


MODES: dict[str, WorkMode] = {
    ASK: WorkMode(
        id=ASK, label="Sor", icon="info",
        hint="Okur, ölçer, anlatır. Hiçbir şeyi değiştirmez.",
        can_mutate=False, autonomous=False,
    ),
    PLAN: WorkMode(
        id=PLAN, label="Planla", icon="report",
        hint="Ne yapacağını adım adım yazar; onayın olmadan uygulamaz.",
        can_mutate=False, autonomous=False,
    ),
    AGENT: WorkMode(
        id=AGENT, label="Uygula", icon="bots",
        hint="Kurar, çalıştırır ve 7/24 kendi kendine denetler.",
        can_mutate=True, autonomous=True,
    ),
}


def resolve(value: str | None) -> WorkMode:
    """Bilinmeyen değer güvenli tarafa düşer — asla Uygula'ya değil."""
    return MODES.get((value or "").strip().lower(), MODES[DEFAULT])


def catalog() -> list[dict[str, Any]]:
    return [m.to_dict() for m in MODES.values()]


# --------------------------------------------------------------------------- #
#  Araç kapısı
# --------------------------------------------------------------------------- #
#
# Bazı araçlar `mutating` işaretli olmasa da kullanıcıyı riske sokar ya da
# dışarıya bir şey gönderir. Bunlar Sor/Planla modunda da kapalıdır.

_ALWAYS_BLOCKED_WITHOUT_AGENT = frozenset({
    "enable_live_trading",       # gerçek paraya geçiş
    "control_bot",               # bot başlat/durdur
    "run_bot_cycle",             # tur çalıştır → pozisyon açabilir
    "run_skill",                 # beceri bir zincir çalıştırır
    "run_automation",
})

# Acil fren HER MODDA çalışır. Riski AZALTAN bir araç, "yetkin yok" diye
# reddedilemez — reddedildiği an kullanıcı korumasız kalır.
_ALWAYS_ALLOWED = frozenset({
    "activate_kill_switch",
    "emergency_flatten",
    "get_safety_status",
})


def allowed(mode: str, tool_name: str, mutating: bool) -> tuple[bool, str]:
    """
    Bu araç bu modda çalışabilir mi?

    İkinci dönen değer reddin GEREKÇESİDİR ve modele verilir: ne yapacağını
    bilmeyen bir model aynı aracı tekrar dener.
    """
    entry = resolve(mode)
    if tool_name in _ALWAYS_ALLOWED:
        return True, ""
    if entry.can_mutate:
        return True, ""
    if not mutating and tool_name not in _ALWAYS_BLOCKED_WITHOUT_AGENT:
        return True, ""

    if entry.id == PLAN:
        return False, (
            f"`{tool_name}` PLANLA modunda çalıştırılamaz. Bu modda ne "
            f"yapacağını YAZ, yapma. Ölçüm araçlarını serbestçe kullan; "
            f"sonunda adım adım bir plan ver ve kullanıcıdan 'Uygula' moduna "
            f"geçmesini iste.")
    return False, (
        f"`{tool_name}` SOR modunda çalıştırılamaz. Bu modda sistemde hiçbir "
        f"şey değişmez. Kullanıcının sorusunu ölçüm araçlarıyla cevapla; "
        f"bir şey KURULMASI ya da ÇALIŞTIRILMASI gerekiyorsa bunu söyle ve "
        f"'Uygula' moduna geçmesini iste — kendin geçemezsin.")


def visible_tools(mode: str, registry: dict[str, Any]) -> list[str]:
    """
    Modele SUNULACAK araç listesi.

    Kullanamayacağı araçları modele hiç göstermemek, onu boşuna denemekten
    kurtarır ve bağlam penceresini de boşa harcamaz.
    """
    names: list[str] = []
    for name, tool in registry.items():
        ok, _ = allowed(mode, name, getattr(tool, "mutating", False))
        if ok:
            names.append(name)
    return sorted(names)


# --------------------------------------------------------------------------- #
#  Prompt eki
# --------------------------------------------------------------------------- #

_ASK_PROMPT = """## ÇALIŞMA MODU: SOR

Kullanıcı senden BİLGİ istiyor, eylem değil.

* Ölç, oku, anlat. Sistemde hiçbir şey değiştirme.
* Durumu değiştiren araçlar bu modda KAPALIDIR — çağırırsan reddedilirsin.
* Bir şeyin kurulması gerekiyorsa "bunu Uygula modunda yapabilirim" de.
  Kullanıcı adına modu sen değiştiremezsin.
* Sayı verirken ölç. Hafızadan fiyat söyleme."""

_PLAN_PROMPT = """## ÇALIŞMA MODU: PLANLA

Kullanıcı ne yapacağını GÖRMEK istiyor, yapılmış olmasını değil.

Çıktın şu yapıda olsun:

1. **Durum** — ölçtüğün sayılarla, bugün ne var.
2. **Öneri** — ne kurulacak/değişecek, hangi enstrümanda, hangi sistemle.
3. **Risk** — işlem başına risk, en kötü senaryo, hangi koşulda durur.
4. **Adımlar** — sırayla, her adımda hangi aracı çağıracağın.
5. **Vazgeçme koşulu** — bu plan hangi durumda YANLIŞ olur.

Kurallar:
* Ölçüm araçlarını serbestçe kullan; durumu değiştirenler KAPALI.
* Getiri vaat etme. "Şu kadar kazandırır" cümlesi kurma.
* Beşinci maddeyi atlama. Vazgeçme koşulu olmayan plan, plan değil temennidir."""

_AGENT_PROMPT = """## ÇALIŞMA MODU: UYGULA

Yapma yetkin var. Bu yüzden sorumluluğun da var.

### ÖNCE ŞUNU OKU: OYALANMA

Kullanıcı senden bir SONUÇ istedi, bir süreç anlatımı değil. Gerçek bir
konuşmada şu oldu ve bir daha olmayacak: kullanıcı "100 dolarımı yönet"
dedi; ajan yedi araç çağırıp ölçtü, sonra "kanıt topluyorum" dedi, sonra
tekrar ölçtü. Kullanıcı BEŞ KEZ "artık söyle" demek zorunda kaldı.

Kurallar:

1. **Bu turda bitir.** İstenen iş bir turda yapılabiliyorsa yap. "Şimdi
   şunu yapacağım" deyip durma — yap, sonra ne yaptığını söyle.
2. **Ölçüm amaç değil, araçtır.** Yalnızca VEREBİLECEĞİN KARAR için gereken
   ölçümü al. Kullanacağın bir sayı değilse çağırma.
3. **Aynı soruyu iki kez sorma.** Kullanıcı "sen karar ver", "tamam kullan",
   "sağlam ilerle" dediyse KARAR SENİN ve onay alınmıştır. Tekrar sorma,
   uygula.
4. **Kullanıcının cevapladığını unutma.** Konuşma geçmişini oku; verilmiş
   bir bilgiyi (sermaye, piyasa, risk tercihi) yeniden isteme.
5. **Sayı ver.** "Portföyünüzü çeşitlendirelim" değil: "100 doların 37'si
   BTC/USDT'ye, kalan 63'ü nakit; işlem başına risk 1 dolar."

### YAPTIĞINI SÖYLE

Durumu değiştiren bir araç çalıştıysa (bot kurma, başlatma, pozisyon,
ayar değişikliği) turu SONUCU SÖYLEMEDEN bitirme. Kullanıcı, işin olup
olmadığını ekrandan anlayamaz — senin söylemen gerekir.

Yanlış: "Araçları çalıştırdım."
Doğru: "Çift Momentum sistemini BTC/USDT üzerinde 100 USDT ile kurdum,
sanal modda çalışıyor. İşlem başına risk 1 dolar, stop zorunlu."

### SINIRLAR

* Kurduğun her sistemi İZLE. Kurup bırakmak işi yarım bırakmaktır.
* Her kurulan bot SANAL modda başlar. Gerçek paraya geçişi YALNIZCA kullanıcı
  panelden yapar — isteyemez, veremez, yükseltemezsin.
* Geri alınamayan bir şey yapmadan önce ne yapacağını tek cümleyle söyle.
* Bir şey ters giderse SAKLAMA. "Şunu denedim, şu yüzden olmadı" de.
* Zorlama işlem en pahalı işlemdir: net kurulum yoksa bekle ve bunu söyle."""

_PROMPTS = {ASK: _ASK_PROMPT, PLAN: _PLAN_PROMPT, AGENT: _AGENT_PROMPT}


def prompt_block(mode: str) -> str:
    return _PROMPTS.get(resolve(mode).id, _ASK_PROMPT)
