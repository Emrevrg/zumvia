/* ==========================================================================
   ZUMVIA — Kabuk: profil menüsü ve kontrol merkezi
   --------------------------------------------------------------------------
   Kullanıcı günlük kullanımda hiçbir teknik ayarla uğraşmaz. Yine de her şey
   profil düğmesinin altındaki tek bir merkezden erişilebilir:
     Anahtarlar · Gerçek Para Yetkisi · Acil Fren · Model Sicili · Raporlar
   ========================================================================== */

import { t } from "/ui/i18n.js";

/* ------------------------------ profil menüsü ---------------------------- */

export function openProfileMenu(anchor) {
  const { el, esc, state, navigate, logout, api, toast } = window.VQ;
  document.querySelector(".menu")?.remove();

  const rect = anchor.getBoundingClientRect();
  const menu = el(`
    <div class="menu">
      <div class="menu-head">${esc(state.email || "hesap")}</div>
      <div class="menu-item" data-act="settings"><span data-icon="settings" data-icon-size="15"></span> ${t("nav.settings")}</div>
      <div class="menu-item" data-act="vault"><span data-icon="key" data-icon-size="15"></span> ${t("nav.vault")}</div>
      <div class="menu-item" data-act="bots"><span data-icon="bots" data-icon-size="15"></span> ${t("nav.bots")}</div>
      <div class="menu-item" data-act="backtest"><span data-icon="backtest" data-icon-size="15"></span> ${t("nav.backtest")}</div>
      <div class="menu-sep"></div>
      <div class="menu-item" data-act="killswitch"><span data-icon="brake" data-icon-size="15"></span> ${t("shell.killSwitch")}</div>
      <div class="menu-item" data-act="paper"><span data-icon="flask" data-icon-size="15"></span> ${t("shell.allPaper")}</div>
      <div class="menu-item" data-act="stopall"><span data-icon="stop" data-icon-size="15"></span> ${t("shell.stopAll")}</div>
      <div class="menu-sep"></div>
      <div class="menu-sep"></div>
      <div class="menu-title" data-i18n="common.language">Dil</div>
      <div class="lang-row" id="lang-row"></div>
      <div class="menu-sep"></div>
      <div class="menu-item" data-act="logout"><span data-icon="logout" data-icon-size="15"></span> ${t("shell.logout")}</div>
    </div>`);

  menu.style.left = `${Math.max(10, rect.left)}px`;
  menu.style.bottom = `${Math.max(10, window.innerHeight - rect.top + 8)}px`;
  document.body.appendChild(menu);

  // Dil düğmeleri — seçim anında arayüz yeniden çizilir
  const langRow = menu.querySelector("#lang-row");
  if (langRow) {
    window.VQ.LANGUAGES.forEach((option) => {
      const button = el(`
        <button class="lang-btn ${option.id === window.VQ.lang() ? "active" : ""}"
                type="button">${esc(option.label)}</button>`);
      button.onclick = () => {
        window.VQ.setLang(option.id);
        menu.remove();
      };
      langRow.appendChild(button);
    });
  }
  window.VQ.applyI18n(menu);

  const close = () => {
    menu.remove();
    document.removeEventListener("mousedown", onOutside, true);
  };
  const onOutside = (e) => {
    if (!menu.contains(e.target)) close();
  };
  setTimeout(() => document.addEventListener("mousedown", onOutside, true), 0);

  menu.addEventListener("click", async (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    close();

    if (act === "settings") return openSettingsCenter();
    if (["vault", "bots", "backtest"].includes(act)) return navigate(act);
    if (act === "logout") return logout();

    try {
      if (act === "killswitch") {
        // Onay metni artık GERÇEĞİ söylüyor.
        //
        // Eskiden "mevcut pozisyonlar izlenmeye devam eder" yazıyordu ve bu
        // DOĞRU DEĞİLDİ: botlar durduğu için hiçbiri izlenmiyordu. Kullanıcı
        // korunduğunu sanarak bakmayı bırakıyordu.
        const ok = await window.VQ.confirmModal(
          "Acil fren",
          "<b>Tüm açık pozisyonlar piyasa fiyatından KAPATILACAK</b>, kâr da "
          + "zarar da realize edilecek. Sonra botlar durdurulacak ve yeni "
          + "işlem açılmayacak.<br><br>"
          + "<span class='small'>Frenden sonra sistemin ne zaman kalkacağı "
          + "belli olmadığı için pozisyonlar açıkta bırakılmaz — açıkta "
          + "kalsalar stopları da izlenmezdi.</span>",
          "Kapat ve durdur",
        );
        if (!ok) return;
        const r = await api("/api/safety/kill-switch", {
          method: "POST", body: { active: true, note: "Panelden çekildi" },
        });
        toast(r.message, "err");
        return window.VQ.render();
      }

      const bots = await api("/api/bots");
      if (act === "stopall") {
        await Promise.all(bots.filter((b) => b.status === "running")
          .map((b) => api(`/api/bots/${b.id}/stop`, { method: "POST" })));
        toast(t("msg.allBotsStopped"));
      }
      if (act === "paper") {
        await Promise.all(bots.filter((b) => b.mode === "live")
          .map((b) => api(`/api/bots/${b.id}`, { method: "PATCH", body: { mode: "paper" } })));
        toast(t("msg.allBotsPaper"));
      }
      window.VQ.render();
    } catch (err) {
      toast(err.message, "err");
    }
  });
}

