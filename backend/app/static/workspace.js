/**
 * ARAŞTIRMA TEZGÂHI — yan panelin çalışan beyni
 * ==============================================
 *
 * Kullanıcının en çok istediği şey şuydu: "model nerede ne yapıyor görebilelim,
 * vereceği raporu buradan okuyabilelim, oradaki şeyi sohbete sürükleyebilelim."
 *
 * Panel üç sekmelidir ve her biri farklı bir soruya cevap verir:
 *
 *   CANLI     Param ne durumda?          (portföy, botlar, açık risk)
 *   TEZGÂH    Şu an ne yapılıyor?        (aşamalar, uzmanlar, ölçümler)
 *   RAPORLAR  Ne öğrendik?               (okunabilir, dipnotlu raporlar)
 *
 * Tezgâh sekmesi tamamen olay güdümlüdür: sunucudan gelen her aşama/rol/
 * doğrulama olayı anında ekrana düşer. Sorgulama (polling) yoktur, çünkü
 * kullanıcının görmek istediği şey "şu an" olanıdır.
 *
 * Sürükle-bırak: paneldeki her ölçüm, uzman görüşü ve rapor sohbet kutusuna
 * sürüklenebilir. Sürüklenen şey METİN olarak iner — böylece kullanıcı
 * "şuna bak" derken neyi kastettiğini tarif etmek zorunda kalmaz.
 */

import { t } from "/ui/i18n.js";

const state = {
  tab: "live",
  running: false,
  question: "",
  stages: {},          // aşama kimliği -> {label, status, extra}
  roles: {},           // rol kimliği   -> {label, status, preview, guven}
  data: [],            // veri toplama adımları
  facts: 0,
  sources: [],
  trust: null,
  reports: [],
  openReport: null,
  workshop: null,        // ajanın yazdığı beceriler ve otomasyonlar
  browser: { sessionId: null, visits: [], index: -1, unseen: false },
};

const BROWSER_TOOLS = new Set(["browse_page", "browse_follow", "web_read"]);

/**
 * Ajanın tarayıcı araçlarını, sohbet kartlarından bağımsız canlı bir gezinme
 * siciline dönüştürür. Gerçek sayfa iframe'e gömülmez: çoğu finans sitesi
 * bunu zaten engeller ve üçüncü taraf betiklerini ürün kabuğunda çalıştırmak
 * güvenli değildir. Ajanın gerçekten aldığı adres, başlık, metin ve bağlantı
 * aynı anda gösterilir; yani kullanıcı ajanın gördüğü şeyin aynısını görür.
 */
export function handleBrowserMessage(message, { renderNow = true, sessionId = null } = {}) {
  if (message?.role !== "tool" || !BROWSER_TOOLS.has(message.tool_name)) return false;
  if (sessionId !== null && state.browser.sessionId !== null &&
      Number(sessionId) !== Number(state.browser.sessionId)) return false;

  const args = message.tool_args || {};
  const result = message.tool_result || {};
  const url = result.son_adres || result.final_url || result.url || args.url || "";
  const title = result.baslik || result.title || "";
  const text = result.metin || result.text || result.content || "";
  const links = result.baglantilar || result.links || [];
  const visit = {
    id: message.id || `${message.ts || ""}-${url}`,
    tool: message.tool_name,
    url: String(url),
    title: String(title),
    text: String(text),
    links: Array.isArray(links) ? links.slice(0, 40) : [],
    error: String(result.error || ""),
    javascriptOnly: Boolean(result.javascript_gerekiyor || result.javascript_only),
    truncated: Boolean(result.kirpildi || result.truncated),
    suspicious: result.supheli_icerik || result.suspicious || [],
    ts: message.ts || "",
    duration: Number(message.duration_ms || 0),
  };

  const current = state.browser.visits[state.browser.visits.length - 1];
  if (current?.id === visit.id) return true;
  state.browser.visits.push(visit);
  if (state.browser.visits.length > 30) state.browser.visits.shift();
  state.browser.index = state.browser.visits.length - 1;
  state.browser.unseen = state.tab !== "browser";
  if (renderNow) render();
  return true;
}

