/**
 * DÜZEN — sürüklenebilir paneller
 * ================================
 *
 * Kullanıcı kenar çubuğunu ve iş tezgâhını kendi ekranına göre ayarlar.
 * Seçim SAKLANIR: ayarlanabilir olup hatırlamayan bir panel, ayarlanamayan
 * panelden daha sinir bozucudur — her açılışta aynı işi tekrar yaptırır.
 *
 * İki kural:
 *
 *   1. Sınırlar var. Kenar çubuğu 180px'in altına inemez (içindeki görev
 *      adları okunmaz olur) ve ekranın yarısını geçemez. Kullanıcının
 *      kendini kilitleyebildiği bir arayüz, ayarlanabilir sayılmaz.
 *   2. Sürüklerken metin seçilmez. Sürüklemek isterken yazı seçmek,
 *      düzeltilmesi kolay ama fark edilmesi geç bir rahatsızlıktır.
 */

const RAIL_KEY = "vq_rail_w";
const LIVE_KEY = "vq_live_w";

const RAIL_MIN = 180;
const LIVE_MIN = 260;

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function readStored(key, fallback) {
  try {
    const raw = parseInt(localStorage.getItem(key) || "", 10);
    return Number.isFinite(raw) ? raw : fallback;
  } catch {
    // Gizli sekmede localStorage okumak istisna atabilir; düzen bir
    // kolaylıktır, erişilemiyorsa varsayılanla devam eder.
    return fallback;
  }
}

function store(key, value) {
  try { localStorage.setItem(key, String(value)); } catch { /* önemsiz */ }
}

/** Kenar çubuğu genişliğini uygular. */
export function applyRailWidth(px) {
  const max = Math.round(window.innerWidth * 0.5);
  const width = clamp(px, RAIL_MIN, max);
  document.documentElement.style.setProperty("--rail-w", `${width}px`);
  return width;
}

/** İş tezgâhı (sağdaki canlı panel) genişliğini uygular. */
export function applyLiveWidth(px) {
  const max = Math.round(window.innerWidth * 0.6);
  const width = clamp(px, LIVE_MIN, max);
  document.documentElement.style.setProperty("--live-w", `${width}px`);
  return width;
}

/**
 * Bir ayırıcıyı sürüklenebilir yapar.
 *
 * `direction` — "right": kol sağa gidince panel büyür (kenar çubuğu).
 *               "left" : kol sola gidince panel büyür (sağdaki panel).
 */
function bindSplitter(el, { onDrag, direction = "right" }) {
  if (!el || el.dataset.bound) return;
  el.dataset.bound = "1";

  const start = (event) => {
    event.preventDefault();
    const startX = event.touches ? event.touches[0].clientX : event.clientX;
    const base = onDrag(null);                 // mevcut genişliği öğren
    el.classList.add("dragging");
    document.body.classList.add("resizing");

    const move = (e) => {
      const x = e.touches ? e.touches[0].clientX : e.clientX;
      const delta = direction === "right" ? x - startX : startX - x;
      onDrag(base + delta);
    };
    const end = () => {
      el.classList.remove("dragging");
      document.body.classList.remove("resizing");
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", end);
      document.removeEventListener("touchmove", move);
      document.removeEventListener("touchend", end);
      onDrag(undefined);                       // kaydet
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", end);
    document.addEventListener("touchmove", move, { passive: false });
    document.addEventListener("touchend", end);
  };

  el.addEventListener("mousedown", start);
  el.addEventListener("touchstart", start, { passive: false });

  // Klavyeyle de ayarlanabilmeli: fare kullanamayan biri için tek yol bu.
  el.tabIndex = 0;
  el.addEventListener("keydown", (event) => {
    const step = event.shiftKey ? 40 : 12;
    if (event.key === "ArrowLeft") {
      onDrag(onDrag(null) + (direction === "right" ? -step : step));
      onDrag(undefined);
      event.preventDefault();
    } else if (event.key === "ArrowRight") {
      onDrag(onDrag(null) + (direction === "right" ? step : -step));
      onDrag(undefined);
      event.preventDefault();
    }
  });
}

/** Kenar çubuğu ayırıcısı — kabuk kurulurken bir kez çağrılır. */
export function mountRailSplitter() {
  let current = applyRailWidth(readStored(RAIL_KEY, 296));
  bindSplitter(document.querySelector("#rail-splitter"), {
    direction: "right",
    onDrag: (px) => {
      if (px === null) return current;               // sorgu
      if (px === undefined) { store(RAIL_KEY, current); return current; }
      current = applyRailWidth(px);
      return current;
    },
  });
}

/**
 * İş tezgâhı ayırıcısı.
 *
 * Sohbet ekranı her çizimde yeniden kurulduğu için bu, görünüm
 * yenilendikçe tekrar çağrılır; `dataset.bound` çift bağlamayı önler.
 */
export function mountLiveSplitter() {
  let current = applyLiveWidth(readStored(LIVE_KEY, 380));
  bindSplitter(document.querySelector("#live-splitter"), {
    direction: "left",
    onDrag: (px) => {
      if (px === null) return current;
      if (px === undefined) { store(LIVE_KEY, current); return current; }
      current = applyLiveWidth(px);
      return current;
    },
  });
}

/** Pencere küçülünce saklanan genişlik ekrana sığmayabilir. */
export function refitOnResize() {
  window.addEventListener("resize", () => {
    applyRailWidth(readStored(RAIL_KEY, 296));
    applyLiveWidth(readStored(LIVE_KEY, 380));
  });
}
