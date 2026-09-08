/* ==========================================================================
   ZUMVIA — Arayüz Uygulaması (bağımlılıksız ES modülü)
   ========================================================================== */

import { CandleChart, drawEquityCurve, formatNumber } from "/ui/chart.js";
import {
  appendChatMessage, newChatSession, openChatSession, refreshLivePanel,
  startFinanceChat,
  renderRailSessions, updateAgentStatus, viewChat,
} from "/ui/chat.js";
import { handleBrowserMessage, handleResearchEvent } from "/ui/workspace.js";
import { stopFinanceTimers, viewFinance } from "/ui/finance.js";
import { openProfileMenu, openSettingsCenter } from "/ui/shell.js";
import { hydrateIcons, icon, observeIcons } from "/ui/icons.js";
import { bindPaletteShortcut, openCommandPalette } from "/ui/palette.js";
import { mountRailSplitter, refitOnResize } from "/ui/layout.js";
import { applyI18n, LANGUAGES, lang, setLang, t } from "/ui/i18n.js";
import { active as activeTheme, boot as bootTheme, preference as themePreference, setTheme, THEMES } from "/ui/theme.js";

/* ----------------------------------------------------------------- durum */

const state = {
  token: localStorage.getItem("vq_token") || "",
  email: localStorage.getItem("vq_email") || "",
  route: "dashboard",
  // Botlar ekranindaki aktif sekme: "running" | "library".
  // Kutuphane ayri bir sayfa degil, botlarin yanindaki ikinci sekme.
  botsTab: "running",
  routeParam: null,
  bots: [],
  credentials: [],
  catalog: null,
  providers: [],
  meta: null,
  socket: null,
  chart: null,
  liveEvents: [],
  health: null,          // sistem sağlığı (disk durumu dahil)
  timers: [],
};

/* ------------------------------------------------------------------- API */

async function api(path, { method = "GET", body, auth = true } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth && state.token) headers.Authorization = `Bearer ${state.token}`;

  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (res.status === 401 && auth) {
    logout({ expired: true });
    throw new SessionExpired();
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    // 404, bu üründe neredeyse her zaman TEK bir anlama gelir: sunucu süreci
    // arayüzden eski. Uvicorn statik dosyaları diskten taze okur ama Python
    // rotalarını açılışta kaydeder — yeni bir ekran yüklenir, çağırdığı uç
    // yoktur. Ham "İstek başarısız (404)" kullanıcıyı kodda hata var
    // sanmaya iter; asıl yapılacak şey sunucuyu yeniden başlatmaktır.
    if (res.status === 404 && path.startsWith("/api/")) {
      throw new Error(
        `Bu özellik sunucuda bulunamadı (${path}). Çalışan sunucu süreci ` +
        `arayüzden eski — sunucuyu yeniden başlatın.`);
    }
    const detail = data?.detail;
    throw new Error(
      typeof detail === "string" ? detail
        : Array.isArray(detail) ? detail.map((d) => d.msg).join(", ")
          : `İstek başarısız (${res.status})`
    );
  }
  return data;
}

