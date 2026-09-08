/**
 * ZUMVIA FINANCE — canlı piyasa ekranı
 * =====================================
 *
 * Google Finance'in yaptığını yapar, bir farkla: ekrandaki her sayı ajanın
 * gördüğü sayıyla AYNIDIR. `/api/finance/*` uçları, ajanın `market_pulse`
 * aracıyla aynı ölçüm merkezini kullanır — iki ayrı "gerçek" olmaz.
 *
 * Üç arayüz kuralı:
 *
 *   1. Ölçülemeyen kart BOŞ kalmaz, sebebini yazar. Eski fiyatı canlıymış
 *      gibi göstermek, en pahalı arayüz hatasıdır.
 *   2. Verinin YAŞI görünür. "Canlı" demek, 20 saniyelik veriyi 20 saniyelik
 *      olduğunu bilerek göstermektir.
 *   3. Otomatik yenileme sekme arkada ise DURUR. Görünmeyen bir sayfa için
 *      borsayı dövmek, kotayı boşa harcamaktır.
 */
import { icon } from "/ui/icons.js";
import { CandleChart, formatNumber } from "/ui/chart.js";
import { t } from "/ui/i18n.js";

const REFRESH_MS = 30_000;
const NEWS_REFRESH_MS = 180_000;

const financeState = {
  board: null,
  news: null,
  tab: "overview",     // overview | watchlist | news
  detail: null,        // açık enstrüman anahtarı
  timer: null,
  newsTimer: null,
  loading: false,
};

/* ============================== yardımcılar ============================== */

function vq() {
  return window.VQ;
}

/** Değişim yüzdesini renkli ve işaretli yazar. */
function changeChip(pct) {
  if (pct === null || pct === undefined) return `<span class="fin-chip dim">—</span>`;
  const dir = pct > 0 ? "up" : pct < 0 ? "down" : "flat";
  const arrow = pct > 0 ? "▲" : pct < 0 ? "▼" : "•";
  return `<span class="fin-chip ${dir}">${arrow} ${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%</span>`;
}

/**
 * Fiyatı büyüklüğüne göre biçimlendirir.
 *
 * XRP 1.4231 ile Bitcoin 79.594 aynı sayıda ondalıkla yazılamaz: birinde
 * anlamlı basamak kaybolur, diğerinde ekran gereksiz gürültüyle dolar.
 */
function priceText(value) {
  if (value === null || value === undefined) return "—";
  const abs = Math.abs(value);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 6;
  return value.toLocaleString("tr-TR", { minimumFractionDigits: 2,
                                         maximumFractionDigits: digits });
}

/** Kart içi mini grafik. Eksen yok — amaç ölçmek değil, yönü göstermek. */
function sparkline(points, positive) {
  if (!points || points.length < 2) return "";
  const w = 96, h = 28;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const step = w / (points.length - 1);
  const path = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${(h - ((p - min) / span) * h).toFixed(1)}`)
    .join(" ");
  const color = positive ? "var(--emerald)" : "var(--danger)";
  return `<svg class="fin-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
               aria-hidden="true"><path d="${path}" fill="none" stroke="${color}"
               stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

function ageText(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 2) return t("fin.ago.now");
  if (seconds < 60) return `${Math.round(seconds)} ${t("fin.ago.sec")}`;
  return `${Math.round(seconds / 60)} ${t("fin.ago.min")}`;
}

/* ================================ kartlar ================================ */

/**
 * Enstrümanın EKRANDA görünen adı.
 *
 * Sunucu sembolü ve varsayılan adı verir; çevirisi varsa arayüz kendi
 * dilinde yazar. "Altın (ons)" İngilizce arayüzde "Gold (oz)" olur;
 * "BTC/USDT" gibi semboller olduğu gibi kalır — onların çevirisi yok.
 */
function instrumentLabel(row) {
  const key = `inst.${row.symbol}`;
  const translated = t(key);
  return translated === key ? row.label : translated;
}