/** Açılan görevin geçmiş tarayıcı adımlarını tekrar kurar. */
export function hydrateBrowserMessages(messages, sessionId) {
  state.browser = { sessionId, visits: [], index: -1, unseen: false };
  (messages || []).forEach((message) => handleBrowserMessage(
    message, { renderNow: false, sessionId }));
  state.browser.unseen = false;
}

const STAGE_ORDER = ["brif", "veri", "analiz", "dogrulama", "risk", "rapor"];

/* --------------------------------------------------------------------------
   Olaylar — sunucudan gelen her şey buraya düşer
   -------------------------------------------------------------------------- */

/**
 * Araştırma olaylarını işler.
 *
 * `true` dönerse olay tüketilmiştir; çağıran başka bir şey yapmamalıdır.
 */
export function handleResearchEvent(event) {
  switch (event.type) {
    case "research_start":
      Object.assign(state, {
        running: true, question: event.question || "", stages: {}, roles: {},
        data: [], facts: 0, sources: [], trust: null, openReport: null,
        tab: "work",
      });
      window.VQ?.toast?.("Araştırma başladı — tezgâh panelinde izleyebilirsiniz.", "ok");
      break;

    case "research_stage":
      state.stages[event.stage] = {
        label: event.label, status: event.status,
        facts: event.facts, sources: event.sources,
        contradicted: event.contradicted, unsupported: event.unsupported,
        trust: event.trust_score,
      };
      if (event.sources) state.sources = event.sources;
      if (typeof event.facts === "number") state.facts = event.facts;
      if (typeof event.trust_score === "number") state.trust = event.trust_score;
      break;

    case "research_data":
      state.data.push({ step: event.step, facts: event.facts });
      state.facts = event.facts ?? state.facts;
      break;

    case "research_role":
      state.roles[event.role] = {
        ...(state.roles[event.role] || {}),
        label: event.label, status: event.status, mission: event.mission,
        error: event.error, ms: event.duration_ms, preview: event.preview,
      };
      break;

    case "research_audit":
      state.roles[event.role] = {
        ...(state.roles[event.role] || {}),
        label: event.label, audit: {
          supported: event.supported, contradicted: event.contradicted,
          unsupported: event.unsupported, projections: event.projections || 0,
          trust: event.trust_score,
          claims: event.claims || [], unknown: event.unknown_sources || [],
        },
      };
      break;

    case "research_done":
      state.running = false;
      state.trust = event.trust_score;
      state.facts = event.facts ?? state.facts;
      loadReports().then(() => render());
      window.VQ?.toast?.("Araştırma bitti — rapor hazır.", "ok");
      break;

    case "research_error":
      state.running = false;
      window.VQ?.toast?.(event.message || "Araştırma durdu.", "err");
      break;

    default:
      return false;
  }
  render();
  return true;
}

/* --------------------------------------------------------------------------
   Çizim
   -------------------------------------------------------------------------- */

export function render() {
  const host = document.querySelector("#live-body");
  if (!host) return;

  const tabs = document.querySelector("#ws-tabs");
  if (tabs) paintTabs(tabs);

  if (state.tab === "work") host.innerHTML = workbench();
  else if (state.tab === "reports") host.innerHTML = reportsView();
  else if (state.tab === "workshop") host.innerHTML = workshopView();
  else if (state.tab === "browser") host.innerHTML = browserView();
  else return;                       // "canlı" sekmesini chat.js çizer

  bindDrag(host);
  bindReports(host);
  bindWorkshop(host);
  bindBrowser(host);
  window.VQ?.paintIcons?.(host);
}