/* ---------------------------- kontrol merkezi ---------------------------- */

export async function openSettingsCenter(initialTab = "safety") {
  const { el, esc, api, toast, navigate } = window.VQ;

  const backdrop = el(`
    <div class="modal-backdrop">
      <div class="panel modal">
        <div class="panel-head">
          <div class="panel-title">Kontrol merkezi</div>
          <button class="icon-btn" data-act="close" aria-label="Kapat" data-icon="close"></button>
        </div>
        <div class="tabs">
          <div class="tab" data-tab="safety">Güvenlik</div>
          <div class="tab" data-tab="keys">Anahtarlar</div>
          <div class="tab" data-tab="models">Model Sicili</div>
          <div class="tab" data-tab="reports">Raporlar</div>
          <div class="tab" data-tab="connect">Bağlantılar</div>
          <div class="tab" data-tab="limits">Sınırlar</div>
        </div>
        <div id="settings-body"><div class="empty">Yükleniyor…</div></div>
      </div>
    </div>`);

  document.getElementById("modal-root").appendChild(backdrop);

  const body = backdrop.querySelector("#settings-body");
  const setTab = async (tab) => {
    backdrop.querySelectorAll("[data-tab]").forEach((t) =>
      t.classList.toggle("active", t.dataset.tab === tab));
    body.innerHTML = `<div class="empty">Yükleniyor…</div>`;
    try {
      if (tab === "safety") await renderSafety(body, backdrop);
      if (tab === "keys") await renderKeys(body, backdrop, navigate);
      if (tab === "models") await renderModels(body);
      if (tab === "reports") await renderReports(body);
      if (tab === "connect") await renderConnections(body);
      if (tab === "limits") await renderLimits(body);
    } catch (err) {
      body.innerHTML = `<div class="callout red">${esc(err.message)}</div>`;
    }
  };

  backdrop.addEventListener("click", (e) => {
    const tab = e.target.closest("[data-tab]")?.dataset.tab;
    if (tab) return setTab(tab);
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "close" || e.target === backdrop) backdrop.remove();
  });

  await setTab(initialTab);
}

/* ------------------------------- güvenlik -------------------------------- */