function pulseCard(row) {
  const { esc } = vq();
  if (!row.ok) {
    return `
      <div class="fin-pulse bad" title="${esc(row.error)}">
        <div class="fin-pulse-label">${esc(instrumentLabel(row))}</div>
        <div class="fin-pulse-err">${t("fin.failed")}</div>
      </div>`;
  }
  return `
    <div class="fin-pulse" data-open="${esc(row.key)}">
      <div class="fin-pulse-label">${esc(instrumentLabel(row))}</div>
      <div class="fin-pulse-price">${priceText(row.price)}</div>
      ${changeChip(row.change_pct)}
    </div>`;
}

function tickRow(row) {
  const { esc } = vq();
  if (!row.ok) {
    return `
      <tr class="fin-row bad">
        <td>
          <div class="fin-name">${esc(instrumentLabel(row))}</div>
          <div class="fin-sym">${esc(row.symbol)}</div>
        </td>
        <td colspan="4" class="fin-err">${t("fin.notMeasured")} — ${esc(row.error)}</td>
        <td class="right"><button class="icon-btn sm" data-remove="${esc(row.key)}"
                                  title="${t('fin.remove')}" data-icon="trash"></button></td>
      </tr>`;
  }
  const up = (row.change_pct ?? 0) >= 0;
  return `
    <tr class="fin-row" data-open="${esc(row.key)}">
      <td>
        <div class="fin-name">${esc(instrumentLabel(row))}</div>
        <div class="fin-sym">${row.label === row.symbol ? "" : esc(row.symbol) + " · "}${esc(row.exchange)}</div>
      </td>
      <td class="mono right">${priceText(row.price)}</td>
      <td class="right">${changeChip(row.change_pct)}</td>
      <td class="fin-spark-cell">${sparkline(row.spark, up)}</td>
      <td class="mono right dim small">
        ${row.day_high !== null && row.day_low !== null
          ? `${priceText(row.day_low)} – ${priceText(row.day_high)}` : "—"}
      </td>
      <td class="right">
        <button class="icon-btn sm" data-remove="${esc(row.key)}"
                title="${t('fin.remove')}" data-icon="trash"></button>
      </td>
    </tr>`;
}

function moversPanel(title, rows, kind) {
  const { esc } = vq();
  if (!rows.length) return "";
  return `
    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">${esc(title)}</div>
      </div>
      <div class="fin-movers">
        ${rows.map((r) => `
          <div class="fin-mover ${kind}" data-open="${esc(r.key)}">
            <div>
              <div class="fin-name">${esc(instrumentLabel(r))}</div>
              <div class="fin-sym">${esc(r.symbol)}</div>
            </div>
            <div class="right">
              <div class="mono">${priceText(r.price)}</div>
              ${changeChip(r.change_pct)}
            </div>
          </div>`).join("")}
      </div>
    </div>`;
}

function newsPanel(news, compact = false) {
  const { esc } = vq();
  if (!news) return `<div class="empty"><div class="spinner"></div>${t("fin.newsLoading")}</div>`;
  if (!news.count) {
    return `
      <div class="panel">
        <div class="callout">${esc(news.not || t("fin.newsUnavailable"))}</div>
      </div>`;
  }
  const rows = compact ? news.headlines.slice(0, 8) : news.headlines;
  const moodClass = news.mood > 1 ? "up" : news.mood < -1 ? "down" : "flat";
  return `
    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">${t("fin.newsTitle")}</div>
        <span class="fin-chip ${moodClass}">${t("fin.mood")}: ${esc(t("mood." + (news.mood_code || "mixed")))} (${news.mood})</span>
      </div>
      <div class="fin-news">
        ${rows.map((h) => {
          const tone = h.score > 0 ? "up" : h.score < 0 ? "down" : "flat";
          return `
            <a class="fin-news-item" href="${esc(h.link || "#")}" target="_blank"
               rel="noopener noreferrer">
              <span class="fin-score ${tone}">${h.score >= 0 ? "+" : ""}${h.score}</span>
              <span class="fin-news-body">
                <span class="fin-news-title">${esc(h.title)}</span>
                <span class="fin-news-meta">${esc(h.source)} · ${esc(h.published || "")}</span>
              </span>
            </a>`;
        }).join("")}
      </div>
      <div class="hint mt">
        ${t("fin.newsDisclaimer")}
      </div>
    </div>`;
}