export function tabsMarkup() {
  return `
    <div class="ws-tabs" id="ws-tabs">
      <button class="ws-tab" data-tab="live">${t("workspace.live")}</button>
      <button class="ws-tab" data-tab="work">${t("workspace.work")}</button>
      <button class="ws-tab" data-tab="reports">${t("workspace.reports")}</button>
      <button class="ws-tab" data-tab="workshop">${t("workspace.workshop")}</button>
      <button class="ws-tab" data-tab="browser">${t("workspace.browser")}</button>
    </div>`;
}

export function bindTabs(root, onLive) {
  const tabs = root.querySelector("#ws-tabs");
  if (!tabs) return;
  tabs.addEventListener("click", (e) => {
    const button = e.target.closest(".ws-tab");
    if (!button) return;
    state.tab = button.dataset.tab;
    paintTabs(tabs);
    if (state.tab === "live") onLive?.();
    else if (state.tab === "reports") loadReports().then(render);
    else if (state.tab === "workshop") loadWorkshop().then(render);
    else {
      if (state.tab === "browser") state.browser.unseen = false;
      render();
    }
  });
  paintTabs(tabs);
}

function paintTabs(tabs) {
  tabs.querySelectorAll(".ws-tab").forEach((b) => {
    b.classList.toggle("on", b.dataset.tab === state.tab);
  });
  const work = tabs.querySelector('[data-tab="work"]');
  if (work) work.classList.toggle("busy", state.running);
  const browser = tabs.querySelector('[data-tab="browser"]');
  if (browser) browser.classList.toggle("busy", state.browser.unseen);
}

/* ------------------------------ tarayıcı -------------------------------- */

function safeWebUrl(value) {
  try {
    const parsed = new URL(String(value || ""));
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "";
  } catch { return ""; }
}

function browserLink(link) {
  const label = link?.metin || link?.text || link?.label || "";
  const href = safeWebUrl(link?.adres || link?.url || link?.href || "");
  if (!href) return "";
  return `<a class="ws-browser-link" href="${esc(href)}" target="_blank"
             rel="noopener noreferrer" title="${esc(href)}">${esc(label || href)}</a>`;
}

function browserView() {
  const { visits, index } = state.browser;
  const visit = visits[index];
  if (!visit) {
    return `<div class="ws-empty ws-browser-empty">
      <div class="ws-empty-title">${t("browser.emptyTitle")}</div>
      <p>${t("browser.emptyBody")}</p>
      <p class="dim small">${t("browser.agentHint")}</p>
    </div>`;
  }

  const href = safeWebUrl(visit.url);
  const links = visit.links.map(browserLink).filter(Boolean).join("");
  const status = visit.error ? t("browser.error") : t("browser.success");
  return `<div class="ws-browser">
    <div class="ws-browser-toolbar">
      <button class="ws-browser-nav" data-browser-nav="back" type="button"
              ${index <= 0 ? "disabled" : ""} aria-label="${t("browser.back")}">←</button>
      <button class="ws-browser-nav" data-browser-nav="forward" type="button"
              ${index >= visits.length - 1 ? "disabled" : ""}
              aria-label="${t("browser.forward")}">→</button>
      <div class="ws-browser-address" title="${esc(visit.url)}">${esc(visit.url || "—")}</div>
      ${href ? `<a class="ws-browser-open" href="${esc(href)}" target="_blank"
        rel="noopener noreferrer">${t("browser.open")}</a>` : ""}
    </div>
    <div class="ws-browser-meta ${visit.error ? "bad" : "good"}">
      <span>${esc(visit.title || visit.url || visit.tool)}</span>
      <span class="ws-browser-status">${status} · ${index + 1}/${visits.length}</span>
    </div>
    ${visit.javascriptOnly ? `<div class="ws-browser-warning">${t("browser.jsOnly")}</div>` : ""}
    ${visit.suspicious?.length ? `<div class="ws-browser-warning">${t("browser.suspicious")}</div>` : ""}
    ${visit.error ? `<div class="ws-role-err">${esc(visit.error)}</div>` : `
      <div class="live-section">${t("browser.content")}</div>
      <pre class="ws-browser-content" draggable="true" data-drag="${esc(visit.text.slice(0, 4000))}">${esc(visit.text || t("browser.noContent"))}</pre>`}
    ${links ? `<div class="live-section">${t("browser.links")}</div>
      <div class="ws-browser-links">${links}</div>` : ""}
  </div>`;
}