/* -------------------------------------------------------------- yardımcı */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const el = (html) => {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const money = (v, digits = 2) =>
  Number(v ?? 0).toLocaleString("tr-TR", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const pct = (v) => `${Number(v ?? 0) >= 0 ? "+" : ""}${Number(v ?? 0).toFixed(2)}%`;
const cls = (v) => (Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "neutral");
const timeOf = (iso) => (iso ? new Date(iso).toLocaleTimeString("tr-TR", { hour12: false }) : "--:--:--");
const dateOf = (iso) => (iso ? new Date(iso).toLocaleString("tr-TR") : "—");

function toast(message, kind = "ok") {
  const node = el(`<div class="toast ${kind}">${esc(message)}</div>`);
  const host = $("#toasts") || document.body;
  host.appendChild(node);
  setTimeout(() => {
    node.style.opacity = "0";
    node.style.transition = "opacity .3s";
    setTimeout(() => node.remove(), 300);
  }, kind === "err" ? 6500 : 3800);
}

function confirmModal(title, message, confirmLabel = "Onayla") {
  return new Promise((resolve) => {
    const backdrop = el(`
      <div class="modal-backdrop">
        <div class="panel modal" style="max-width:460px">
          <div class="panel-title mb">${esc(title)}</div>
          <div class="small dim mb">${message}</div>
          <div class="row" style="justify-content:flex-end">
            <button class="btn" data-act="cancel">Vazgeç</button>
            <button class="btn btn-danger" data-act="ok">${esc(confirmLabel)}</button>
          </div>
        </div>
      </div>`);
    backdrop.addEventListener("click", (e) => {
      const act = e.target.dataset?.act;
      if (!act && e.target !== backdrop) return;
      backdrop.remove();
      resolve(act === "ok");
    });
    $("#modal-root").appendChild(backdrop);
  });
}

/**
 * Tek satırlık metin soran kip pencere.
 *
 * `window.prompt` tarayıcıda çirkin ve mobilde bloklanabiliyor; ayrıca
 * uygulamanın koyu temasıyla uyumsuz. Bu, aynı işi yapan kendi penceremiz.
 */
function promptModal(title, message, initial = "", placeholder = "") {
  return new Promise((resolve) => {
    const backdrop = el(`
      <div class="modal-backdrop">
        <div class="panel modal" style="max-width:460px">
          <div class="panel-title mb">${esc(title)}</div>
          ${message ? `<div class="small dim mb">${esc(message)}</div>` : ""}
          <input class="input" id="prompt-value" value="${esc(initial)}"
                 placeholder="${esc(placeholder)}" />
          <div class="row mt" style="justify-content:flex-end">
            <button class="btn" data-act="cancel">${esc(t("common.cancel"))}</button>
            <button class="btn btn-primary" data-act="ok">${esc(t("common.save"))}</button>
          </div>
        </div>
      </div>`);

    const finish = (value) => { backdrop.remove(); resolve(value); };
    const input = backdrop.querySelector("#prompt-value");

    backdrop.addEventListener("click", (e) => {
      const act = e.target.dataset?.act;
      if (act === "ok") return finish(input.value.trim() || null);
      if (act === "cancel" || e.target === backdrop) return finish(null);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") finish(input.value.trim() || null);
      if (e.key === "Escape") finish(null);
    });

    $("#modal-root").appendChild(backdrop);
    setTimeout(() => input.focus(), 30);
  });
}

function clearTimers() {
  state.timers.forEach(clearInterval);
  state.timers = [];
  if (state.chart) { state.chart.destroy(); state.chart = null; }
}

/* ------------------------------------------------------------ kimlik akışı */

function setSession(data) {
  state.token = data.access_token;
  state.email = data.email;
  localStorage.setItem("vq_token", data.access_token);
  localStorage.setItem("vq_email", data.email);
  localStorage.setItem("vq_last_email", data.email);
}

/** Oturum düştüğünde fırlatılır; arayüz bunu hata olarak GÖSTERMEZ. */
class SessionExpired extends Error {
  constructor() {
    super("session-expired");
    this.name = "SessionExpired";
  }
}

function logout({ expired = false } = {}) {
  state.token = "";
  localStorage.removeItem("vq_token");
  localStorage.removeItem("vq_email");
  state.socket?.close();
  state.socket = null;
  clearTimers();
  document.querySelector(".palette-backdrop")?.remove();
  document.querySelector(".modal-backdrop")?.remove();
  $("#app-view")?.classList.add("hidden");
  $("#auth-view")?.classList.remove("hidden");
  const pageBody = $("#page-body");
  if (pageBody) pageBody.innerHTML = "";

  const notice = $("#auth-error");
  if (notice && expired) {
    notice.textContent = "Oturumunuz sona erdi. Güvenlik için tekrar giriş yapın.";
    notice.classList.remove("hidden");
  } else if (notice) {
    notice.classList.add("hidden");
  }
  const email = $("#auth-email");
  const password = $("#auth-password");
  if (email) email.value = localStorage.getItem("vq_last_email") || "";
  if (password) password.value = "";
}

function initAuthScreen() {
  applyI18n(document);
  let mode = "login";
  const mark = $("#auth-mark");
  if (mark) mark.innerHTML = AUTH_MARK;
  $("#auth-email").value = localStorage.getItem("vq_last_email") || "";
  $$("[data-auth-tab]").forEach((tab) => {
    tab.addEventListener("click", () => {
      mode = tab.dataset.authTab;
      $$("[data-auth-tab]").forEach((t) => t.classList.toggle("active", t === tab));
      $("#auth-submit").textContent = mode === "login" ? "Giriş yap" : "Hesap oluştur";
      $("#auth-error").classList.add("hidden");
      $("#auth-password").setAttribute("autocomplete", mode === "login" ? "current-password" : "new-password");
    });
  });

  $("#auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const button = $("#auth-submit");
    const notice = $("#auth-error");
    notice.classList.add("hidden");
    button.disabled = true;
    button.textContent = mode === "login" ? "Giriş yapılıyor…" : "Hesap oluşturuluyor…";
    try {
      const payload = { email: $("#auth-email").value.trim(), password: $("#auth-password").value };
      const data = await api(`/api/auth/${mode}`, { method: "POST", body: payload, auth: false });
      setSession(data);
      await bootApp();
      toast(mode === "login" ? "Hoş geldiniz." : "Hesabınız oluşturuldu.");
    } catch (err) {
      notice.textContent = err.message || "Giriş yapılamadı.";
      notice.classList.remove("hidden");
    } finally {
      button.disabled = false;
      button.textContent = mode === "login" ? "Giriş yap" : "Hesap oluştur";
    }
  });
}

/* ---------------------------------------------------------------- yönlendirme */

function navigate(route, param = null) {
  const target = param ? `${route}/${param}` : route;
  const current = (location.hash || "").slice(1);

  state.route = route;
  state.routeParam = param;

  if (current === target) {
    render();
  } else {
    // Hash değişimi render'ı tetikler — burada doğrudan render YOK (çifte çizimi önler)
    location.hash = target;
  }
}

function readHash() {
  const [route, param] = (location.hash || "#chat").slice(1).split("/");
  state.route = route || "chat";
  state.routeParam = param || null;
}

/* ----------------------------------------------------------------- WebSocket */

function connectSocket() {
  if (state.socket) state.socket.close();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/ws/stream?token=${encodeURIComponent(state.token)}`);
  state.socket = socket;

  const status = $("#conn-status");
  const setStatus = (dot, label) => {
    if (status) status.innerHTML = `<span class="dot ${dot}"></span><span>${label}</span>`;
  };

  socket.onopen = () => setStatus("dot-live", t("rail.live"));
  socket.onclose = () => {
    setStatus("dot-off", t("conn.lost"));
    if (state.token) setTimeout(connectSocket, 4000);
  };
  socket.onerror = () => setStatus("dot-lock", t("conn.error"));

  socket.onmessage = (raw) => {
    const event = JSON.parse(raw.data);

    // Araştırma olayları tezgâh panelinin işidir; oraya düşer ve burada
    // tekrar işlenmez.
    if (event.type?.startsWith("research_") && handleResearchEvent(event)) return;

    if (event.type === "agent") {
      // Tarayıcı aracı, sohbet kartına eklenirken aynı anda güvenli canlı
      // tarayıcı paneline de düşer. İşleyici diğer araçlarda hiçbir şey yapmaz.
      handleBrowserMessage(event.message, { sessionId: event.session_id });
      appendChatMessage(event.message);
      return;
    }
    if (event.type === "agent_status") {
      updateAgentStatus(event.status, event.error);
      return;
    }

    if (event.type === "event") {
      state.liveEvents.push(event);
      if (["exec", "risk", "engine"].includes(event.category)) refreshLivePanel();
      if (state.liveEvents.length > 400) state.liveEvents.shift();
      appendLogLine(event);
      if (event.level === "trade" || event.category === "exec") {
        toast(`${event.bot_name}: ${event.message.slice(0, 110)}`,
          event.level === "error" ? "err" : "ok");
        refreshCurrentView();
      }
      if (event.category === "risk" && event.level === "error") {
        toast(`${event.message.slice(0, 140)}`, "err");
        refreshCurrentView();
      }
    }
  };
}

function appendLogLine(event) {
  const terminal = $("#log-terminal");
  if (!terminal) return;
  if (state.routeParam && String(event.bot_id) !== String(state.routeParam) &&
      state.route === "bot") return;
  terminal.appendChild(logLine(event));
  terminal.scrollTop = terminal.scrollHeight;
}

function logLine(event) {
  const prefix = state.route === "dashboard" ? `[${esc(event.bot_name ?? "")}] ` : "";
  return el(`
    <div class="log-line log-${esc(event.level)}">
      <span class="log-ts">${timeOf(event.ts)}</span>
      <span class="log-cat">${esc(LOG_CATEGORY[event.category] || event.category)}</span>
      <span class="log-msg">${prefix}${esc(event.message)}</span>
    </div>`);
}

/* -------------------------------------------------------------- ana yükleme */

async function bootApp() {
  $("#auth-view")?.classList.add("hidden");
  $("#app-view")?.classList.remove("hidden");
  const userEmail = $("#user-email");
  if (userEmail) userEmail.textContent = state.email;

  $$(".rail-item").forEach((item) => {
    item.onclick = () => {
      navigate(item.dataset.route);
      $("#app-view").classList.remove("rail-open");
    };
  });

  // Bağlamalar savunmalıdır: arayüz şablonu değişirse tek bir eksik düğme
  // tüm başlatmayı çökertmemelidir.
  bind("#new-session-btn", () => newChatSession());
  // Kenar çubuğu için TEK düğme: başlıkta durur, iki yönde de çalışır.
  // Düğme olayı burada sonlandırılır; açık profil menüsü veya dış-tıklama
  // dinleyicisi bu tıklamayı "çıkış" eylemi olarak yeniden yorumlayamaz.
  bind("#rail-toggle", (event) => {
    event.preventDefault();
    event.stopPropagation();
    document.querySelector(".menu")?.remove();
    const shell = $("#app-view");
    shell.classList.toggle("collapsed");
    try { localStorage.setItem("vq_rail_collapsed", shell.classList.contains("collapsed") ? "1" : "0"); }
    catch { /* tercih kaydı zorunlu değil */ }
  });
  bind("#mobile-menu", () => $("#app-view").classList.toggle("rail-open"));
  // Mobilde menü dışına dokunulunca kapansın
  const appShell = $("#app-view");
  if (appShell && !appShell.dataset.drawerBound) {
    appShell.dataset.drawerBound = "1";
    appShell.addEventListener("click", (event) => {
      if (!appShell.classList.contains("rail-open")) return;
      if (event.target === appShell) appShell.classList.remove("rail-open");
    });
  }
  bind("#open-settings", () => openSettingsCenter());
  bind("#rail-search", () => openCommandPalette());
  bind("#profile-btn", (e) => openProfileMenu(e.currentTarget));
  // Dil desteği iskeleti — TR/EN
  bind("#lang-toggle", (e) => openLanguageMenu(e.currentTarget));
  bind("#theme-toggle", (e) => openThemeMenu(e.currentTarget));
  // `boot()` sistem tercihini DİNLEMEYE başlar: kullanıcı "sistem"de
  // kaldığı sürece işletim sistemi gece moduna geçince uygulama da geçer.
  bootTheme();
  paintThemeButton();
  // Panel genişlikleri kullanıcıya ait ve HATIRLANIR.
  mountRailSplitter();
  refitOnResize();
  try { $("#app-view").classList.toggle("collapsed", localStorage.getItem("vq_rail_collapsed") === "1"); }
  catch { /* gizli sekmede depolama kapalı olabilir */ }
  syncLanguageButton();

  const avatar = $("#profile-avatar");
  if (avatar) avatar.textContent = (state.email || "V")[0].toUpperCase();
  const brand = $("#brand-mark");
  if (brand) brand.innerHTML = BRAND_MARK;
  hydrateIcons(document);
  applyI18n(document);
  observeIcons();

  try {
    const [catalog, providers, meta, health] = await Promise.all([
      api("/api/bots/catalog", { auth: false }),
      api("/api/keys/providers", { auth: false }),
      api("/api/market/meta", { auth: false }),
      api("/api/health", { auth: false }),
    ]);
    state.catalog = catalog;
    state.providers = providers.llm;
    state.meta = meta;
    state.health = health;
    checkServerFreshness(health);
  } catch (err) {
    toast(`Katalog yüklenemedi: ${err.message}`, "err");
  }

  // Disk zamanla dolar; açılıştaki ölçüm birkaç saat sonra eskir.
  // Beş dakikada bir tazelenir — ucuz bir çağrı, sessiz bir arızayı önler.
  setInterval(async () => {
    try { state.health = await api("/api/health", { auth: false }); } catch { /* ağ */ }
  }, 300_000);

  connectSocket();
  readHash();
  await render();
}

async function refreshCurrentView() {
  if (["dashboard", "bots", "bot"].includes(state.route)) await render();
}

window.addEventListener("hashchange", () => { readHash(); render(); });

/** Marka işareti — kaynak görselden üretilmiş şeffaf PNG (yeniden çizim YOK). */
const BRAND_MARK = `<img src="/ui/brand/logo-128.png" alt="ZUMVIA"
  width="26" height="26" class="brand-img" decoding="async" />`;

/** Giriş ekranındaki büyük işaret (yüksek çözünürlük). */
const AUTH_MARK = `<img src="/ui/brand/wordmark-512.png" alt="ZUMVIA"
  width="260" height="260" class="auth-img" decoding="async" />`;

/** Log kategorilerinin okunur karşılıkları (büyük harf dönüşümü sorunsuz). */
const LOG_CATEGORY = {
  data: "veri", math: "hesap", ai: "model", risk: "risk",
  exec: "işlem", engine: "motor",
};

const ROUTE_ICON = {
  chat: "command", dashboard: "portfolio", bots: "bots", bot: "bots",
  finance: "candles",
  systems: "bots", skills: "scales", new: "bots", analysis: "analysis", backtest: "backtest",
  vault: "vault",
};


/** Öğe varsa tıklama bağlar; yoksa sessizce atlar ve konsola not düşer. */
function bind(selector, handler) {
  const node = $(selector);
  if (!node) {
    console.warn(`[arayüz] ${selector} bulunamadı, bağlama atlandı.`);
    return null;
  }
  node.onclick = handler;
  return node;
}


/* --- Paylaşılan yardımcılar (chat.js bu köprüden okur) --- */
window.VQ = {
  state, api, $, $$, el, esc, toast, confirmModal, promptModal, timeOf, dateOf,
  money, pct, cls, navigate, logout, hydrateIcons,
  t, lang, setLang, LANGUAGES, applyI18n,
  openSettingsCenter, openCommandPalette, newChatSession, openChatSession,
  startFinanceChat,
  render: () => render(),
};


/* ------------------------------------------------------------------ görünüm */

/* ==========================================================================
   SİSTEM BOTLARI (PLAYBOOK KÜTÜPHANESİ)
   --------------------------------------------------------------------------
   "Yapay zeka bot yazmaz" ilkesinin görünen yüzü: sistemler kodun içinde
   hazır ve testlidir; ajan yalnızca uygun olanı seçip kurar ve denetler.
   Kullanıcı da buradan doğrudan kurabilir.
   ========================================================================== */

async function viewPlaybooks(root) {
  const data = await api("/api/bots/playbooks");
  const books = data.playbooks || [];

  // Aileye göre grupla: kullanıcı 122 kart yerine 14 yaklaşım görür,
  // her yaklaşımın vade/risk sürümleri kendi içinde açılır.
  const families = new Map();
  books.forEach((pb) => {
    const key = pb.family || pb.id;
    if (!families.has(key)) families.set(key, { base: null, variants: [] });
    const group = families.get(key);
    if ((pb.family || pb.id) === pb.id) group.base = pb;
    else group.variants.push(pb);
  });
  // Çekirdeği silinmiş grup olmasın (özel sistemler ailesizdir)
  families.forEach((group, key) => {
    if (!group.base) group.base = group.variants.shift() || null;
    if (!group.base) families.delete(key);
  });

  // Bu görünüm hem tek başına hem de Botlar sekmesinin İÇİNDE çiziliyor.
  // Kendi `page-pad`ini dayatırsa iç içe kullanımda çift dolgu oluşuyor.
  root.innerHTML = `
    <div>
      <div class="callout mb">
        ${t("bots.intro")}
        <div class="dim small mt">
          ${data.count} ${t("bots.systems")} · ${families.size} ${t("bots.approaches")} ·
          ${data.custom} ${t("bots.customCount")}
        </div>
      </div>

      <div id="pb-evidence"></div>

      <div class="row mb" style="gap:10px;flex-wrap:wrap">
        <input class="input" id="pb-search" placeholder="Sistem, strateji veya etiket ara…"
               style="flex:1;min-width:220px;max-width:420px" />
        <select class="select" id="pb-filter" style="max-width:220px">
          <option value="">${t("bots.approaches")}</option>
          <option value="trend">Trend takibi</option>
          <option value="kırılım">Kırılım</option>
          <option value="yatay">Yatay piyasa</option>
          <option value="savunma">Savunma / kurtarma</option>
          <option value="gün içi">Gün içi</option>
          <option value="özel">Size özel</option>
        </select>
        <span class="spacer"></span>
        <span class="small dim" id="pb-count"></span>
      </div>

      <div class="grid g2" id="pb-grid"></div>
    </div>`;

  const grid = $("#pb-grid");
  renderEvidence();

  // "Özel botlarım": ajanın sizin için tasarladığı ya da hazır bir sistemi
  // temel alıp değiştirdiği sistemler. Cihazınızdaki veritabanında durur.
  const mine = books.filter((pb) => pb.custom);
  if (mine.length) {
    const host = el(`
      <div class="mb">
        <div class="panel-head">
          <div>
            <div class="panel-title">Özel botlarım</div>
            <div class="panel-hint">
              Size özel tasarlanan sistemler — bu cihazdaki veritabanında saklanır,
              hiçbir yere gönderilmez.
            </div>
          </div>
          <span class="tag tag-accent">${mine.length}</span>
        </div>
        <div class="grid g2" id="pb-mine"></div>
      </div>`);
    $("#pb-grid").before(host);
    const mineGrid = host.querySelector("#pb-mine");
    mine.forEach((pb) => mineGrid.appendChild(playbookCard(pb, [])));
    hydrateIcons(host);
  }

  const draw = (query = "", tag = "") => {
    grid.innerHTML = "";
    const q = fold(query.trim());
    let shown = 0;

    families.forEach((group) => {
      const pb = group.base;
      if (pb.custom) return;                    // kendi bölümünde gösteriliyor
      const haystack = fold([pb.label, pb.thesis, ...(pb.strategies || []),
                             ...(pb.tags || [])].join(" "));
      if (q && !haystack.includes(q)) return;
      if (tag && !haystack.includes(fold(tag))) return;
      shown++;
      grid.appendChild(playbookCard(pb, group.variants));
    });

    $("#pb-count").textContent = `${shown} ${t("bots.showing")}`;
    if (!shown) {
      grid.innerHTML = `<div class="panel"><div class="empty">
        Aramanıza uyan sistem yok.</div></div>`;
    }
    hydrateIcons(grid);
  };

  $("#pb-search").addEventListener("input", (e) =>
    draw(e.target.value, $("#pb-filter").value));
  $("#pb-filter").addEventListener("change", (e) =>
    draw($("#pb-search").value, e.target.value));

  draw();
  hydrateIcons(root);
}


/**
 * Görülmemiş veri kanıtı şeridi.
 *
 * Kütüphanenin "kaç sistem var" değil, "kaçı görülmemiş veride geçti"
 * sorusuna cevap verir. Rapor yoksa nasıl üretileceğini söyler — boş bir
 * alan bırakıp kullanıcıyı tahmine zorlamaz.
 */
async function renderEvidence() {
  const host = $("#pb-evidence");
  if (!host) return;

  let report = null;
  try {
    report = await api("/api/bots/evidence");
  } catch {
    return;                                   // uç yoksa şerit de olmasın
  }

  if (!report || !report.available) {
    host.innerHTML = `
      <div class="callout mb small">${t("bots.noEvidence")}</div>`;
    return;
  }

  const passRate = report.pass_rate_pct ?? 0;
  const tone = passRate >= 60 ? "green" : passRate >= 35 ? "" : "red";

  host.innerHTML = `
    <div class="callout ${tone} mb">
      <div class="row" style="gap:16px;flex-wrap:wrap;align-items:baseline">
        <b>${t("bots.evidenceTitle")}</b>
        <span class="small dim">${esc(dateOf(report.generated_at) || "")}</span>
        <span class="spacer"></span>
        <span class="small dim">${esc((report.symbols || []).join(" · "))}</span>
      </div>
      <div class="grid g4 mt" style="gap:10px">
        <div><div class="hstat-n">${report.systems_passed}/${report.systems_measured}</div>
          <div class="hstat-l">${t("bots.passed")}</div></div>
        <div><div class="hstat-n">%${passRate}</div>
          <div class="hstat-l">${t("bots.passRate")}</div></div>
        <div><div class="hstat-n">${report.median_profit_factor}</div>
          <div class="hstat-l">${t("bots.medianPf")}</div></div>
        <div><div class="hstat-n">${report.thresholds?.min_profit_factor ?? "—"}</div>
          <div class="hstat-l">${t("bots.passThreshold")}</div></div>
      </div>
      <div class="small dim mt">${t("bots.evidenceNote")}</div>
    </div>`;
}


/**
 * Görülmemiş veri karnesi rozeti.
 *
 * Bir sistem geçmiş veride kaybettiriyorsa bu kullanıcıdan saklanmaz —
 * kartın üstünde görünür. "122 sistem" sayısı, hangisinin gerçekten
 * çalıştığını bilmeden bir işe yaramaz.
 */
function evidenceBadge(evidence) {
  if (!evidence || !evidence.trades) return "";
  const pf = evidence.profit_factor;
  const good = evidence.passed;
  return `
    <div class="ev-badge ${good ? "ok" : "bad"}" title="${esc(evidence.verdict || "")}">
      <span data-icon="${good ? "shieldCheck" : "alert"}" data-icon-size="12"></span>
      ${t("bots.oos")} <b>${pf}</b>
      <span class="dim">· ${evidence.trades} ${t("bots.tradesWin").replace("{w}", evidence.win_rate)}</span>
    </div>`;
}

/** Tek bir yaklaşımın kartı; vade/risk sürümleri içinde açılır. */
/**
 * Koruma cümlesini SÖZLÜKTEN kurar.
 *
 * Sunucu artık `guards_detail` içinde anahtar+değer gönderiyor; hazır
 * Türkçe cümle yalnızca yedek. 10 şablonu çevirmek 122 sistemin her
 * kartındaki koruma listesini birden çözüyor.
 */
/**
 * Vade etiketi. Sunucu "kısa/orta/uzun" gönderir; kelimeyi arayüz yazar.
 * Üç değer, 122 kartın hepsinde geçiyor.
 */
function horizonLabel(horizon) {
  const key = `horizon.${horizon}`;
  const translated = t(key);
  return translated === key ? horizon : translated;
}

function guardSentences(pb) {
  const detail = pb.guards_detail;
  if (!Array.isArray(detail) || !detail.length) return pb.mitigations || [];
  return detail.map((g) => {
    const key = `guard.${g.key}`;
    const template = t(key);
    if (template === key) return g.text;          // çevirisi yok → sunucu metni
    return template.includes("{v}")
      ? template.replace("{v}", g.value) : template;
  });
}

function playbookCard(pb, variants) {
  const mitigations = pb.mitigations || [];
  const card = el(`
    <div class="panel pb-card">
      <div class="panel-head">
        <div style="min-width:0">
          <div class="panel-title">
            ${esc(pb.label)}
            ${pb.custom ? `<span class="tag tag-accent">${t("bots.custom")}</span>` : ""}
            ${pb.custom && pb.validated === false
              ? `<span class="tag tag-warn">${t("bots.unvalidated")}</span>` : ""}
          </div>
          <div class="panel-hint">${esc(pb.timeframe)} · risk %${pb.risk_pct} ·
            ${pb.min_agree}/${pb.strategies.length} ${t("bots.confirmations")} · ${esc(horizonLabel(pb.horizon))}</div>
          ${evidenceBadge(pb.evidence)}
        </div>
        <div class="row" style="gap:6px;flex-shrink:0">
          ${pb.custom
            ? `<button class="btn btn-sm btn-ghost" data-remove="1" title="Sil"
                       aria-label="Sil" data-icon="trash" data-icon-size="14"></button>`
            : ""}
          <button class="btn btn-sm btn-primary" data-deploy="1">${t("bots.deploy")}</button>
        </div>
      </div>

      <div class="pb-thesis">${esc(pb.thesis)}</div>

      <div class="pb-row"><span class="pb-k">${t("bots.strength")}</span>
        <span class="pb-v">${esc(pb.strength)}</span></div>

      ${mitigations.length ? `
        <div class="pb-guards">
          <div class="pb-guards-head">
            <span data-icon="shieldCheck" data-icon-size="14"></span>
            ${mitigations.length} ${t("bots.guardsActive")}
          </div>
          <ul>${guardSentences(pb).map((m) => `<li>${esc(m)}</li>`).join("")}</ul>
        </div>` : ""}

      <details class="pb-spec">
        <summary>${t("bots.spec")}</summary>
        <div class="pb-row"><span class="pb-k">${t("bots.weakness")}</span>
          <span class="pb-v">${esc(pb.weakness)}</span></div>
        <div class="pb-row"><span class="pb-k">${t("bots.avoidWhen")}</span>
          <span class="pb-v">${esc(pb.avoid_when)}</span></div>
      </details>

      <div class="pb-tags">
        ${pb.strategies.map((x) => `<span class="tag">${esc(x)}</span>`).join("")}
      </div>

      ${variants.length ? `
        <div class="pb-variants">
          <button class="pb-variants-toggle" type="button">
            <span data-icon="chevronRight" data-icon-size="13"></span>
            ${variants.length} ${t("bots.variants")}
          </button>
          <div class="pb-variants-list hidden"></div>
        </div>` : ""}
    </div>`);

  card.querySelector("[data-deploy]").onclick = () => openDeployModal(pb);

  card.querySelector("[data-remove]")?.addEventListener("click", async () => {
    const ok = await confirmModal("Özel sistemi sil",
      `<b>${esc(pb.label)}</b> silinecek. Bu sistemle kurulmuş botlar çalışmaya
       devam eder; yalnızca şablon kaldırılır.`, "Sil");
    if (!ok) return;
    try {
      await api(`/api/bots/custom-playbooks/${encodeURIComponent(pb.id)}`,
                { method: "DELETE" });
      toast(t("msg.customDeleted"), "ok");
      render();
    } catch (err) { toast(err.message, "err"); }
  });

  const toggle = card.querySelector(".pb-variants-toggle");
  if (toggle) {
    const list = card.querySelector(".pb-variants-list");
    toggle.onclick = () => {
      list.classList.toggle("hidden");
      toggle.classList.toggle("open");
      if (list.childElementCount) return;
      variants.forEach((v) => {
        const row = el(`
          <div class="pb-variant">
            <div class="pb-variant-main">
              <div class="pb-variant-name">${esc(v.label.split("·").slice(1).join("·").trim())}</div>
              <div class="pb-variant-sub">${esc(v.timeframe)} · risk %${v.risk_pct} ·
                ${v.min_agree} teyit · günde en fazla ${v.guards?.max_trades_per_day ?? "—"} işlem</div>
            </div>
            <button class="btn btn-sm">Kur</button>
          </div>`);
        row.querySelector("button").onclick = () => openDeployModal(v);
        list.appendChild(row);
      });
      hydrateIcons(list);
    };
  }

  return card;
}

/** Seçilen sistemi sanal modda kurar (parite ve kasa sorulur). */
function openDeployModal(pb) {
  const backdrop = el(`
    <div class="modal-backdrop">
      <div class="panel modal" style="max-width:520px">
        <div class="panel-head">
          <div class="panel-title">${esc(pb.label)} kur</div>
          <button class="btn btn-sm btn-ghost" data-act="close">Kapat</button>
        </div>

        <div class="small dim mb">${esc(pb.thesis)}</div>

        <div class="grid g2">
          <div class="field"><label class="label">Piyasa</label>
            <select class="select" id="pb-market">
              <option value="crypto">Kripto</option>
              <option value="stock">Hisse</option>
              <option value="demo">Demo (veri gerektirmez)</option>
            </select></div>
          <div class="field"><label class="label">Parite / sembol</label>
            <input class="input" id="pb-symbol" value="BTC/USDT" /></div>
        </div>

        <div class="field"><label class="label">Başlangıç kasası (sanal)</label>
          <input class="input mono" type="number" id="pb-balance" value="1000"
                 min="100" step="100" /></div>

        <div class="callout small">
          Bot <b>sanal (paper)</b> modda kurulur. Zaman dilimi, strateji seti ve risk
          sistemin doğrulanmış değerlerinden gelir:
          ${esc(pb.timeframe)} · ${pb.strategies.length} strateji · %${pb.risk_pct} risk.
          <div class="mt"><b>Zayıf yanı:</b> ${esc(pb.weakness)}</div>
        </div>

        <div class="row mt" style="justify-content:flex-end">
          <button class="btn" data-act="close">Vazgeç</button>
          <button class="btn btn-primary" data-act="deploy">Sanal modda kur</button>
        </div>
      </div>
    </div>`);

  backdrop.addEventListener("click", async (e) => {
    const act = e.target.dataset?.act;
    if (act === "close" || e.target === backdrop) return backdrop.remove();
    if (act !== "deploy") return;

    const symbol = $("#pb-symbol", backdrop).value.trim().toUpperCase();
    if (!symbol) return toast(t("msg.needSymbol"), "err");

    // Görülmemiş veride kaybettiren sistem sessizce kurulmaz.
    if (pb.evidence && pb.evidence.trades && !pb.evidence.passed) {
      const proceed = await confirmModal(
        "Bu sistem doğrulamayı geçemedi",
        `<b>${esc(pb.label)}</b> görülmemiş veride kâr faktörü
         <b>${pb.evidence.profit_factor}</b> verdi
         (${pb.evidence.trades} işlem). ${esc(pb.evidence.verdict || "")}
         <div class="mt">Yine de kurmak istiyor musunuz?</div>`,
        "Yine de kur");
      if (!proceed) return;
    }

    e.target.disabled = true;
    try {
      const res = await api("/api/bots/from-playbook", {
        method: "POST",
        body: {
          playbook_id: pb.id,
          market: $("#pb-market", backdrop).value,
          symbol,
          initial_balance: Number($("#pb-balance", backdrop).value) || 1000,
        },
      });
      backdrop.remove();
      toast(`${pb.label} kuruldu — sanal modda çalışıyor.`, "ok");
      navigate(`bot/${res.bot_id}`);
    } catch (err) {
      e.target.disabled = false;
      toast(err.message, "err");
    }
  });

  $("#modal-root").appendChild(backdrop);
}

const VIEWS = {
  chat: { title: "Komuta", tag: "tag.agent", render: viewChat },
  dashboard: { title: "Portföy", tag: "tag.live", render: viewDashboard },
  finance: { title: "Finans", tag: "tag.liveMarket", render: viewFinance },
  bots: { title: "Botlar", tag: "tag.library", render: viewBots },
  // "Sistemler" ve "Yeni bot" ARTIK AYRI SAYFA DEĞİL.
  //
  // Kütüphane Botlar ekranının bir sekmesi oldu; sıfırdan kurma sihirbazı
  // kaldırıldı. Bu iki yol, eski bağlantılar kırılmasın diye kütüphaneye
  // düşürülüyor — ölü bir rota, hata ekranı demektir.
  systems: { title: "Botlar", tag: "kütüphane", render: viewLibraryTab },
  new: { title: "Botlar", tag: "kütüphane", render: viewLibraryTab },
  skills: { title: "Yetenekler", tag: "50+ skill", render: viewSkills },
  bot: { title: "Bot detayı", tag: "canlı", render: viewBotDetail },
  analysis: { title: "Piyasa analizi", tag: "otomatik", render: viewAnalysis },
  backtest: { title: "Geri test", tag: "otomatik", render: viewBacktest },
  // Etiket SABİT DEĞİL: sağlayıcı sayısı listeden gelir, kelime sözlükten.
  // "21 sağlayıcı" yazan sabit bir etiket hem dili hem sayıyı yanlış tutar.
  vault: { title: "Kasa", tag: "vault.tag", render: viewVault },
};

/**
 * Sayfa başlığındaki eylem düğmesi.
 *
 * Doğrudan `appendChild` kullanmak, aynı sayfa iki kez çizildiğinde düğmeyi
 * çoğaltıyordu. Bu yardımcı aynı kimlikli düğme varsa onu değiştirir.
 */
function pageAction(key, label, onClick, kind = "btn-primary") {
  const host = $("#page-actions");
  if (!host) return null;
  host.querySelector(`[data-action="${key}"]`)?.remove();
  const button = el(`<button class="btn ${kind}" data-action="${key}">${esc(label)}</button>`);
  button.onclick = onClick;
  host.appendChild(button);
  return button;
}

/**
 * Dil seçici.
 *
 * Eskiden yalnızca "TR"/"EN" yazısını değiştiren sahte bir düğme vardı; hiçbir
 * metni çevirmiyordu. Artık gerçek sözlüğü kullanır ve seçim anında tüm arayüz
 * yeniden çizilir (Arapça'da yön de sağdan sola döner).
 */
/**
 * TEMA MENÜSÜ — koyu / açık / sistem.
 *
 * Düğme, seçili tercihi DEĞİL o an ekranda olan temayı simgeler: kullanıcı
 * "sistem"deyken bile hangi temada olduğunu bir bakışta görür.
 */
function openThemeMenu(anchor) {
  document.querySelector(".theme-menu")?.remove();
  const current = themePreference();

  const menu = el(`
    <div class="menu theme-menu">
      ${THEMES.map((entry) => `
        <div class="menu-item ${entry.id === current ? "active" : ""}"
             data-theme-set="${entry.id}">
          <span data-icon="${entry.id === current ? "check" : entry.icon}"
                data-icon-size="14"></span>
          ${esc(t(entry.labelKey))}
        </div>`).join("")}
    </div>`);

  const rect = anchor.getBoundingClientRect();
  menu.style.position = "fixed";
  menu.style.top = `${rect.bottom + 8}px`;
  menu.style.left = `${Math.max(10, Math.min(rect.left - 60, window.innerWidth - 190))}px`;
  menu.style.zIndex = 95;
  document.body.appendChild(menu);
  hydrateIcons(menu);

  const close = () => menu.remove();
  setTimeout(() => document.addEventListener("click", close, { once: true }), 0);

  menu.addEventListener("click", (event) => {
    const value = event.target.closest("[data-theme-set]")?.dataset.themeSet;
    if (!value) return;
    setTheme(value);
    close();
    paintThemeButton();
  });
}

/** Düğme simgesi: ekranda olan temayı gösterir, tercihi değil. */
function paintThemeButton() {
  const button = $("#theme-toggle");
  if (!button) return;
  button.innerHTML = icon(activeTheme() === "light" ? "live" : "brake", 16);
}


function openLanguageMenu(anchor) {
  document.querySelector(".lang-menu")?.remove();

  const menu = el(`
    <div class="lang-menu">
      ${LANGUAGES.map((option) => `
        <div class="menu-item ${option.id === lang() ? "active" : ""}" data-lang="${option.id}">
          <span class="lang-code">${option.id.toUpperCase()}</span>
          <span>${esc(option.label)}</span>
          ${option.id === lang() ? `<span class="lang-check" data-icon="check" data-icon-size="14"></span>` : ""}
        </div>`).join("")}
    </div>`);

  const rect = anchor.getBoundingClientRect();
  menu.style.top = `${rect.bottom + 8}px`;
  menu.style.right = `${Math.max(10, window.innerWidth - rect.right)}px`;
  document.body.appendChild(menu);
  hydrateIcons(menu);

  const close = () => {
    menu.remove();
    document.removeEventListener("mousedown", outside, true);
  };
  const outside = (e) => { if (!menu.contains(e.target)) close(); };
  setTimeout(() => document.addEventListener("mousedown", outside, true), 0);

  menu.addEventListener("click", (e) => {
    const code = e.target.closest("[data-lang]")?.dataset.lang;
    if (!code) return;
    close();
    setLang(code);
    syncLanguageButton();
  });
}

/** Başlıktaki düğme her zaman etkin dilin kodunu gösterir. */
function syncLanguageButton() {
  const button = $("#lang-toggle");
  if (button) button.textContent = lang().toUpperCase();
}

/**
 * Türkçe-güvenli metin karşılaştırma.
 *
 * JavaScript'te "İ".toLowerCase() sonucu "i̇" (i + birleşik nokta) olur ve
 * "ince" araması "İnce" metnini bulamaz. Türkçe birincil dil olduğu için bu,
 * aramayı sessizce bozan bir hata sınıfıdır.
 */
const TR_FOLD = { "İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ğ": "g",
                  "ğ": "g", "Ü": "u", "ü": "u", "Ö": "o", "ö": "o", "Ç": "c", "ç": "c" };

function fold(text) {
  return String(text || "")
    .replace(/[İIıŞşĞğÜüÖöÇç]/g, (ch) => TR_FOLD[ch] || ch)
    .toLowerCase()
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "");
}

let renderSeq = 0;

/**
 * SÜRÜM EL SIKIŞMASI — "sunucu arayüzden eski mi?"
 *
 * Bu kontrol, bir kez gerçekten canımızı yaktığı için var.
 *
 * Uvicorn statik dosyaları HER İSTEKTE diskten okur; Python rotalarını ise
 * yalnızca AÇILIŞTA kaydeder. Yani yeni bir ekran eklenip süreç yeniden
 * başlatılmadığında: `finance.js` yüklenir, çalışır, çağırdığı
 * `/api/finance/board` 404 döner. Ekranda "not found" yazar, kodda hata
 * yoktur ve kimse sebebini anlamaz.
 *
 * Artık arayüz beklediği uç gruplarını söyler, sunucu KENDİ ROTA TABLOSUNDAN
 * neyi taşıdığını söyler ve fark varsa bunu tek bir cümleyle bildiririz.
 */
const REQUIRED_CAPABILITIES = [
  "agent", "auth", "bots", "finance", "keys",
  "market", "portfolio", "safety", "skills",
];

function checkServerFreshness(health) {
  const has = health?.capabilities;
  // Yetenek listesi hiç yoksa sunucu bu kontrolden de eski demektir.
  const missing = !has
    ? ["(sunucu yetenek listesi bildirmiyor)"]
    : REQUIRED_CAPABILITIES.filter((c) => !has.includes(c));
  if (!missing.length) return;

  document.querySelector("#stale-server")?.remove();
  const banner = el(`
    <div class="stale-banner" id="stale-server">
      <span data-icon="alert" data-icon-size="16"></span>
      <div>
        <b>Sunucu arayüzden eski.</b>
        Şu bölümler yüklenmeyecek: ${esc(missing.join(", "))}.
        Sunucu sürecini yeniden başlatın — statik dosyalar güncel ama
        rotalar açılışta kaydedildiği için eski kaldı.
      </div>
      <button class="icon-btn" data-close data-icon="close"></button>
    </div>`);
  document.body.appendChild(banner);
  hydrateIcons(banner);
  banner.querySelector("[data-close]").onclick = () => banner.remove();
  console.warn("[zumvia] sunucuda eksik uç grupları:", missing);
}


async function render() {
  // İki render aynı anda başlarsa (örn. navigate + hashchange), eskisi
  // tamamlanınca düğmelerini yeni sayfanın üzerine ekliyordu; "Yeni bot"
  // düğmesinin iki kez görünmesinin sebebi buydu. Sıra numarası ile eski
  // render'in çıktısı yok sayılır.
  const seq = ++renderSeq;
  clearTimers();
  if (state.route !== "finance") stopFinanceTimers();
  const view = VIEWS[state.route] || VIEWS.chat;
  const titleNode = $("#page-title");
  if (titleNode) titleNode.textContent = t(`page.${state.route}`) || view.title;
  const subNode = $("#page-sub");
  // Etiket bir SÖZLÜK ANAHTARI ("tag.live"); çevirisi yoksa olduğu gibi
  // yazılır, böylece yeni bir sayfa eklendiğinde boş kalmaz.
  //
  // Kasa etiketi ayrıca SAYI taşır ve o sayı listeden gelir: sabit yazılan
  // "21 sağlayıcı" hem dili hem sayıyı yanlış tutar.
  if (subNode) {
    subNode.textContent = view.tag === "vault.tag"
      ? `${state.providers?.length ?? 0} ${t("vault.providers")}`
      : view.tag ? t(view.tag) : "zumvia";
  }
  const iconNode = $("#page-icon");
  if (iconNode) iconNode.innerHTML = icon(ROUTE_ICON[state.route] || "command", 14);
  const actions = $("#page-actions");
  if (actions) actions.innerHTML = "";
  $$(".rail-item").forEach((item) =>
    item.classList.toggle("active", item.dataset.route === state.route ||
      (["bot", "systems", "new"].includes(state.route)
       && item.dataset.route === "bots")));

  const host = $("#page-body");
  if (!host) {
    showAppRecovery(new Error("Ana içerik alanı bulunamadı."));
    return;
  }
  host.innerHTML = "";

  // Sohbet tüm alanı kaplar; diğer sayfalar dolgulu kapsayıcıda çizilir.
  const body = state.route === "chat" ? host : el(`<div class="page-pad"></div>`);
  if (body !== host) host.appendChild(body);

  body.innerHTML = `<div class="empty"><div class="spinner"></div>Yükleniyor…</div>`;
  try {
    await view.render(body);
    if (seq !== renderSeq) return;              // araya yeni bir render girdi
    applyI18n(body);
    renderRailSessions();
    hydrateIcons(body);
  } catch (err) {
    if (err instanceof SessionExpired) return;      // giriş ekranı zaten açıldı
    body.innerHTML = `
      <div class="panel">
        <div class="callout red">${esc(err.message || "Beklenmeyen bir hata oluştu.")}</div>
        <button class="btn btn-sm mt" id="retry-view">Tekrar dene</button>
      </div>`;
    const retry = $("#retry-view");
    if (retry) retry.onclick = () => render();
  }
}

/**
 * Başlatma veya çizim kodunda beklenmeyen bir hata olsa bile kullanıcı boş
 * bir yüzeyde bırakılmaz. Kimlik bilgisi silinmez; yeniden deneme ve güvenli
 * Komuta ekranına dönme yolları aynı kabuk içinde kalır.
 */
function showAppRecovery(error) {
  const app = $("#app-view");
  const auth = $("#auth-view");
  const host = $("#page-body");
  auth?.classList.add("hidden");
  app?.classList.remove("hidden");
  if (!host) return;

  host.innerHTML = `
    <div class="page-pad">
      <div class="panel app-recovery" role="alert">
        <div class="panel-title">Görünüm kurtarıldı</div>
        <div class="callout red mt">${esc(error?.message || "Beklenmeyen bir arayüz hatası oluştu.")}</div>
        <div class="row mt">
          <button class="btn btn-primary" id="recover-retry">Tekrar dene</button>
          <button class="btn" id="recover-command">Komuta ekranına dön</button>
        </div>
      </div>
    </div>`;
  $("#recover-retry")?.addEventListener("click", () => render());
  $("#recover-command")?.addEventListener("click", () => navigate("chat"));
  hydrateIcons(host);
}

/* ================================ PANEL ================================== */

async function viewDashboard(root) {
  const [overview, equity, treasury] = await Promise.all([
    api("/api/portfolio/overview"),
    api("/api/portfolio/equity"),
    // Sermaye haritası ayrı bir uçtan gelir çünkü anlık fiyat çeker; özetin
    // hızlı yüklenmesini bekletmemesi için paralel istenir.
    api("/api/portfolio/treasury").catch(() => null),
  ]);
  state.bots = overview.bots;

  const c = overview.capital;
  const p = overview.performance;

  root.innerHTML = `
    <div class="grid g4 mb">
      ${statCard(t("stat.capital"), money(c.total_balance), `${t("stat.start")}: ${money(c.total_initial)}`)}
      ${statCard(t("stat.pnl"), `${c.net_pnl >= 0 ? "+" : ""}${money(c.net_pnl)}`,
        `${t("stat.return")} ${pct(c.return_pct)}`, cls(c.net_pnl))}
      ${statCard(t("stat.winRate"), `%${p.win_rate_pct.toFixed(1)}`,
        `${p.wins} ${t("stat.wins")} / ${p.losses} ${t("stat.losses")} · ${p.total_trades} ${t("stat.trades")}`)}
      ${statCard(t("stat.profitFactor"), p.profit_factor >= 999 ? "∞" : p.profit_factor.toFixed(2),
        `Ort. ${p.avg_r.toFixed(2)}R · DD %${c.drawdown_pct.toFixed(2)}`,
        p.profit_factor >= 1.5 ? "pos" : p.profit_factor >= 1 ? "neutral" : "neg")}
    </div>

    ${treasuryPanel(treasury)}

    <div class="grid g2 mb" style="grid-template-columns: 1.35fr 1fr">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">${t("panel.equityCurve")}</div>
          <div class="panel-hint">${t("panel.allBots")}</div>
        </div>
        <div style="height:260px"><canvas id="equity-canvas" style="width:100%;height:100%"></canvas></div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">Bot filosu</div>
          <div class="panel-hint">${overview.running} ${t("panel.running")} · ${overview.bot_count} ${t("panel.total")}</div>
        </div>
        <div id="fleet"></div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">${t("panel.terminal")}</div>
        <div class="panel-hint">${t("panel.terminalSub")}</div>
      </div>
      <div class="terminal" id="log-terminal"></div>
    </div>`;

  drawEquityCurve($("#equity-canvas"), equity, { baseline: c.total_initial });

  const fleet = $("#fleet");
  if (!overview.bots.length) {
    fleet.innerHTML = `<div class="empty"><div class="big" data-icon="bots" data-icon-size="30"></div>
      ${t("panel.noBots")}<br><span class="small">${t("panel.noBotsHint")}</span></div>`;
  } else {
    overview.bots.forEach((bot) => {
      const node = el(`
        <div class="choice mb" style="cursor:pointer">
          <div class="row">
            <span class="dot ${bot.status === "running" ? "dot-live" : bot.status === "locked" ? "dot-lock" : "dot-off"}"></span>
            <b>${esc(bot.name)}</b>
            <span class="badge badge-slate">${esc(bot.symbol)}</span>
            ${bot.mode === "paper" ? '<span class="badge badge-blue">PAPER</span>'
              : '<span class="badge badge-amber">CANLI</span>'}
            ${bot.recovery_mode ? '<span class="badge badge-amber">TOPARLANMA</span>' : ""}
            <span class="spacer"></span>
            <span class="mono ${cls(bot.return_pct)}">${pct(bot.return_pct)}</span>
          </div>
          <div class="choice-desc">Bakiye ${money(bot.balance)} · ${decisionModeLabel(bot.decision_mode)}</div>
        </div>`);
      node.onclick = () => navigate("bot", bot.id);
      fleet.appendChild(node);
    });
  }

  const terminal = $("#log-terminal");
  state.liveEvents.slice(-60).forEach((e) => terminal.appendChild(logLine(e)));
  if (!state.liveEvents.length) {
    terminal.innerHTML = `<div class="dim small">${t("panel.terminalEmpty")}</div>`;
  }
  terminal.scrollTop = terminal.scrollHeight;

  state.timers.push(setInterval(() => {
    if (state.route === "dashboard") render();
  }, 30000));
}

/**
 * SERMAYE HARİTASI — "param şu an tam olarak nerede?"
 *
 * Üç sayı ve aralarındaki fark, bu ekranın tamamının sebebidir:
 *
 *   BOŞTA     Nakit. Kaybedilemez.
 *   PİYASADA  Pozisyonların içinde. DALGALANIR.
 *   RİSKTE    Stop'lara kadar olan mesafe. Her şey ters giderse
 *             GERÇEKTEN kaybedilecek olan budur.
 *
 * "Piyasada 8.000 dolarım var" cümlesi korkutucudur ve kullanıcıyı iyi bir
 * pozisyonu panikle kapatmaya iter. "Riskte 240 dolarım var" cümlesi
 * gerçektir. Bu yüzden ikisi yan yana ve AYRI gösterilir.
 */
function treasuryPanel(data) {
  if (!data || !data.ozet) return "";
  const o = data.ozet;
  const bar = (label, amount, share, cssClass) => share <= 0 ? "" : `
    <div class="tre-seg ${cssClass}" style="width:${Math.max(share, 3)}%"
         title="${esc(label)}: ${money(amount)} (%${share.toFixed(1)})"></div>`;

  const cashShare = o.nakit + o.piyasada ? o.nakit / (o.nakit + o.piyasada) * 100 : 100;
  const marketShare = 100 - cashShare;

  return `
    <div class="panel mb tre">
      <div class="panel-head">
        <div class="panel-title">${t("tre.title")}</div>
        <div class="panel-hint">${esc(data.sayim.acik_pozisyon)} ${t("tre.openPositions")} ·
          ${esc(data.sayim.calisan)}/${esc(data.sayim.bot)} ${t("tre.botsRunning")}</div>
      </div>

      <div class="tre-line">${esc(treasuryHeadline(o))}</div>

      <div class="tre-bar">
        ${bar(t("tre.idleShort"), o.nakit, cashShare, "cash")}
        ${bar(t("tre.inMarket"), o.piyasada, marketShare, "market")}
      </div>

      <div class="tre-grid">
        ${treasuryCell(t("tre.deposited"), money(o.toplam_yatirilan), t("tre.depositedFoot"))}
        ${treasuryCell(t("tre.equity"), money(o.guncel_ozkaynak),
          `${o.net_kar_zarar >= 0 ? "+" : ""}${money(o.net_kar_zarar)} · ${pct(o.getiri_pct)}`,
          cls(o.net_kar_zarar))}
        ${treasuryCell(t("tre.cash"), money(o.nakit), t("tre.cashFoot"))}
        ${treasuryCell(t("tre.inMarket"), money(o.piyasada), t("tre.inMarketFoot"))}
        ${treasuryCell(t("tre.atRisk"), money(o.riskte),
          `%${o.riskte_pct.toFixed(2)} · ${t("tre.atRiskFoot")}`,
          o.riskte_pct > 5 ? "neg" : "")}
      </div>

      ${data.dagilim.enstrumana_gore.length ? `
        <div class="tre-split">
          <div>
            <div class="tre-sub">${t("tre.byInstrument")}</div>
            ${data.dagilim.enstrumana_gore.slice(0, 6).map((row) => `
              <div class="tre-row">
                <span>${esc(row.etiket)}</span>
                <span class="mono">${money(row.tutar)}
                  <span class="dim">%${row.pay_pct.toFixed(0)}</span></span>
              </div>`).join("")}
          </div>
          <div>
            <div class="tre-sub">${t("tre.byMarket")}</div>
            ${[...data.dagilim.piyasaya_gore, ...data.dagilim.moda_gore]
              .slice(0, 6).map((row) => `
              <div class="tre-row">
                <span>${esc(row.etiket)}</span>
                <span class="mono">${money(row.tutar)}
                  <span class="dim">%${row.pay_pct.toFixed(0)}</span></span>
              </div>`).join("")}
          </div>
        </div>` : `<div class="hint mt">${t("tre.noPositions")}</div>`}

      ${data.uyarilar.map((w) => `
        <div class="callout ${w.includes("GERÇEK PARA") ? "red" : ""} small mt">
          ${esc(w)}
        </div>`).join("")}
    </div>`;
}

/**
 * Sermaye haritasının tek cümlelik özeti.
 *
 * Bu cümle SUNUCUDAN hazır geliyordu ve Türkçeydi — yani çevrilemezdi.
 * Sunucu artık sayıyı verir, cümleyi arayüz kendi dilinde kurar. Aynı
 * ayrımı her yerde uyguluyoruz: sunucu ÖLÇER, arayüz ANLATIR.
 */
function treasuryHeadline(o) {
  if (!o.toplam_yatirilan) return t("tre.noCapital");
  const change = o.net_kar_zarar;
  const verb = change > 0 ? t("tre.gain") : change < 0 ? t("tre.loss") : t("tre.flat");
  return t("tre.headline")
    .replace("{deposited}", money(o.toplam_yatirilan))
    .replace("{equity}", money(o.guncel_ozkaynak))
    .replace("{change}", money(Math.abs(change)))
    .replace("{verb}", verb)
    .replace("{cash}", money(o.nakit))
    .replace("{market}", money(o.piyasada))
    .replace("{risk}", money(o.riskte));
}

function treasuryCell(label, value, foot, valueClass = "") {
  return `
    <div class="tre-cell">
      <div class="tre-label">${esc(label)}</div>
      <div class="tre-value ${valueClass}">${value}</div>
      <div class="tre-foot">${esc(foot)}</div>
    </div>`;
}

function statCard(label, value, foot, valueClass = "") {
  return `
    <div class="panel stat">
      <div class="stat-label">${esc(label)}</div>
      <div class="stat-value ${valueClass}">${value}</div>
      <div class="stat-foot">${foot}</div>
    </div>`;
}

function decisionModeLabel(mode) {
  return {
    hybrid: "Hibrit",
    ai_first: "Yapay zeka öncelikli",
    algo_only: "Yalnızca algoritma",
    ai_only: "Yalnızca yapay zeka",
  }[mode] || mode;
}

/* =============================== BOTLARIM ================================ */

/** Eski `systems` / `new` bağlantılarını kütüphane sekmesine düşürür. */
async function viewLibraryTab(root) {
  state.botsTab = "library";
  await viewBots(root);
}


/**
 * BOTLAR — çalışanlar ve kütüphane tek yerde.
 *
 * Eskiden burada "Yeni bot" sihirbazı vardı ve kullanıcıyı sıfırdan sistem
 * kurmaya davet ediyordu. Oysa kütüphanede 122 hazır sistem duruyor: her
 * biri kendi zayıf yanını kapatan korumalarla geliyor ve geçmiş veride
 * sınanmış. Elle kurulan bir bot, bunların hiçbirine sahip değildi.
 *
 * Artık akış tek yönlü: kütüphaneden seç → Kur → çalışıyor.
 */
async function viewBots(root) {
  const tab = state.botsTab || "running";
  const bots = await api("/api/bots");
  state.bots = bots;

  // Hiç botu olmayan kullanıcı doğrudan kütüphanede açılır: boş bir liste
  // gösterip "bir yerlerden bul" demek, aracın işini kullanıcıya yıkmaktır.
  const active = bots.length ? tab : "library";

  root.innerHTML = `
    <div class="tabs mb" id="bots-tabs">
      <div class="tab ${active === "running" ? "active" : ""}" data-bots-tab="running">
        ${t("bots.tabRunning")}${bots.length ? ` (${bots.length})` : ""}
      </div>
      <div class="tab ${active === "library" ? "active" : ""}" data-bots-tab="library">
        ${t("bots.tabLibrary")}
      </div>
    </div>
    <div id="bots-body"></div>`;

  root.querySelectorAll("[data-bots-tab]").forEach((node) => {
    node.addEventListener("click", () => {
      state.botsTab = node.dataset.botsTab;
      render();
    });
  });

  const body = $("#bots-body");
  if (active === "library") {
    await viewPlaybooks(body);
    applyI18n(body);
    hydrateIcons(body);
    return;
  }
  renderRunningBots(body, bots);
}


/** Çalışan botların kartları. */
function renderRunningBots(root, bots) {
  if (!bots.length) {
    root.innerHTML = `<div class="panel"><div class="empty">
      <div class="big" data-icon="bots" data-icon-size="30"></div>
      ${t("bots.emptyTitle")}<br>
      <span class="small dim">${t("bots.emptyHint")}</span>
    </div></div>`;
    hydrateIcons(root);
    return;
  }

  root.innerHTML = `<div class="grid g2" id="bot-grid"></div>`;
  const grid = $("#bot-grid");

  bots.forEach((bot) => {
    const card = el(`
      <div class="panel">
        <div class="panel-head">
          <div class="row">
            <span class="dot ${bot.status === "running" ? "dot-live" : bot.status === "locked" ? "dot-lock" : "dot-off"}"></span>
            <div>
              <div class="panel-title">${esc(bot.name)}</div>
              <div class="panel-hint">${esc(bot.symbol)} · ${esc(bot.timeframe)} · ${esc(bot.exchange)}</div>
            </div>
          </div>
          <div class="row">
            ${bot.mode === "paper" ? '<span class="badge badge-blue">PAPER</span>'
              : '<span class="badge badge-amber">CANLI</span>'}
            ${bot.risk.recovery_mode ? '<span class="badge badge-amber">TOPARLANMA</span>' : ""}
          </div>
        </div>

        <div class="grid g3 mb" style="gap:10px">
          <div><div class="stat-label">Bakiye</div><div class="mono" style="font-size:17px">${money(bot.capital.balance)}</div></div>
          <div><div class="stat-label">Getiri</div><div class="mono ${cls(bot.capital.total_return_pct)}" style="font-size:17px">${pct(bot.capital.total_return_pct)}</div></div>
          <div><div class="stat-label">İşlem</div><div class="mono" style="font-size:17px">${bot.stats.total_trades}</div></div>
        </div>

        <div class="row small dim mb">
          <span class="badge badge-slate">${decisionModeLabel(bot.decision_mode)}</span>
          <span class="badge badge-slate">Risk %${bot.risk.effective_risk_pct}</span>
          <span class="badge badge-slate">${bot.stats.open_positions} açık</span>
        </div>

        ${bot.risk.locked_until ? `<div class="callout red small mb">Kilitli: ${esc(bot.risk.lock_reason)}</div>` : ""}

        <div class="row">
          <button class="btn btn-sm" data-act="open">Detay</button>
          ${bot.status === "running"
            ? '<button class="btn btn-sm btn-danger" data-act="stop">Durdur</button>'
            : '<button class="btn btn-sm btn-primary" data-act="start">Başlat</button>'}
          <button class="btn btn-sm btn-ghost" data-act="once">Tek tur çalıştır</button>
          ${bot.risk.locked_until ? '<button class="btn btn-sm" data-act="unlock">Kilidi Aç</button>' : ""}
          <span class="spacer"></span>
          <button class="btn btn-sm btn-ghost" data-act="delete" title="Botu sil"><span data-icon="trash" data-icon-size="14"></span></button>
        </div>
      </div>`);

    card.addEventListener("click", async (e) => {
      const act = e.target.dataset?.act;
      if (!act) return;
      try {
        if (act === "open") return navigate("bot", bot.id);
        if (act === "start") { await api(`/api/bots/${bot.id}/start`, { method: "POST" }); toast(t("msg.botStarted")); }
        if (act === "stop") { await api(`/api/bots/${bot.id}/stop`, { method: "POST" }); toast(t("msg.botStopped")); }
        if (act === "unlock") { await api(`/api/bots/${bot.id}/unlock`, { method: "POST" }); toast(t("msg.unlocked")); }
        if (act === "once") {
          const r = await api(`/api/bots/${bot.id}/run-once`, { method: "POST" });
          toast(`Tur tamamlandı: ${r.action || r.error || "—"}`);
        }
        if (act === "delete") {
          const ok = await confirmModal("Botu sil",
            `<b>${esc(bot.name)}</b> ve tüm işlem geçmişi kalıcı olarak silinecek. Bu işlem geri alınamaz.`, "Kalıcı olarak sil");
          if (!ok) return;
          await api(`/api/bots/${bot.id}`, { method: "DELETE" });
          toast(t("msg.botDeleted"));
        }
        render();
      } catch (err) {
        toast(err.message, "err");
      }
    });
    grid.appendChild(card);
  });
}

/* ============================= BOT DETAYI ================================ */

async function viewBotDetail(root) {
  const id = state.routeParam;
  const [bot, positions, events, equity] = await Promise.all([
    api(`/api/bots/${id}`),
    api(`/api/bots/${id}/positions`),
    api(`/api/bots/${id}/events?limit=150`),
    api(`/api/bots/${id}/equity`),
  ]);

  $("#page-title").textContent = bot.name;
  $("#page-sub").textContent = `${bot.symbol} · ${bot.timeframe} · ${decisionModeLabel(bot.decision_mode)}`;

  pageAction("back", "Botlara dön", () => navigate("bots"), "btn-ghost btn-sm");
  const runBtn = pageAction("run-once", "Tek tur çalıştır", null, "btn-sm");
  runBtn.onclick = async () => {
    runBtn.disabled = true; runBtn.textContent = "Analiz ediliyor…";
    try {
      const r = await api(`/api/bots/${id}/run-once`, { method: "POST" });
      toast(`Sonuç: ${r.action || r.error}`);
      render();
    } catch (err) { toast(err.message, "err"); runBtn.disabled = false; }
  };
  const running = bot.status === "running";
  const toggle = pageAction("toggle", running ? "Durdur" : "Başlat", null,
                            running ? "btn-sm btn-danger" : "btn-sm btn-primary");
  toggle.onclick = async () => {
    try {
      await api(`/api/bots/${id}/${bot.status === "running" ? "stop" : "start"}`, { method: "POST" });
      render();
    } catch (err) { toast(err.message, "err"); }
  };
  pageAction("bot-settings", "Bot ayarları", () => openSettingsModal(bot), "btn-sm btn-ghost");

  const openPos = positions.open[0];
  root.innerHTML = `
    ${bot.risk.locked_until ? `<div class="callout red mb"><b>Devre kesici aktif:</b> ${esc(bot.risk.lock_reason)}
      <button class="btn btn-sm mt" id="unlock-btn">Kilidi Kaldır</button></div>` : ""}
    ${bot.risk.recovery_mode ? `<div class="callout mb"><b>Toparlanma modu aktif.</b>
      Risk yarıya indirildi (%${bot.risk.effective_risk_pct}), güven eşiği %${(bot.risk.effective_min_confidence * 100).toFixed(0)}'e yükseltildi.
      Sistem yalnızca A+ kurulumları alacak.</div>` : ""}

    <div class="grid g4 mb">
      ${statCard("Bakiye", money(bot.capital.balance), `Zirve ${money(bot.capital.peak_equity)}`)}
      ${statCard("Getiri", pct(bot.capital.total_return_pct), `Gerçekleşen ${money(bot.capital.realized_pnl)}`, cls(bot.capital.total_return_pct))}
      ${statCard("Kazanma Oranı", `%${bot.stats.win_rate_pct.toFixed(1)}`, `${bot.stats.wins} kazanç / ${bot.stats.losses} zarar`)}
      ${statCard("Durum", bot.status === "running" ? "ÇALIŞIYOR" : bot.status === "locked" ? "KİLİTLİ" : "DURDU",
        bot.next_run_at ? `Sonraki tur ${timeOf(bot.next_run_at)}` : `Son tur ${timeOf(bot.last_run_at)}`,
        bot.status === "running" ? "pos" : bot.status === "locked" ? "neg" : "neutral")}
    </div>

    <div class="panel mb">
      <div class="panel-head">
        <div class="panel-title">${esc(bot.symbol)} · ${esc(bot.timeframe)}</div>
        <div class="chart-legend">
          <span style="color:#38bdf8">— EMA 50</span>
          <span style="color:#f0a5ff">— EMA 200</span>
          <span style="color:#00f59b">-- Supertrend</span>
          ${openPos ? `<span style="color:#facc15">— Giriş</span><span style="color:#ff4d6d">— Stop</span><span style="color:#00e676">— Hedef</span>` : ""}
        </div>
      </div>
      <div class="chart-wrap" id="chart-wrap"></div>
    </div>

    <div class="grid g2 mb" style="grid-template-columns: 1fr 1fr">
      <div class="panel">
        <div class="panel-head"><div class="panel-title">Açık pozisyonlar</div>
          <div class="panel-hint">${positions.open.length} açık · ${positions.pending.length} onay bekliyor</div></div>
        <div id="open-positions"></div>
      </div>
      <div class="panel">
        <div class="panel-head"><div class="panel-title">Sermaye eğrisi</div></div>
        <div style="height:220px"><canvas id="equity-canvas" style="width:100%;height:100%"></canvas></div>
      </div>
    </div>

    <div class="panel mb">
      <div class="panel-head">
        <div class="panel-title">Karar terminali</div>
        <div class="panel-hint">Veri - Matematik - Algoritma - Yapay Zeka - Risk - İcra</div>
      </div>
      <div class="terminal" id="log-terminal"></div>
    </div>

    <div class="panel">
      <div class="panel-head"><div class="panel-title">İşlem geçmişi</div>
        <div class="panel-hint">${positions.closed.length} kapanmış işlem</div></div>
      <div style="overflow-x:auto">
        <table class="table">
          <thead><tr>
            <th>Zaman</th><th>Yön</th><th class="num">Giriş</th><th class="num">Çıkış</th>
            <th class="num">Miktar</th><th class="num">PnL</th><th class="num">R</th><th>Sebep</th>
          </tr></thead>
          <tbody id="history-body"></tbody>
        </table>
      </div>
    </div>`;

  const unlockBtn = $("#unlock-btn");
  if (unlockBtn) unlockBtn.onclick = async () => {
    await api(`/api/bots/${id}/unlock`, { method: "POST" });
    toast(t("msg.unlocked")); render();
  };

  // Grafik
  const chart = new CandleChart($("#chart-wrap"));
  state.chart = chart;
  const candles = await api(
    `/api/market/candles?market=${bot.market}&exchange=${bot.exchange}&symbol=${encodeURIComponent(bot.symbol)}&timeframe=${bot.timeframe}&limit=300`
  );
  chart.setData(candles);
  if (openPos) {
    chart.setLevels([
      { price: openPos.entry_price, color: "#facc15", label: "GİRİŞ" },
      { price: openPos.stop_loss, color: "#ff4d6d", label: "STOP" },
      { price: openPos.take_profit, color: "#00e676", label: "HEDEF" },
    ]);
  }

  drawEquityCurve($("#equity-canvas"), equity, { baseline: bot.capital.initial_balance });

  // Pozisyonlar
  const openBox = $("#open-positions");
  const all = [...positions.pending, ...positions.open];
  if (!all.length) {
    openBox.innerHTML = `<div class="empty small"><div class="spinner"></div>
      Açık pozisyon yok. Bot uygun kurulum bekliyor.</div>`;
  }
  all.forEach((p) => {
    const isPending = p.status === "pending";
    const unreal = p.unrealized_pnl ?? 0;
    const node = el(`
      <div class="choice mb">
        <div class="row mb">
          <span class="badge ${p.side === "long" ? "badge-green" : "badge-red"}">
            ${p.side === "long" ? "LONG" : "SHORT"}</span>
          <b>${esc(p.symbol)}</b>
          ${isPending ? '<span class="badge badge-amber">ONAY BEKLİYOR</span>' : ""}
          <span class="spacer"></span>
          ${isPending ? "" : `<b class="mono ${cls(unreal)}">${unreal >= 0 ? "+" : ""}${money(unreal)}</b>`}
        </div>
        <div class="grid g3 small mono" style="gap:8px">
          <div><span class="dim">Giriş</span><br>${formatNumber(p.entry_price)}</div>
          <div><span class="dim">Stop</span><br><span class="neg">${formatNumber(p.stop_loss)}</span></div>
          <div><span class="dim">Hedef</span><br><span class="pos">${formatNumber(p.take_profit)}</span></div>
          <div><span class="dim">Miktar</span><br>${formatNumber(p.qty)}</div>
          <div><span class="dim">Risk</span><br>${money(p.risk_amount)}</div>
          <div><span class="dim">Güven</span><br>%${(p.confidence * 100).toFixed(0)}</div>
        </div>
        ${p.reasoning ? `<div class="small dim mt">${esc(p.reasoning)}</div>` : ""}
        <div class="row mt">
          ${isPending
            ? `<button class="btn btn-sm btn-primary" data-act="approve">Onayla ve aç</button>
               <button class="btn btn-sm btn-danger" data-act="reject">Reddet</button>`
            : `<button class="btn btn-sm btn-danger" data-act="close">Pozisyonu Kapat</button>`}
        </div>
      </div>`);

    node.addEventListener("click", async (e) => {
      const act = e.target.dataset?.act;
      if (!act) return;
      try {
        if (act === "close") {
          const ok = await confirmModal("Pozisyonu kapat",
            "Pozisyon güncel piyasa fiyatından kapatılacak.", "Kapat");
          if (!ok) return;
          await api(`/api/bots/${id}/positions/close`, { method: "POST", body: { position_id: p.id } });
        } else {
          await api(`/api/bots/${id}/positions/approve`, {
            method: "POST", body: { position_id: p.id, approve: act === "approve" },
          });
        }
        toast(t("msg.done")); render();
      } catch (err) { toast(err.message, "err"); }
    });
    openBox.appendChild(node);
  });

  // Terminal
  const terminal = $("#log-terminal");
  events.forEach((e) => terminal.appendChild(logLine(e)));
  if (!events.length) terminal.innerHTML = `<div class="dim small">Henüz kayıt yok — "Tek Tur Çalıştır" ile deneyin.</div>`;
  terminal.scrollTop = terminal.scrollHeight;

  // Geçmiş
  const body = $("#history-body");
  if (!positions.closed.length) {
    body.innerHTML = `<tr><td colspan="8" class="dim center">Kapanmış işlem yok.</td></tr>`;
  }
  positions.closed.forEach((p) => {
    body.appendChild(el(`
      <tr>
        <td class="small dim">${dateOf(p.closed_at)}</td>
        <td><span class="badge ${p.side === "long" ? "badge-green" : "badge-red"}">${p.side === "long" ? "LONG" : "SHORT"}</span></td>
        <td class="num">${formatNumber(p.entry_price)}</td>
        <td class="num">${formatNumber(p.exit_price)}</td>
        <td class="num">${formatNumber(p.qty)}</td>
        <td class="num ${cls(p.pnl)}">${p.pnl >= 0 ? "+" : ""}${money(p.pnl)}</td>
        <td class="num ${cls(p.r_multiple)}">${p.r_multiple.toFixed(2)}R</td>
        <td class="small">${esc(closeReasonLabel(p.close_reason))}</td>
      </tr>`));
  });

  state.timers.push(setInterval(() => { if (state.route === "bot") render(); }, 45000));
}

function closeReasonLabel(reason) {
  return {
    TAKE_PROFIT: "Hedefe ulaştı", STOP_LOSS: "Stop-loss", AI_CLOSE: "Yapay zeka çıkışı",
    CIRCUIT_BREAKER: "Devre kesici", MANUAL: "Manuel kapanış", TRAILING_STOP: "İz süren stop",
  }[reason] || reason;
}

/* ==================== YENİ BOT SİHİRBAZI (KALDIRILDI) ==================== */
/*
   Burada 3 adımlı bir 'sıfırdan bot kur' sihirbazı vardı; kaldırıldı.

   Sebebi: kütüphanede 122 hazır sistem duruyor ve her biri kendi zayıf
   yanını kapatan korumalarla, geçmiş veride sınanmış olarak geliyor.
   Sihirbazla elle kurulan bot bunların HİÇBİRİNE sahip değildi — yani
   kullanıcıyı, hazır duran korumalı bir şeyi korumasız yeniden kurmaya
   davet ediyorduk.

   Yeni akış: Botlar → Kütüphane → Kur. `systems` ve `new` rotaları da
   oraya düşürülüyor, böylece eski bağlantılar kırılmıyor.
*/

/* ============================ AYAR MODALI ================================ */

function openSettingsModal(bot) {
  const llmKeys = state.credentials.filter((c) => c.kind === "llm");
  const exKeys = state.credentials.filter((c) => c.kind === "exchange");
  const tgKeys = state.credentials.filter((c) => c.kind === "telegram");
  const strategies = state.catalog?.strategies || [];
  const active = new Set(bot.strategies);

  const backdrop = el(`
    <div class="modal-backdrop">
      <div class="panel modal">
        <div class="panel-head">
          <div class="panel-title">${esc(bot.name)} · Ayarlar</div>
          <button class="btn btn-sm btn-ghost" data-act="close"></button>
        </div>

        <div class="grid g2">
          <div class="field"><label class="label">Bot adı</label>
            <input class="input" id="s-name" value="${esc(bot.name)}" /></div>
          <div class="field"><label class="label">Tarama sıklığı (saniye)</label>
            <input class="input mono" type="number" id="s-poll" value="${bot.poll_seconds}" min="30" step="30" /></div>
        </div>

        <div class="grid g2">
          <div class="field"><label class="label">Karar mimarisi</label>
            <select class="select" id="s-decision">
              ${(state.catalog?.decision_modes || []).map((m) =>
                `<option value="${m.id}" ${m.id === bot.decision_mode ? "selected" : ""}>${esc(m.label)}</option>`).join("")}
            </select></div>
          <div class="field"><label class="label">Otonomi seviyesi</label>
            <select class="select" id="s-autonomy">
              ${(state.catalog?.autonomy_modes || []).map((m) =>
                `<option value="${m.id}" ${m.id === bot.autonomy ? "selected" : ""}>${esc(m.label)}</option>`).join("")}
            </select></div>
        </div>

        <div class="grid g3">
          <div class="field"><label class="label">Yapay zeka anahtarı</label>
            <select class="select" id="s-llm">
              <option value="">— yok —</option>
              ${llmKeys.map((k) => `<option value="${k.id}" ${k.id === bot.llm_credential_id ? "selected" : ""}>${esc(k.label)}</option>`).join("")}
            </select></div>
          <div class="field"><label class="label">Borsa anahtarı (gerçek para için)</label>
            <select class="select" id="s-exchange">
              <option value="">— yok —</option>
              ${exKeys.map((k) => `<option value="${k.id}" ${k.id === bot.exchange_credential_id ? "selected" : ""}>${esc(k.label)}</option>`).join("")}
            </select></div>
          <div class="field"><label class="label">Telegram bildirimi</label>
            <select class="select" id="s-telegram">
              <option value="">— kapalı —</option>
              ${tgKeys.map((k) => `<option value="${k.id}" ${k.id === bot.telegram_credential_id ? "selected" : ""}>${esc(k.label)}</option>`).join("")}
            </select></div>
        </div>

        <div class="field"><label class="label">Model adı</label>
          <input class="input mono" id="s-model" value="${esc(bot.llm_model)}" placeholder="örn. gemini-2.5-flash" /></div>

        <div class="panel-title mt mb">Aktif stratejiler (algoritmik motor)</div>
        <div class="grid g2" id="s-strategies">
          ${strategies.map((s) => `
            <label class="switch mb">
              <input type="checkbox" data-strategy="${s.id}" ${active.has(s.id) ? "checked" : ""} />
              <span class="track"></span><span class="small">${esc(s.label)}</span>
            </label>`).join("")}
        </div>

        <div class="grid g3 mt">
          <div class="field"><label class="label">Konsensüs eşiği (kaç strateji)</label>
            <input class="input mono" type="number" id="s-agree" value="${bot.min_agree}" min="1" max="8" /></div>
          <div class="field"><label class="label">İşlem Riski (%) — tavan ${state.catalog?.hard_limits.max_risk_pct}</label>
            <input class="input mono" type="number" id="s-risk" value="${bot.risk.risk_pct}" step="0.1" min="0.1" /></div>
          <div class="field"><label class="label">Minimum güven (0-1)</label>
            <input class="input mono" type="number" id="s-conf" value="${bot.risk.min_confidence}" step="0.01" min="0.75" max="0.99" /></div>
        </div>

        <div class="grid g3">
          <div class="field"><label class="label">Minimum risk/ödül</label>
            <input class="input mono" type="number" id="s-rr" value="${bot.risk.min_rr}" step="0.1" min="2" /></div>
          <div class="field"><label class="label">En fazla eşzamanlı pozisyon</label>
            <input class="input mono" type="number" id="s-maxpos" value="${bot.risk.max_open_positions}" min="1" max="10" /></div>
          <div class="field"><label class="label">En fazla düşüş — drawdown (%)</label>
            <input class="input mono" type="number" id="s-dd" value="${bot.risk.max_drawdown_pct}" step="1" min="1" /></div>
        </div>

        <div class="row mb">
          <label class="switch"><input type="checkbox" id="s-short" ${bot.allow_short ? "checked" : ""} />
            <span class="track"></span><span class="small">Short işlemlere izin ver</span></label>
          <label class="switch"><input type="checkbox" id="s-trail" ${bot.risk.trailing_stop ? "checked" : ""} />
            <span class="track"></span><span class="small">İz süren stop</span></label>
        </div>

        <div class="field"><label class="label">Strateji notlarınız (ajana iletilir)</label>
          <textarea class="textarea" id="s-notes" placeholder="örn. Sadece Avrupa seansında işlem aç. Hafta sonu pozisyon taşıma.">${esc(bot.strategy_notes)}</textarea></div>

        <div class="callout red mb">
          <b>Gerçek para modu:</b> ${bot.mode === "live" ? "AÇIK — gerçek paranızla işlem yapılıyor." :
            "Kapalı (paper). Açmak için borsa anahtarı seçip aşağıdaki anahtarı etkinleştirin."}
          <div class="mt"><label class="switch"><input type="checkbox" id="s-live" ${bot.mode === "live" ? "checked" : ""} />
            <span class="track"></span><span class="small">Gerçek para ile işlem yap</span></label></div>
        </div>

        <div class="row" style="justify-content:flex-end">
          <button class="btn" data-act="close">Vazgeç</button>
          <button class="btn btn-primary" data-act="save">Kaydet</button>
        </div>
      </div>
    </div>`);

  backdrop.addEventListener("click", async (e) => {
    const act = e.target.dataset?.act;
    if (act === "close" || e.target === backdrop) return backdrop.remove();
    if (act !== "save") return;

    const wantsLive = $("#s-live", backdrop).checked;
    if (wantsLive && bot.mode !== "live") {
      const ok = await confirmModal("Gerçek para modu",
        "Bot bundan sonra <b>gerçek paranızla</b> emir iletecek. Risk kalkanı çalışmaya devam eder, " +
        "ancak piyasa riski gerçektir ve kayıplar kalıcıdır.<br><br>" +
        "En az 30 gün paper trading yaptığınızdan ve sonuçların hedef metrikleri karşıladığından emin misiniz?",
        "Evet, gerçek parayla başlat");
      if (!ok) return;
    }

    const patch = {
      name: $("#s-name", backdrop).value.trim(),
      poll_seconds: Number($("#s-poll", backdrop).value),
      decision_mode: $("#s-decision", backdrop).value,
      autonomy: $("#s-autonomy", backdrop).value,
      llm_credential_id: Number($("#s-llm", backdrop).value) || null,
      exchange_credential_id: Number($("#s-exchange", backdrop).value) || null,
      telegram_credential_id: Number($("#s-telegram", backdrop).value) || null,
      llm_model: $("#s-model", backdrop).value.trim(),
      strategies: $$("[data-strategy]", backdrop).filter((c) => c.checked).map((c) => c.dataset.strategy),
      min_agree: Number($("#s-agree", backdrop).value),
      risk_pct: Number($("#s-risk", backdrop).value),
      min_confidence: Number($("#s-conf", backdrop).value),
      min_rr: Number($("#s-rr", backdrop).value),
      max_open_positions: Number($("#s-maxpos", backdrop).value),
      max_drawdown_pct: Number($("#s-dd", backdrop).value),
      allow_short: $("#s-short", backdrop).checked,
      trailing_stop: $("#s-trail", backdrop).checked,
      strategy_notes: $("#s-notes", backdrop).value,
      mode: wantsLive ? "live" : "paper",
    };

    try {
      await api(`/api/bots/${bot.id}`, { method: "PATCH", body: patch });
      backdrop.remove();
      toast(t("msg.settingsSaved"));
      render();
    } catch (err) { toast(err.message, "err"); }
  });

  $("#modal-root").appendChild(backdrop);
}

/* ============================ PİYASA ANALİZİ ============================= */

async function viewAnalysis(root) {
  const defaults = { market: "crypto", exchange: "binance", symbol: "BTC/USDT", timeframe: "1h" };

  root.innerHTML = `
    <div class="panel mb">
      <div class="row">
        <select class="select" id="a-market" style="max-width:150px">
          <option value="crypto">Kripto</option><option value="stock">Hisse</option><option value="demo">Demo</option>
        </select>
        <input class="input mono" id="a-symbol" value="${defaults.symbol}" style="max-width:190px" />
        <select class="select" id="a-timeframe" style="max-width:110px">
          ${(state.meta?.timeframes || ["1h"]).map((tf) =>
            `<option ${tf === "1h" ? "selected" : ""}>${tf}</option>`).join("")}
        </select>
        <button class="btn btn-primary" id="a-run">Analiz et</button>
        <span class="spacer"></span>
        <span class="small dim">Bu analiz yapay zeka çağırmaz — anında ve ücretsizdir.</span>
      </div>
    </div>
    <div id="a-result"></div>`;

  $("#a-run").onclick = runAnalysis;
  await runAnalysis();

  async function runAnalysis() {
    const market = $("#a-market").value;
    const exchange = market === "crypto" ? "binance" : market === "stock" ? "yfinance" : "demo";
    const symbol = $("#a-symbol").value.trim().toUpperCase();
    const timeframe = $("#a-timeframe").value;
    const box = $("#a-result");
    box.innerHTML = `<div class="empty"><div class="spinner"></div>Göstergeler hesaplanıyor…</div>`;

    try {
      const data = await api(
        `/api/market/analyze?market=${market}&exchange=${exchange}&symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}`);
      const s = data.snapshot;
      const bias = data.technical_bias;
      const consensus = data.consensus;

      box.innerHTML = `
        <div class="grid g4 mb">
          ${statCard("Fiyat", formatNumber(s.price), esc(s.symbol))}
          ${statCard("Rejim", s.regime.replaceAll("_", " "), `ADX ${s.indicators.adx_14?.toFixed(1) ?? "—"}`)}
          ${statCard("Teknik Skor", `${bias.score > 0 ? "+" : ""}${bias.score}`, bias.label, cls(bias.score))}
          ${statCard("Konsensüs", consensus.action, consensus.summary,
            consensus.action === "BUY" ? "pos" : consensus.action === "SELL" ? "neg" : "neutral")}
        </div>

        <div class="grid g2 mb">
          <div class="panel">
            <div class="panel-head"><div class="panel-title">Deterministik göstergeler</div>
              <div class="panel-hint">Katman 2 · %0 halüsinasyon</div></div>
            <div class="grid g2" style="gap:8px">
              ${Object.entries(s.indicators).map(([k, v]) => `
                <div class="row small" style="justify-content:space-between">
                  <span class="dim">${esc(k)}</span><b class="mono">${formatNumber(v)}</b>
                </div>`).join("")}
            </div>
          </div>

          <div class="panel">
            <div class="panel-head"><div class="panel-title">Piyasa yapısı</div></div>
            <div class="grid" style="gap:8px">
              ${Object.entries(s.structure).map(([k, v]) => `
                <div class="row small" style="justify-content:space-between">
                  <span class="dim">${esc(k.replaceAll("_", " "))}</span>
                  <b>${typeof v === "boolean" ? (v ? "evet" : "hayır") : esc(v)}</b>
                </div>`).join("")}
            </div>
            <div class="panel-title mt mb">Seviyeler</div>
            <div class="small">
              <div class="dim">Direnç: <b class="mono neg">${s.resistance.map(formatNumber).join(" · ") || "—"}</b></div>
              <div class="dim">Destek: <b class="mono pos">${s.support.map(formatNumber).join(" · ") || "—"}</b></div>
            </div>
            ${data.order_book?.available ? `
              <div class="panel-title mt mb">Emir Defteri</div>
              <div class="small dim">Baskı: <b>${esc(data.order_book.pressure)}</b> ·
                Dengesizlik: <b class="mono ${cls(data.order_book.imbalance_pct)}">${data.order_book.imbalance_pct}%</b></div>` : ""}
          </div>
        </div>

        <div class="panel mb">
          <div class="panel-head"><div class="panel-title">Strateji motoru oylaması</div>
            <div class="panel-hint">Her strateji bağımsız karar verir, motor ağırlıklı oyla birleştirir</div></div>
          <table class="table">
            <thead><tr><th>Strateji</th><th>Karar</th><th class="num">Güven</th><th class="num">Ağırlık</th><th>Gerekçe</th></tr></thead>
            <tbody>
              ${consensus.signals.map((sig) => `
                <tr>
                  <td>${esc(sig.name)}</td>
                  <td><span class="badge ${sig.action === "BUY" ? "badge-green" : sig.action === "SELL" ? "badge-red" : "badge-slate"}">${sig.action}</span></td>
                  <td class="num">${sig.confidence ? `%${(sig.confidence * 100).toFixed(0)}` : "—"}</td>
                  <td class="num">${sig.weight}</td>
                  <td class="small dim">${esc(sig.reason)}</td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>

        <div class="panel">
          <div class="panel-head"><div class="panel-title">Grafik</div></div>
          <div class="chart-wrap" id="a-chart"></div>
        </div>`;

      const chart = new CandleChart($("#a-chart"));
      state.chart = chart;
      chart.setData(await api(
        `/api/market/candles?market=${market}&exchange=${exchange}&symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=300`));
      if (consensus.action !== "WAIT" && consensus.stop_loss) {
        chart.setLevels([
          { price: s.price, color: "#facc15", label: "GİRİŞ" },
          { price: consensus.stop_loss, color: "#ff4d6d", label: "STOP" },
          { price: consensus.take_profit, color: "#00e676", label: "HEDEF" },
        ]);
      }
    } catch (err) {
      box.innerHTML = `<div class="panel"><div class="callout red">${esc(err.message)}</div></div>`;
    }
  }
}