/* =============================== ana görünüm ============================== */

export async function viewFinance(root) {
  const { $, api, esc, toast, el } = vq();

  root.innerHTML = `
    <div class="fin-head">
      <div class="fin-search">
        <span class="fin-search-ico">${icon("search", 15)}</span>
        <input class="input" id="fin-q" placeholder="${t('fin.searchPlaceholder')}"
               autocomplete="off" />
        <div class="fin-results hidden" id="fin-results"></div>
      </div>
      <span class="spacer"></span>
      <div class="tabs fin-tabs">
        <div class="tab ${financeState.tab === "overview" ? "active" : ""}" data-fin-tab="overview">${t("fin.overview")}</div>
        <div class="tab ${financeState.tab === "watchlist" ? "active" : ""}" data-fin-tab="watchlist">${t("fin.watchlist")}</div>
        <div class="tab ${financeState.tab === "news" ? "active" : ""}" data-fin-tab="news">${t("fin.news")}</div>
      </div>
      <button class="icon-btn" id="fin-refresh" title="${t('fin.refreshNow')}" data-icon="refresh"></button>
    </div>

    <div class="fin-pulse-strip" id="fin-pulse">
      <div class="empty sm"><div class="spinner"></div>${t("fin.measuring")}</div>
    </div>

    <div id="fin-body">
      <div class="empty"><div class="spinner"></div>${t("fin.loading")}</div>
    </div>

    <div class="fin-foot dim small" id="fin-foot"></div>
  `;

  vq().hydrateIcons?.(root);
  bindHead(root);

  await loadBoard(root);
  if (financeState.tab === "news" || financeState.tab === "overview") loadNews(root);

  startTimers(root);
}

function bindHead(root) {
  const { $, toast } = vq();

  root.querySelectorAll("[data-fin-tab]").forEach((tab) => {
    tab.addEventListener("click", () => {
      financeState.tab = tab.dataset.finTab;
      root.querySelectorAll("[data-fin-tab]").forEach((t) =>
        t.classList.toggle("active", t === tab));
      renderBody(root);
      if (financeState.tab === "news" && !financeState.news) loadNews(root);
    });
  });

  root.querySelector("#fin-refresh")?.addEventListener("click", async () => {
    try {
      await vq().api("/api/finance/refresh", { method: "POST" });
      financeState.board = null;
      financeState.news = null;
      await loadBoard(root);
      loadNews(root);
      toast(vq().t("msg.marketRefreshed"));
    } catch (err) {
      toast(err.message, "err");
    }
  });

  bindSearch(root);
}

/* ================================ arama ================================== */

function bindSearch(root) {
  const { api, esc, toast } = vq();
  const input = root.querySelector("#fin-q");
  const box = root.querySelector("#fin-results");
  if (!input || !box) return;

  let timer = null;
  let lastQuery = "";

  const close = () => box.classList.add("hidden");

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    // Her tuş vuruşunda arama yapmak borsayı gereksiz döver.
    timer = setTimeout(async () => {
      if (q === lastQuery) return;
      lastQuery = q;
      try {
        const data = await api(`/api/finance/search?q=${encodeURIComponent(q)}`);
        if (!data.results.length) {
          box.innerHTML = `<div class="fin-result dim">${t("fin.noMatch")}</div>`;
        } else {
          // Etiket sembolle aynıysa iki kez yazma: "DOGE/USDT DOGE/USDT"
          // okunmaz ve sütunu şişirir.
          box.innerHTML = data.results.map((r) => `
            <div class="fin-result" data-add="${esc(r.symbol)}" data-market="${esc(r.market)}">
              <span>${esc(r.label)}</span>
              <span class="dim small">${r.label === r.symbol ? "" : esc(r.symbol) + " · "}${esc(r.group)}</span>
            </div>`).join("");
          box.querySelectorAll("[data-add]").forEach((node) => {
            node.addEventListener("click", async () => {
              try {
                const res = await vq().api("/api/finance/watchlist", {
                  method: "POST",
                  body: { symbol: node.dataset.add, market: node.dataset.market },
                });
                toast(res.added ? `${node.dataset.add} listene eklendi.`
                                : (res.not || "Zaten listede."));
                input.value = "";
                close();
                financeState.board = null;
                await loadBoard(root);
              } catch (err) {
                toast(err.message, "err");
              }
            });
          });
        }
        box.classList.remove("hidden");
      } catch (err) {
        box.innerHTML = `<div class="fin-result dim">${esc(err.message)}</div>`;
        box.classList.remove("hidden");
      }
    }, 280);
  });

  input.addEventListener("blur", () => setTimeout(close, 180));
}

