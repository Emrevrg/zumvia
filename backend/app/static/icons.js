/* ==========================================================================
   ZUMVIA — İkon Sistemi
   --------------------------------------------------------------------------
   Emoji yok. Tüm ikonlar 24×24 ızgarada, 1.75 kalınlıkta, `currentColor` ile
   çizilen satır içi SVG'lerdir; böylece tema, boyut ve durum renklerini
   otomatik devralırlar.

   Kullanım:  icon("chart", 16)   →  <svg …>
              iconEl("shield")    →  DOM elemanı
   ========================================================================== */

const PATHS = {
  /* --- gezinme --- */
  command: '<path d="M9 4H7a3 3 0 0 0 0 6h10a3 3 0 0 1 0 6h-2"/><rect x="4" y="14" width="6" height="6" rx="1.5"/><rect x="14" y="4" width="6" height="6" rx="1.5"/>',
  portfolio: '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M7 15l3.5-4 3 2.5L20 7"/>',
  bots: '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 14h.01M15 14h.01M9.5 17.5h5"/><path d="M2 13v2M22 13v2"/>',
  analysis: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/><path d="M8.5 12.5l2-2.5 2 2 2.5-3"/>',
  backtest: '<path d="M3 3v18h18"/><rect x="6" y="12" width="3" height="6" rx="1"/><rect x="11" y="8" width="3" height="10" rx="1"/><rect x="16" y="5" width="3" height="13" rx="1"/>',
  vault: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="12" cy="12" r="3.5"/><path d="M12 8.5V6M12 18v-2.5M15.5 12H18M6 12h2.5"/>',

  /* --- eylem --- */
  plus: '<path d="M12 5v14M5 12h14"/>',
  upload: '<path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
  paperclip: '<path d="M20 11.5 11.7 19.8a4.6 4.6 0 0 1-6.5-6.5l8.8-8.8a3.1 3.1 0 0 1 4.4 4.4l-8.8 8.8a1.5 1.5 0 0 1-2.2-2.2l8-8"/>',
  star: '<path d="m12 3.5 2.6 5.4 5.9.8-4.3 4.1 1.1 5.8L12 16.9 6.7 19.6l1.1-5.8L3.5 9.7l5.9-.8Z"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5a13 13 0 0 1 0 17 13 13 0 0 1 0-17Z"/>',
  trendUp: '<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
  trendDown: '<path d="M3 7l6 6 4-4 8 8"/><path d="M15 17h6v-6"/>',
  send: '<path d="m4 12 16-8-6 8 6 8z"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-3.2-6.9"/><path d="M21 4v5h-5"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/>',
  more: '<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 9 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 9a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1Z"/>',
  play: '<path d="M7 4.5v15l12-7.5z"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  trash: '<path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/><path d="M10 11v6M14 11v6"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z"/>',
  check: '<path d="m5 13 4 4L19 7"/>',
  chevronRight: '<path d="m9 6 6 6-6 6"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  arrowUp: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5M21 12H9"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',

  /* --- durum / anlam --- */
  shield: '<path d="M12 3l7.5 3v6c0 4.6-3.1 8.2-7.5 9.5C7.6 20.2 4.5 16.6 4.5 12V6Z"/>',
  shieldCheck: '<path d="M12 3l7.5 3v6c0 4.6-3.1 8.2-7.5 9.5C7.6 20.2 4.5 16.6 4.5 12V6Z"/><path d="m9 12 2 2 4-4"/>',
  alert: '<path d="M12 4.5 2.8 20h18.4z"/><path d="M12 10v4M12 17h.01"/>',
  brake: '<circle cx="12" cy="12" r="8.5"/><path d="M12 8v5M12 16h.01"/>',
  scan: '<path d="M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 2h-2M8 20H6a2 2 0 0 1-2-2v-2"/><path d="M7 12h2l2-3 2 6 2-3h2"/>',
  brain: '<path d="M9.5 4A2.5 2.5 0 0 0 7 6.5 2.5 2.5 0 0 0 5 9a2.5 2.5 0 0 0 .6 1.6A2.5 2.5 0 0 0 5 13a2.5 2.5 0 0 0 2 2.4V17a2.5 2.5 0 0 0 5 0V6.5A2.5 2.5 0 0 0 9.5 4Z"/><path d="M14.5 4A2.5 2.5 0 0 1 17 6.5 2.5 2.5 0 0 1 19 9a2.5 2.5 0 0 1-.6 1.6A2.5 2.5 0 0 1 19 13a2.5 2.5 0 0 1-2 2.4V17a2.5 2.5 0 0 1-5 0"/>',
  council: '<circle cx="7" cy="8" r="2.5"/><circle cx="17" cy="8" r="2.5"/><circle cx="12" cy="16" r="2.5"/><path d="M9 9.5 11 14M15 9.5 13 14M9.5 8h5"/>',
  scales: '<path d="M12 4v16M7 20h10"/><path d="M12 6 5 9M12 6l7 3"/><path d="M2.5 14a2.5 2.5 0 0 0 5 0L5 9Z"/><path d="M16.5 14a2.5 2.5 0 0 0 5 0L19 9Z"/>',
  key: '<circle cx="8" cy="12" r="4"/><path d="M12 12h9M17 12v3.5M20 12v2.5"/>',
  bell: '<path d="M18 9a6 6 0 1 0-12 0c0 5-2 6.5-2 6.5h16S18 14 18 9Z"/><path d="M10.5 19a2 2 0 0 0 3 0"/>',
  news: '<path d="M4 5h13a1 1 0 0 1 1 1v12a2 2 0 0 0 2 2H5a2 2 0 0 1-2-2V6a1 1 0 0 1 1-1Z"/><path d="M7 9h7M7 13h7M7 17h4"/>',
  candles: '<path d="M7 4v3.5M7 16.5V20M17 4v6M17 18v2"/><rect x="4.5" y="7.5" width="5" height="9" rx="1"/><rect x="14.5" y="10" width="5" height="8" rx="1"/>',
  live: '<circle cx="12" cy="12" r="3"/><path d="M6.5 6.5a7.8 7.8 0 0 0 0 11M17.5 6.5a7.8 7.8 0 0 1 0 11"/><path d="M3.5 3.5a12.4 12.4 0 0 0 0 17M20.5 3.5a12.4 12.4 0 0 1 0 17"/>',
  flask: '<path d="M9 3h6M10 3v6.5L4.8 18a2 2 0 0 0 1.7 3h11a2 2 0 0 0 1.7-3L14 9.5V3"/><path d="M7.5 15h9"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  loop: '<path d="M4 9a5 5 0 0 1 5-5h9M20 15a5 5 0 0 1-5 5H6"/><path d="m15 1 3 3-3 3M9 17l-3 3 3 3"/>',
  pause: '<rect x="7" y="5" width="3.5" height="14" rx="1"/><rect x="13.5" y="5" width="3.5" height="14" rx="1"/>',
  report: '<path d="M8 3h8l4 4v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z"/><path d="M15 3v5h5"/><path d="M8 13h8M8 17h5"/>',
  target: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1"/>',
  recovery: '<path d="M3 12a9 9 0 0 1 15.5-6.2"/><path d="M21 4v5h-5"/><path d="M7 16.5h3.5V20"/><path d="m21 12-4.5 4.5-3-3L10 17"/>',
  user: '<circle cx="12" cy="8" r="3.5"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 8h.01"/>',
  wrench: '<path d="M14.5 3.5a5.5 5.5 0 0 0-6.9 7L3 15.1V21h5.9l4.6-4.6a5.5 5.5 0 0 0 7-6.9l-3.2 3.2-3-.8-.8-3Z"/>',
  database: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
};

