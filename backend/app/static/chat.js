/* ==========================================================================
   ZUMVIA — KOMUTA (agentic chat)
   --------------------------------------------------------------------------
   Kullanıcı tek cümle yazar. Ajan piyasayı okur, algoritmik motorla çapraz
   doğrular, haberlere bakar, botu kurar/çalıştırır ve HER ADIMI burada gösterir.

   Paylaşılan yardımcılar `window.VQ` köprüsünden gelir (app.js doldurur).
   ========================================================================== */

import { icon, TOOL_ICON } from "/ui/icons.js";
import { mountLiveSplitter } from "/ui/layout.js";
import { lang, t } from "/ui/i18n.js";
import {
  bindChatDropZone, bindTabs as bindWorkspaceTabs, loadReports,
  hydrateBrowserMessages, render as renderWorkspace,
  tabsMarkup as workspaceTabs, workspaceState,
} from "/ui/workspace.js";
import {
  afterSend as afterModeSend, applyPlaceholder, loadModeCatalog,
  messagePayload, modeState, openModeMenu, renderContextBar, resetModes,
} from "/ui/modes.js";

/**
 * ÇALIŞMA MODLARI — ajan bu turda ne YAPABİLİR.
 *
 * Eskiden burada dört seçenek vardı (otomatik/tek model/konsey/sağlamcı) ve
 * hepsi aynı şeyi anlatıyordu: karar KALİTESİ. Hiçbiri kullanıcının asıl
 * merak ettiğini söylemiyordu — "bu ajan şimdi bir şey yapacak mı?"
 *
 * Ayrıca ayrı bir "otonom" düğmesi vardı ve iki çelişkili durum üretiyordu:
 * Uygula modunda otonomu kapatmak (kurduğunu izlemeyen ajan) ve Sor modunda
 * açmak (hiçbir şey yapamayan ama sürekli uyanan ajan). Artık otonomluk
 * moddan türüyor.
 */
const WORK_MODES = ["ask", "plan", "agent"].map((id) => ({
  id,
  // Getter: dil değiştiğinde etiket de değişsin. Sabit bir dizeyle
  // saklansaydı, dili değiştiren kullanıcı eski dilde kalırdı.
  get label() { return t(`work.${id}.label`); },
  get hint() { return t(`work.${id}.hint`); },
}));

const chatState = {
  sessionId: null,
  sessions: [],
  catalog: null,
  credentials: [],
  status: "idle",
};

/* ========================================================================== */
/*  Sol kolondaki oturum listesi                                              */
/* ========================================================================== */

export function renderRailSessions() {
  const { $, el, esc, render, state } = window.VQ;
  const host = $("#rail-sessions");
  if (!host) return;

  host.innerHTML = "";
  if (!chatState.sessions.length) {
    host.appendChild(el(`<div class="rail-group-title">${t("rail.noTasks")}</div>`));
    return;
  }

  host.appendChild(el(`<div class="rail-group-title">${t("rail.tasks")}</div>`));
  chatState.sessions.forEach((s) => {
    const active = s.id === chatState.sessionId && state.route === "chat";
    const dot = s.status === "idle" ? "dot-off" : s.status === "error" ? "dot-lock" : "dot-live";
    const row = el(`
      <div class="session-row ${active ? "active" : ""}">
        <span class="dot ${dot}"></span>
        <span class="label">${esc(s.title)}</span>
        ${s.work_mode === "agent"
          ? `<span class="row-flag" title="Uygula modu — ajan 7/24 kendi kendine denetliyor">•</span>`
          : ""}
        <button class="session-more" data-more="${s.id}" title="${t('common.options')}" aria-label="${t('common.options')}" type="button">
          <span class="more-dots">⋯</span>
        </button>
      </div>`);
    // Ana alana tıklama — navigasyon, sayfa YENİLEMEZ
    row.addEventListener("click", (e) => {
      if (e.target.closest("[data-more]")) return;
      e.preventDefault();
      chatState.sessionId = s.id;
      if (state.route !== "chat") return window.VQ.navigate("chat");
      render();
    });
    // 3 nokta — sil / yeniden adlandır menüsü
    const moreBtn = row.querySelector("[data-more]");
    moreBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      openSessionMenu(s, moreBtn);
    });
    host.appendChild(row);
  });
}

function openSessionMenu(session, anchor) {
  document.querySelector(".session-menu")?.remove();
  const { el, esc, api, toast } = window.VQ;
  const rect = anchor.getBoundingClientRect();
  const menu = el(`
    <div class="session-menu menu" style="min-width:180px">
      <div class="menu-item" data-act="open">${icon("command", 13)} ${t("common.open")}</div>
      <div class="menu-item" data-act="delete">${icon("trash", 13)} ${t("common.delete")}</div>
    </div>`);
  menu.style.position = "fixed";
  menu.style.left = Math.min(rect.left, window.innerWidth - 190) + "px";
  menu.style.top = (rect.bottom + 6) + "px";
  menu.style.zIndex = 95;
  document.body.appendChild(menu);
  const close = () => { menu.remove(); document.removeEventListener("mousedown", onOut, true); };
  const onOut = (e) => { if (!menu.contains(e.target)) close(); };
  setTimeout(() => document.addEventListener("mousedown", onOut, true), 0);
  menu.addEventListener("click", async (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    close();
    if (act === "open") {
      chatState.sessionId = session.id;
      if (window.VQ.state.route !== "chat") window.VQ.navigate("chat");
      else window.VQ.render();
      return;
    }
    if (act === "delete") {
      const ok = await window.VQ.confirmModal("Görevi sil", `<b>${esc(session.title)}</b> silinecek.`, "Sil");
      if (!ok) return;
      try {
        await api(`/api/agent/sessions/${session.id}`, { method: "DELETE" });
        if (chatState.sessionId === session.id) chatState.sessionId = null;
        toast(t("msg.taskDeleted"));
        window.VQ.render();
      } catch (err) { toast(err.message, "err"); }
    }
  });
}

/**
 * Finans ekranındaki "Ajana sor" düğmesi.
 *
 * Yeni bir görev açar ve CANLI PİYASA modunu seçer: ajan konuşmaya
 * kullanıcının ekranda gördüğü sayılarla başlar. Soruyu kullanıcı yazar —
 * onun adına bir cümle uydurup göndermek, sormadığı bir şeyi sormuş gibi
 * davranmaktır.
 */