function bindBrowser(host) {
  host.querySelectorAll("[data-browser-nav]").forEach((button) => {
    button.addEventListener("click", () => {
      const delta = button.dataset.browserNav === "back" ? -1 : 1;
      state.browser.index = Math.max(0,
        Math.min(state.browser.visits.length - 1, state.browser.index + delta));
      render();
    });
  });
}

/* ------------------------------- tezgâh ---------------------------------- */

function workbench() {
  if (!state.question && !Object.keys(state.stages).length) {
    return `
      <div class="ws-empty">
        <div class="ws-empty-title">Tezgâh boş</div>
        <p>Bir araştırma başlattığınızda modelin <b>hangi aşamada, hangi
        uzmanla, hangi ölçümle</b> çalıştığını buradan adım adım izlersiniz.</p>
        <p class="dim small">Sohbete “BTC için derin araştırma yap” gibi bir şey
        yazmanız yeterli.</p>
      </div>`;
  }

  return `
    <div class="ws-question" draggable="true" data-drag="${esc(state.question)}">
      ${esc(state.question)}
    </div>

    ${trustBar()}

    <div class="live-section">Aşamalar</div>
    <div class="ws-stages">
      ${STAGE_ORDER.map(stageRow).join("")}
    </div>

    ${state.data.length ? `
      <div class="live-section">Toplanan veri</div>
      <div class="ws-data">
        ${state.data.slice(-8).map((d) => `
          <div class="ws-data-row" draggable="true" data-drag="${esc(d.step)}">
            <span class="dot dot-live"></span>
            <span class="ws-data-step">${esc(d.step)}</span>
            <span class="ws-data-n">${d.facts}</span>
          </div>`).join("")}
      </div>` : ""}

    ${Object.keys(state.roles).length ? `
      <div class="live-section">Uzmanlar</div>
      <div class="ws-roles">${Object.entries(state.roles).map(roleCard).join("")}</div>
    ` : ""}

    ${state.sources.length ? `
      <div class="live-section">Kaynaklar</div>
      <div class="ws-sources">
        ${state.sources.map((s) => `<span class="ws-src">${esc(s)}</span>`).join("")}
      </div>` : ""}
  `;
}

function trustBar() {
  if (state.trust === null || state.trust === undefined) {
    return `<div class="ws-trust pending">
      <span class="ws-trust-l">Doğrulama</span>
      <span class="ws-trust-v">${state.running ? "sürüyor…" : "henüz yok"}</span>
    </div>`;
  }
  const pct = Math.round(state.trust * 100);
  const tone = pct >= 85 ? "good" : pct >= 60 ? "mid" : pct >= 35 ? "warn" : "bad";
  return `
    <div class="ws-trust ${tone}">
      <span class="ws-trust-l">Sayısal iddiaların ölçümle doğrulanan oranı</span>
      <div class="ws-trust-track"><div class="ws-trust-fill" style="width:${pct}%"></div></div>
      <span class="ws-trust-v">%${pct} · ${state.facts} ölçüm</span>
    </div>`;
}

function stageRow(id) {
  const stage = state.stages[id];
  const label = stage?.label || DEFAULT_STAGE_LABELS[id];
  const status = stage?.status || "pending";
  const detail = stageDetail(id, stage);
  return `
    <div class="ws-stage ${status}">
      <span class="ws-stage-mark"></span>
      <span class="ws-stage-label">${esc(label)}</span>
      ${detail ? `<span class="ws-stage-detail">${esc(detail)}</span>` : ""}
    </div>`;
}