const SIZE_DEFAULT = 18;

/** İkonun SVG dizesini üretir. */
export function icon(name, size = SIZE_DEFAULT, extraClass = "") {
  const path = PATHS[name];
  if (!path) return "";
  return (
    `<svg class="ico ${extraClass}" width="${size}" height="${size}" viewBox="0 0 24 24" ` +
    `fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" ` +
    `stroke-linejoin="round" aria-hidden="true" focusable="false">${path}</svg>`
  );
}

/** İkonu DOM elemanı olarak döner. */
export function iconEl(name, size = SIZE_DEFAULT, extraClass = "") {
  const wrapper = document.createElement("span");
  wrapper.innerHTML = icon(name, size, extraClass);
  return wrapper.firstElementChild;
}

/** Bir kaptaki `data-icon` niteliklerini gerçek ikonlarla doldurur. */
export function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((node) => {
    if (node.dataset.iconDone) return;
    const size = Number(node.dataset.iconSize || SIZE_DEFAULT);
    node.innerHTML = icon(node.dataset.icon, size);
    node.dataset.iconDone = "1";
  });
}

/**
 * Sayfaya sonradan eklenen her `[data-icon]` öğesini otomatik doldurur.
 * Menü, modal ve sohbet kartları dinamik oluşturulduğu için tek tek
 * `hydrateIcons` çağırmak yerine tek bir gözlemci kullanılır.
 */
export function observeIcons() {
  hydrateIcons(document);
  new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (node.nodeType !== 1) continue;
        if (node.matches?.("[data-icon]")) hydrateIcons(node.parentElement || document);
        if (node.querySelector?.("[data-icon]")) hydrateIcons(node);
      }
    }
  }).observe(document.body, { childList: true, subtree: true });
}


/** Araç adı → ikon eşlemesi (chat adım kartları). */
export const TOOL_ICON = {
  get_market_snapshot: "candles",
  run_strategy_engine: "scales",
  scan_markets: "scan",
  validate_strategy: "shieldCheck",
  optimize_setup: "wrench",
  run_backtest: "backtest",
  get_news: "news",
  get_quote: "candles",
  get_portfolio: "portfolio",
  get_portfolio_risk: "shield",
  get_recovery_plan: "recovery",
  get_bot_detail: "bots",
  create_bot: "plus",
  update_bot: "edit",
  control_bot: "play",
  run_bot_cycle: "refresh",
  open_position: "target",
  close_position: "check",
  ask_sub_model: "brain",
  send_notification: "bell",
  system_health: "shieldCheck",
  get_safety_status: "shield",
  enable_live_trading: "live",
  disable_live_trading: "flask",
  activate_kill_switch: "brake",
  get_model_scoreboard: "council",
  list_credentials: "key",
};