export async function startFinanceChat(symbol, market) {
  const { navigate, $ } = window.VQ;
  resetModes();
  await loadModeCatalog();
  modeState.mode = "market";
  chatState.sessionId = null;
  navigate("chat");
  setTimeout(() => {
    renderContextBar(null);
    const input = $("#chat-input");
    if (input) {
      input.value = `${symbol} hakkında ne düşünüyorsun? `;
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  }, 220);
}


/** Belirli bir görevi açar (komut paleti ve bildirimlerden çağrılır). */
export function openChatSession(sessionId) {
  if (chatState.sessionId !== sessionId) resetModes();
  chatState.sessionId = sessionId;
  if (window.VQ.state.route !== "chat") return window.VQ.navigate("chat");
  return window.VQ.render();
}

export async function newChatSession() {
  const { api, navigate, toast, state } = window.VQ;
  try {
    const credentials = await api("/api/keys");
    const llmKeys = credentials.filter((c) => c.kind === "llm");
    chatState.credentials = credentials;

    const cred = llmKeys[0];
    const provider = state.providers?.find((p) => p.id === cred?.provider);
    const session = await api("/api/agent/sessions", {
      method: "POST",
      body: {
        title: "Yeni Görev",
        control_tool: "api",
        control_credential_id: cred?.id ?? null,
        control_model: cred?.extra?.model || provider?.models?.[0] || "",
        sub_credential_id: cred?.id ?? null,
        sub_model: provider?.models?.[1] || cred?.extra?.model || "",
        council_mode: localStorage.getItem("vq_council_mode") || "auto",
        // Yeni görev, kullanıcının son çalışma modunda açılır; hiç seçim
        // yapmadıysa en güvenli olanda ("Sor").
        work_mode: localStorage.getItem("vq_work_mode") || "ask",
        mandate: {},
        heartbeat_seconds: 900,
      },
    });
    chatState.sessions.unshift(session);
    chatState.sessionId = session.id;
    navigate("chat");
    return session;
  } catch (err) {
    toast(err.message, "err");
    return null;
  }
}

/* ========================================================================== */
/*  Ana görünüm                                                               */
/* ========================================================================== */

export async function viewChat(root) {
  const { api } = window.VQ;

  const [catalog, sessions, credentials] = await Promise.all([
    api("/api/agent/catalog", { auth: false }),
    api("/api/agent/sessions"),
    api("/api/keys"),
  ]);
  chatState.catalog = catalog;
  chatState.sessions = sessions;
  chatState.credentials = credentials;

  const llmKeys = credentials.filter((c) => c.kind === "llm");
  if (!chatState.sessionId || !sessions.some((s) => s.id === chatState.sessionId)) {
    chatState.sessionId = sessions[0]?.id ?? null;
  }
  let session = sessions.find((s) => s.id === chatState.sessionId) || null;

  let messages = [];
  if (session) {
    try {
      messages = await api(`/api/agent/sessions/${session.id}/messages`);
    } catch (err) {
      // Silinmiş veya yetkisiz oturum — sessizce temizle, hata ekranı gösterme
      if (String(err.message).includes("404") || String(err.message).includes("bulunam")) {
        chatState.sessions = chatState.sessions.filter((x) => x.id !== session.id);
        chatState.sessionId = chatState.sessions[0]?.id ?? null;
        if (!chatState.sessionId) {
          root.innerHTML = homeShell(catalog, llmKeys.length > 0);
          mountComposer(null, llmKeys);
          return;
        }
        // Kalan oturumla yeniden dene
        try {
          messages = await api(`/api/agent/sessions/${chatState.sessionId}/messages`);
          session = chatState.sessions.find((x) => x.id === chatState.sessionId) || null;
        } catch { messages = []; }
      } else {
        throw err;
      }
    }
  }

  hydrateBrowserMessages(messages, session?.id ?? null);

  renderRailSessions();

  // Konuşma başlamışsa çalışma görünümü, başlamamışsa karşılama ekranı.
  if (session && messages.length) {
    root.innerHTML = workShell();
    mountComposer(session, llmKeys);
    mountWorkspace(root);          // sekmeler ve sürükle-bırak burada da bağlanmalı
    const inner = window.VQ.$("#chat-inner");
    messages.forEach((m) => inner.appendChild(chatMessageNode(m)));
    scrollToEnd();
    updateAgentStatus(session.status);
    // İş tezgâhı genişliği kullanıcıya ait: her çizimde yeniden bağlanır
    // çünkü düzen DOM'u baştan yazılıyor.
    mountLiveSplitter();
    refreshLivePanel();
  } else {
    root.innerHTML = homeShell(catalog, llmKeys.length > 0);
    mountComposer(session, llmKeys);
  }
}

/** Hero işareti — yüksek çözünürlüklü şeffaf PNG. */
const BRAND_MARK_LG = `<img src="/ui/brand/logo-256.png" alt="ZUMVIA"
  width="112" height="112" class="hero-img" decoding="async" />`;

/* ------------------------------ ana sayfa -------------------------------- */

function homeShell(catalog, hasKeys) {
  const cap = catalog?.capabilities || {};
  const stats = [
    [cap.strategies ?? 12, t('home.stat.strategies')],
    [cap.playbooks ?? 9, t('home.stat.playbooks')],
    [cap.tools ?? 30, t('home.stat.tools')],
    [`%${cap.max_risk_pct ?? 1.5}`, t('home.stat.risk')],
  ];

  return `
    <div class="cmd cmd-home">
      <div class="home-scroll">
        <div class="home-hero">
          <div class="mark">${BRAND_MARK_LG}</div>
          <div class="hero-glass">
            <h1>${t('home.title')}</h1>
            <p class="home-sub">${t("home.sub")}</p>
          </div>

          <div class="composer home-composer">
            <textarea id="chat-input" rows="2"
              placeholder="${t('home.placeholder')}"></textarea>
            <div class="composer-bar" id="composer-bar"></div>
          </div>
          <span class="sr-only" id="agent-status-live" role="status"
                aria-live="polite"></span>

          <div class="chips" id="mission-chips"></div>

          ${localStorage.getItem("vq_paper_notice") === "off" ? "" : `
            <div class="paper-notice" id="paper-notice">
              <span data-icon="flask" data-icon-size="15"></span>
              <span>${t('home.paperNotice')}</span>
              <button class="paper-close" id="paper-dismiss" title="${t('common.close')}"
                      aria-label="${t('common.close')}" data-icon="close"
                      data-icon-size="13"></button>
            </div>`}

          ${hasKeys ? "" : `
            <div class="welcome-gate">
              <div>${t('home.needKey')}</div>
              <button class="btn btn-primary btn-sm" id="welcome-vault">${t('home.addKey')}</button>
            </div>`}

          <div class="legal-line">${t('legal.disclaimer')}</div>

          <div class="home-stats">
            ${stats.map(([n, l]) => `
              <div class="hstat"><div class="hstat-n">${n}</div><div class="hstat-l">${l}</div></div>
            `).join("")}
          </div>
        </div>
      </div>
    </div>`;
}

/* --------------------------- çalışma görünümü ---------------------------- */

function workShell() {
  return `
    <div class="cmd cmd-work" id="cmd-work">
      <div class="cmd-main">
        <div class="chat-stream" id="chat-stream">
          <div class="chat-inner" id="chat-inner"></div>
        </div>
        <div class="composer-zone">
          <div class="chips" id="mission-chips"></div>
          <div class="composer">
            <textarea id="chat-input" rows="1"
              placeholder="${t('composer.placeholder')}"></textarea>
            <div class="composer-bar" id="composer-bar"></div>
          </div>
          <span class="sr-only" id="agent-status-live" role="status"
                aria-live="polite"></span>
        </div>
      </div>

      <div class="splitter" id="live-splitter" role="separator"
           aria-orientation="vertical"
           aria-label="${t('layout.resizeLive')}"></div>

      <aside class="cmd-live" id="cmd-live">
        <div class="live-head">
          <span class="live-title"><span data-icon="candles" data-icon-size="14"></span> ${t("workspace.title")}</span>
          <button class="icon-btn" id="live-refresh" title="${t("live.refresh")}" data-icon="refresh"></button>
          <button class="icon-btn" id="live-close" title="${t("common.close")}" data-icon="close"
                  aria-label="${t("common.close")}"></button>
        </div>
        ${workspaceTabs()}
        <div class="live-body" id="live-body">
          <div class="dim small">Yükleniyor…</div>
        </div>
      </aside>

      <button class="live-fab" id="live-open" aria-expanded="false"
              title="${t("workspace.title")}" data-icon="portfolio"></button>
    </div>`;
}

const LIVE_PANEL_KEY = "vq_live_collapsed";

/**
 * Sağ panel tek bir durum kapısından yönetilir. Önceki uygulama yalnızca
 * `live-shown` sınıfını silip bırakıyordu; ekran genişliği değişince CSS
 * çekmecesi ile masaüstü grid'i farklı durumlar okuyabiliyor ve ana alan
 * görünmez kalabiliyordu. Bu fonksiyon sınıfları, erişilebilirlik durumunu
 * ve kalıcı tercihi atomik olarak eşler.
 */
function setLivePanel(open, { remember = true } = {}) {
  const shell = window.VQ.$("#cmd-work");
  if (!shell) return;
  const panel = window.VQ.$("#cmd-live");
  const opener = window.VQ.$("#live-open");
  const isDrawer = window.matchMedia("(max-width: 980px)").matches;

  shell.classList.toggle("live-collapsed", !open);
  shell.classList.toggle("live-shown", open && isDrawer);
  panel?.setAttribute("aria-hidden", open ? "false" : "true");
  if (panel) panel.inert = !open;
  opener?.setAttribute("aria-expanded", open ? "true" : "false");

  if (remember) {
    try { localStorage.setItem(LIVE_PANEL_KEY, open ? "0" : "1"); }
    catch { /* depolama kapalıysa oturumluk durum yeterli */ }
  }
}

function restoreLivePanel() {
  let open = true;
  try { open = localStorage.getItem(LIVE_PANEL_KEY) !== "1"; }
  catch { /* varsayılan açık */ }
  setLivePanel(open, { remember: false });
}

/** Composer (her iki düzende de aynı kimliklerle) bağlanır. */
function mountComposer(session, llmKeys) {
  const { $, navigate } = window.VQ;

  renderComposerBar(session, llmKeys);
  renderMissionChips(chatState.catalog?.quick_missions);

  // Durum ışığını HEMEN yak.
  //
  // Eskiden `updateAgentStatus` yalnızca açık bir görev varken çağrılıyordu;
  // ana ekranda yazı kutusu ışıksız kalıyordu. Oysa "hazırım" işareti tam da
  // ilk mesajdan ÖNCE gerekli: kullanıcı yazmaya başlamadan önce sistemin
  // ayakta olduğunu görmeli.
  updateAgentStatus(session?.status || "idle");

  // Mod kataloğu bir kez yüklenir; şerit her çizimde yeniden kurulur çünkü
  // düzen değiştiğinde (ana ekran → çalışma ekranı) DOM baştan yazılıyor.
  const baseInput = $("#chat-input");
  if (baseInput && !baseInput.dataset.basePlaceholder) {
    baseInput.dataset.basePlaceholder = baseInput.placeholder;
  }
  loadModeCatalog().then(() => {
    renderContextBar(session?.id ?? chatState.sessionId);
    applyPlaceholder();
  });

  $("#welcome-vault")?.addEventListener("click", () => navigate("vault"));

  // Sanal mod ibaresi kapatılabilir; tercih tarayıcıda hatırlanır.
  $("#paper-dismiss")?.addEventListener("click", () => {
    localStorage.setItem("vq_paper_notice", "off");
    $("#paper-notice")?.remove();
  });
  $("#live-refresh")?.addEventListener("click", () => refreshLivePanel(true));
  restoreLivePanel();
  $("#live-open")?.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    setLivePanel(true);
  });
  $("#live-close")?.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    setLivePanel(false);
  });

  const input = $("#chat-input");
  if (!input) return;
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submitChat();
    }
  });
  input.focus();
}

