/**
 * SOHBET MODLARI VE DOSYA EKLERİ — yazı kutusundaki "+" düğmesi
 * ==============================================================
 *
 * Kullanıcı her seferinde ne istediğini uzun uzun anlatmasın: bir mod
 * seçtiğinde ajan o işin gereğini bilerek başlar.
 *
 * Arayüz tarafındaki tek kural şu: SEÇİLİ MOD GÖRÜNÜR OLMALI. Gizli bir
 * mod, kullanıcının istemediği bir davranışı sessizce açar. Seçim yazı
 * kutusunun hemen üstünde bir rozet olarak durur ve tek tıkla kapanır.
 *
 * Dosyalar mesajla birlikte DEĞİL, seçildiği anda yüklenir: 5 MB'lık bir
 * CSV'yi "Gönder"e bastıktan sonra beklemek, gönderdiğini sanan kullanıcıyı
 * bekletmektir. Yükleme biterse rozet çıkar; bitmezse sebebi yazar.
 */
import { icon } from "/ui/icons.js";
import { t } from "/ui/i18n.js";

/**
 * Modun EKRANDA görünen adı ve açıklaması.
 *
 * Sunucu modun KİMLİĞİNİ verir (id, ikon, hangi araçlar açılır); adını
 * arayüz kendi dilinde yazar. Ad bir sunu meselesidir, davranış değil —
 * sunucudan sabit Türkçe gelince, arayüzü İngilizce yapan kullanıcı
 * "+" menüsünde hâlâ Türkçe görüyordu.
 *
 * Sözlükte karşılığı yoksa sunucunun metnine düşer: yarın eklenen bir mod,
 * çevirisi yazılmadan önce de görünür kalır.
 */
function modeLabel(mode) {
  const key = `mode.${mode.id}.label`;
  const translated = t(key);
  return translated === key ? mode.label : translated;
}

function modeHint(mode) {
  const key = `mode.${mode.id}.hint`;
  const translated = t(key);
  return translated === key ? mode.hint : translated;
}

export const modeState = {
  catalog: null,
  mode: "normal",
  attachments: [],   // [{id, filename, kind, chars, truncated, warnings}]
  uploading: false,
};

function vq() {
  return window.VQ;
}

/** Mod ve ekleri temizler — yeni bir göreve geçerken çağrılır. */
export function resetModes() {
  modeState.mode = "normal";
  modeState.attachments = [];
}

export async function loadModeCatalog() {
  if (modeState.catalog) return modeState.catalog;
  try {
    modeState.catalog = await vq().api("/api/agent/modes");
  } catch {
    // Katalog alınamazsa "+" düğmesi yine çalışsın: dosya yükleme ve
    // temel modlar sabit tanımlıdır, sunucudan gelmesi bir kolaylıktır.
    modeState.catalog = { modes: [], default: "normal", upload: null };
  }
  return modeState.catalog;
}

/* ============================== "+" menüsü =============================== */

