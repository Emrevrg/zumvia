/* ==========================================================================
   ZUMVIA — Komut Paleti (Ctrl+K)
   --------------------------------------------------------------------------
   Tek bir arama kutusundan sayfalara, görevlere, botlara ve hızlı eylemlere
   erişim. Klavyeyle tam kullanılabilir: ↑ ↓ gezin, ⏎ seç, Esc kapat.
   ========================================================================== */

import { icon } from "/ui/icons.js";
import { t } from "/ui/i18n.js";

let openPalette = null;

export function openCommandPalette() {
  const { $, el, esc, api, navigate, toast, state } = window.VQ;
  if (openPalette) return;

  const backdrop = el(`
    <div class="palette-backdrop">
      <div class="palette" role="dialog" aria-label="Komut paleti">
        <div class="palette-input">
          ${icon("search", 16)}
          <input id="palette-query" placeholder="Sayfa, görev, bot veya komut ara…"
                 autocomplete="off" spellcheck="false" />
          <kbd>Esc</kbd>
        </div>
        <div class="palette-list" id="palette-list"></div>
        <div class="palette-foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> gezin</span>
          <span><kbd>⏎</kbd> seç</span>
          <span><kbd>Esc</kbd> kapat</span>
        </div>
      </div>
    </div>`);

  document.getElementById("modal-root").appendChild(backdrop);
  openPalette = backdrop;

  const close = () => {
    backdrop.remove();
    openPalette = null;
    document.removeEventListener("keydown", onKey, true);
  };

  /* ------------------------------- kayıtlar ------------------------------ */
  const entries = [
    { group: "Sayfa", label: "Komuta", hint: "ajanla konuş", ico: "command",
      run: () => navigate("chat") },
    { group: "Sayfa", label: "Portföy", hint: "sermaye ve performans", ico: "portfolio",
      run: () => navigate("dashboard") },
    { group: "Sayfa", label: "Botlar", hint: "otonom stratejiler", ico: "bots",
      run: () => navigate("bots") },
    { group: "Sayfa", label: "Piyasa analizi", hint: "göstergeler ve konsensüs",
      ico: "analysis", run: () => navigate("analysis") },
    { group: "Sayfa", label: "Geri test", hint: "geçmiş veride kanıt", ico: "backtest",
      run: () => navigate("backtest") },
    { group: "Sayfa", label: "Anahtarlar", hint: "API anahtarı kasası", ico: "vault",
      run: () => navigate("vault") },

    { group: "Eylem", label: "Yeni görev başlat", hint: "ajana yeni iş ver", ico: "plus",
      run: () => window.VQ.newChatSession?.() },
    { group: "Eylem", label: "Kontrol merkezi", hint: "güvenlik, sicil, raporlar",
      ico: "settings", run: () => window.VQ.openSettingsCenter?.() },
    { group: "Eylem", label: "Rapor üret", hint: "JSON + Markdown çıktı", ico: "report",
      run: async () => {
        toast(t("msg.reportRunning"));
        await api("/api/safety/reports", { method: "POST" });
        toast(t("msg.reportReady"));
      } },
    { group: "Eylem", label: "Acil fren çek", hint: "pozisyonları kapat ve durdur",
      ico: "brake", danger: true, run: async () => {
        // Bu metin GERÇEĞİ söylemeli. Eskiden "mevcut pozisyonlar izlenmeye
        // devam eder" yazıyordu — doğru değildi: botlar durduğu için hiçbiri
        // izlenmiyordu. Kullanıcı korunduğunu sanarak bakmayı bırakıyordu.
        const ok = await window.VQ.confirmModal(
          "Acil fren",
          "<b>Tüm açık pozisyonlar piyasa fiyatından KAPATILACAK</b>, kâr da "
          + "zarar da realize edilecek. Sonra botlar durdurulacak.",
          "Kapat ve durdur");
        if (!ok) return;
        const r = await api("/api/safety/kill-switch",
          { method: "POST", body: { active: true, note: "Komut paleti" } });
        toast(r.message, "err");
        window.VQ.render();
      } },
    { group: "Eylem", label: "Tüm botları durdur", hint: "zamanlayıcıdan çıkar",
      ico: "stop", run: async () => {
        const bots = await api("/api/bots");
        const running = bots.filter((b) => b.status === "running");
        await Promise.all(running.map((b) =>
          api(`/api/bots/${b.id}/stop`, { method: "POST" })));
        toast(`${running.length} bot durduruldu.`);
        window.VQ.render();
      } },
    { group: "Sayfa", label: t("bots.tabLibrary"),
      hint: "hazır ticaret sistemleri kütüphanesi",
      ico: "bots", run: () => window.VQ.navigate("systems") },
    { group: "Eylem", label: "Çıkış yap", hint: "oturumu kapat", ico: "logout",
      run: () => window.VQ.logout() },
  ];

  /* Görevler ve botlar canlı olarak eklenir */
  (async () => {
    try {
      const [sessions, bots] = await Promise.all([
        api("/api/agent/sessions"),
        api("/api/bots"),
      ]);
      sessions.forEach((s) => entries.push({
        group: "Görev", label: s.title,
        hint: s.autonomous ? "otonom görev" : "görev geçmişi", ico: "command",
        run: () => window.VQ.openChatSession?.(s.id),
      }));
      bots.forEach((b) => entries.push({
        group: "Bot", label: b.name,
        hint: `${b.symbol} · ${b.mode === "live" ? "gerçek para" : "sanal"}`,
        ico: "bots", run: () => navigate("bot", b.id),
      }));
      draw($("#palette-query").value);
    } catch {
      /* liste zenginleştirilemezse temel kayıtlarla devam edilir */
    }
  })();

  /* --------------------------------- çizim ------------------------------- */
  let cursor = 0;
  let visible = [];

  function draw(query = "") {
    const q = query.trim().toLocaleLowerCase("tr");
    visible = entries.filter((e) =>
      !q || `${e.label} ${e.hint} ${e.group}`.toLocaleLowerCase("tr").includes(q));
    cursor = Math.min(cursor, Math.max(0, visible.length - 1));

    const list = $("#palette-list");
    if (!visible.length) {
      list.innerHTML = `<div class="palette-empty">Sonuç yok</div>`;
      return;
    }

    let lastGroup = "";
    list.innerHTML = visible.map((e, i) => {
      const header = e.group !== lastGroup
        ? `<div class="palette-group">${esc(e.group)}</div>` : "";
      lastGroup = e.group;
      return header + `
        <div class="palette-row ${i === cursor ? "active" : ""} ${e.danger ? "danger" : ""}"
             data-index="${i}">
          <span class="palette-ico">${icon(e.ico, 16)}</span>
          <span class="palette-label">${esc(e.label)}</span>
          <span class="palette-hint">${esc(e.hint)}</span>
        </div>`;
    }).join("");

    list.querySelectorAll("[data-index]").forEach((row) => {
      row.onclick = () => choose(Number(row.dataset.index));
      row.onmouseenter = () => {
        cursor = Number(row.dataset.index);
        list.querySelectorAll("[data-index]").forEach((r) =>
          r.classList.toggle("active", r === row));
      };
    });
  }

  async function choose(index) {
    const entry = visible[index];
    if (!entry) return;
    close();
    try {
      await entry.run();
    } catch (err) {
      toast(err.message || "İşlem tamamlanamadı", "err");
    }
  }

  function onKey(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      return close();
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      cursor = Math.min(cursor + 1, visible.length - 1);
      return draw($("#palette-query").value);
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      cursor = Math.max(cursor - 1, 0);
      return draw($("#palette-query").value);
    }
    if (event.key === "Enter") {
      event.preventDefault();
      return choose(cursor);
    }
  }

  backdrop.addEventListener("mousedown", (e) => {
    if (e.target === backdrop) close();
  });
  document.addEventListener("keydown", onKey, true);

  const input = $("#palette-query");
  input.addEventListener("input", () => {
    cursor = 0;
    draw(input.value);
  });

  draw("");
  input.focus();
}

/** Ctrl+K / Cmd+K kısayolunu bağlar. */
export function bindPaletteShortcut() {
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if (!document.getElementById("app-view")?.classList.contains("hidden")) {
        openCommandPalette();
      }
    }
  });
}