/**
 * Karşılama ekranından çalışma görünümüne anında geçiş (sunucu yanıtı
 * beklenmez — kullanıcı yazdığı anda ekran çalışma moduna döner).
 */
function switchToWorkLayout() {
  const { $ } = window.VQ;
  if ($("#chat-stream")) return;                 // zaten çalışma görünümündeyiz

  const body = $("#page-body");
  if (!body) return;
  const session = chatState.sessions.find((s) => s.id === chatState.sessionId) || null;
  const llmKeys = chatState.credentials.filter((c) => c.kind === "llm");

  body.innerHTML = workShell();
  mountComposer(session, llmKeys);
  mountWorkspace(body);
  mountLiveSplitter();
  refreshLivePanel();
}

/**
 * Tezgâh panelini bağlar: sekmeler, sürükle-bırak, rapor listesi.
 *
 * Sohbet kutusu bir bırakma alanıdır: paneldeki bir ölçüm, uzman görüşü ya
 * da rapor sürüklenip bırakıldığında alıntı olarak iner. Kullanıcı böylece
 * "şu raporda geçen risk bölümü" diye tarif etmek zorunda kalmaz.
 */
function mountWorkspace(root) {
  bindWorkspaceTabs(root, () => refreshLivePanel());
  bindChatDropZone();
  loadReports();
}

/* ----------------------------- canlı panel ------------------------------- */

let liveTimer = null;