/* =============================== GERİ TEST =============================== */

async function viewBacktest(root) {
  root.innerHTML = `
    <div class="panel mb">
      <div class="grid g4">
        <div class="field"><label class="label">Piyasa</label>
          <select class="select" id="b-market">
            <option value="crypto">Kripto</option><option value="stock">Hisse</option><option value="demo">Demo</option>
          </select></div>
        <div class="field"><label class="label">Sembol</label>
          <input class="input mono" id="b-symbol" value="BTC/USDT" /></div>
        <div class="field"><label class="label">Zaman dilimi</label>
          <select class="select" id="b-timeframe">
            ${(state.meta?.timeframes || ["1h"]).map((tf) => `<option ${tf === "1h" ? "selected" : ""}>${tf}</option>`).join("")}
          </select></div>
        <div class="field"><label class="label">Mum sayısı</label>
          <input class="input mono" type="number" id="b-candles" value="1000" min="300" max="1000" /></div>
      </div>
      <div class="grid g4">
        <div class="field"><label class="label">Sermaye</label>
          <input class="input mono" type="number" id="b-balance" value="1000" /></div>
        <div class="field"><label class="label">İşlem riski (%)</label>
          <input class="input mono" type="number" id="b-risk" value="1.0" step="0.1" max="1.5" /></div>
        <div class="field"><label class="label">Konsensüs eşiği</label>
          <input class="input mono" type="number" id="b-agree" value="2" min="1" max="8" /></div>
        <div class="field"><label class="label">&nbsp;</label>
          <label class="switch"><input type="checkbox" id="b-short" /><span class="track"></span>
          <span class="small">Short dahil</span></label></div>
      </div>
      <button class="btn btn-primary" id="b-run">Geri testi çalıştır</button>
      <span class="small dim" style="margin-left:12px">Look-ahead yok · komisyon ve slipaj dahil</span>
    </div>
    <div id="b-result"></div>`;

  $("#b-run").onclick = async (e) => {
    const market = $("#b-market").value;
    e.target.disabled = true;
    e.target.textContent = "Simülasyon çalışıyor…";
    const box = $("#b-result");
    box.innerHTML = `<div class="empty"><div class="spinner"></div>Bar bar simüle ediliyor…</div>`;
    try {
      const report = await api("/api/market/backtest", {
        method: "POST",
        body: {
          market,
          exchange: market === "crypto" ? "binance" : market === "stock" ? "yfinance" : "demo",
          symbol: $("#b-symbol").value.trim().toUpperCase(),
          timeframe: $("#b-timeframe").value,
          candles: Number($("#b-candles").value),
          initial_balance: Number($("#b-balance").value),
          risk_pct: Number($("#b-risk").value),
          min_agree: Number($("#b-agree").value),
          allow_short: $("#b-short").checked,
        },
      });
      renderBacktest(box, report);
    } catch (err) {
      box.innerHTML = `<div class="panel"><div class="callout red">${esc(err.message)}</div></div>`;
    } finally {
      e.target.disabled = false;
      e.target.textContent = "Geri testi çalıştır";
    }
  };
}