const DEFAULT_STAGE_LABELS = {
  brif: "Soruyu netleştir", veri: "Veri topla", analiz: "Uzmanlar çalışsın",
  dogrulama: "Sayıları doğrula", risk: "Riski ölç", rapor: "Raporu yaz",
};

function stageDetail(id, stage) {
  if (!stage || stage.status !== "done") return "";
  if (id === "veri") return `${stage.facts ?? 0} ölçüm`;
  if (id === "dogrulama") {
    const bad = stage.contradicted || 0;
    return bad ? `${bad} çelişki` : "çelişki yok";
  }
  return "";
}

function roleCard([id, role]) {
  const audit = role.audit;
  const tone = !role.status ? "" :
    role.status === "failed" ? "failed" :
    role.status === "running" ? "running" : "done";

  return `
    <div class="ws-role ${tone}" data-role="${esc(id)}">
      <div class="ws-role-head">
        <span class="ws-role-name">${esc(role.label || id)}</span>
        ${role.status === "running" ? `<span class="ws-spin"></span>`
          : role.ms ? `<span class="ws-role-ms">${(role.ms / 1000).toFixed(1)} sn</span>` : ""}
      </div>
      ${role.mission ? `<div class="ws-role-mission">${esc(role.mission)}</div>` : ""}
      ${role.error ? `<div class="ws-role-err">${esc(role.error)}</div>` : ""}
      ${audit ? `
        <div class="ws-role-audit">
          <span class="ok">${audit.supported}✓ doğrulandı</span>
          ${audit.contradicted ? `<span class="bad">${audit.contradicted}⚠ çelişki</span>` : ""}
          ${audit.projections ? `<span class="fwd">${audit.projections}→ öngörü</span>` : ""}
          ${audit.unsupported ? `<span class="mid">${audit.unsupported}○ dayanaksız</span>` : ""}
        </div>` : ""}
      ${role.preview ? `
        <div class="ws-role-preview" draggable="true" data-drag="${esc(role.preview)}">
          ${esc(role.preview)}
        </div>` : ""}
    </div>`;
}

/* ------------------------------ raporlar --------------------------------- */

export async function loadReports() {
  try {
    state.reports = await window.VQ.api("/api/research/reports");
  } catch {
    state.reports = [];
  }
  return state.reports;
}

function reportsView() {
  if (state.openReport) return reportDetail(state.openReport);

  if (!state.reports.length) {
    return `<div class="ws-empty">
      <div class="ws-empty-title">Henüz rapor yok</div>
      <p>Tamamlanan her araştırma buraya kaynaklı ve dipnotlu bir rapor olarak
      düşer. Raporlar <b>bu cihazda</b> kalır.</p>
    </div>`;
  }

  return `<div class="ws-reports">${state.reports.map((r) => {
    const pct = Math.round((r.trust_score ?? 0) * 100);
    const tone = pct >= 85 ? "good" : pct >= 60 ? "mid" : pct >= 35 ? "warn" : "bad";
    return `
      <div class="ws-report" data-report="${esc(r.id)}" draggable="true"
           data-drag="Araştırma raporu: ${esc(r.question)}">
        <div class="ws-report-q">${esc(r.question)}</div>
        <div class="ws-report-meta">
          <span class="ws-badge ${tone}">%${pct} doğrulanmış</span>
          <span>${r.facts} ölçüm</span>
          <span>${(r.duration_ms / 1000).toFixed(0)} sn</span>
          ${r.warnings ? `<span class="ws-badge warn">${r.warnings} uyarı</span>` : ""}
        </div>
      </div>`;
  }).join("")}</div>`;
}

function reportDetail(report) {
  return `
    <div class="ws-report-head">
      <button class="ws-back" id="ws-back">← Raporlar</button>
      <button class="ws-send" id="ws-send-report">Sohbete gönder</button>
    </div>
    <article class="ws-md" draggable="true"
             data-drag="${esc(report.markdown.slice(0, 4000))}">
      ${markdown(report.markdown)}
    </article>`;
}