export async function refreshLivePanel(manual = false) {
  const { $, api, esc, money, pct, cls } = window.VQ;
  const host = $("#live-body");
  if (!host) return;

  // Panel üç sekmelidir. Kullanıcı tezgâhı ya da raporları açıkken portföy
  // yenilemesi ekranı ELİNDEN ALMAMALI: 20 saniyede bir okuduğu raporun
  // üzerine yazmak, panelin kullanılamaz olması demektir.
  if (workspaceState.tab !== "live") {
    renderWorkspace();
    clearTimeout(liveTimer);
    liveTimer = setTimeout(() => refreshLivePanel(), 20000);
    return;
  }

  try {
    const [ov, safety] = await Promise.all([
      api("/api/portfolio/overview"),
      api("/api/safety/status").catch(() => null),
    ]);

    const c = ov.capital, p = ov.performance;
    const open = p.open_positions;

    const botRows = (ov.bots || []).slice(0, 6).map((b) => `
      <div class="live-bot" data-bot="${b.id}">
        <span class="dot ${b.status === "running" ? "dot-live"
                          : b.status === "locked" ? "dot-lock" : "dot-off"}"></span>
        <div class="live-bot-main">
          <div class="live-bot-name">${esc(b.name)}</div>
          <div class="live-bot-sub">${esc(b.symbol)} · ${esc(b.mode)}</div>
        </div>
        <div class="live-bot-val ${cls(b.return_pct)}">${pct(b.return_pct)}</div>
      </div>`).join("") || `<div class="dim small">${t("panel.noBots")}</div>`;

    host.innerHTML = `
      <div class="live-metric">
        <div class="live-metric-l">${t('live.capital')}</div>
        <div class="live-metric-v">${money(c.total_balance)}</div>
        <div class="live-metric-d ${cls(c.net_pnl)}">
          ${c.net_pnl >= 0 ? "+" : ""}${money(c.net_pnl)} (${pct(c.return_pct)})
        </div>
      </div>

      <div class="live-grid">
        <div class="live-cell" style="background:rgba(0,230,118,.07);border-color:rgba(0,230,118,.18)"><span>${t('live.openPositions')}</span><b>${open}</b></div>
        <div class="live-cell" style="background:rgba(99,45,255,.07);border-color:rgba(99,45,255,.18)"><span>${t('live.runningBots')}</span><b>${ov.running}/${ov.bot_count}</b></div>
        <div class="live-cell" style="background:rgba(0,245,155,.06);border-color:rgba(0,245,155,.16)"><span>${t('live.winRate')}</span><b>${p.win_rate_pct}%</b></div>
        <div class="live-cell" style="background:rgba(255,176,32,.06);border-color:rgba(255,176,32,.18)"><span>${t('live.profitFactor')}</span><b>${p.profit_factor}</b></div>
        <div class="live-cell" style="background:rgba(98,176,255,.07);border-color:rgba(98,176,255,.18)"><span>${t('live.avgR')}</span><b>${p.avg_r}</b></div>
        <div class="live-cell" style="background:rgba(255,92,114,.06);border-color:rgba(255,92,114,.18)"><span>${t('live.drawdown')}</span><b>${c.drawdown_pct.toFixed(2)}%</b></div>
      </div>

      ${window.VQ?.state?.health?.disk && window.VQ.state.health.disk.level !== "ok" ? `
        <div class="live-alert ${window.VQ.state.health.disk.level === "critical" ? "" : "warn"}">
          <span data-icon="alert" data-icon-size="14"></span>
          ${esc(window.VQ.state.health.disk.message)}
        </div>` : ""}
      ${safety?.kill_switch ? `
        <div class="live-alert">
          <span data-icon="brake" data-icon-size="14"></span>
          Acil fren açık — pozisyonlar kapatıldı, yeni işlem açılmıyor.
        </div>` : ""}
      ${ov.locked ? `
        <div class="live-alert warn">
          <span data-icon="alert" data-icon-size="14"></span>
          ${ov.locked} bot günlük devre kesiciyle kilitli.
        </div>` : ""}
      ${ov.recovery ? `
        <div class="live-alert warn">
          <span data-icon="recovery" data-icon-size="14"></span>
          ${ov.recovery} bot toparlanma modunda — risk otomatik küçültüldü.
        </div>` : ""}

      <div class="live-section">Otonom akış — son adımlar</div>
      <div class="live-steps">
        ${(window.VQ?.state?.liveEvents || []).slice(-4).reverse().map(e => `
          <div class="live-step">
            <span class="dot ${e.level==='error'?'dot-lock':e.level==='trade'?'dot-live':'dot-off'}" style="width:6px;height:6px"></span>
            <span class="live-step-msg">${esc(e.message||'').slice(0,72)}</span>
            <span class="live-step-time">${(e.ts||'').slice(11,16)}</span>
          </div>`).join("") || `<div class="dim small">Ajan çalışınca adımlar burada canlı akar — her araç çağrısı Hostinger tarzı ilerleme çubuğu gibi izlenir.</div>`}
      </div>

      <div class="live-section">Botlar</div>
      ${botRows}
    `;

    host.querySelectorAll(".live-bot").forEach((row) => {
      row.onclick = () => window.VQ.navigate(`bot/${row.dataset.bot}`);
    });
  } catch (err) {
    host.innerHTML = `<div class="dim small">Panel yüklenemedi: ${err.message}</div>`;
  }

  if (manual) window.VQ.toast(t("msg.panelRefreshed"), "ok");

  clearTimeout(liveTimer);
  liveTimer = setTimeout(() => {
    if (window.VQ.$("#live-body")) refreshLivePanel();
  }, 20000);
}

/**
 * Besteci çubuğu — TEK SIRA.
 *
 * Kullanıcı yalnızca üç şey görür: sağlayıcı, model ve gönder. Karar kalitesi
 * modu ile otonom anahtarı da buradadır çünkü kullanıcının seçmesi gereken
 * yegâne ayarlar bunlardır. Görev silme / kontrol aracı gibi nadir işlemler
 * "…" menüsünde durur; ekranı kalabalıklaştırmazlar.
 */