async function renderSafety(body, backdrop) {
  const { el, esc, api, toast, confirmModal } = window.VQ;
  const status = await api("/api/safety/status");
  const auth = status.live_authorization;

  body.innerHTML = `
    <div class="callout ${status.kill_switch ? "red" : "green"} mb">
      ${status.kill_switch
        ? `<b>Acil fren açık</b> — pozisyonlar kapatıldı, botlar durdu,
           yeni işlem açılmıyor.<br>
           <span class="small">${esc(status.kill_switch_reason)}</span>`
        : `Sistem normal çalışıyor. Acil fren kapalı.<br>
           <span class="small">Fren çekilirse önce tüm pozisyonlar kapatılır,
           sonra sistem durur.</span>`}
      <div class="mt row">
        <button class="btn btn-sm ${status.kill_switch ? "btn-primary" : "btn-danger"}"
                id="toggle-kill">
          ${status.kill_switch ? "Freni kaldır" : "Acil freni çek"}
        </button>
        ${status.kill_switch ? "" : `
          <button class="btn btn-sm" id="flatten-all"
                  title="Pozisyonlardan çık ama sistemi kapatma">
            Her şeyi kapat
          </button>`}
      </div>
    </div>

    <div class="panel-title mb">Gerçek para yetkisi</div>

    ${status.system_paper_only ? `
      <div class="callout">
        Bu kurulumda canlı ticaret <b>sistem genelinde kapalı</b>
        (<code>VQ_FORCE_PAPER_ONLY=true</code>). Yalnızca sanal işlem yapılabilir.
      </div>` : auth.authorized ? `
      <div class="callout green">
        <b>Yetki aktif.</b><br>
        Üst sermaye limiti: <b>${auth.max_capital}</b><br>
        Bitiş: <b>${new Date(auth.expires_at).toLocaleString("tr-TR")}</b>
        (${auth.remaining_hours} saat kaldı)<br>
        <span class="small">Ajan bu limitin üstüne çıkamaz, süreyi uzatamaz.
        Süre dolunca sistem otomatik olarak sanal moda döner.</span>
        <div class="mt">
          <button class="btn btn-sm btn-danger" id="revoke-live">Yetkiyi hemen iptal et</button>
        </div>
      </div>
      ${status.live_bots.length ? `
        <div class="mt small dim">Gerçek parayla çalışan botlar:
          ${status.live_bots.map((b) => `<b>${esc(b.name)}</b> (${b.balance})`).join(", ")}
        </div>` : ""}
      ` : `
      <div class="callout">
        Şu an <b>yalnızca sanal (paper) işlem</b> yapılıyor — gerçek para riski yok.
        <div class="small mt">${esc(auth.reason)}</div>
      </div>

      <div class="grid g2 mt">
        <div class="field">
          <label class="label">Üst sermaye limiti</label>
          <input class="input mono" type="number" id="live-capital" value="100" min="1" step="10" />
          <div class="hint">Ajan bu tutarın üstünde gerçek sermaye kullanamaz.</div>
        </div>
        <div class="field">
          <label class="label">Yetki süresi (saat)</label>
          <input class="input mono" type="number" id="live-hours" value="168" min="1" max="720" />
          <div class="hint">Süre dolunca sistem otomatik olarak sanal moda döner.</div>
        </div>
      </div>

      <div class="field">
        <label class="label">Onay metni</label>
        <input class="input mono" id="live-confirm" placeholder="${esc(status.confirmation_phrase)}" />
        <div class="hint">Devam etmek için birebir
          <code>${esc(status.confirmation_phrase)}</code> yazın.</div>
      </div>

      <div class="callout red">
        <b>Bilmeniz gerekenler:</b>
        <br>• Gerçek para ile işlem kalıcı kayıp riski taşır.
        <br>• Bir bot canlıya ancak <b>en az 20 sanal işlemde kâr faktörü 1.3+</b>
          gösterdiyse alınabilir.
        <br>• Borsa anahtarınızda <b>para çekme yetkisi kapalı</b> olmalıdır.
        <br>• Yetkiyi her an tek tıkla iptal edebilirsiniz.
      </div>

      <button class="btn btn-primary btn-block" id="grant-live">Yetkiyi ver</button>
    `}`;

  const toggle = body.querySelector("#toggle-kill");
  if (toggle) toggle.onclick = async () => {
    // Fren ÇEKERKEN onay istenir, kaldırırken istenmez: biri geri alınamaz
    // bir kapatma dizisi başlatır, diğeri sadece kapıyı açar.
    if (!status.kill_switch) {
      const ok = await confirmModal(
        "Acil fren",
        "<b>Tüm açık pozisyonlar piyasa fiyatından KAPATILACAK</b>, kâr da "
        + "zarar da realize edilecek. Sonra botlar durdurulacak.",
        "Kapat ve durdur");
      if (!ok) return;
    }
    const r = await api("/api/safety/kill-switch", {
      method: "POST", body: { active: !status.kill_switch, note: "Kontrol merkezi" },
    });
    toast(r.message, status.kill_switch ? "ok" : "err");
    await renderSafety(body, backdrop);
  };

  const flatten = body.querySelector("#flatten-all");
  if (flatten) flatten.onclick = async () => {
    const ok = await confirmModal(
      "Her şeyi kapat",
      "Tüm açık pozisyonlar kapatılacak ve botlar durdurulacak. "
      + "<b>Sistem çalışmaya devam eder</b> — acil frenden farkı budur.",
      "Pozisyonları kapat");
    if (!ok) return;
    const r = await api("/api/safety/flatten", { method: "POST" });
    toast(r.message, r.ok ? "ok" : "err");
    await renderSafety(body, backdrop);
  };

  const revoke = body.querySelector("#revoke-live");
  if (revoke) revoke.onclick = async () => {
    const ok = await confirmModal("Yetkiyi iptal et",
      "Gerçek para yetkisi iptal edilecek ve canlı çalışan tüm botlar <b>sanal moda</b> alınacak.",
      "İptal et");
    if (!ok) return;
    const r = await api("/api/safety/live-authorization", { method: "DELETE" });
    toast(r.message);
    await renderSafety(body, backdrop);
  };

  const grant = body.querySelector("#grant-live");
  if (grant) grant.onclick = async () => {
    try {
      const r = await api("/api/safety/live-authorization", {
        method: "POST",
        body: {
          max_capital: Number(body.querySelector("#live-capital").value),
          hours: Number(body.querySelector("#live-hours").value),
          confirmation: body.querySelector("#live-confirm").value,
        },
      });
      toast(r.message);
      await renderSafety(body, backdrop);
    } catch (err) {
      toast(err.message, "err");
    }
  };
}

/* ------------------------------- anahtarlar ------------------------------ */