function bindReports(host) {
  host.querySelectorAll(".ws-report").forEach((card) => {
    card.addEventListener("click", async () => {
      try {
        const data = await window.VQ.api(
          `/api/research/reports/${encodeURIComponent(card.dataset.report)}?fmt=md`);
        state.openReport = data;
        render();
      } catch (err) {
        window.VQ.toast(err.message, "err");
      }
    });
  });

  host.querySelector("#ws-back")?.addEventListener("click", () => {
    state.openReport = null;
    render();
  });

  host.querySelector("#ws-send-report")?.addEventListener("click", () => {
    if (state.openReport) dropIntoChat(state.openReport.markdown.slice(0, 4000));
  });
}

/* -------------------------------- atölye --------------------------------- */

/**
 * Ajanın kendi yazdığı beceriler ve kurduğu otomasyonlar.
 *
 * Burada gösterilen tek şey sayı değil SİCİLDİR: bir beceri kaydedilmiş
 * olduğu için değil, çalıştığı ölçüldüğü için güvenilirdir. Denenmemiş bir
 * beceri açıkça "denenmedi" diye işaretlenir.
 */
export async function loadWorkshop() {
  const { api } = window.VQ;
  try {
    const [skills, automations, summary] = await Promise.all([
      api("/api/workshop/skills"),
      api("/api/workshop/automations"),
      api("/api/workshop/summary"),
    ]);
    state.workshop = { skills: skills.skills || [],
                       automations: automations.automations || [],
                       summary };
  } catch {
    state.workshop = { skills: [], automations: [], summary: null };
  }
  return state.workshop;
}

function workshopView() {
  const data = state.workshop;
  if (!data) return `<div class="dim small">Yükleniyor…</div>`;

  const { skills, automations, summary } = data;
  if (!skills.length && !automations.length) {
    return `
      <div class="ws-empty">
        <div class="ws-empty-title">Atölye boş</div>
        <p>Ajan kendi <b>becerilerini</b> yazabilir ve kendi
        <b>otomasyonlarını</b> kurabilir. Tekrar eden bir iş varsa
        “bunu her sabah yap” demen yeterli.</p>
        <p class="dim small">Yazdığı her şey burada görünür, sicili tutulur ve
        istediğin an durdurabilirsin.</p>
      </div>`;
  }

  return `
    ${summary ? `
      <div class="ws-trust ${summary.skills.untested ? "warn" : "good"}">
        <span class="ws-trust-l">Ajanın yazdıkları</span>
        <span class="ws-trust-v">${summary.skills.working}/${summary.skills.total}
          beceri çalıştığı doğrulanmış · ${summary.automations.enabled} otomasyon açık</span>
      </div>` : ""}

    ${skills.length ? `
      <div class="live-section">Beceriler</div>
      <div class="ws-roles">${skills.map(skillCard).join("")}</div>` : ""}

    ${automations.length ? `
      <div class="live-section">Otomasyonlar</div>
      <div class="ws-roles">${automations.map(automationCard).join("")}</div>` : ""}
  `;
}

function skillCard(skill) {
  const untested = !skill.runs;
  const rate = skill.basari_orani;
  const tone = untested ? "" : (rate ?? 0) >= 0.8 ? "done" : "failed";
  const steps = (skill.steps || []).map((s, i) =>
    `<div class="ws-step-line">${i + 1}. <code>${esc(s.tool)}</code></div>`).join("");

  return `
    <div class="ws-role ${tone}" draggable="true"
         data-drag="Beceri '${esc(skill.label)}': ${esc(skill.description)}">
      <div class="ws-role-head">
        <span class="ws-role-name">${esc(skill.label)}</span>
        <span class="ws-role-ms">v${skill.version}</span>
      </div>
      <div class="ws-role-mission">${esc(skill.when_used || skill.description)}</div>
      <div class="ws-steps-mini">${steps}</div>
      <div class="ws-role-audit">
        ${untested
          ? `<span class="mid">henüz denenmedi</span>`
          : `<span class="${(rate ?? 0) >= 0.8 ? "ok" : "bad"}">${skill.runs} çalışma,
             ${skill.failures} hata</span>`}
        ${skill.author_model ? `<span class="mid">${esc(skill.author_model)}</span>` : ""}
      </div>
      ${skill.last_error ? `<div class="ws-role-err">${esc(skill.last_error)}</div>` : ""}
      <div class="ws-card-actions">
        <button class="ws-mini danger" data-del-skill="${esc(skill.slug)}">Sil</button>
      </div>
    </div>`;
}