function renderComposerBar(session, llmKeys) {
  const { $, api, esc, toast, render, state, navigate } = window.VQ;
  const bar = $("#composer-bar");
  if (!bar) return;

  const hasKeys = llmKeys.length > 0;
  // Çalışma modu: ajan bu turda ne YAPABİLİR. Oturum yoksa kullanıcının son
  // seçimi hatırlanır — ama varsayılan her zaman en güvenli olandır.
  const workMode = session?.work_mode
    || localStorage.getItem("vq_work_mode") || "ask";

  /* --------------------------- sağlayıcı + model -------------------------- */

  const activeCred = session
    ? (llmKeys.find((k) => k.id === session.control_credential_id) || llmKeys[0])
    : llmKeys[0];

  const modelsOf = (cred) => {
    const provider = state.providers?.find((p) => p.id === cred?.provider);
    const list = provider?.models?.length ? [...provider.models] : [];
    const own = cred?.extra?.model;
    if (own && !list.includes(own)) list.unshift(own);
    return list.length ? list : [t("composer.noModel")];
  };

  const models = activeCred ? modelsOf(activeCred) : [];
  const activeModel = session?.control_model || activeCred?.extra?.model || models[0] || "";

  const credOptions = llmKeys.map((k) =>
    `<option value="${k.id}" ${k.id === activeCred?.id ? "selected" : ""}>
       ${esc(k.label)} · ${esc(k.provider)}
     </option>`).join("");

  const modelOptions = models.map((m) =>
    `<option value="${esc(m)}" ${m === activeModel ? "selected" : ""}>${esc(m)}</option>`).join("");

  bar.innerHTML = `
    <button class="icon-btn" id="ctx-plus" title="${t('composer.addMode')}"
            aria-label="${t('composer.addModeShort')}" data-icon="plus"></button>

    ${hasKeys ? `
      <select class="bar-select" id="ctl-cred" title="Sağlayıcı">${credOptions}</select>
      <select class="bar-select" id="ctl-model" title="Model">${modelOptions}</select>
    ` : `
      <button class="btn btn-sm btn-primary" id="add-key">${t('composer.addKey')}</button>
    `}

    <div class="work-switch" role="group" aria-label="${t('composer.workMode')}">
      ${WORK_MODES.map((w) => `
        <button class="work-opt ${w.id === workMode ? "on" : ""}" data-work="${w.id}"
                title="${esc(w.hint)}">${esc(w.label)}</button>`).join("")}
    </div>

    <span class="spacer"></span>

    ${session ? `<button class="icon-btn" id="bar-more" title="${t('composer.more')}"
                         aria-label="${t('composer.more')}" data-icon="more"></button>` : ""}
    <button class="send-btn" id="chat-send" title="${t('composer.send')}"
            aria-label="${t('composer.sendShort')}">${icon("arrowUp", 17)}</button>
  `;


  window.VQ.hydrateIcons?.(bar);

  /* ------------------------------- olaylar -------------------------------- */

  const patch = async (body) => {
    if (!session) return;
    try {
      const updated = await api(`/api/agent/sessions/${session.id}`, { method: "PATCH", body });
      Object.assign(session, updated);
      const index = chatState.sessions.findIndex((s) => s.id === session.id);
      if (index >= 0) chatState.sessions[index] = updated;
      renderComposerBar(session, llmKeys);
      renderRailSessions();
    } catch (err) {
      toast(err.message, "err");
    }
  };

  bar.querySelector("#ctx-plus")?.addEventListener("click", (event) => {
    event.stopPropagation();
    openModeMenu(event.currentTarget, session?.id ?? chatState.sessionId);
  });

  bar.querySelector("#add-key")?.addEventListener("click", () => navigate("vault"));

  bar.querySelectorAll("[data-work]").forEach((button) => {
    button.addEventListener("click", () => {
      const next = button.dataset.work;
      localStorage.setItem("vq_work_mode", next);
      const entry = WORK_MODES.find((w) => w.id === next);
      toast(entry ? `${entry.label}: ${entry.hint}` : next);
      if (session) {
        // Otonomluk moddan TÜRER; ayrıca gönderilmez.
        patch({ work_mode: next });
      } else {
        bar.querySelectorAll("[data-work]").forEach((other) =>
          other.classList.toggle("on", other === button));
      }
    });
  });

  bar.querySelector("#ctl-cred")?.addEventListener("change", (e) => {
    const cred = llmKeys.find((k) => String(k.id) === e.target.value);
    const list = modelsOf(cred);
    const modelSelect = bar.querySelector("#ctl-model");
    if (modelSelect) {
      modelSelect.innerHTML = list.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    }
    if (session) {
      patch({ control_credential_id: Number(e.target.value) || null, control_model: list[0] || "" });
    }
    if (cred) refreshModelList(cred, bar, list[0]);
  });

  bar.querySelector("#ctl-model")?.addEventListener("change", (e) => {
    if (e.target.value === "__manual__") return promptManualModel(activeCred, bar, patch);
    if (session) patch({ control_model: e.target.value });
  });

  // Modelleri sağlayıcıdan CANLI çek: koda gömülü liste birkaç ayda eskir.
  if (activeCred) refreshModelList(activeCred, bar, activeModel);

  bar.querySelector("#chat-send").addEventListener("click", () => {
    if (chatState.status === "thinking" || chatState.status === "running") {
      return stopAgent();
    }
    submitChat();
  });

  bar.querySelector("#bar-more")?.addEventListener("click", (e) =>
    openComposerMenu(e.currentTarget, session, llmKeys));
}


/**
 * Sağlayıcının güncel model listesini çeker ve seçiciyi tazeler.
 *
 * Uç nokta yanıt vermezse koddaki bilinen liste kalır — kurulum hiçbir zaman
 * bu yüzden durmaz. Listede olmayan model için "elle yaz" seçeneği hep vardır.
 */
async function refreshModelList(cred, bar, selected) {
  const { api } = window.VQ;
  const select = bar.querySelector("#ctl-model");
  if (!select || !cred) return;

  try {
    const data = await api(`/api/keys/${cred.id}/models`);
    const models = data.models || [];
    if (!models.length) return;

    const current = selected || data.selected || models[0];
    select.innerHTML = [
      ...models.map((m) =>
        `<option value="${window.VQ.esc(m)}" ${m === current ? "selected" : ""}>${window.VQ.esc(m)}</option>`),
      `<option value="__manual__">${window.VQ.t("composer.manualModel")}</option>`,
    ].join("");
    select.title = data.note || "";
    if (data.source === "live") select.dataset.live = "1";
  } catch {
    /* sessiz: liste tazelenemedi, mevcut liste geçerli kalır */
  }
}

/** Listede olmayan bir modeli elle girmek için. */
async function promptManualModel(cred, bar, patch) {
  const { promptModal, api, toast, t } = window.VQ;
  const name = await promptModal(
    t("composer.manualModel"),
    t("composer.manualModelHint"),
    "",
  );
  const select = bar.querySelector("#ctl-model");
  if (!name) {
    if (select) select.selectedIndex = 0;
    return;
  }
  try {
    if (cred) await api(`/api/keys/${cred.id}/models`, { method: "POST", body: { model: name } });
    if (select) {
      select.insertAdjacentHTML("afterbegin",
        `<option value="${window.VQ.esc(name)}" selected>${window.VQ.esc(name)}</option>`);
    }
    patch?.({ control_model: name });
    toast(`Model: ${name}`, "ok");
  } catch (err) {
    toast(err.message, "err");
  }
}

