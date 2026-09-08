"""
ARAYÜZ İŞARETLEME SÖZLEŞMELERİ

CSS sınıflarının yazılmamış kuralları vardır ve unutulduklarında hata
vermezler — sadece ekran bozulur.

Bu dosya, gerçekten yaşanmış bir hatadan sonra yazıldı: finans ekranındaki
enstrüman penceresi `class="modal fin-modal"` ile açılıyordu. `.modal` bu
projede YALNIZCA boyut verir; yüzeyi (zemin, kenar, dolgu) `.panel` taşır.
`panel` unutulduğu için pencere saydam açıldı ve kullanıcı bulanık perdenin
üzerinde okunmayan bir metin gördü.

Tarayıcı böyle bir şeye hata vermez. Bu yüzden burada kontrol ediliyor.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _js_files() -> list[Path]:
    return sorted(STATIC.glob("*.js"))


NODE = shutil.which("node")

# UTF-8 metnin cp1252/cp1254 gibi tek baytlık bir kod sayfasıyla okunup
# yeniden yazılmasından doğan izler. "canlı" → "canlÄ±" gibi.
MOJIBAKE = ("Ä±", "Ã¼", "Ã¶", "ÅŸ", "Ã§", "ÄŸ", "â€”", "Ä°", "Åž", "Ã–", "Ãœ")


def test_no_source_file_is_double_encoded() -> None:
    """
    Kaynak dosyalar UTF-8 kalmalı.

    ÖLÇÜLDÜ: PowerShell'de `Get-Content -Raw` + `Set-Content -Encoding UTF8`
    zinciri iki CSS dosyasını çift kodladı — "canlı" → "canlÄ±". Dosya
    çalışmaya devam eder (CSS ayrıştırılır), sadece yorumlar ve içerik
    okunmaz hâle gelir; kimse fark etmez.

    Bu test o izleri arar.
    """
    offenders: list[str] = []
    for path in sorted(STATIC.glob("*.css")) + sorted(STATIC.glob("*.js")) + \
            [STATIC / "index.html"]:
        text = path.read_text(encoding="utf-8")
        hits = [m for m in MOJIBAKE if m in text]
        if hits:
            offenders.append(f"{path.name}: {hits}")
    assert not offenders, "çift kodlanmış dosyalar:\n  " + "\n  ".join(offenders)


@pytest.mark.skipif(NODE is None, reason="node kurulu değil")
def test_every_script_parses() -> None:
    """
    Her JS dosyası SÖZDİZİMİ olarak geçerli olmalı.

    Bu test bir kez gerçek bir çökme yakaladı: bir şablon dizesinin
    (`template literal`) içine yorum olarak backtick içeren bir metin
    yazıldı, dize erken kapandı ve `finance.js` parse edilemedi. Onu içe
    aktaran `app.js` de düştü — UYGULAMA TAMAMEN BOŞ AÇILDI.

    Tarayıcı bunu yalnızca konsola yazar; sunucu 200 döndürmeye devam eder.
    Yani hiçbir sunucu testi bunu yakalayamaz.
    """
    broken: list[str] = []
    for path in _js_files():
        result = subprocess.run(  # noqa: S603
            [NODE, "--input-type=module", "--check"],
            input=path.read_text(encoding="utf-8"),
            capture_output=True, text=True, timeout=60, encoding="utf-8")
        if result.returncode != 0:
            first = (result.stderr or "").strip().splitlines()
            detail = next((ln for ln in first if "Error" in ln), first[-1] if first else "?")
            broken.append(f"{path.name}: {detail[:120]}")
    assert not broken, "sözdizimi hatası:\n  " + "\n  ".join(broken)


def test_no_html_comment_hides_inside_a_script() -> None:
    """
    JS dosyalarında `<!--` kullanılmaz.

    Şablon dizesi içine HTML yorumu yazmak iki yönden tehlikeli: yorumun
    içindeki backtick dizeyi erken kapatır ve `${...}` hâlâ değerlendirilir.
    "Yorum" sandığın şey aslında çalışan koddur.
    """
    offenders = [
        f"{p.name}:{n}"
        for p in _js_files()
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "<!--" in line
    ]
    assert not offenders, f"JS içinde HTML yorumu: {offenders}"


def test_no_screen_claims_positions_keep_being_watched_after_the_brake() -> None:
    """
    Acil fren anlatımı GERÇEĞİ söylemeli.

    Arayüzün üç ayrı yerinde (kontrol merkezi, profil menüsü, komut paleti)
    "mevcut pozisyonlar izlenmeye devam eder" yazıyordu. Doğru değildi:
    fren botları durdurduğu için hiçbiri izlenmiyordu. Kullanıcı korunduğunu
    sanıp bakmayı bırakıyordu — yani yanlış cümle, eksik özellikten daha
    tehlikeliydi.

    İkisini birden düzelttim ama üçüncüsü (palet) gözden kaçmıştı. Bu test
    üçünü birden bekler.
    """
    yalan = re.compile(r"izlenmeye\s+(?:ve\s+)?devam|izlenmeye\s+ve\s+stop", re.I)
    # Yorum satırları hariç: bu davranışın NEDEN değiştiğini anlatan
    # açıklamalar kalmalı, kullanıcıya gösterilen metin kalmamalı.
    yorum = re.compile(r"^\s*(//|/\*|\*)")
    offenders = [
        f"{p.name}:{n}"
        for p in _js_files()
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if yalan.search(line) and not yorum.match(line)
    ]
    assert not offenders, (
        "Acil frenden sonra pozisyonların izlendiğini söyleyen metin: "
        + ", ".join(offenders))


def test_every_modal_also_carries_the_panel_surface() -> None:
    """
    `.modal` boyut sınıfıdır; yüzeyi `.panel` verir.

    `class="modal ..."` yazıp `panel` eklememek, saydam bir pencere üretir.
    """
    offenders: list[str] = []
    for path in _js_files():
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in re.finditer(r'class="([^"]*\bmodal\b[^"]*)"', line):
                classes = match.group(1).split()
                # `modal-backdrop`, `modal-head`, `modal-body`, `modal-title`
                # ayrı sınıflardır; kural yalnızca `modal`'ın kendisi için.
                if "modal" not in classes:
                    continue
                if "panel" not in classes:
                    offenders.append(f"{path.name}:{line_no} → {match.group(1)}")

    assert not offenders, (
        "`.modal` var ama `.panel` yok — pencere saydam açılır:\n  "
        + "\n  ".join(offenders))


def test_no_stylesheet_link_is_missing_from_the_page() -> None:
    """
    `app/static` altındaki her stil dosyası sayfaya bağlı olmalı.

    Bağlanmamış bir stil dosyası sessizce yok sayılır: kurallar yazılır,
    hiçbir şey değişmez ve sebebi aranırken zaman kaybedilir.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    linked = set(re.findall(r'href="/ui/([a-z0-9_-]+\.css)"', html))
    on_disk = {p.name for p in STATIC.glob("*.css")}
    missing = sorted(on_disk - linked)
    assert not missing, f"sayfaya bağlanmamış stil dosyaları: {missing}"