export async function openModeMenu(anchor, sessionId) {
  const { el, esc, toast } = vq();
  document.querySelector(".mode-menu")?.remove();

  const catalog = await loadModeCatalog();
  const modes = catalog.modes || [];
  const upload = catalog.upload;

  const menu = el(`
    <div class="mode-menu">
      ${modes.filter((m) => m.id !== "files").map((m) => `
        <div class="mode-item ${m.id === modeState.mode ? "on" : ""}" data-mode="${esc(m.id)}">
          <span class="mode-ico" data-icon="${esc(m.icon)}" data-icon-size="16"></span>
          <span class="mode-text">
            <span class="mode-label">${esc(modeLabel(m))}</span>
            <span class="mode-hint">${esc(modeHint(m))}</span>
          </span>
        </div>`).join("")}

      <div class="mode-sep"></div>

      <div class="mode-item" data-act="upload">
        <span class="mode-ico" data-icon="upload" data-icon-size="16"></span>
        <span class="mode-text">
          <span class="mode-label">${esc(t("mode.files.label"))}</span>
          <span class="mode-hint">
            ${upload ? esc(upload.extensions.join(", ")) : "CSV, PDF, JSON"} ·
            ${esc(t("mode.uploadLimit"))} ${upload ? Math.round(upload.max_bytes / 1024 / 1024) : 5} MB
          </span>
        </span>
      </div>

      ${modeState.mode !== "normal" || modeState.attachments.length ? `
        <div class="mode-sep"></div>
        <div class="mode-item" data-act="clear">
          <span class="mode-ico" data-icon="close" data-icon-size="16"></span>
          <span class="mode-text"><span class="mode-label">${esc(t("mode.clear"))}</span></span>
        </div>` : ""}
    </div>`);

  const rect = anchor.getBoundingClientRect();
  menu.style.left = `${Math.max(12, rect.left)}px`;
  menu.style.bottom = `${window.innerHeight - rect.top + 8}px`;
  document.body.appendChild(menu);
  vq().hydrateIcons?.(menu);

  const close = () => menu.remove();
  setTimeout(() => document.addEventListener("click", close, { once: true }), 0);

  menu.addEventListener("click", async (event) => {
    const item = event.target.closest(".mode-item");
    if (!item) return;

    if (item.dataset.mode) {
      // Aynı moda ikinci kez tıklamak onu kapatır: seçtiğini geri almak
      // için ayrı bir yer aramak gerekmemeli.
      modeState.mode = modeState.mode === item.dataset.mode ? "normal" : item.dataset.mode;
      close();
      renderContextBar(sessionId);
      applyPlaceholder();
      return;
    }

    if (item.dataset.act === "clear") {
      close();
      const ids = modeState.attachments.map((a) => a.id);
      resetModes();
      renderContextBar(sessionId);
      applyPlaceholder();
      // Sunucuda da sil: kullanıcı "temizle" dediyse dosyası kalmasın.
      for (const id of ids) {
        try { await vq().api(`/api/agent/attachments/${id}`, { method: "DELETE" }); }
        catch { /* zaten yoksa sorun değil */ }
      }
      return;
    }

    if (item.dataset.act === "upload") {
      close();
      if (!sessionId) {
        toast(vq().t("msg.needTask"), "warn");
        return;
      }
      pickFiles(sessionId);
    }
  });
}

/* ============================== dosya yükleme ============================ */

function pickFiles(sessionId) {
  const catalog = modeState.catalog;
  const input = document.createElement("input");
  input.type = "file";
  input.multiple = true;
  if (catalog?.upload?.extensions?.length) {
    input.accept = catalog.upload.extensions.join(",");
  }
  input.addEventListener("change", async () => {
    for (const file of Array.from(input.files || [])) {
      await uploadOne(sessionId, file);
    }
  });
  input.click();
}

async function uploadOne(sessionId, file) {
  const { toast, state } = vq();
  const max = modeState.catalog?.upload?.max_bytes || 5 * 1024 * 1024;
  if (file.size > max) {
    toast(`${vq().t("msg.fileTooBig")} ${file.name} — `
          + `${(file.size / 1024 / 1024).toFixed(1)} MB / `
          + `${Math.round(max / 1024 / 1024)} MB`, "err");
    return;
  }

  modeState.uploading = true;
  renderContextBar(sessionId);

  const form = new FormData();
  form.append("file", file);
  try {
    // Bu tek istek `api()` yardımcısını kullanamaz: o JSON gönderir, burada
    // multipart gerekiyor. Yetki başlığı elle eklenir.
    const res = await fetch(`/api/agent/sessions/${sessionId}/attachments`, {
      method: "POST",
      headers: state.token ? { Authorization: `Bearer ${state.token}` } : {},
      body: form,
    });
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (!res.ok) {
      throw new Error(typeof data?.detail === "string" ? data.detail
                                                       : `Yükleme başarısız (${res.status})`);
    }
    modeState.attachments.push(data);

    // Kırpma ve şüpheli içerik SESSİZ geçilmez: kullanıcı ajanın dosyanın
    // tamamını görmediğini bilmeli.
    (data.warnings || []).forEach((w) => toast(w, "warn"));
    if (!data.warnings?.length) toast(`${data.filename} — ${vq().t("msg.fileAdded")}`);
  } catch (err) {
    toast(err.message, "err");
  } finally {
    modeState.uploading = false;
    renderContextBar(sessionId);
  }
}