/** Nadir işlemler: denetim turu, kontrol aracı, görevi sil. */
function openComposerMenu(anchor, session, llmKeys) {
  const { $, el, esc, api, toast, render } = window.VQ;
  document.querySelector(".bar-menu")?.remove();

  const tools = chatState.catalog?.control_tools || [];
  const menu = el(`
    <div class="bar-menu">
      <div class="menu-title">Kontrol aracı</div>
      ${tools.map((t) => `
        <div class="menu-item ${t.id === session.control_tool ? "active" : ""}
             ${t.available === false ? "disabled" : ""}" data-tool="${esc(t.id)}">
          <span data-icon="${t.id === session.control_tool ? "check" : "wrench"}"
                data-icon-size="15"></span>
          ${esc(t.label)}${t.available === false ? " <span class='dim'>(kurulu değil)</span>" : ""}
        </div>`).join("")}
      <div class="menu-sep"></div>
      <div class="menu-item" data-act="tick">
        <span data-icon="refresh" data-icon-size="15"></span> Şimdi denetim turu çalıştır
      </div>
      <div class="menu-item danger" data-act="delete">
        <span data-icon="trash" data-icon-size="15"></span> Görevi sil
      </div>
    </div>`);

  const rect = anchor.getBoundingClientRect();
  menu.style.left = `${Math.max(12, rect.right - 250)}px`;
  menu.style.bottom = `${window.innerHeight - rect.top + 8}px`;
  document.body.appendChild(menu);
  window.VQ.hydrateIcons?.(menu);

  const close = () => menu.remove();
  setTimeout(() => document.addEventListener("click", close, { once: true }), 0);

  menu.addEventListener("click", async (e) => {
    const item = e.target.closest(".menu-item");
    if (!item || item.classList.contains("disabled")) return;
    close();

    if (item.dataset.tool) {
      try {
        const updated = await api(`/api/agent/sessions/${session.id}`,
          { method: "PATCH", body: { control_tool: item.dataset.tool } });
        Object.assign(session, updated);
        renderComposerBar(session, llmKeys);
        toast(t("msg.toolChanged"), "ok");
      } catch (err) { toast(err.message, "err"); }
      return;
    }

    if (item.dataset.act === "tick") {
      try {
        await api(`/api/agent/sessions/${session.id}/tick`, { method: "POST" });
        updateAgentStatus("thinking");
      } catch (err) { toast(err.message, "err"); }
      return;
    }

    if (item.dataset.act === "delete") {
      const ok = await window.VQ.confirmModal("Görevi sil",
        `<b>${esc(session.title)}</b> ve tüm konuşma geçmişi silinecek.`, "Sil");
      if (!ok) return;
      await api(`/api/agent/sessions/${session.id}`, { method: "DELETE" });
      chatState.sessionId = null;
      render();
    }
  });
}

/**
 * Hızlı görev butonları.
 *
 * Sunucu görevin KİMLİĞİNİ ve komutunu verir; etiketi arayüz kendi dilinde
 * yazar. Sunucudan sabit Türkçe gelince, arayüzü İngilizce yapan kullanıcı
 * başlığı İngilizce ama altındaki dört butonu Türkçe görüyordu — aynı
 * ekranda iki dil.
 *
 * Sözlükte karşılığı yoksa sunucunun metnine düşer: yeni bir hızlı görev
 * eklendiğinde, çevirisi yazılmadan önce de görünür kalır.
 */
function missionLabel(mission) {
  const key = `mission.${mission.id}`;
  const translated = t(key);
  return translated === key ? mission.label : translated;
}

function renderMissionChips(missions) {
  const { $, el, esc } = window.VQ;
  const box = $("#mission-chips");
  if (!box) return;

  box.innerHTML = "";
  (missions || []).forEach((m) => {
    const chip = el(`<div class="chip">${esc(missionLabel(m))}</div>`);
    chip.onclick = () => {
      const input = $("#chat-input");
      input.value = m.prompt;
      input.dispatchEvent(new Event("input"));
      submitChat();
    };
    box.appendChild(chip);
  });
}


/**
 * Çalışan ajan turunu durdurur.
 *
 * Ajan bulunduğu adımı bitirip durur — bir aracın ortasında kesilmez, çünkü
 * yarım kalan bir emir gönderimi veritabanı ile borsa arasında uyumsuzluk
 * bırakabilir. Açık pozisyonlara, koruyucu stop emirlerine ve çalışan botlara
 * DOKUNULMAZ; onları durdurmak için acil fren kullanılır.
 */
async function stopAgent() {
  const { api, toast } = window.VQ;
  if (!chatState.sessionId) return;

  const send = document.querySelector("#chat-send");
  if (send) send.disabled = true;          // çift tıklama durdurmayı hızlandırmaz

  try {
    const res = await api(`/api/agent/sessions/${chatState.sessionId}/stop`,
                          { method: "POST" });
    // Durumu KENDİMİZ "hazır" yapmayız: gerçek durum, tur temiz kapandığında
    // WebSocket'ten gelir. Aksi hâlde düğme durdu der, ajan çalışmaya devam eder.
    toast(res?.message || "Durduruluyor…", "ok");
  } catch (err) {
    toast(err.message, "err");
  } finally {
    if (send) send.disabled = false;
  }
}

/* --------------------------------- gönder -------------------------------- */

async function submitChat() {
  const { $, api, toast, navigate } = window.VQ;
  const input = $("#chat-input");
  const content = input.value.trim();
  if (!content) return;

  const llmKeys = chatState.credentials.filter((c) => c.kind === "llm");
  if (!llmKeys.length) {
    toast(t("msg.needKey"), "err");
    navigate("vault");
    return;
  }

  let sessionId = chatState.sessionId;
  if (!sessionId) {
    const session = await newChatSession();
    if (!session) return;
    sessionId = session.id;
    await new Promise((r) => setTimeout(r, 350));
  }

  switchToWorkLayout();

  const inner = $("#chat-inner");
  if (inner?.querySelector(".welcome")) inner.innerHTML = "";
  inner?.appendChild(chatMessageNode({ role: "user", content, ts: new Date().toISOString() }));
  scrollToEnd();

  const liveInput = $("#chat-input");
  if (liveInput) { liveInput.value = ""; liveInput.style.height = "auto"; }
  updateAgentStatus("thinking");

  try {
    await api(`/api/agent/sessions/${sessionId}/messages`, {
      method: "POST",
      body: { content, language: lang(), ...messagePayload() },
    });
    // Dosyalar tek mesaja aittir; mod oturum boyunca korunur.
    afterModeSend();
    renderContextBar(sessionId);
  } catch (err) {
    toast(err.message, "err");
    updateAgentStatus("idle");
  }
}

function scrollToEnd() {
  const stream = window.VQ.$("#chat-stream");
  if (stream) stream.scrollTop = stream.scrollHeight;
}

/* ========================================================================== */
/*  Mesaj / araç adımı çizimi                                                 */
/* ========================================================================== */