/* ============================== veri yükleme ============================= */

async function loadBoard(root) {
  const { api, esc } = vq();
  if (financeState.loading) return;
  financeState.loading = true;
  try {
    financeState.board = await api("/api/finance/board");
  } catch (err) {
    const body = root.querySelector("#fin-body");
    if (body) {
      body.innerHTML = `<div class="panel"><div class="callout red">
        Piyasa verisi alınamadı: ${esc(err.message)}
      </div></div>`;
    }
    return;
  } finally {
    financeState.loading = false;
  }
  renderPulse(root);
  renderBody(root);
  renderFoot(root);
}

async function loadNews(root) {
  try {
    financeState.news = await vq().api("/api/finance/news?limit=40");
  } catch {
    financeState.news = { count: 0, not: t("fin.newsUnavailable") };
  }
  if (financeState.tab === "news" || financeState.tab === "overview") renderBody(root);
}

function startTimers(root) {
  clearInterval(financeState.timer);
  clearInterval(financeState.newsTimer);

  // Sekme arka plandayken yenileme YAPILMAZ: görünmeyen bir sayfa için
  // borsayı dövmek kotayı boşa harcar.
  financeState.timer = setInterval(() => {
    if (document.hidden || !document.body.contains(root)) return;
    loadBoard(root);
  }, REFRESH_MS);

  financeState.newsTimer = setInterval(() => {
    if (document.hidden || !document.body.contains(root)) return;
    loadNews(root);
  }, NEWS_REFRESH_MS);
}

/* ================================ çizim ================================== */

function renderPulse(root) {
  const host = root.querySelector("#fin-pulse");
  const board = financeState.board;
  if (!host || !board) return;
  host.innerHTML = board.pulse.map(pulseCard).join("");
  bindOpen(root, host);
}

function renderFoot(root) {
  const host = root.querySelector("#fin-foot");
  const board = financeState.board;
  if (!host || !board) return;
  const ages = [...board.pulse, ...board.watchlist]
    .map((r) => r.age_seconds).filter((a) => a !== null && a !== undefined);
  const oldest = ages.length ? Math.max(...ages) : 0;
  host.innerHTML = `
    ${board.measured} ${t("fin.measured")}${board.unavailable ? `, ${board.unavailable} ${t("fin.unavailable")}` : ""} ·
    ${t("fin.oldest")} ${vq().esc(ageText(oldest))} ·
    ${board.failed_count ? `${board.failed_count} ${t("fin.someFailed")}` : t("fin.allMeasured")}`;
}

