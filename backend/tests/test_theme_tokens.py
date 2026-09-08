"""
TEMA JETONLARI — iki tema, tek sözleşme

Renk sistemi CSS'te yaşıyor ama bir SÖZLEŞMESİ var ve o sözleşme bozulunca
kimse fark etmiyor: sayfa açılıyor, hata çıkmıyor, sadece bir yazı okunmaz
hale geliyor.

Bu dosya iki gerçek hatayı yakaladıktan sonra yazıldı:

1. Toplu bir arama-değiştirme, `--on-emerald: #06170d;` tanımını da
   `--on-emerald: var(--on-emerald);` yaptı. CSS kendine referans veren bir
   değişkeni geçersiz sayar ve DEĞER BOŞ KALIR. Koyu temada birincil
   düğmelerin yazısı açık griye düştü: parlak yeşil üzerinde 1.41 kontrast,
   yani okunamaz. Tarayıcı hata vermez.

2. Açık tema paletine ilk seçtiğim renkler "yeterince koyu görünüyordu" ama
   ölçülünce üçü de AA'nın altındaydı. Göz kararı, kontrast ölçümünün
   yerini tutmuyor.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS_DIR = Path(__file__).resolve().parents[1] / "app" / "static"
STYLES = CSS_DIR / "styles.css"


def _block(name: str) -> str:
    """Bir `:root` bloğunun gövdesini döndürür."""
    text = STYLES.read_text(encoding="utf-8")
    pattern = (r"(?s)^:root \{(.*?)^\}" if name == "dark"
               else r'(?s)^:root\[data-theme="light"\] \{(.*?)^\}')
    match = re.search(pattern, text, re.M)
    assert match, f"{name} paleti bulunamadı"
    return match.group(1)


def _tokens(body: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"(--[a-z0-9-]+):\s*([^;]+);", body)}


DARK = _tokens(_block("dark"))
LIGHT = _tokens(_block("light"))


# --------------------------------------------------------------------------- #
#  Sözleşme
# --------------------------------------------------------------------------- #

def test_no_token_refers_to_itself() -> None:
    """
    `--x: var(--x)` CSS'te geçersizdir ve değişkeni BOŞ bırakır.

    Bu tam olarak bir kez oldu: toplu değiştirme tanım satırını da ezdi ve
    koyu temada birincil düğme yazısı 1.41 kontrasta düştü. Tarayıcı hata
    vermediği için ancak ölçerken fark edildi.
    """
    for path in CSS_DIR.glob("*.css"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"(--[a-z0-9-]+):\s*var\(\s*(--[a-z0-9-]+)", text):
            assert match.group(1) != match.group(2), (
                f"{path.name}: {match.group(1)} kendine referans veriyor — "
                f"değeri boş kalır")


def test_every_light_token_exists_in_dark() -> None:
    """
    Açık temada tanımlı her jeton koyu temada da olmalı.

    Koyu tema temeldir; açık tema onun üzerine yazar. Yalnızca açık temada
    tanımlı bir jeton, koyu temada BOŞ demektir.
    """
    missing = sorted(set(LIGHT) - set(DARK))
    assert not missing, f"koyu temada tanımsız: {missing}"


def test_no_token_is_empty() -> None:
    for name, palette in (("koyu", DARK), ("açık", LIGHT)):
        for key, value in palette.items():
            assert value, f"{name} temada {key} boş"


# --------------------------------------------------------------------------- #
#  Okunabilirlik
# --------------------------------------------------------------------------- #

def _rgb(value: str) -> tuple[float, float, float]:
    value = value.split("/*")[0].strip()
    if value.startswith("#"):
        h = value.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    nums = re.findall(r"[\d.]+", value)
    return tuple(float(n) for n in nums[:3])


def _luminance(rgb: tuple[float, float, float]) -> float:
    def channel(x: float) -> float:
        x /= 255.0
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(_rgb(a)), _luminance(_rgb(b))), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize("palette_name", ["koyu", "açık"])
def test_body_text_is_readable(palette_name: str) -> None:
    palette = DARK if palette_name == "koyu" else LIGHT
    ratio = contrast(palette["--text"], palette["--bg"])
    assert ratio >= 7.0, f"{palette_name}: gövde yazısı {ratio:.2f} (AAA için 7.0)"


@pytest.mark.parametrize("palette_name", ["koyu", "açık"])
def test_dim_text_is_readable(palette_name: str) -> None:
    palette = DARK if palette_name == "koyu" else LIGHT
    ratio = contrast(palette["--text-dim"], palette["--bg"])
    assert ratio >= 4.5, f"{palette_name}: sönük yazı {ratio:.2f} (AA için 4.5)"


@pytest.mark.parametrize("palette_name", ["koyu", "açık"])
def test_the_faintest_text_is_still_readable(palette_name: str) -> None:
    """
    En sönük yazı da OKUNMALI — hem sayfada hem panelin üzerinde.

    Panel zemini (`--bg-raise`) daha zorlu yüzeydir: koyu temada sayfadan
    açık, açık temada sayfadan beyazdır, yani kontrast orada daralır.
    Yalnızca `--bg`'ye bakan bir test bu düşüşü kaçırır — nitekim kaçırdı:
    koyu temada #6b6b6b panel üzerinde 3.27'ye iniyordu.

    "Sembol adı" ya da "zaman damgası" gibi ikincil görünen yazılar da
    okunmak için oradadır; okunmayacaksa hiç yazılmamalı.
    """
    palette = DARK if palette_name == "koyu" else LIGHT
    # Üç yüzey de sınanır. Kenar çubuğu (`--bg-rail`) en zorlusudur ve
    # yalnızca `--bg`'ye bakan bir test onu kaçırır: açık temada sönük yazı
    # orada 4.44'te kalıyordu, yani eşiğin bir tık altında.
    for surface in ("--bg", "--bg-raise", "--bg-rail"):
        ratio = contrast(palette["--text-faint"], palette[surface])
        assert ratio >= 4.5, (
            f"{palette_name}: en sönük yazı {surface} üzerinde {ratio:.2f} "
            f"(AA için 4.5)")


@pytest.mark.parametrize("palette_name", ["koyu", "açık"])
def test_no_stylesheet_hardcodes_a_text_colour(palette_name: str) -> None:
    """
    Yazı rengi SABİT yazılamaz.

    Kahraman başlığı `color: #fff` idi. Koyu temada doğru, açık temada
    1.16 kontrast — yani görünmez. Sabit bir renk, tanımı gereği tek bir
    temada doğrudur.

    Beyaz/siyah sabitleri yalnızca jeton TANIMLARINDA serbesttir; kural
    gövdesinde değil.
    """
    del palette_name  # kural her iki tema için de aynı
    banned = re.compile(r"color:\s*(#fff\b|#ffffff\b|#000\b|#000000\b|white\b|black\b)",
                        re.I)
    offenders: list[str] = []
    for path in CSS_DIR.glob("*.css"):
        inside_root = False
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(":root"):
                inside_root = True
            elif stripped == "}" and inside_root:
                inside_root = False
            if inside_root or stripped.startswith("--"):
                continue          # jeton tanımı: sabit renk burada normaldir
            if banned.search(line):
                offenders.append(f"{path.name}:{line_no} → {stripped[:70]}")
    assert not offenders, "sabit yazı rengi:\n  " + "\n  ".join(offenders)


@pytest.mark.parametrize("palette_name", ["koyu", "açık"])
def test_text_on_the_accent_is_readable(palette_name: str) -> None:
    """
    Yeşil zemin üzerindeki yazı — birincil düğmenin ta kendisi.

    Bu, `--on-emerald` boş kaldığında düşen testtir.
    """
    palette = DARK if palette_name == "koyu" else LIGHT
    ratio = contrast(palette["--on-emerald"], palette["--emerald"])
    assert ratio >= 4.5, (
        f"{palette_name}: yeşil üzerine yazı {ratio:.2f} — birincil düğme okunmaz")


@pytest.mark.parametrize("palette_name", ["koyu", "açık"])
def test_status_text_is_readable_on_its_own_wash(palette_name: str) -> None:
    """
    Durum yazısı, kendi %10-12'lik yıkaması üzerinde okunmalı.

    Vurgu renginin KENDİSİ bu iş için yetmez: `.fin-chip.up` yeşil yazıyı
    yeşil yıkama üzerine koyar ve aynı ton kullanılırsa kaybolur. Ayrı
    `--*-text` jetonları bu yüzden var.
    """
    palette = DARK if palette_name == "koyu" else LIGHT
    base = palette["--bg-raise"]
    for token in ("--emerald-text", "--danger-text", "--warn-text", "--info-text"):
        ratio = contrast(palette[token], base)
        assert ratio >= 4.5, f"{palette_name}: {token} {ratio:.2f} (AA için 4.5)"


def test_the_light_theme_is_actually_light() -> None:
    """Açık temanın zemini gerçekten açık, koyununki gerçekten koyu olmalı."""
    assert _luminance(_rgb(LIGHT["--bg"])) > 0.7, "açık tema zemini açık değil"
    assert _luminance(_rgb(DARK["--bg"])) < 0.1, "koyu tema zemini koyu değil"


def test_command_home_uses_theme_tokens_not_a_fixed_black_gradient() -> None:
    """Komuta kahraman alanı açık temada siyah kalmamalı."""
    command = (CSS_DIR / "command.css").read_text(encoding="utf-8")
    assert "var(--cmd-bg-start)" in command
    assert "var(--cmd-bg-mid)" in command
    assert "var(--cmd-bg-end)" in command
    for token in ("--cmd-bg-start", "--cmd-bg-mid", "--cmd-bg-end"):
        assert token in DARK and token in LIGHT
    assert _luminance(_rgb(LIGHT["--cmd-bg-start"])) > 0.9
    assert _luminance(_rgb(LIGHT["--cmd-bg-end"])) > 0.75


def test_every_stylesheet_is_balanced() -> None:
    """
    Süslü parantezler dengeli olmalı.

    Bir fazla `}` sayfayı çökertmez — sadece o noktadan sonraki kuralları
    sessizce yutar. Tarayıcı konsola bile yazmaz. Betikle toplu düzenleme
    yaparken bu iki kez oldu ve ancak sayarak fark edildi.
    """
    for path in sorted(CSS_DIR.glob("*.css")):
        text = path.read_text(encoding="utf-8")
        delta = text.count("{") - text.count("}")
        assert delta == 0, (
            f"{path.name}: parantez dengesi {delta:+d} — "
            f"{'eksik kapanış' if delta > 0 else 'fazla kapanış'}")


def test_no_stylesheet_declares_an_unknown_token() -> None:
    """
    `var(--x)` ile çağrılan her jeton bir yerde TANIMLI olmalı.

    Tanımsız bir jeton sessizce boş değer verir: yazı rengi kaybolur, zemin
    saydam kalır ve hiçbir hata çıkmaz. `--on-emerald` boş kaldığında tam
    olarak bu yaşandı.
    """
    declared: set[str] = set()
    used: dict[str, str] = {}
    for path in CSS_DIR.glob("*.css"):
        text = path.read_text(encoding="utf-8")
        declared |= set(re.findall(r"(--[a-z0-9-]+)\s*:", text))
        for name in re.findall(r"var\(\s*(--[a-z0-9-]+)", text):
            used.setdefault(name, path.name)

    # Yedeği olanlar sorun değil: var(--x, #fff) tanımsızken de çalışır.
    unknown = sorted(
        f"{name} ({where})" for name, where in used.items()
        if name not in declared
    )
    assert not unknown, f"tanımsız jetonlar: {unknown}"


def test_no_background_hardcodes_a_theme_colour() -> None:
    """
    Zemin rengi SABİT yazılamaz — jetondan gelmeli.

    Yapışkan üst çubuk `rgba(13,13,13,.92)` ile boyanıyordu: koyu temada
    doğru, açık temada KOYU BİR ŞERİT olarak kalıyordu ve üzerindeki koyu
    yazı 2.41 kontrastla okunmuyordu.

    İzin verilenler: jeton tanımları (`:root` içi), `transparent`,
    `currentColor` ve `--tint-rgb`/`--shade-rgb` üzerinden yazılmış
    katman/gölge renkleri — bunlar zaten temayla dönüyor.
    """
    # Hem `rgba(13,13,13,…)` hem `#0a0a0a` biçimi yakalanır. İlk sürüm
    # yalnızca rgba'ya bakıyordu ve terminal kutusunun `#0a0a0a` zeminini
    # kaçırdı: açık temada siyah bir kutu olarak kaldı.
    literal_rgba = re.compile(
        r"background(?:-color)?:\s*[^;]*(?:rgba?\(\s*\d|#[0-9a-fA-F]{3,8}\b)")
    offenders: list[str] = []
    for path in CSS_DIR.glob("*.css"):
        inside_root = False
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(":root"):
                inside_root = True
            elif stripped == "}" and inside_root:
                inside_root = False
            if inside_root or stripped.startswith("--"):
                continue
            if literal_rgba.search(stripped):
                offenders.append(f"{path.name}:{line_no} → {stripped[:70]}")
    assert not offenders, "sabit zemin rengi:\n  " + "\n  ".join(offenders)


def test_overlay_and_shadow_are_tokenised() -> None:
    """
    Katman ve gölge sabit renk OLAMAZ.

    Sabit kalırlarsa tema çevrildiğinde beyaz bir yıkama açık zeminde
    görünmez olur ya da koyu bir gölge kâğıt gibi duran bir arayüzü kirletir.
    Tek tek 49 yer vardı; biri unutulsa kimse fark etmezdi.
    """
    for token in ("--tint-rgb", "--shade-rgb"):
        assert token in DARK, f"{token} koyu temada yok"
        assert token in LIGHT, f"{token} açık temada yok"

    leftovers: list[str] = []
    for path in CSS_DIR.glob("*.css"):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Jeton TANIMI sabit renk içerebilir — zaten temayla değişen şey
            # odur. Kural, jetonu KULLANAN kurallar için geçerli.
            if line.strip().startswith("--"):
                continue
            if re.search(r"rgba\(\s*255\s*,\s*255\s*,\s*255\s*,", line) or \
               re.search(r"rgba\(\s*0\s*,\s*0\s*,\s*0\s*,\s*[.\d]", line):
                leftovers.append(f"{path.name}:{line_no}")
    assert not leftovers, f"sabit kalmış katman/gölge renkleri: {leftovers}"