const TOOL_LABEL = {
  get_market_snapshot: "Piyasa verisi okundu (deterministik göstergeler)",
  run_strategy_engine: "Algoritmik strateji motoru çalıştırıldı",
  get_news: "Haber başlıkları tarandı",
  get_quote: "Anlık fiyat alındı",
  run_backtest: "Geçmiş veride geri test yapıldı",
  get_portfolio: "Portföy durumu incelendi",
  get_bot_detail: "Bot detayı okundu",
  create_bot: "Yeni bot kuruldu",
  update_bot: "Bot ayarları güncellendi",
  control_bot: "Bot başlat / durdur / kilit",
  run_bot_cycle: "Botun karar turu çalıştırıldı",
  open_position: "Pozisyon açma talebi (risk kalkanından geçti)",
  close_position: "Pozisyon kapatıldı",
  ask_sub_model: "Alt modele (analist) danışıldı",
  send_notification: "Telegram bildirimi gönderildi",
  system_health: "Sistem sağlığı kontrol edildi",
  list_credentials: "Kayıtlı anahtarlar listelendi",
  scan_markets: "Onlarca parite eş zamanlı tarandı, fırsatlar skorlandı",
  validate_strategy: "Walk-forward: görülmemiş veride doğrulandı, aşırı uyum ölçüldü",
  optimize_setup: "En iyi zaman dilimi ve strateji seti arandı",
  get_recovery_plan: "Toparlanma planı okundu (başabaş yolu)",
  get_portfolio_risk: "Portföy ısısı ve korelasyon riski ölçüldü",
  get_safety_status: "Acil fren ve canlı yetki durumu okundu",
  enable_live_trading: "Gerçek para moduna geçiş denendi",
  disable_live_trading: "Sanal moda alındı",
  activate_kill_switch: "Acil fren — pozisyonlar kapatıldı, sistem durdu",
  get_model_scoreboard: "Model sicili okundu",
};

const TOOL_TITLE = {
  get_market_snapshot: "Piyasayı okudu",
  run_strategy_engine: "Algoritmayla doğruladı",
  get_news: "Haberlere baktı",
  get_quote: "Fiyat aldı",
  run_backtest: "Geri test yaptı",
  get_portfolio: "Portföye baktı",
  get_bot_detail: "Botu inceledi",
  create_bot: "Bot kurdu",
  update_bot: "Ayar değiştirdi",
  control_bot: "Botu yönetti",
  run_bot_cycle: "Bot turu çalıştırdı",
  open_position: "Pozisyon açtı",
  close_position: "Pozisyon kapattı",
  ask_sub_model: "Alt modele sordu",
  send_notification: "Bildirim gönderdi",
  system_health: "Sistemi denetledi",
  list_credentials: "Anahtarları listeledi",
  scan_markets: "Piyasaları taradı",
  validate_strategy: "Walk-forward doğrulama yaptı",
  optimize_setup: "En iyi kurulumu aradı",
  get_recovery_plan: "Toparlanma planını okudu",
  get_portfolio_risk: "Portföy riskini ölçtü",
  get_safety_status: "Güvenliği kontrol etti",
  enable_live_trading: "Gerçek paraya geçmeyi denedi",
  disable_live_trading: "Sanal moda aldı",
  activate_kill_switch: "Acil fren çekti (pozisyonları kapatıyor)",
  get_model_scoreboard: "Model sicilini okudu",
};

const MUTATING_TOOLS = new Set([
  "create_bot", "update_bot", "control_bot", "run_bot_cycle",
  "open_position", "close_position", "send_notification",
  "enable_live_trading", "disable_live_trading", "activate_kill_switch",
]);

function toolSummary(message) {
  const args = message.tool_args || {};
  const result = message.tool_result || {};
  if (result.error) return `${String(result.error).slice(0, 110)}`;

  switch (message.tool_name) {
    case "get_market_snapshot":
      return `${args.symbol ?? ""} ${args.timeframe ?? ""} · ${result.snapshot?.regime ?? ""} · ${result.snapshot?.price ?? "?"}`;
    case "run_strategy_engine":
      return `${result.action ?? ""} — ${result.summary ?? ""}`;
    case "get_news":
      return `${result.count ?? 0} başlık · duygu ${result.sentiment_label ?? "?"}`;
    case "run_backtest":
      return `${result.metrics?.trade_count ?? 0} işlem · PF ${result.metrics?.profit_factor ?? "?"} · DD %${result.metrics?.max_drawdown_pct ?? "?"}`;
    case "get_portfolio":
      return `${result.bot_count ?? 0} bot · ${result.total_balance ?? 0} · %${result.return_pct ?? 0}`;
    case "create_bot":
      return `#${result.bot_id ?? "?"} ${result.name ?? ""} · ${result.mode ?? ""}`;
    case "update_bot":
      return Object.keys(result.changes || {}).join(", ") || "değişiklik yok";
    case "control_bot":
      return `${args.action ?? ""} · ${result.status ?? result.error ?? ""}`;
    case "open_position":
      return result.opened
        ? `${result.side} · giriş ${result.entry} · stop ${result.stop_loss} · R/R 1:${result.rr}`
        : `risk kalkanı reddetti — ${result.reason ?? ""}`;
    case "close_position":
      return result.closed ? `PnL ${result.pnl} (${result.r_multiple}R)` : String(result.error ?? "");
    case "run_bot_cycle":
      return `${result.action ?? result.error ?? ""}`;
    case "system_health":
      return `${result.bots_running ?? 0}/${result.bots_total ?? 0} çalışıyor · ${(result.recent_errors || []).length} hata`;
    case "scan_markets":
      return result.headline ?? `${result.scanned ?? 0} parite tarandı`;
    case "validate_strategy":
      return String(result.verdict ?? "").slice(0, 110);
    case "optimize_setup":
      return result.headline ?? (result.found ? "yapılandırma bulundu" : "uygun kurulum yok");
    case "get_recovery_plan":
      return result.headline ?? "";
    case "get_portfolio_risk":
      return `ısı %${result.heat_pct ?? 0} · ${result.open_positions ?? 0} pozisyon · `
        + `${result.risk_free_positions ?? 0} risksiz`;
    case "get_safety_status":
      return `acil fren ${result.kill_switch ? "AÇIK" : "kapalı"} · canlı yetki `
        + `${result.live_authorization?.authorized ? "var" : "yok"}`;
    case "enable_live_trading":
      return result.enabled
        ? `canlı moda alındı · sermaye ${result.capital}`
        : `engellendi — ${result.reason ?? ""}`;
    case "disable_live_trading":
      return `sanal moda alındı — ${args.reason ?? ""}`;
    case "activate_kill_switch":
      return `acil fren çekildi — ${args.reason ?? ""}`;
    case "get_model_scoreboard":
      return `${(result.models || []).length} model sicili`;
    case "ask_sub_model":
      return String(result.answer ?? result.reason ?? "").slice(0, 110);
    default:
      return Object.entries(args).map(([k, v]) => `${k}=${v}`).join(" ").slice(0, 110);
  }
}