function renderBody(root) {
  const host = root.querySelector("#fin-body");
  const board = financeState.board;
  if (!host) return;
  if (!board) {
    host.innerHTML = `<div class="empty"><div class="spinner"></div>${t("fin.loading")}</div>`;
    return;
  }

  if (financeState.tab === "news") {
    host.innerHTML = newsPanel(financeState.news, false);
    return;
  }

  const table = `
    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">${t("fin.watchlist")}</div>
        <span class="dim small">${t("fin.watchHint")}</span>
      </div>
      <div class="table-wrap">
        <table class="table fin-table">
          <thead>
            <tr><th>${t("fin.instrument")}</th><th class="right">${t("fin.price")}</th><th class="right">${t("fin.change")}</th>
                <th></th><th class="right">${t("fin.range")}</th><th></th></tr>
          </thead>
          <tbody>${board.watchlist.map(tickRow).join("")}</tbody>
        </table>
      </div>
    </div>`;

  if (financeState.tab === "watchlist") {
    host.innerHTML = table;
  } else {
    host.innerHTML = `
      <div class="grid g2 mb">
        ${moversPanel(t("fin.gainers"), board.gainers, "up")}
        ${moversPanel(t("fin.losers"), board.losers, "down")}
      </div>
      ${table}
      <div class="mt">${newsPanel(financeState.news, true)}</div>`;
  }

  vq().hydrateIcons?.(host);
  bindOpen(root, host);
  bindRemove(root, host);
}

function bindOpen(root, host) {
  host.querySelectorAll("[data-open]").forEach((node) => {
    node.addEventListener("click", (event) => {
      if (event.target.closest("[data-remove]")) return;
      openDetail(node.dataset.open);
    });
  });
}

function bindRemove(root, host) {
  const { api, toast } = vq();
  host.querySelectorAll("[data-remove]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      event.stopPropagation();
      const key = node.dataset.remove;
      try {
        const list = await api("/api/finance/watchlist");
        const row = list.items.find((i) => i.key === key);
        if (!row) {
          toast(vq().t("msg.defaultWatchlist"), "warn");
          return;
        }
        await api(`/api/finance/watchlist/${row.id}`, { method: "DELETE" });
        financeState.board = null;
        await loadBoard(root);
      } catch (err) {
        toast(err.message, "err");
      }
    });
  });
}

/* ============================ enstrüman dosyası =========================== */

/**
 * Enstrüman dosyası.
 *
 * Pencere `panel modal` sınıflarıyla açılır ve ikisi de ŞART: `.modal`
 * yalnızca boyut verir, yüzeyi (zemin, kenar, dolgu) `.panel` taşır.
 * `panel` unutulduğunda pencere saydam açılıyor ve içindekiler bulanık
 * perdenin üzerinde okunmaz hâle geliyordu.
 */