def test_every_module_the_page_imports_exists() -> None:
    """
    `import ... from "/ui/x.js"` hedefi diskte olmalı.

    Olmayan bir modül, tarayıcıda TÜM betiği durdurur: sayfa boş açılır.
    """
    missing: list[str] = []
    for path in _js_files():
        text = path.read_text(encoding="utf-8")
        for target in re.findall(r'from\s+"/ui/([a-z0-9_./-]+)"', text):
            if not (STATIC / target).exists():
                missing.append(f"{path.name} → /ui/{target}")
    assert not missing, f"eksik modüller: {missing}"


def test_every_language_defines_the_same_keys() -> None:
    """
    Diller ARASINDA anahtar farkı olmamalı.

    Eksik bir anahtar `t()` çağrısından ham anahtar adıyla döner: kullanıcı
    "hazır" yerine `status.idle` görür. Bu gerçekten oldu — ekran okuyucu
    satırı `status.idle` diye duyuruyordu.
    """
    text = (STATIC / "i18n.js").read_text(encoding="utf-8")
    blocks = list(re.finditer(r"^  ([a-z]{2}): \{", text, re.M))
    assert len(blocks) >= 2, "dil blokları bulunamadı"

    per_lang: dict[str, set[str]] = {}
    for match in blocks:
        end = text.find("\n  },", match.end())
        body = text[match.end():end]
        per_lang[match.group(1)] = set(re.findall(r'"([a-zA-Z][a-zA-Z.]*)":', body))

    reference = per_lang["tr"]
    problems = []
    for lang, keys in per_lang.items():
        if keys != reference:
            missing = sorted(reference - keys)
            extra = sorted(keys - reference)
            problems.append(f"{lang}: eksik={missing} fazla={extra}")
    assert not problems, "diller eşit değil:\n  " + "\n  ".join(problems)