function toolStepNode(message) {
  const { el, esc } = window.VQ;
  const failed = !message.ok || Boolean(message.tool_result?.error);
  const mutating = MUTATING_TOOLS.has(message.tool_name);

  const node = el(`
    <div class="tool-step ${failed ? "err" : mutating ? "mut" : ""}">
      <div class="tool-head">
        <span class="tool-ico">${icon(TOOL_ICON[message.tool_name] || "wrench", 15)}</span>
        <span class="tool-name">${esc(TOOL_TITLE[message.tool_name] || message.tool_name)}</span>
        <span class="tool-sum">${esc(toolSummary(message))}</span>
        <span class="tool-ms">${message.duration_ms ? `${message.duration_ms}ms` : ""}</span>
        <span class="caret">${icon("chevronRight", 13)}</span>
      </div>
      <div class="tool-body hidden">
        <div class="dim mb">${esc(TOOL_LABEL[message.tool_name] || "Araç çağrısı")} · <code>${esc(message.tool_name)}</code></div>
        <pre>${esc(JSON.stringify({ girdi: message.tool_args, sonuç: message.tool_result }, null, 2))}</pre>
      </div>
    </div>`);

  node.querySelector(".tool-head").onclick = () => {
    node.querySelector(".tool-body").classList.toggle("hidden");
    const collapsed = node.querySelector(".tool-body").classList.contains("hidden");
    node.querySelector(".caret").innerHTML = icon(collapsed ? "chevronRight" : "chevronDown", 13);
  };
  return node;
}

function renderMarkdown(text, esc) {
  return esc(text)
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h3>$1</h3>")
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/^- (.+)$/gm, "• $1");
}


/**
 * Mesaj üzerindeki eylemler: kopyala ve (kendi mesajınsa) düzenle.
 *
 * "Düzenle" mesajı SİLMEZ — metni yazma kutusuna geri koyar. Konuşma geçmişi
 * ajanın kararlarının kaydıdır; geçmişe dönük değiştirilmesi, sonradan
 * "neden bu işlemi açtı?" sorusunu cevaplanamaz hâle getirirdi.
 */
function bindMessageActions(node, text) {
  const { toast, $ } = window.VQ;

  node.querySelector('[data-act="copy"]')?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast(t("msg.copied"), "ok");
    } catch {
      // Pano izni yoksa: metni seçili hâle getir, kullanıcı Ctrl+C yapabilsin
      const range = document.createRange();
      range.selectNodeContents(node.querySelector(".msg-user, .msg-body"));
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      toast(t("msg.textSelected"));
    }
  });

  node.querySelector('[data-act="edit"]')?.addEventListener("click", () => {
    const input = $("#chat-input");
    if (!input) return;
    input.value = text;
    input.dispatchEvent(new Event("input"));
    input.focus();
    input.setSelectionRange(text.length, text.length);
    toast(t("msg.editing"));
  });
}

function chatMessageNode(message) {
  const { el, esc, timeOf } = window.VQ;

  if (message.role === "user") {
    const node = el(`
      <div class="msg msg-mine">
        <div class="msg-user">${esc(message.content)}</div>
        <div class="msg-actions">
          <button class="msg-act" data-act="copy" title="Kopyala" aria-label="Kopyala"
                  type="button">${icon("copy", 13)}</button>
          <button class="msg-act" data-act="edit" title="Düzenle ve yeniden gönder"
                  aria-label="Düzenle" type="button">${icon("edit", 13)}</button>
        </div>
      </div>`);
    bindMessageActions(node, message.content || "");
    return node;
  }
  if (message.role === "system") {
    return el(`<div class="msg-system">${esc(message.content)}</div>`);
  }
  if (message.role === "context") {
    // Ajanın gördüğü bağlam gizlenmez ama sohbeti de boğmaz: katlanır.
    // Kullanıcı, modelin tam olarak neyi okuduğunu açıp görebilmeli.
    const text = message.content || "";
    const firstLine = text.split("\n").find((l) => l.trim()) || "Bağlam";
    return el(`
      <details class="msg-context">
        <summary>${icon("paperclip", 13)} ${esc(firstLine.replace(/^#+\s*/, ""))}
          <span class="dim">· ${text.length.toLocaleString("tr-TR")} karakter · ajana verildi</span>
        </summary>
        <pre>${esc(text)}</pre>
      </details>`);
  }
  if (message.role === "tool") {
    return toolStepNode(message);
  }
  const node = el(`
    <div class="msg msg-assistant">
      <div class="msg-avatar">${icon("command", 15)}</div>
      <div class="msg-body">${renderMarkdown(message.content || "", esc)}
        <div class="msg-time">${timeOf(message.ts)}</div>
      </div>
      <div class="msg-actions">
        <button class="msg-act" data-act="copy" title="Kopyala" aria-label="Kopyala"
                type="button">${icon("copy", 13)}</button>
      </div>
    </div>`);
  bindMessageActions(node, message.content || "");
  return node;
}

/* ========================================================================== */
/*  Canlı akış (WebSocket)                                                    */
/* ========================================================================== */

export function appendChatMessage(message) {
  const { $, state, toast } = window.VQ;

  if (state.route !== "chat") {
    if (message.role === "assistant" && message.content) {
      toast(`Ajan: ${message.content.slice(0, 100)}`);
    }
    return;
  }
  const inner = $("#chat-inner");
  if (!inner || message.role === "user") return;

  if (inner.querySelector(".welcome")) inner.innerHTML = "";
  $("#thinking-row")?.remove();
  inner.appendChild(chatMessageNode(message));
  scrollToEnd();
}

export function updateAgentStatus(status, error = "") {
  const { $, el, toast } = window.VQ;
  chatState.status = status;

  const busy = status === "thinking" || status === "running";

  // Durum YAZIYLA değil IŞIKLA anlatılır.
  //
  // Alt satırda duran "hazır / düşünüyor…" rozeti iki sorun üretiyordu: bir
  // satırı tek başına yiyordu ve metin uzunluğu değiştikçe komşu düğmeleri
  // kaydırıyordu. Yazı kutusunun kendi çerçevesi parlayınca hem satır
  // serbest kalıyor hem de durum çevresel görüşle okunuyor — kullanıcının
  // okumak için bakması gerekmiyor.
  document.querySelectorAll(".composer").forEach((box) => {
    box.dataset.state = status;
  });

  // Ekran okuyucular ışığı göremez: durum onlara metin olarak duyurulur.
  const live = $("#agent-status-live");
  if (live) {
    live.textContent = t(`status.${status}`) || status;
  }

  // Gönder düğmesi çalışırken DURDUR olur: kullanıcı ajanı her an kesebilir.
  const send = $("#chat-send");
  if (send) {
    send.classList.toggle("stop", busy);
    send.title = busy ? t("composer.stop") : t("composer.send");
    send.setAttribute("aria-label", busy ? t("composer.stop") : t("composer.sendShort"));
    send.innerHTML = busy ? icon("stop", 15) : icon("arrowUp", 17);
  }

  const inner = $("#chat-inner");
  if (!inner) return;

  $("#thinking-row")?.remove();
  if (status === "thinking" || status === "running") {
    inner.appendChild(el(`
      <div class="thinking" id="thinking-row">
        <span class="bars"><i></i><i></i><i></i></span>
        ${status === "thinking" ? "düşünüyor…" : "araç çalıştırıyor…"}
      </div>`));
    scrollToEnd();
  }
  if (status === "error" && error) toast(error.slice(0, 150), "err");
}
