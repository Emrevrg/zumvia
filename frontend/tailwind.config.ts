import type { Config } from "tailwindcss";

/**
 * ZUMVIA — Emerald & Cyber-Green Dark Fintech teması.
 * Renk paleti backend'in yerleşik arayüzüyle birebir aynıdır.
 */
const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        void: "#06090e",
        deep: "#0a0f18",
        panel: "#0c121c",
        raise: "#111a27",
        emerald: {
          DEFAULT: "#00e676",
          mint: "#00f59b",
          dim: "#0a9d5a",
        },
        danger: "#ff4d6d",
        warn: "#ffb020",
        info: "#38bdf8",
        slate: {
          850: "#1e293b",
          750: "#334155",
        },
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "Cascadia Mono", "SF Mono", "monospace"],
      },
      boxShadow: {
        glow: "0 0 24px rgba(0,230,118,.22)",
        panel: "0 18px 50px rgba(0,0,0,.55)",
      },
      backgroundImage: {
        "grid-glow":
          "radial-gradient(1100px 620px at 12% -8%, rgba(0,230,118,.10), transparent 62%), radial-gradient(900px 520px at 92% 6%, rgba(0,180,216,.07), transparent 60%)",
      },
      keyframes: {
        pulseDot: {
          "0%": { boxShadow: "0 0 0 0 rgba(0,230,118,.55)" },
          "70%": { boxShadow: "0 0 0 9px rgba(0,230,118,0)" },
          "100%": { boxShadow: "0 0 0 0 rgba(0,230,118,0)" },
        },
        floatSoft: {
          "0%, 100%": { transform: "translateY(0)" },
          "50%": { transform: "translateY(-7px)" },
        },
        fadeUp: {
          "0%": { opacity: "0", transform: "translateY(14px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        pulseDot: "pulseDot 1.9s infinite",
        floatSoft: "floatSoft 5s ease-in-out infinite",
        fadeUp: "fadeUp .55s cubic-bezier(.2,.8,.2,1) both",
      },
    },
  },
  plugins: [],
};

export default config;