function automationCard(auto) {
  const tone = !auto.acik ? "" : (auto.hata && auto.hata * 2 > auto.calisma) ? "failed" : "done";
  return `
    <div class="ws-role ${tone}" draggable="true"
         data-drag="Otomasyon '${esc(auto.label)}': ${esc(auto.purpose)}">
      <div class="ws-role-head">
        <span class="ws-role-name">${esc(auto.label)}</span>
        <span class="ws-role-ms">${auto.acik ? auto.ne_zaman : "durduruldu"}</span>
      </div>
      <div class="ws-role-mission">${esc(auto.purpose)}</div>
      <div class="ws-role-mission dim">${esc(auto.ne_calisir)}</div>
      <div class="ws-role-audit">
        ${auto.calisma
          ? `<span class="${auto.hata ? "bad" : "ok"}">${auto.calisma} çalışma,
             ${auto.hata} hata</span>`
          : `<span class="mid">henüz çalışmadı</span>`}
        ${auto.sonraki && auto.acik
          ? `<span class="fwd">sonraki ${esc(auto.sonraki.slice(11, 16))}</span>` : ""}
      </div>
      ${auto.son_hata ? `<div class="ws-role-err">${esc(auto.son_hata)}</div>` : ""}
      <div class="ws-card-actions">
        <button class="ws-mini" data-run-auto="${esc(auto.slug)}">Şimdi çalıştır</button>
        <button class="ws-mini" data-toggle-auto="${esc(auto.slug)}">
          ${auto.acik ? "Durdur" : "Başlat"}</button>
        <button class="ws-mini danger" data-del-auto="${esc(auto.slug)}">Sil</button>
      </div>
    </div>`;
}

function bindWorkshop(host) {
  const { api, toast } = window.VQ;

  const act = async (node, url, method, confirmText) => {
    if (confirmText && !confirm(confirmText)) return;
    node.disabled = true;
    try {
      const res = await api(url, { method });
      toast(res?.message || "Tamam", res?.ok === false ? "err" : "ok");
      await loadWorkshop();
      render();
    } catch (err) {
      toast(err.message, "err");
      node.disabled = false;
    }
  };

  host.querySelectorAll("[data-run-auto]").forEach((b) => {
    b.onclick = () => act(b, `/api/workshop/automations/${b.dataset.runAuto}/run`, "POST");
  });
  host.querySelectorAll("[data-toggle-auto]").forEach((b) => {
    b.onclick = () => act(b, `/api/workshop/automations/${b.dataset.toggleAuto}/toggle`, "POST");
  });
  host.querySelectorAll("[data-del-auto]").forEach((b) => {
    b.onclick = () => act(b, `/api/workshop/automations/${b.dataset.delAuto}`, "DELETE",
                          "Bu otomasyon ve sicili kalıcı olarak silinsin mi?");
  });
  host.querySelectorAll("[data-del-skill]").forEach((b) => {
    b.onclick = () => act(b, `/api/workshop/skills/${b.dataset.delSkill}`, "DELETE",
                          "Bu beceri ve sicili kalıcı olarak silinsin mi?");
  });
}

/* --------------------------- sürükle → sohbet ----------------------------- */

/**
 * Paneldeki her şey sohbete sürüklenebilir.
 *
 * Amaç, kullanıcının "şu raporun risk bölümünde ne demiştin" diye tarif
 * etmek zorunda kalmamasıdır: parçayı alır, kutuya bırakır, üzerine
 * sorusunu yazar.
 */
