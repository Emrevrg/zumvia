/* ==========================================================================
   ZUMVIA — Bağımsız Mum Grafiği Motoru
   --------------------------------------------------------------------------
   TradingView tarzı mum grafiği; hiçbir dış kütüphane kullanmaz (CDN yok,
   npm yok). Yüksek DPI desteği, EMA/Bollinger/Supertrend katmanları, hacim
   paneli, crosshair, pozisyon seviyeleri (giriş / stop / hedef) ve pan-zoom.
   ========================================================================== */

const PALETTE = {
  up: "#00e676",
  down: "#ff4d6d",
  upFill: "rgba(0,230,118,.85)",
  downFill: "rgba(255,77,109,.85)",
  grid: "rgba(148,163,184,.07)",
  axis: "#5b7080",
  text: "#8fa3b0",
  ema50: "#38bdf8",
  ema200: "#f0a5ff",
  supertrend: "#00f59b",
  band: "rgba(148,163,184,.28)",
  entry: "#facc15",
  stop: "#ff4d6d",
  target: "#00e676",
  crosshair: "rgba(0,245,155,.45)",
};

export class CandleChart {
  constructor(container, options = {}) {
    this.container = container;
    this.canvas = document.createElement("canvas");
    this.tooltip = document.createElement("div");
    this.tooltip.className = "chart-tooltip";
    container.innerHTML = "";
    container.appendChild(this.canvas);
    container.appendChild(this.tooltip);

    this.ctx = this.canvas.getContext("2d");
    this.data = { candles: [], volume: [], overlays: {} };
    this.levels = [];
    this.view = { offset: 0, count: options.initialBars || 120 };
    this.hover = null;
    this.priceFormat = options.priceFormat || ((v) => formatNumber(v));

    this._bindEvents();
    this._resizeObserver = new ResizeObserver(() => this.render());
    this._resizeObserver.observe(container);
  }

  destroy() {
    this._resizeObserver.disconnect();
  }

  setData(payload) {
    this.data.candles = payload.candles || [];
    this.data.volume = payload.volume || [];
    this.data.overlays = {
      ema_50: payload.ema_50 || [],
      ema_200: payload.ema_200 || [],
      supertrend: payload.supertrend || [],
      bb_upper: payload.bb_upper || [],
      bb_lower: payload.bb_lower || [],
    };
    this.view.offset = 0;
    this.view.count = Math.min(this.view.count, this.data.candles.length || 1);
    this.render();
  }

  /** Pozisyon seviyeleri: [{price, color, label}] */
  setLevels(levels) {
    this.levels = levels || [];
    this.render();
  }