function renderBacktest(box, report) {
  const m = report.metrics;
  if (!m.trade_count) {
    box.innerHTML = `<div class="panel"><div class="callout">${esc(m.note || "İşlem oluşmadı.")}</div></div>`;
    return;
  }
  const verdictClass = report.verdict.startsWith("HAZIR") ? "green"
    : report.verdict.startsWith("KABUL") ? "" : "red";

  box.innerHTML = `
    <div class="panel mb"><div class="callout ${verdictClass}">${esc(report.verdict)}</div></div>

    <div class="grid g4 mb">
      ${statCard("Toplam Getiri", pct(m.total_return_pct), `${money(report.initial_balance)} - ${money(report.final_balance)}`, cls(m.total_return_pct))}
      ${statCard("Kazanma Oranı", `%${m.win_rate_pct}`, `${m.trade_count} işlem`, m.win_rate_pct >= 55 ? "pos" : "neutral")}
      ${statCard("Kâr Faktörü", m.profit_factor, `Beklenti ${m.expectancy_R}R`, m.profit_factor >= 1.8 ? "pos" : m.profit_factor >= 1 ? "neutral" : "neg")}
      ${statCard("Maks. Drawdown", `%${m.max_drawdown_pct}`, `Sharpe ${m.sharpe_ratio}`, m.max_drawdown_pct <= 15 ? "pos" : "neg")}
    </div>

    <div class="grid g2 mb">
      <div class="panel">
        <div class="panel-head"><div class="panel-title">Sermaye eğrisi</div></div>
        <div style="height:240px"><canvas id="bt-canvas" style="width:100%;height:100%"></canvas></div>
      </div>
      <div class="panel">
        <div class="panel-head"><div class="panel-title">Ayrıntılı metrikler</div></div>
        <div class="grid g2" style="gap:9px">
          ${Object.entries(m).map(([k, v]) => `
            <div class="row small" style="justify-content:space-between">
              <span class="dim">${esc(k)}</span><b class="mono">${typeof v === "number" ? formatNumber(v) : esc(v)}</b>
            </div>`).join("")}
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head"><div class="panel-title">İşlemler</div>
        <div class="panel-hint">Son ${Math.min(report.trades.length, 60)} işlem</div></div>
      <div style="overflow-x:auto;max-height:420px">
        <table class="table">
          <thead><tr><th>Giriş</th><th>Çıkış</th><th>Yön</th><th class="num">Giriş F.</th>
            <th class="num">Çıkış F.</th><th class="num">PnL</th><th class="num">R</th><th>Sebep</th><th>Strateji</th></tr></thead>
          <tbody>
            ${report.trades.slice(-60).reverse().map((t) => `
              <tr>
                <td class="small dim">${esc(String(t.entry_time).slice(0, 16))}</td>
                <td class="small dim">${esc(String(t.exit_time).slice(0, 16))}</td>
                <td><span class="badge ${t.side === "long" ? "badge-green" : "badge-red"}">${t.side.toUpperCase()}</span></td>
                <td class="num">${formatNumber(t.entry)}</td>
                <td class="num">${formatNumber(t.exit)}</td>
                <td class="num ${cls(t.pnl)}">${t.pnl >= 0 ? "+" : ""}${money(t.pnl)}</td>
                <td class="num ${cls(t.r_multiple)}">${t.r_multiple.toFixed(2)}R</td>
                <td class="small">${esc(closeReasonLabel(t.reason))}</td>
                <td class="small dim">${esc(t.strategy)}</td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>
    </div>`;

  drawEquityCurve($("#bt-canvas"), report.equity_curve, { baseline: report.initial_balance });
}

/* ============================ YETENEKLER ============================= */

async function viewSkills(root) {
  const data = await api("/api/skills");
  const skills = data.skills || [];
  const cats = ["risk","analiz","yürütme","otomasyon","veri","kurtarma"];
  const catIcons = { risk:"shieldCheck", analiz:"analysis", yürütme:"live", otomasyon:"council", veri:"database", kurtarma:"recovery" };
  const catLabels = { risk:"Risk & Koruma", analiz:"Analiz", yürütme:"Yürütme", otomasyon:"Otomasyon", veri:"Veri", kurtarma:"Kurtarma" };
  const levelColor = { temel:"badge-slate", ileri:"badge-blue", uzman:"badge-green" };
  let filter = "all";
  root.innerHTML = `
    <div class="callout green mb">Sistemde <b>${skills.length} yetenek</b> hazır — model yetenek YAZMAZ, yalnızca uygun olanı <b>seçer</b>. Geri test, tarama, doğrulama dahil her şeyi ajan kendisi yapar; siz sadece bot/skill/model/hesabı izlersiniz.</div>
    <div class="row mb" id="skill-filters"></div>
    <div class="grid g3" id="skill-grid"></div>
  `;
  const filters = $("#skill-filters");
  const grid = $("#skill-grid");
  const render = () => {
    filters.innerHTML = "";
    [["all","Tümü"], ...cats.map(c=>[c,catLabels[c]])].forEach(([id,label])=>{
      const b = document.createElement("button");
      b.className = `btn btn-sm ${filter===id?"btn-primary":""}`;
      b.innerHTML = `<span data-icon="${id==="all"?"scales":catIcons[id]}" data-icon-size="13"></span> ${label}`;
      b.onclick = ()=>{ filter=id; render(); };
      filters.appendChild(b);
    });
    grid.innerHTML = "";
    skills.filter(s=>filter==="all"||s.category===filter).forEach(s=>{
      const card = document.createElement("div");
      card.className = "panel";
      card.innerHTML = `
        <div style="display:flex;gap:10px;align-items:flex-start">
          <span data-icon="${esc(s.icon)}" data-icon-size="18" style="color:var(--mint);margin-top:2px"></span>
          <div style="flex:1;min-width:0">
            <div class="panel-title" style="font-size:14.5px">${esc(s.label)}</div>
            <div class="small dim">${esc(s.category)} · <span class="badge ${levelColor[s.level]||'badge-slate'}">${esc(s.level)}</span> <span class="badge badge-slate">${esc(s.cost)}</span></div>
          </div>
        </div>
        <div class="small dim mt" style="line-height:1.55">${esc(s.description)}</div>
        <div class="small mt"><span class="dim">Ne zaman:</span> ${esc(s.when_used)}</div>
      `;
      grid.appendChild(card);
    });
    hydrateIcons(filters); hydrateIcons(grid);
  };
  render();
}

/* ============================ ANAHTAR KASASI ============================= */

async function viewVault(root) {
  const credentials = await api("/api/keys");
  state.credentials = credentials;

  root.innerHTML = `
    <div class="callout green mb">${t("vault.encrypted")}</div>

    <div class="grid g2 mb">
      <div class="panel">
        <div class="panel-head"><div class="panel-title">${t("vault.addLlm")}</div></div>
        <div class="field"><label class="label">${t("vault.provider")}</label>
          <select class="select" id="k-provider">
            ${state.providers.map((p) => `<option value="${p.id}">${esc(p.label)}</option>`).join("")}
          </select>
          <div class="hint" id="k-note"></div>
        </div>
        <div class="field"><label class="label">Etiket</label>
          <input class="input" id="k-label" placeholder="${t('vault.labelPlaceholderLlm')}" /></div>
        <div class="field" id="k-key-field"><label class="label">${t("vault.apiKey")}</label>
          <input class="input mono" id="k-api" type="password" placeholder="sk-… / AIza…" /></div>
        <div class="field"><label class="label">Model</label>
          <select class="select" id="k-model"></select>
          <input class="input mono hidden" id="k-model-free"
                 placeholder="${t('vault.modelPlaceholder')}" />
          <div class="hint" id="k-model-hint"></div>
        </div>
        <div class="field hidden" id="k-url-field"><label class="label">Base URL</label>
          <input class="input mono" id="k-url" placeholder="http://localhost:11434/v1" /></div>
        <button class="btn btn-primary btn-block" id="k-save">${t("vault.encryptSave")}</button>
      </div>

      <div class="panel">
        <div class="panel-head"><div class="panel-title">${t("vault.exchangeSection")}</div></div>
        <div class="tabs">
          <div class="tab active" data-vault-tab="exchange">Borsa</div>
          <div class="tab" data-vault-tab="telegram">Telegram</div>
          <div class="tab" data-vault-tab="social">X / Twitter</div>
        </div>
        <div id="vault-secondary"></div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head"><div class="panel-title">${t("vault.saved")}</div>
        <div class="panel-hint">${credentials.length} ${t("vault.records")}</div></div>
      <div id="key-list"></div>
    </div>`;

  // --- LLM formu ---
  const providerSelect = $("#k-provider");
  const modelSelect = $("#k-model");
  const syncProvider = () => {
    const provider = state.providers.find((p) => p.id === providerSelect.value);
    const presetModels = provider?.models || [];
    const freeInput = $("#k-model-free");

    $("#k-note").textContent = provider?.note || "";

    // Hazır model listesi olmayan sağlayıcılarda model adı ELLE yazılır.
    modelSelect.innerHTML = presetModels.map((m) => `<option>${m}</option>`).join("");
    modelSelect.classList.toggle("hidden", presetModels.length === 0);
    freeInput.classList.toggle("hidden", presetModels.length > 0);
    $("#k-model-hint").textContent = presetModels.length
      ? t("vault.modelListed") : t("vault.modelManual");

    const needsUrl = provider?.needs_key === false || provider?.id === "custom";
    $("#k-url-field").classList.toggle("hidden", !needsUrl);
    $("#k-url").value = provider?.base_url || "";
    $("#k-key-field").classList.toggle("hidden", provider?.needs_key === false);
    if (!$("#k-label").value) $("#k-label").value = provider?.label || "";
  };
  providerSelect.onchange = syncProvider;
  syncProvider();

  $("#k-save").onclick = async () => {
    const chosenModel = modelSelect.classList.contains("hidden")
      ? $("#k-model-free").value.trim()
      : modelSelect.value;
    if (!chosenModel) {
      toast(t("msg.needModel"), "err");
      return;
    }
    try {
      await api("/api/keys", {
        method: "POST",
        body: {
          kind: "llm",
          provider: providerSelect.value,
          label: $("#k-label").value.trim() || providerSelect.value,
          api_key: $("#k-api").value.trim(),
          model: (modelSelect.classList.contains("hidden")
            ? $("#k-model-free").value.trim()
            : modelSelect.value),
          base_url: $("#k-url").value.trim(),
        },
      });
      toast(t("msg.keySaved"));
      render();
    } catch (err) { toast(err.message, "err"); }
  };

  // --- borsa / telegram ---
  const secondary = $("#vault-secondary");
  const drawSecondary = (kind) => {
    secondary.innerHTML = kind === "exchange" ? `
      <div class="field"><label class="label">Borsa</label>
        <select class="select" id="x-provider">
          ${(state.meta?.exchanges || ["binance"]).map((x) => `<option ${x === "binance" ? "selected" : ""}>${x}</option>`).join("")}
        </select></div>
      <div class="field"><label class="label">Etiket</label><input class="input" id="x-label" placeholder="${t('vault.labelPlaceholderExchange')}" /></div>
      <div class="field"><label class="label">API Key</label><input class="input mono" id="x-key" type="password" /></div>
      <div class="field"><label class="label">Secret</label><input class="input mono" id="x-secret" type="password" /></div>
      <div class="field"><label class="label">Passphrase (OKX/KuCoin)</label><input class="input mono" id="x-pass" type="password" /></div>
      <label class="switch mb"><input type="checkbox" id="x-sandbox" /><span class="track"></span>
        <span class="small">${t("vault.testnet")}</span></label>
      <div class="callout red small mb">
        ${t("vault.exchangeWarn")}
      </div>
      <button class="btn btn-primary btn-block" id="x-save">${t("vault.encryptSave")}</button>`
      : kind === "social" ? `
      <div class="field"><label class="label">Etiket</label><input class="input" id="s-label" value="X / Twitter" /></div>
      <div class="field"><label class="label">Bearer Token</label><input class="input mono" id="s-token" type="password" placeholder="AAAAAAAAAA…" /></div>
      <div class="hint mb">
        <b>developer.x.com</b> → Projects &amp; Apps → uygulamanızı açın →
        <b>Keys and tokens</b> → <b>Bearer Token</b>. Ücretsiz katman yeterlidir
        ama aylık okuma sınırı düşüktür.
      </div>
      <div class="callout small mb">
        Anahtar eklendiğinde ajan hesapları <b>resmî API üzerinden</b> okur.
        Anahtar yoksa herkese açık aynalar denenir — ölçtük, çoğu kapandı;
        o durumda ajan hesabı okuyamadığını açıkça söyler.
      </div>
      <button class="btn btn-primary btn-block" id="s-save">${t("vault.encryptSave")}</button>`
      : `
      <div class="field"><label class="label">Etiket</label><input class="input" id="t-label" value="Telegram" /></div>
      <div class="field"><label class="label">Bot Token</label><input class="input mono" id="t-token" type="password" placeholder="123456:ABC-DEF…" /></div>
      <div class="field"><label class="label">Chat ID</label><input class="input mono" id="t-chat" placeholder="123456789" /></div>
      <div class="hint mb">@BotFather ile bot oluşturun, sonra @userinfobot ile chat id'nizi öğrenin.</div>
      <button class="btn btn-primary btn-block" id="t-save">${t("vault.encryptSave")}</button>`;

    const save = async (body) => {
      try {
        await api("/api/keys", { method: "POST", body });
        toast(t("msg.saved")); render();
      } catch (err) { toast(err.message, "err"); }
    };

    $("#x-save", secondary)?.addEventListener("click", () => save({
      kind: "exchange", provider: $("#x-provider").value,
      label: $("#x-label").value.trim() || $("#x-provider").value,
      api_key: $("#x-key").value.trim(), secret: $("#x-secret").value.trim(),
      password: $("#x-pass").value.trim(), sandbox: $("#x-sandbox").checked,
    }));

    $("#t-save", secondary)?.addEventListener("click", () => save({
      kind: "telegram", provider: "telegram",
      label: $("#t-label").value.trim() || "Telegram",
      token: $("#t-token").value.trim(), chat_id: $("#t-chat").value.trim(),
    }));

    $("#s-save", secondary)?.addEventListener("click", () => save({
      kind: "social", provider: "x",
      label: $("#s-label").value.trim() || "X / Twitter",
      token: $("#s-token").value.trim(),
    }));
  };
  drawSecondary("exchange");
  $$("[data-vault-tab]").forEach((tab) => {
    tab.onclick = () => {
      $$("[data-vault-tab]").forEach((t) => t.classList.toggle("active", t === tab));
      drawSecondary(tab.dataset.vaultTab);
    };
  });

  // --- kayıtlı anahtarlar ---
  const list = $("#key-list");
  if (!credentials.length) {
    list.innerHTML = `<div class="empty small"><div class="big" data-icon="key" data-icon-size="30"></div>
      ${t("vault.emptyHint")}</div>`;
  }
  credentials.forEach((cred) => {
    const icon = { llm: "brain", exchange: "database", telegram: "bell",
                   social: "news" }[cred.kind] || "key";
    const node = el(`
      <div class="choice mb">
        <div class="row">
          <span class="row-ico" data-icon="${icon}" data-icon-size="15"></span>
          <b>${esc(cred.label)}</b>
          <span class="badge badge-slate">${esc(cred.provider)}</span>
          <span class="mono small dim">${esc(cred.hint)}</span>
          <span class="spacer"></span>
          <button class="btn btn-sm" data-act="test">Bağlantıyı test et</button>
          <button class="btn btn-sm btn-ghost" data-act="delete"></button>
        </div>
      </div>`);
    node.addEventListener("click", async (e) => {
      const act = e.target.dataset?.act;
      if (!act) return;
      try {
        if (act === "test") {
          e.target.disabled = true; e.target.textContent = "Test ediliyor…";
          const r = await api("/api/keys/test", { method: "POST", body: { credential_id: cred.id } });
          toast(r.message, r.ok ? "ok" : "err");
          e.target.disabled = false; e.target.textContent = "Bağlantıyı test et";
        }
        if (act === "delete") {
          const ok = await confirmModal("Anahtarı sil", `<b>${esc(cred.label)}</b> silinecek.`, "Sil");
          if (!ok) return;
          await api(`/api/keys/${cred.id}`, { method: "DELETE" });
          toast(t("msg.deleted")); render();
        }
      } catch (err) { toast(err.message, "err"); }
    });
    list.appendChild(node);
  });
}

/* ================================ başlat ================================= */

initAuthScreen();
bindPaletteShortcut();
observeIcons();
if (state.token) {
  bootApp().catch((err) => {
    // Yalnızca gerçek 401 akışı oturumu kapatır; çizim, katalog veya panel
    // hatası kullanıcının kimliğini silmemelidir. `api()` 401'de zaten
    // `logout({expired:true})` çağırır ve SessionExpired fırlatır.
    if (err instanceof SessionExpired) return;
    console.error("Uygulama başlatılamadı", err);
    showAppRecovery(err);
    toast(`Uygulama yüklenemedi: ${err?.message || "bilinmeyen hata"}`, "err");
  });
}

// Son savunma: modül dışındaki bir olay işleyicisi hata verip ana alanı
// boşaltırsa beyaz ekran yerine kurtarma yüzeyi göster. Oturum bilgisine
// dokunulmaz; yalnızca görünüm yeniden kurulabilir hâle getirilir.
window.addEventListener("unhandledrejection", (event) => {
  if (!state.token || $("#app-view")?.classList.contains("hidden")) return;
  const host = $("#page-body");
  if (host && host.childElementCount > 0) return;
  const reason = event.reason instanceof Error
    ? event.reason : new Error(String(event.reason || "Beklenmeyen hata"));
  console.error("İşlenmeyen arayüz hatası", reason);
  showAppRecovery(reason);
});
