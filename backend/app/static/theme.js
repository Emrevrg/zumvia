/**
 * TEMA — koyu / açık / sistem
 * ============================
 *
 * Üç seçenek var ve üçüncüsü varsayılan:
 *
 *   koyu     Her zaman koyu.
 *   açık     Her zaman açık.
 *   sistem   İşletim sisteminin tercihini izler ve DEĞİŞİNCE ANINDA uyar.
 *
 * "Sistem"in varsayılan olması bilinçli: kullanıcı akşam karanlık moda geçen
 * bir bilgisayarda çalışıyorsa, uygulamanın gündüz ayarında kalıp gözünü
 * yakması bir tercih değil bir ihmaldir.
 *
 * Tema, ilk boyama olmadan ÖNCE uygulanmalıdır — yoksa açık temayı seçmiş
 * bir kullanıcı her yenilemede bir kare koyu ekran görür ("flash of wrong
 * theme"). Bu yüzden `boot()` modül yüklenir yüklenmez, `index.html`
 * içindeki küçük bir betikten çağrılır.
 */

const KEY = "vq_theme";
const VALID = new Set(["dark", "light", "system"]);

const media = typeof window.matchMedia === "function"
  ? window.matchMedia("(prefers-color-scheme: light)")
  : null;

export const THEMES = [
  { id: "system", labelKey: "theme.system", icon: "settings" },
  { id: "dark", labelKey: "theme.dark", icon: "brake" },
  { id: "light", labelKey: "theme.light", icon: "live" },
];

/** Kullanıcının seçimi ("system" dahil). */
export function preference() {
  try {
    const saved = localStorage.getItem(KEY);
    return VALID.has(saved) ? saved : "system";
  } catch {
    // Gizli sekmede localStorage okumak istisna atabilir. Tema bir
    // kolaylıktır; erişilemiyorsa sisteme uyulur, uygulama çalışmaya devam.
    return "system";
  }
}

/** Şu an EKRANDA olan tema ("dark" | "light") — seçim "system" olsa bile. */
export function active() {
  const pref = preference();
  if (pref !== "system") return pref;
  return media?.matches ? "light" : "dark";
}

function paint() {
  const theme = active();
  document.documentElement.dataset.theme = theme;
  // Tarayıcı çubuğu ve mobil durum çubuğu da uyum sağlasın; yoksa açık
  // temada ekranın üst şeridi koyu kalır ve ekran ikiye bölünmüş görünür.
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "light" ? "#f7f8fa" : "#0d0d0d");
}

export function setTheme(value) {
  const next = VALID.has(value) ? value : "system";
  try {
    localStorage.setItem(KEY, next);
  } catch {
    /* yazılamıyorsa oturum boyunca geçerli olur */
  }
  paint();
  return next;
}

/**
 * İlk boyamadan önce çağrılır.
 *
 * Ayrıca sistem tercihini DİNLER: kullanıcı "sistem"de kaldığı sürece
 * işletim sistemi gece moduna geçtiğinde uygulama da geçer — sayfayı
 * yenilemesi gerekmez.
 */
export function boot() {
  paint();
  media?.addEventListener?.("change", () => {
    if (preference() === "system") paint();
  });
}