async function renderKeys(body, backdrop, navigate) {
  const { el, esc, api } = window.VQ;
  const credentials = await api("/api/keys");

  body.innerHTML = `
    <div class="hint mb">
      Ajanın çalışması için tek gereken bir yapay zeka anahtarıdır. Google Gemini
      ücretsiz kotasıyla aylık ~$0 maliyetle başlayabilirsiniz. Anahtarlar
      AES-256 ile şifrelenerek saklanır ve loglara asla düz metin yazılmaz.
    </div>
    <div id="key-rows"></div>
    <button class="btn btn-primary btn-block mt" id="go-vault">Anahtar ekle / yönet</button>`;

  const rows = body.querySelector("#key-rows");
  if (!credentials.length) {
    rows.innerHTML = `<div class="empty small">Henüz anahtar yok — ajan komuta edemez.</div>`;
  }
  credentials.forEach((cred) => {
    const kindIcon = { llm: "brain", exchange: "database", telegram: "bell" }[cred.kind] ?? "key";
    rows.appendChild(el(`
      <div class="row mb" style="justify-content:space-between">
        <span><span class="row-ico" data-icon="${kindIcon}" data-icon-size="15"></span> <b class="small">${esc(cred.label)}</b>
          <span class="badge">${esc(cred.provider)}</span></span>
        <span class="mono small dim">${esc(cred.hint)}</span>
      </div>`));
  });

  body.querySelector("#go-vault").onclick = () => {
    backdrop.remove();
    navigate("vault");
  };
}

/* ------------------------------ model sicili ----------------------------- */