def test_status_keys_cover_every_agent_state() -> None:
    """
    Ajanın her durumu için bir çeviri olmalı.

    `updateAgentStatus` durumu `t("status." + status)` ile arar; koddaki
    durum adlarıyla sözlükteki anahtarlar birebir uyuşmazsa kullanıcı ham
    anahtarı görür.
    """
    text = (STATIC / "i18n.js").read_text(encoding="utf-8")
    tr = text[text.index("  tr: {"):text.index("\n  },", text.index("  tr: {"))]
    for state in ("idle", "thinking", "running", "error"):
        assert f'"status.{state}"' in tr, f"status.{state} tanımlı değil"


def test_the_rail_only_links_to_routes_that_exist() -> None:
    """
    Kenar çubuğundaki her `data-route`, `VIEWS` içinde tanımlı olmalı.

    Tanımsız bir rota boş ekran açar. "Sistemler" öğesi kaldırılırken
    rotanın da kaldırılıp kaldırılmadığını burası doğrular.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")

    block = re.search(r"(?s)const VIEWS = \{(.*?)\n\};", app_js)
    assert block, "VIEWS bloğu bulunamadı"
    defined = set(re.findall(r"^\s{2}([a-z][a-zA-Z]*):", block.group(1), re.M))

    used = set(re.findall(r'data-route="([a-z]+)"', html))
    unknown = sorted(used - defined)
    assert not unknown, f"tanımsız rotaya bağlanan menü öğesi: {unknown}"


def test_sidebar_toggle_cannot_log_the_user_out() -> None:
    """Panel görünürlüğü, kimlik durumundan tamamen ayrı kalmalı."""
    text = (STATIC / "app.js").read_text(encoding="utf-8")
    match = re.search(
        r'bind\("#rail-toggle",\s*\(event\)\s*=>\s*\{(.*?)\n\s*\}\);',
        text, re.S)
    assert match, "kenar çubuğu için açık ve sınanabilir bir işleyici yok"
    handler = match.group(1)
    assert "stopPropagation" in handler
    assert 'classList.toggle("collapsed")' in handler
    assert "logout" not in handler.lower(), "panel düğmesi oturum kapatma yoluna bağlı"


def test_non_auth_boot_errors_do_not_clear_the_session() -> None:
    """Bir çizim/panel hatası, geçerli giriş bilgisini silmemeli."""
    text = (STATIC / "app.js").read_text(encoding="utf-8")
    tail = text[text.rfind("if (state.token)"):]
    assert "err instanceof SessionExpired" in tail
    assert "catch(() => logout())" not in tail


def test_panel_close_paths_are_reversible_and_keep_the_main_surface() -> None:
    """Her iki panel kapanınca ana alan kalmalı ve geri açma yolu olmalı."""
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    chat = (STATIC / "chat.js").read_text(encoding="utf-8")
    command = (STATIC / "command.css").read_text(encoding="utf-8")

    assert "showAppRecovery(err)" in app
    assert "function setLivePanel" in chat
    assert "restoreLivePanel();" in chat
    assert "panel.inert = !open" in chat
    assert ".cmd-work.live-collapsed { grid-template-columns: minmax(0, 1fr); }" in command
    assert ".cmd-work.live-collapsed > .live-fab { display: flex; }" in command
    assert "6px 280px" in command, "orta genişlikte splitter kolonu kayboluyor"


def test_chat_vertical_spacing_stays_compact() -> None:
    """Mesaj ve komut akışı gereksiz boş dikey bantlar oluşturmamalı."""
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")
    command = (STATIC / "command.css").read_text(encoding="utf-8")

    margins = [int(v) for v in re.findall(
        r"\.msg\s*\{[^}]*margin-bottom:\s*(\d+)px", styles)]
    paddings = [int(v) for v in re.findall(
        r"\.cmd-main \.chat-stream\s*\{[^}]*padding:\s*(\d+)px", command)]
    assert margins and max(margins) <= 12
    assert paddings and max(paddings) <= 14


def test_agent_browser_has_a_live_workspace_and_history_restore() -> None:
    """Tarayıcı paneli hem canlı araç adımlarını hem eski mesajları göstermeli."""
    workspace = (STATIC / "workspace.js").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    chat = (STATIC / "chat.js").read_text(encoding="utf-8")
    for tool in ("browse_page", "browse_follow", "web_read"):
        assert f'"{tool}"' in workspace
    assert 'data-tab="browser"' in workspace
    assert "handleBrowserMessage(event.message" in app
    assert "hydrateBrowserMessages(messages" in chat
    assert 'rel="noopener noreferrer"' in workspace