async function openDetail(key) {
  const { $, api, esc, el, toast } = vq();
  const [market, exchange, ...rest] = key.split(":");
  const symbol = rest.join(":");

  const backdrop = el(`
    <div class="modal-backdrop">
      <div class="panel modal fin-modal">
        <div class="modal-head">
          <div class="modal-title">${esc(symbol)}</div>
          <button class="icon-btn" data-close data-icon="close"></button>
        </div>
        <div class="modal-body" id="fin-detail">
          <div class="empty"><div class="spinner"></div>${t("fin.loading")}</div>
        </div>
      </div>
    </div>`);
  $("#modal-root").appendChild(backdrop);
  vq().hydrateIcons?.(backdrop);

  const close = () => backdrop.remove();
  backdrop.querySelector("[data-close]").onclick = close;
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(); });

  let data;
  try {
    data = await api(`/api/finance/detail?symbol=${encodeURIComponent(symbol)}`
                     + `&market=${encodeURIComponent(market)}`
                     + `&exchange=${encodeURIComponent(exchange)}&timeframe=1h`);
  } catch (err) {
    backdrop.querySelector("#fin-detail").innerHTML =
      `<div class="callout red">${esc(err.message)}</div>`;
    return;
  }

  const host = backdrop.querySelector("#fin-detail");
  if (data.error) {
    host.innerHTML = `<div class="callout red">${esc(data.error)}</div>`;
    return;
  }

  const q = data.quote || {};
  const ind = data.snapshot?.indicators || {};
  const cons = data.consensus;

  host.innerHTML = `
    <div class="fin-detail-top">
      <div>
        <div class="fin-detail-price">${priceText(q.price)}</div>
        ${changeChip(q.change_pct)}
        <span class="dim small"> · ${esc(ageText(q.age_seconds))}</span>
      </div>
      <div class="right dim small">
        ${esc(data.snapshot?.regime || "")} · ${esc(data.timeframe)}
      </div>
    </div>

    <div class="fin-chart" id="fin-chart"></div>

    <div class="grid g4 mt">
      ${miniStat("RSI (14)", ind.rsi_14?.toFixed(1) ?? "—")}
      ${miniStat("ADX (14)", ind.adx_14?.toFixed(1) ?? "—")}
      ${miniStat("ATR %", ind.atr_percent?.toFixed(2) ?? "—")}
      ${miniStat("EMA 50", ind.ema_50 ? priceText(ind.ema_50) : "—")}
    </div>

    ${cons ? `
      <div class="panel mt">
        <div class="panel-head">
          <div class="panel-title">${t("fin.algoVote")}</div>
          <span class="fin-chip ${cons.action === "BUY" ? "up" : cons.action === "SELL" ? "down" : "flat"}">
            ${esc(cons.action)}
          </span>
        </div>
        <div class="small dim">${esc(cons.summary || "")}</div>
        <div class="hint mt">
          Bu oy on iki stratejinin deterministik sonucudur — bir modelin görüşü
          değildir. Kendi hikâyeni bu ölçümle karşılaştır.
        </div>
      </div>` : ""}

    ${(data.news && data.news.length) ? `
      <div class="panel mt">
        <div class="panel-head"><div class="panel-title">${t("fin.instrumentNews")}</div></div>
        <div class="fin-news">
          ${data.news.map((h) => `
            <a class="fin-news-item" href="${esc(h.link || "#")}" target="_blank" rel="noopener noreferrer">
              <span class="fin-score ${h.score > 0 ? "up" : h.score < 0 ? "down" : "flat"}">
                ${h.score >= 0 ? "+" : ""}${h.score}</span>
              <span class="fin-news-body">
                <span class="fin-news-title">${esc(h.title)}</span>
                <span class="fin-news-meta">${esc(h.source)}</span>
              </span>
            </a>`).join("")}
        </div>
      </div>` : ""}

    <div class="row mt">
      <button class="btn btn-primary" id="fin-ask">${t("fin.askAgent")}</button>
      <button class="btn" id="fin-watch">${t("fin.addToWatch")}</button>
    </div>
  `;

  // Mum grafiği — panelin geri kalanıyla AYNI uçtan gelir. Ayrı bir mum
  // kaynağı açmak, grafikle rakamların farklı barlara bakması demektir.
  try {
    const chartHost = host.querySelector("#fin-chart");
    if (chartHost) {
      const payload = await api(
        `/api/market/candles?market=${encodeURIComponent(market)}`
        + `&exchange=${encodeURIComponent(exchange)}`
        + `&symbol=${encodeURIComponent(symbol)}&timeframe=1h&limit=300`);
      new CandleChart(chartHost).setData(payload);
    }
  } catch (err) {
    const chartHost = host.querySelector("#fin-chart");
    if (chartHost) {
      chartHost.innerHTML =
        `<div class="callout small">Grafik çizilemedi: ${esc(err.message)}</div>`;
    }
  }

  host.querySelector("#fin-watch").onclick = async () => {
    try {
      const res = await api("/api/finance/watchlist", {
        method: "POST", body: { symbol, market },
      });
      toast(res.added ? "Listene eklendi." : (res.not || "Zaten listede."));
    } catch (err) { toast(err.message, "err"); }
  };

  host.querySelector("#fin-ask").onclick = () => {
    close();
    // Canlı piyasa modunda yeni bir görev: ajan ekrandaki sayılarla başlar.
    window.VQ.startFinanceChat?.(symbol, market);
  };
}

function miniStat(label, value) {
  const { esc } = vq();
  return `
    <div class="panel stat sm">
      <div class="stat-label">${esc(label)}</div>
      <div class="stat-value">${esc(String(value))}</div>
    </div>`;
}

export function stopFinanceTimers() {
  clearInterval(financeState.timer);
  clearInterval(financeState.newsTimer);
  financeState.timer = null;
  financeState.newsTimer = null;
}