async function renderModels(body) {
  const { esc, api } = window.VQ;
  const scores = await api("/api/safety/model-scores");

  if (!scores.length) {
    body.innerHTML = `
      <div class="empty">
        <div class="big" data-icon="council" data-icon-size="30"></div>
        Henüz sicil oluşmadı.<br>
        <span class="small">Konsey kararlar verdikçe her modelin gerçek performansı
        burada birikir ve oy ağırlıkları buna göre otomatik güncellenir.</span>
      </div>`;
    return;
  }

  body.innerHTML = `
    <div class="hint mb">
      Oy ağırlığı <b>gerçek performansa</b> göre otomatik hesaplanır (10 işlemden
      sonra devreye girer). Doğru karar veren modelin sözü artar, sürekli yanılanınki azalır.
    </div>
    <div style="overflow-x:auto">
      <table class="table">
        <thead><tr>
          <th>Model</th><th class="num">Karar</th><th class="num">İşlem</th>
          <th class="num">Kazanma</th><th class="num">Toplam R</th>
          <th class="num">Beklenti</th><th class="num">Ağırlık</th>
        </tr></thead>
        <tbody>
          ${scores.map((s) => `
            <tr>
              <td><b>${esc(s.model)}</b><br><span class="small dim">${esc(s.provider)}</span></td>
              <td class="num">${s.decisions}</td>
              <td class="num">${s.trades}</td>
              <td class="num">%${s.win_rate_pct}</td>
              <td class="num ${s.total_r >= 0 ? "pos" : "neg"}">${s.total_r}</td>
              <td class="num ${s.expectancy_r >= 0 ? "pos" : "neg"}">${s.expectancy_r}R</td>
              <td class="num"><b>${s.weight}</b></td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

/* -------------------------------- raporlar ------------------------------- */

async function renderReports(body) {
  const { el, esc, api, toast } = window.VQ;
  const reports = await api("/api/safety/reports");

  body.innerHTML = `
    <div class="hint mb">
      Her rapor hem makine okunur JSON hem insan okunur Markdown olarak
      <code>backend/reports/</code> altında saklanır: sermaye, performans, portföy
      riski, kararlar, vetolar, hatalar ve denetim izi.
    </div>
    <button class="btn btn-primary btn-block mb" id="make-report">Yeni rapor üret</button>
    <div id="report-list"></div>
    <div id="report-view"></div>`;

  const list = body.querySelector("#report-list");
  if (!reports.length) {
    list.innerHTML = `<div class="empty small">Henüz rapor üretilmedi.</div>`;
  }
  reports.forEach((r) => {
    const row = el(`
      <div class="row mb" style="justify-content:space-between;cursor:pointer">
        <span><span class="row-ico" data-icon="report" data-icon-size="15"></span> <b class="small">${esc(r.date)}</b>
          <span class="mono small dim">${esc(r.run_id)}</span></span>
        <span class="small dim">${r.size_kb} KB</span>
      </div>`);
    row.onclick = async () => {
      const data = await api(`/api/safety/reports/${r.date}/${r.run_id}`);
      body.querySelector("#report-view").innerHTML = `
        <div class="panel mt" style="max-height:50vh;overflow:auto">
          <pre style="white-space:pre-wrap;font-size:11.5px;line-height:1.6">${esc(data.markdown)}</pre>
        </div>`;
    };
    list.appendChild(row);
  });

  body.querySelector("#make-report").onclick = async (e) => {
    e.target.disabled = true;
    e.target.textContent = "Üretiliyor…";
    try {
      const r = await api("/api/safety/reports", { method: "POST" });
      toast(t("msg.reportReady"));
      body.querySelector("#report-view").innerHTML = `
        <div class="panel mt" style="max-height:50vh;overflow:auto">
          <pre style="white-space:pre-wrap;font-size:11.5px;line-height:1.6">${esc(r.markdown)}</pre>
        </div>`;
    } catch (err) {
      toast(err.message, "err");
    } finally {
      e.target.disabled = false;
      e.target.textContent = "Yeni rapor üret";
    }
  };
}

/* ------------------------------- bağlantılar ----------------------------- */

async function renderConnections(body) {
  const { el, esc, api, toast, confirmModal } = window.VQ;
  const [info, tokens, catalog, desktop] = await Promise.all([
    api("/mcp", { auth: false }),
    api("/api/safety/mcp-tokens"),
    api("/api/agent/catalog", { auth: false }).catch(() => ({ control_tools: [] })),
    api("/api/agent/desktop-tools", { auth: false }).catch(() => ({ tools: [] })),
  ]);
  const base = location.origin;
  const active = tokens.filter((t) => !t.revoked);
  const tools = (catalog.control_tools || []).filter(t => t.id !== "api");

  body.innerHTML = `
    <div class="hint mb">
      Platform bir <b>MCP sunucusu</b> olarak da çalışır. Bağladığınız araç
      (Claude Code, Claude Desktop, Codex, Cursor…) bu sistemin komutanına dönüşür:
      aynı ${info.tools} araç, aynı risk kalkanı, aynı sınırlar geçerlidir.
    </div>

    <div class="panel mb" style="background:var(--bg-input)">
      <div class="panel-title" style="display:flex;align-items:center;gap:8px">
        <span data-icon="command" data-icon-size="15"></span> Bu makinedeki araçlar
      </div>
      <div class="hint mb">
        Terminal ajanları PATH üzerinden, masaüstü uygulamaları da bilinen kurulum
        klasörlerinden algılanır. Kurulu olanları buradan <b>başlatabilirsiniz</b>;
        MCP anahtarınızı verdiğinizde bu platformun komutanına dönüşürler.
      </div>
      <div id="local-tools" class="grid g2" style="gap:8px"></div>
    </div>

    <div class="callout green mb">
      Bağlı araç da <b>gerçek para yetkisi veremez</b>, risk tavanlarını aşamaz,
      stop-loss'suz emir açamaz. Yetki yalnızca Güvenlik sekmesinden verilir.
    </div>

    <div class="panel-title mb">1. Erişim anahtarı</div>
    <div class="hint mb">
      Her araç için ayrı anahtar üretin. Anahtar tek seferlik gösterilir,
      veritabanında yalnızca özeti saklanır ve istediğiniz an iptal edilir.
    </div>

    <div class="grid g2 mb">
      <div class="field">
        <label class="label">Anahtar adı</label>
        <input class="input" id="tok-name" placeholder="örn. Claude Desktop — ev bilgisayarı" />
      </div>
      <div class="field">
        <label class="label">Geçerlilik (gün)</label>
        <input class="input mono" id="tok-days" type="number" min="1" max="365"
               placeholder="boş = süresiz" />
      </div>
    </div>
    <button class="btn btn-primary btn-block mb" id="tok-create">Yeni anahtar üret</button>

    <div id="tok-fresh"></div>
    <div id="tok-list" class="mb"></div>

    <div class="panel-title mb">2. Bağlantı komutları</div>
    <div id="tok-commands"></div>`;

  // --- Bu makinedeki araçlar (terminal + masaüstü) ---
  const lt = body.querySelector("#local-tools");
  if (lt) {
    const rows = (desktop.tools || []).length ? desktop.tools : tools;
    if (!rows.length) {
      lt.innerHTML = `<div class="dim small">Hiç araç bulunamadı.</div>`;
    } else {
      rows.forEach((t) => {
        const ok = !!t.available;
        const card = el(`
          <div class="choice ${ok ? "selected" : ""}" style="padding:10px 12px;gap:10px">
            <span class="dot ${ok ? "dot-live" : "dot-off"}"></span>
            <div style="flex:1;min-width:0">
              <div class="choice-title" style="font-size:14px">
                ${esc(t.label)} <span class="dim">· ${ok ? "kurulu" : "yüklü değil"}</span>
              </div>
              <div class="mono small dim"
                   style="font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                ${esc(t.path || t.install || "")}
              </div>
            </div>
            ${ok && t.kind
              ? `<button class="btn btn-sm" data-launch="${esc(t.id)}">Başlat</button>`
              : ok ? `<span class="badge badge-green">hazır</span>`
                   : `<span class="badge">kurulu değil</span>`}
          </div>`);

        card.querySelector("[data-launch]")?.addEventListener("click", async (e) => {
          const button = e.currentTarget;
          button.disabled = true;
          button.textContent = "Açılıyor…";
          try {
            await api(`/api/agent/desktop-tools/${button.dataset.launch}/launch`,
                      { method: "POST" });
            toast(`${t.label} başlatıldı.`, "ok");
          } catch (err) {
            toast(err.message, "err");
          } finally {
            button.disabled = false;
            button.textContent = "Başlat";
          }
        });

        lt.appendChild(card);
      });
    }
  }

  /* ----------------------------- anahtar listesi ---------------------------- */
  const list = body.querySelector("#tok-list");
  if (!tokens.length) {
    list.innerHTML = `<div class="empty small">Henüz anahtar üretilmedi.</div>`;
  }
  tokens.forEach((t) => {
    const row = el(`
      <div class="row mb" style="justify-content:space-between">
        <span>
          <span class="row-ico" data-icon="key" data-icon-size="14"></span>
          <b class="small">${esc(t.name)}</b>
          <span class="mono small dim">${esc(t.prefix)}</span>
          ${t.revoked ? `<span class="badge badge-red">iptal</span>` : ""}
        </span>
        <span class="small dim">
          ${t.calls} çağrı${t.last_used_at
            ? ` · son: ${new Date(t.last_used_at).toLocaleString("tr-TR")}` : " · hiç kullanılmadı"}
          ${t.revoked ? "" : `<button class="btn btn-sm btn-ghost" data-revoke="${t.id}">İptal et</button>`}
        </span>
      </div>`);
    list.appendChild(row);
  });

  list.querySelectorAll("[data-revoke]").forEach((btn) => {
    btn.onclick = async () => {
      const ok = await confirmModal("Anahtarı iptal et",
        "Bu anahtarla bağlı araç <b>anında</b> erişimini kaybeder.", "İptal et");
      if (!ok) return;
      const r = await api(`/api/safety/mcp-tokens/${btn.dataset.revoke}`, { method: "DELETE" });
      toast(r.message);
      renderConnections(body);
    };
  });

  /* ------------------------------- komutlar -------------------------------- */
  const drawCommands = (token) => {
    const shown = token || (active.length
      ? "<ANAHTARINIZ>"
      : "<önce yukarıdan anahtar üretin>");

    const commands = [
      {
        title: "Claude Code — tek komut (önerilen)",
        desc: "Terminali aç, yapıştır — anında bağlanır, hiçbir dosya elle düzenlenmez.",
        code: `claude mcp add --transport http zumvia ${base}/mcp --header "Authorization: Bearer ${shown}"`,
      },
      {
        title: "Claude Desktop / Cursor — JSON (Ayarlar → Connectors → Add custom server)",
        desc: "claude_desktop_config.json veya Cursor → Settings → MCP → Add server içine yapıştırın.",
        code: `{
  "mcpServers": {
    "zumvia": {
      "type": "http",
      "url": "${base}/mcp",
      "headers": { "Authorization": "Bearer ${shown}" }
    }
  }
}`,
      },
      {
        title: "Terminal ajanları (stdio — ağ gerekmez)",
        desc: "Sunucu doğrudan süreç olarak çalışır; HTTP bağlantısı kurulmaz.",
        code: (info.nasil_baglanir?.stdio_alternatifi || "python backend/mcp_server.py")
          + `
# ortam değişkeni:  VQ_MCP_TOKEN=${shown}`,
      },
    ];

    body.querySelector("#tok-commands").innerHTML = commands.map((c, i) => `
      <div class="panel mb" style="background:var(--bg-input)">
        <div class="panel-title">${esc(c.title)}</div>
        <div class="hint mb">${esc(c.desc)}</div>
        <pre class="code-block" id="code-${i}">${esc(c.code)}</pre>
        <button class="btn btn-sm mt" data-copy="${i}">Kopyala</button>
      </div>`).join("");

    body.querySelectorAll("[data-copy]").forEach((btn) => {
      btn.onclick = async () => {
        const text = body.querySelector(`#code-${btn.dataset.copy}`).textContent;
        try {
          await navigator.clipboard.writeText(text);
          toast(t("msg.copied"));
        } catch {
          toast(t("msg.copyFailed"), "err");
        }
      };
    });
  };
  drawCommands(null);

  /* ------------------------------ anahtar üret ----------------------------- */
  body.querySelector("#tok-create").onclick = async (event) => {
    event.target.disabled = true;
    try {
      const days = Number(body.querySelector("#tok-days").value) || null;
      const result = await api("/api/safety/mcp-tokens", {
        method: "POST",
        body: { name: body.querySelector("#tok-name").value || "MCP istemcisi", days },
      });

      body.querySelector("#tok-fresh").innerHTML = `
        <div class="callout green mb">
          <b>Anahtar hazır — yalnızca şimdi görebilirsiniz.</b>
          <pre class="code-block mt" id="fresh-token">${esc(result.token)}</pre>
          <div class="small mt">${esc(result.uyari)}</div>
        </div>`;
      drawCommands(result.token);

      // Listeyi anında güncelle (yeniden çizmeden — düz metin ekranda kalsın)
      const fresh = el(`
        <div class="row mb" style="justify-content:space-between">
          <span>
            <span class="row-ico" data-icon="key" data-icon-size="14"></span>
            <b class="small">${esc(result.name)}</b>
            <span class="mono small dim">${esc(result.token.slice(0, 10))}…</span>
            <span class="badge badge-green">yeni</span>
          </span>
          <span class="small dim">henüz kullanılmadı</span>
        </div>`);
      const listBox = body.querySelector("#tok-list");
      listBox.querySelector(".empty")?.remove();
      listBox.prepend(fresh);

      body.querySelector("#tok-name").value = "";
      toast(t("msg.tokenCreated"));
    } catch (err) {
      toast(err.message, "err");
    } finally {
      event.target.disabled = false;
    }
  };
}

/* -------------------------------- sınırlar ------------------------------- */

async function renderLimits(body) {
  const { esc, api } = window.VQ;
  const [health, catalog, safety] = await Promise.all([
    api("/api/health", { auth: false }),
    api("/api/bots/catalog", { auth: false }),
    api("/api/safety/status").catch(() => ({ preferences: {} })),
  ]);
  const prefs = safety.preferences || {};

  const limits = [
    ["Tek işlemde en fazla risk", `%${health.hard_limits.max_risk_pct}`,
     "20 üst üste zararda bile hesap durur, sıfırlanmaz"],
    ["Zorunlu stop-loss", "her işlemde",
     "Stopsuz veya mantıksız stoplu emir açılamaz"],
    ["Minimum risk/ödül", `1:${health.hard_limits.min_rr}`,
     "Asimetri yoksa işlem yok"],
    ["Minimum güven eşiği", `%${(health.hard_limits.min_confidence * 100).toFixed(0)}`,
     "Kararsız modelin işlemi geçmez"],
    ["Günlük kayıp devre kesici", `%${health.hard_limits.max_daily_loss_pct}`,
     `Tüm pozisyonlar kapanır, bot ${catalog.hard_limits.circuit_breaker_lock_hours} saat kilitlenir`],
    ["Portföy ısısı tavanı", "%3 (ayarlanabilir)",
     "Tüm açık risklerin toplamı bu oranı aşamaz"],
    ["Korelasyon kalkanı", "aktif",
     "Aynı yönde, birbirine bağlı varlıklarda küme kurulamaz"],
    ["Gerçek paraya geçiş", "yalnızca kullanıcı",
     "Ajan yetki veremez; verilen yetkinin limitini yükseltemez"],
  ];

  body.innerHTML = `
    <div class="callout green mb">
      Bu sınırlar <b>kodda sabittir</b> (<code>app/layers/l4_risk.py</code>).
      Ne siz ne ajan ne de MCP ile bağlanan bir araç bunların üstüne çıkabilir.
    </div>
    <div style="overflow-x:auto">
      <table class="table">
        <thead><tr><th>Kural</th><th>Değer</th><th>Ne işe yarar</th></tr></thead>
        <tbody>
          ${limits.map(([k, v, why]) => `
            <tr>
              <td>${esc(k)}</td>
              <td class="mono"><b>${esc(v)}</b></td>
              <td class="small dim">${esc(why)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>
    <div class="panel-title mt mb">Tercihler</div>

    <div class="pref-row">
      <div>
        <div class="pref-title">Risk bütçesi</div>
        <div class="pref-hint">
          Tüm botlar için işlem başına üst sınır. <b>%0</b> yaparsanız sistem yeni
          pozisyon açmaz; yalnızca izler ve açık pozisyonları yönetir.
        </div>
      </div>
      <div class="row" style="gap:6px;flex-shrink:0">
        <input class="input mono" id="pref-risk" type="number" min="0"
               max="${health.hard_limits.max_risk_pct}" step="0.05"
               value="${prefs.risk_budget_pct ?? 0.5}" style="width:88px" />
        <button class="btn btn-sm" id="pref-risk-save">Kaydet</button>
      </div>
    </div>

    <div class="pref-row">
      <div>
        <div class="pref-title">Başlangıç modu</div>
        <div class="pref-hint">
          Yeni botlar hangi modda başlasın? <b>Gerçek para</b> seçiliyse ve borsa
          anahtarınız + canlı yetkiniz varsa botlar doğrudan gerçek parayla açılır.
          Eksik varsa sanal başlar ve sebebi söylenir.
        </div>
      </div>
      <select class="select" id="pref-mode" style="max-width:170px;flex-shrink:0">
        <option value="paper" ${prefs.default_trading_mode !== "live" ? "selected" : ""}>Sanal (paper)</option>
        <option value="live" ${prefs.default_trading_mode === "live" ? "selected" : ""}>Gerçek para</option>
      </select>
    </div>

    <div class="pref-row">
      <div>
        <div class="pref-title">Çoklu model stratejisi</div>
        <div class="pref-hint" id="pool-hint">
          Birden çok model çalıştırma biçimi. Aynı sağlayıcının modelleri aynı
          kotayı paylaşır; farklı sağlayıcılar paylaşmaz.
        </div>
      </div>
      <select class="select" id="pref-pool" style="max-width:200px;flex-shrink:0">
        <option value="single" ${prefs.model_strategy === "single" ? "selected" : ""}>Tek model</option>
        <option value="same_provider" ${prefs.model_strategy === "same_provider" ? "selected" : ""}>Aynı sağlayıcı, çok model</option>
        <option value="multi_provider" ${prefs.model_strategy === "multi_provider" ? "selected" : ""}>Farklı sağlayıcılar</option>
      </select>
    </div>

    <div class="pref-row">
      <div>
        <div class="pref-title">İstemi otomatik çevir</div>
        <div class="pref-hint">
          Arayüz dilinizden farklı bir dilde yazarsanız mesajınız anlamı
          bozulmadan arayüz diline çevrilir; orijinali de kayıtta kalır.
        </div>
      </div>
      <label class="switch" style="flex-shrink:0">
        <input type="checkbox" id="pref-translate"
               ${prefs.auto_translate_prompt === false ? "" : "checked"} />
        <span>Açık</span>
      </label>
    </div>

    <div class="callout mt small">
      <b>Sorumluluk reddi:</b> ZUMVIA finansal tavsiye vermez. Piyasa
      dalgalanmaları getiriyi etkiler; geçmiş performans gelecek getiriyi
      garanti etmez. Kararların sorumluluğu kullanıcıya aittir.
    </div>`;

  body.querySelector("#pref-risk-save")?.addEventListener("click", async (e) => {
    const value = Number(body.querySelector("#pref-risk").value);
    e.target.disabled = true;
    try {
      const r = await api("/api/safety/risk-budget",
                          { method: "POST", body: { risk_budget_pct: value } });
      window.VQ.toast(r.message, "ok");
    } catch (err) { window.VQ.toast(err.message, "err"); }
    e.target.disabled = false;
  });

  const savePref = async (patch, okMessage) => {
    try {
      const r = await api("/api/safety/preferences", { method: "POST", body: patch });
      window.VQ.toast(okMessage(r), "ok");
      return r;
    } catch (err) {
      window.VQ.toast(err.message, "err");
      return null;
    }
  };

  body.querySelector("#pref-mode")?.addEventListener("change", (e) =>
    savePref({ default_trading_mode: e.target.value },
             (r) => r.effective_mode === "live"
               ? "Yeni botlar GERÇEK PARA ile başlayacak."
               : `Sanal modda başlayacak — ${r.mode_reason}`));

  const poolHint = body.querySelector("#pool-hint");
  const refreshPool = async (strategy) => {
    try {
      const pool = await api(`/api/safety/model-pool?strategy=${strategy}&size=3`);
      if (!poolHint) return;
      poolHint.innerHTML = pool.usable
        ? `<b>${pool.size} model</b> · dakikada ${pool.combined_rpm} istek ·
           ${pool.parallel_capacity} bağımsız kanal.
           ${(pool.notes || []).map((n) => esc(n)).join(" ")}`
        : esc(pool.reason || "Model havuzu kurulamadı.");
    } catch { /* ipucu güncellenemedi; ayar yine de çalışır */ }
  };

  body.querySelector("#pref-pool")?.addEventListener("change", async (e) => {
    await savePref({ model_strategy: e.target.value }, () => "Model stratejisi kaydedildi.");
    refreshPool(e.target.value);
  });
  refreshPool(prefs.model_strategy || "single");

  body.querySelector("#pref-translate")?.addEventListener("change", async (e) => {
    try {
      await api("/api/safety/preferences",
                { method: "POST", body: { auto_translate_prompt: e.target.checked } });
      window.VQ.toast(e.target.checked ? "İstem çevirisi açık" : "İstem çevirisi kapalı", "ok");
    } catch (err) {
      window.VQ.toast(err.message, "err");
      e.target.checked = !e.target.checked;
    }
  });
}