function bindDrag(host) {
  host.querySelectorAll("[data-drag]").forEach((node) => {
    node.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", node.dataset.drag);
      e.dataTransfer.effectAllowed = "copy";
      node.classList.add("dragging");
    });
    node.addEventListener("dragend", () => node.classList.remove("dragging"));
    node.addEventListener("dblclick", () => dropIntoChat(node.dataset.drag));
  });
}

export function bindChatDropZone() {
  const input = document.querySelector("#chat-input");
  if (!input || input.dataset.dropBound) return;
  input.dataset.dropBound = "1";

  input.addEventListener("dragover", (e) => {
    e.preventDefault();
    input.classList.add("drop-target");
  });
  input.addEventListener("dragleave", () => input.classList.remove("drop-target"));
  input.addEventListener("drop", (e) => {
    e.preventDefault();
    input.classList.remove("drop-target");
    dropIntoChat(e.dataTransfer.getData("text/plain"));
  });
}

function dropIntoChat(text) {
  const input = document.querySelector("#chat-input");
  if (!input || !text) return;
  const quoted = text.trim().split("\n").map((l) => `> ${l}`).join("\n");
  input.value = (input.value ? input.value + "\n\n" : "") + quoted + "\n\n";
  input.focus();
  input.dispatchEvent(new Event("input"));
  input.scrollTop = input.scrollHeight;
}

/* ------------------------------- yardımcı -------------------------------- */

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/**
 * Küçük Markdown çizici.
 *
 * Dış kütüphane yok: platform sıfır-derleme çalışır ve rapor biçimi bizim
 * ürettiğimiz sınırlı bir alt kümedir (başlık, liste, tablo, alıntı, kalın,
 * kod). Girdi ÖNCE kaçırılır, sonra biçimlendirilir — sıra tersine dönerse
 * rapor içeriği HTML olarak çalışabilir.
 */
function markdown(source) {
  const lines = esc(source).split("\n");
  const out = [];
  let inTable = false;

  const closeTable = () => { if (inTable) { out.push("</table>"); inTable = false; } };

  for (const raw of lines) {
    const line = raw.trimEnd();

    if (/^\|.*\|$/.test(line)) {
      const cells = line.split("|").slice(1, -1).map((c) => c.trim());
      if (/^[-: ]+$/.test(cells.join(""))) continue;      // hizalama satırı
      if (!inTable) { out.push('<table class="ws-table">'); inTable = true; }
      const tag = out[out.length - 1]?.startsWith("<table") ? "th" : "td";
      out.push("<tr>" + cells.map((c) => `<${tag}>${inline(c)}</${tag}>`).join("") + "</tr>");
      continue;
    }
    closeTable();

    if (!line.trim()) { out.push(""); continue; }
    if (line.startsWith("### ")) { out.push(`<h4>${inline(line.slice(4))}</h4>`); continue; }
    if (line.startsWith("## ")) { out.push(`<h3>${inline(line.slice(3))}</h3>`); continue; }
    if (line.startsWith("# ")) { out.push(`<h2>${inline(line.slice(2))}</h2>`); continue; }
    if (line.startsWith("---")) { out.push("<hr>"); continue; }
    if (line.startsWith("&gt; ")) {
      out.push(`<blockquote>${inline(line.slice(5))}</blockquote>`);
      continue;
    }
    if (/^[-*] /.test(line)) { out.push(`<li>${inline(line.slice(2))}</li>`); continue; }
    out.push(`<p>${inline(line)}</p>`);
  }
  closeTable();
  return out.join("\n").replace(/(<li>[\s\S]*?<\/li>\n?)+/g, (m) => `<ul>${m}</ul>`);
}

function inline(text) {
  return text
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`(.+?)`/g, "<code>$1</code>")
    .replace(/_(.+?)_/g, "<i>$1</i>");
}

export const workspaceState = state;