/* ============================= bağlam şeridi ============================= */

/** Yazı kutusunun üstündeki "şu an bu moddasın" şeridi. */
export function renderContextBar(sessionId) {
  const { $, esc } = vq();
  const zone = $(".composer-zone") || $(".home-composer")?.parentElement;
  if (!zone) return;

  let bar = zone.querySelector(".composer-context");
  const active = modeState.mode !== "normal";
  const hasFiles = modeState.attachments.length > 0;

  if (!active && !hasFiles && !modeState.uploading) {
    bar?.remove();
    return;
  }

  if (!bar) {
    bar = document.createElement("div");
    bar.className = "composer-context";
    const composer = zone.querySelector(".composer");
    zone.insertBefore(bar, composer || zone.firstChild);
  }

  const entry = (modeState.catalog?.modes || []).find((m) => m.id === modeState.mode);

  bar.innerHTML = `
    ${active ? `
      <span class="ctx-pill">
        ${icon(entry?.icon || "command", 12)} ${esc(entry ? modeLabel(entry) : modeState.mode)}
        <button data-clear-mode title="Modu kapat">${icon("close", 11)}</button>
      </span>` : ""}

    ${modeState.attachments.map((a) => `
      <span class="ctx-pill file ${a.truncated ? "warn" : ""}"
            title="${esc(a.summary || "")}${a.truncated ? " — KIRPILDI" : ""}">
        ${icon("paperclip", 12)} ${esc(a.filename)}
        ${a.truncated ? " (kırpıldı)" : ""}
        <button data-drop="${a.id}" title="Kaldır">${icon("close", 11)}</button>
      </span>`).join("")}

    ${modeState.uploading ? `<span class="ctx-pill">yükleniyor…</span>` : ""}
  `;

  bar.querySelector("[data-clear-mode]")?.addEventListener("click", () => {
    modeState.mode = "normal";
    renderContextBar(sessionId);
    applyPlaceholder();
  });

  bar.querySelectorAll("[data-drop]").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = Number(button.dataset.drop);
      modeState.attachments = modeState.attachments.filter((a) => a.id !== id);
      renderContextBar(sessionId);
      try { await vq().api(`/api/agent/attachments/${id}`, { method: "DELETE" }); }
      catch { /* zaten yoksa sorun değil */ }
    });
  });
}

/** Seçili moda göre yazı kutusunun ipucu metnini değiştirir. */
export function applyPlaceholder() {
  const input = vq().$("#chat-input");
  if (!input) return;
  const entry = (modeState.catalog?.modes || []).find((m) => m.id === modeState.mode);
  if (entry?.placeholder) {
    input.placeholder = entry.placeholder;
  } else if (input.dataset.basePlaceholder) {
    input.placeholder = input.dataset.basePlaceholder;
  }
  input.focus();
}

/** Mesajla birlikte gönderilecek alanlar. */
export function messagePayload() {
  return {
    mode: modeState.mode,
    attachment_ids: modeState.attachments.map((a) => a.id),
  };
}

/**
 * Gönderim sonrası temizlik.
 *
 * Mod KORUNUR, dosyalar bırakılır. Sebebi: bir moda girmek bir oturum
 * kararıdır ("bu görevde derin araştırma yapıyorum"), oysa bir dosya tek bir
 * soruya aittir ve ikinci mesajda yeniden gönderilmesi hem bağlamı şişirir
 * hem de kullanıcıyı şaşırtır.
 */
export function afterSend() {
  modeState.attachments = [];
}