  // ------------------------------------------------------------------ olaylar
  _bindEvents() {
    const c = this.canvas;

    c.addEventListener("mousemove", (e) => {
      const rect = c.getBoundingClientRect();
      this.hover = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      this.render();
    });
    c.addEventListener("mouseleave", () => {
      this.hover = null;
      this.tooltip.style.display = "none";
      this.render();
    });
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      const total = this.data.candles.length;
      const step = Math.max(3, Math.round(this.view.count * 0.12));
      this.view.count = clamp(
        this.view.count + (e.deltaY > 0 ? step : -step), 25, total || 25
      );
      this.view.offset = clamp(this.view.offset, 0, Math.max(0, total - this.view.count));
      this.render();
    }, { passive: false });

    let dragging = false;
    let dragStart = 0;
    let offsetStart = 0;
    c.addEventListener("mousedown", (e) => {
      dragging = true; dragStart = e.clientX; offsetStart = this.view.offset;
      c.style.cursor = "grabbing";
    });
    window.addEventListener("mouseup", () => { dragging = false; c.style.cursor = "crosshair"; });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const total = this.data.candles.length;
      const barWidth = this.canvas.clientWidth / this.view.count;
      const shift = Math.round((e.clientX - dragStart) / barWidth);
      this.view.offset = clamp(offsetStart + shift, 0, Math.max(0, total - this.view.count));
      this.render();
    });
    c.style.cursor = "crosshair";
  }

  // ------------------------------------------------------------------- çizim
  render() {
    const dpr = window.devicePixelRatio || 1;
    const width = this.container.clientWidth;
    const height = this.container.clientHeight;
    if (!width || !height) return;

    this.canvas.width = width * dpr;
    this.canvas.height = height * dpr;
    this.canvas.style.width = `${width}px`;
    this.canvas.style.height = `${height}px`;

    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const candles = this.data.candles;
    if (!candles.length) {
      ctx.fillStyle = PALETTE.text;
      ctx.font = "13px Inter, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("Veri bekleniyor…", width / 2, height / 2);
      return;
    }

    // Görünüm penceresi
    const total = candles.length;
    const count = Math.min(this.view.count, total);
    const end = total - this.view.offset;
    const start = Math.max(0, end - count);
    const view = candles.slice(start, end);

    const padRight = 62;
    const padBottom = 22;
    const volHeight = Math.round(height * 0.16);
    const plotW = width - padRight;
    const plotH = height - padBottom - volHeight - 6;

    // Fiyat aralığı (görünen mumlar + katmanlar + seviyeler)
    let min = Infinity, max = -Infinity;
    view.forEach((c) => { min = Math.min(min, c.low); max = Math.max(max, c.high); });
    const times = new Set(view.map((c) => c.time));
    Object.values(this.data.overlays).forEach((series) => {
      series.forEach((p) => {
        if (times.has(p.time)) { min = Math.min(min, p.value); max = Math.max(max, p.value); }
      });
    });
    this.levels.forEach((l) => { min = Math.min(min, l.price); max = Math.max(max, l.price); });

    const pad = (max - min) * 0.08 || max * 0.01 || 1;
    min -= pad; max += pad;

    const barW = plotW / view.length;
    const bodyW = Math.max(1, Math.min(barW * 0.66, 16));
    const yOf = (price) => plotH - ((price - min) / (max - min)) * plotH;
    const xOf = (i) => i * barW + barW / 2;

    // --- ızgara + fiyat ekseni ---
    ctx.font = "10px 'JetBrains Mono', monospace";
    ctx.textAlign = "left";
    const gridLines = 6;
    for (let i = 0; i <= gridLines; i++) {
      const y = (plotH / gridLines) * i;
      const price = max - ((max - min) / gridLines) * i;
      ctx.strokeStyle = PALETTE.grid;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(plotW, y); ctx.stroke();
      ctx.fillStyle = PALETTE.axis;
      ctx.fillText(this.priceFormat(price), plotW + 7, y + 3);
    }

    // --- Bollinger dolgusu ---
    this._fillBand(ctx, view, start, plotW, yOf, xOf);

    // --- hacim ---
    const volTop = plotH + 10;
    const maxVol = Math.max(...view.map((c, i) => this._volumeAt(start + i)), 1);
    view.forEach((c, i) => {
      const v = this._volumeAt(start + i);
      const h = (v / maxVol) * volHeight;
      ctx.fillStyle = c.close >= c.open ? "rgba(0,230,118,.22)" : "rgba(255,77,109,.22)";
      ctx.fillRect(xOf(i) - bodyW / 2, volTop + volHeight - h, bodyW, h);
    });

    // --- mumlar ---
    view.forEach((c, i) => {
      const up = c.close >= c.open;
      const x = xOf(i);
      ctx.strokeStyle = up ? PALETTE.up : PALETTE.down;
      ctx.fillStyle = up ? PALETTE.upFill : PALETTE.downFill;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, yOf(c.high));
      ctx.lineTo(Math.round(x) + 0.5, yOf(c.low));
      ctx.stroke();
      const yOpen = yOf(c.open);
      const yClose = yOf(c.close);
      const top = Math.min(yOpen, yClose);
      const h = Math.max(1, Math.abs(yClose - yOpen));
      ctx.fillRect(x - bodyW / 2, top, bodyW, h);
    });

    // --- gösterge çizgileri ---
    this._line(ctx, this.data.overlays.ema_50, view, start, xOf, yOf, PALETTE.ema50, 1.4);
    this._line(ctx, this.data.overlays.ema_200, view, start, xOf, yOf, PALETTE.ema200, 1.4);
    this._line(ctx, this.data.overlays.supertrend, view, start, xOf, yOf, PALETTE.supertrend, 1.1, true);

    // --- pozisyon seviyeleri ---
    this.levels.forEach((level) => {
      const y = yOf(level.price);
      if (y < 0 || y > plotH) return;
      ctx.strokeStyle = level.color;
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(plotW, y); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = level.color;
      ctx.fillRect(plotW, y - 8, padRight, 16);
      ctx.fillStyle = "#04120a";
      ctx.font = "bold 9.5px 'JetBrains Mono', monospace";
      ctx.fillText(level.label, plotW + 4, y + 3);
    });

    // --- son fiyat etiketi ---
    const last = view[view.length - 1];
    const lastY = yOf(last.close);
    const lastUp = last.close >= last.open;
    ctx.strokeStyle = lastUp ? PALETTE.up : PALETTE.down;
    ctx.setLineDash([2, 3]);
    ctx.beginPath(); ctx.moveTo(0, lastY); ctx.lineTo(plotW, lastY); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = lastUp ? PALETTE.up : PALETTE.down;
    ctx.fillRect(plotW, lastY - 9, padRight, 18);
    ctx.fillStyle = "#04120a";
    ctx.font = "bold 10px 'JetBrains Mono', monospace";
    ctx.fillText(this.priceFormat(last.close), plotW + 4, lastY + 3.5);

    // --- zaman ekseni ---
    ctx.fillStyle = PALETTE.axis;
    ctx.font = "10px 'JetBrains Mono', monospace";
    const tickEvery = Math.max(1, Math.floor(view.length / 6));
    view.forEach((c, i) => {
      if (i % tickEvery !== 0) return;
      const d = new Date(c.time * 1000);
      const label = `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
      ctx.fillText(label, xOf(i) - 20, height - 6);
    });

    // --- crosshair + tooltip ---
    if (this.hover && this.hover.x < plotW) {
      const idx = clamp(Math.floor(this.hover.x / barW), 0, view.length - 1);
      const c = view[idx];
      ctx.strokeStyle = PALETTE.crosshair;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(xOf(idx), 0); ctx.lineTo(xOf(idx), plotH + volHeight + 10);
      ctx.moveTo(0, this.hover.y); ctx.lineTo(plotW, this.hover.y);
      ctx.stroke();
      ctx.setLineDash([]);

      const d = new Date(c.time * 1000);
      const change = ((c.close - c.open) / c.open) * 100;
      this.tooltip.style.display = "block";
      this.tooltip.style.left = `${Math.min(this.hover.x + 14, plotW - 150)}px`;
      this.tooltip.style.top = `${Math.max(8, this.hover.y - 76)}px`;
      this.tooltip.innerHTML =
        `${d.toLocaleString("tr-TR")}\n` +
        `A ${this.priceFormat(c.open)}   Y ${this.priceFormat(c.high)}\n` +
        `D ${this.priceFormat(c.low)}   K ${this.priceFormat(c.close)}\n` +
        `<span style="color:${change >= 0 ? PALETTE.up : PALETTE.down}">${change >= 0 ? "+" : ""}${change.toFixed(2)}%</span>`;
    }
  }

  _volumeAt(index) {
    const v = this.data.volume[index];
    return v ? v.value : 0;
  }

  _line(ctx, series, view, start, xOf, yOf, color, width, dashed = false) {
    if (!series || !series.length) return;
    const map = new Map(series.map((p) => [p.time, p.value]));
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    if (dashed) ctx.setLineDash([4, 3]);
    ctx.beginPath();
    let started = false;
    view.forEach((c, i) => {
      const value = map.get(c.time);
      if (value === undefined) { started = false; return; }
      const x = xOf(i), y = yOf(value);
      if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
    });
    ctx.stroke();
    ctx.setLineDash([]);
  }

  _fillBand(ctx, view, start, plotW, yOf, xOf) {
    const upper = this.data.overlays.bb_upper;
    const lower = this.data.overlays.bb_lower;
    if (!upper?.length || !lower?.length) return;
    const up = new Map(upper.map((p) => [p.time, p.value]));
    const lo = new Map(lower.map((p) => [p.time, p.value]));

    ctx.fillStyle = "rgba(148,163,184,.055)";
    ctx.beginPath();
    let started = false;
    view.forEach((c, i) => {
      const v = up.get(c.time);
      if (v === undefined) return;
      const x = xOf(i), y = yOf(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
    });
    for (let i = view.length - 1; i >= 0; i--) {
      const v = lo.get(view[i].time);
      if (v === undefined) continue;
      ctx.lineTo(xOf(i), yOf(v));
    }
    ctx.closePath();
    ctx.fill();
  }
}

/* ---------------------------------------------------------------- yardımcılar */

export function formatNumber(value) {
  const v = Number(value);
  if (!isFinite(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1000) return v.toLocaleString("tr-TR", { maximumFractionDigits: 2 });
  if (abs >= 1) return v.toFixed(2);
  if (abs >= 0.01) return v.toFixed(4);
  return v.toPrecision(4);
}

function clamp(value, lo, hi) {
  return Math.max(lo, Math.min(hi, value));
}

/** Basit alan (sermaye eğrisi) grafiği. */
export function drawEquityCurve(canvas, points, options = {}) {
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  if (!points || points.length < 2) {
    ctx.fillStyle = PALETTE.text;
    ctx.font = "12px Inter, system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Henüz yeterli veri yok — bot çalıştıkça burası dolacak.", width / 2, height / 2);
    return;
  }

  const values = points.map((p) => p.equity);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const padY = 14;
  const xOf = (i) => (i / (points.length - 1)) * (width - 52);
  const yOf = (v) => height - padY - ((v - min) / range) * (height - padY * 2);

  // ızgara
  ctx.font = "10px 'JetBrains Mono', monospace";
  for (let i = 0; i <= 4; i++) {
    const y = padY + ((height - padY * 2) / 4) * i;
    ctx.strokeStyle = PALETTE.grid;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width - 52, y); ctx.stroke();
    ctx.fillStyle = PALETTE.axis;
    ctx.fillText(formatNumber(max - (range / 4) * i), width - 47, y + 3);
  }

  const rising = values[values.length - 1] >= values[0];
  const stroke = rising ? PALETTE.up : PALETTE.down;

  const gradient = ctx.createLinearGradient(0, 0, 0, height);
  gradient.addColorStop(0, rising ? "rgba(0,230,118,.30)" : "rgba(255,77,109,.28)");
  gradient.addColorStop(1, "rgba(0,0,0,0)");

  ctx.beginPath();
  points.forEach((p, i) => (i ? ctx.lineTo(xOf(i), yOf(p.equity)) : ctx.moveTo(xOf(i), yOf(p.equity))));
  ctx.lineTo(xOf(points.length - 1), height);
  ctx.lineTo(0, height);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  ctx.beginPath();
  points.forEach((p, i) => (i ? ctx.lineTo(xOf(i), yOf(p.equity)) : ctx.moveTo(xOf(i), yOf(p.equity))));
  ctx.strokeStyle = stroke;
  ctx.lineWidth = 2;
  ctx.stroke();

  // başlangıç referans çizgisi
  if (options.baseline !== undefined && options.baseline >= min && options.baseline <= max) {
    const y = yOf(options.baseline);
    ctx.strokeStyle = "rgba(148,163,184,.35)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width - 52, y); ctx.stroke();
    ctx.setLineDash([]);
  }
}
