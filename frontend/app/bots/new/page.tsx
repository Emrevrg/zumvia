"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import useSWR from "swr";
import { Shell } from "@/components/Shell";
import { api, fetcher, money } from "@/lib/api";

type Catalog = {
  risk_presets: {
    id: string; label: string; risk_pct: number; daily_loss_limit_pct: number;
    min_confidence: number; min_rr: number; max_trades_per_day: number;
  }[];
  decision_modes: { id: string; label: string; desc: string }[];
  hard_limits: {
    max_risk_pct: number; max_daily_loss_pct: number; min_confidence: number;
    min_rr: number; circuit_breaker_lock_hours: number;
  };
};

type Credential = { id: number; kind: string; provider: string; label: string; hint: string };
type Meta = { exchanges: string[]; timeframes: string[]; popular: { crypto: string[]; stock: string[] } };

const MARKETS = [
  { id: "crypto", title: "Kripto", desc: "Binance, Bybit, OKX… 100+ borsa" },
  { id: "stock", title: "Hisse ve endeks", desc: "NASDAQ, BIST, emtia (yfinance)" },
  { id: "demo", title: "Demo (çevrimdışı)", desc: "İnternet gerekmez, sistemi tanıyın" },
];

export default function NewBotPage() {
  const router = useRouter();
  const { data: catalog } = useSWR<Catalog>("/api/bots/catalog", fetcher);
  const { data: credentials } = useSWR<Credential[]>("/api/keys", fetcher);
  const { data: meta } = useSWR<Meta>("/api/market/meta", fetcher);

  const llmKeys = (credentials ?? []).filter((c) => c.kind === "llm");
  const [step, setStep] = useState(1);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    market: "crypto",
    exchange: "binance",
    symbol: "BTC/USDT",
    timeframe: "1h",
    risk_level: "dengeli",
    initial_balance: 1000,
    decision_mode: "algo_only",
    llm_credential_id: null as number | null,
    llm_model: "",
  });

  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));

  async function create() {
    setBusy(true);
    try {
      const bot = await api<{ id: number }>("/api/bots/quickstart", {
        method: "POST",
        body: {
          symbol: form.symbol, risk_level: form.risk_level, market: form.market,
          exchange: form.exchange, timeframe: form.timeframe,
          initial_balance: form.initial_balance,
          llm_credential_id: form.decision_mode === "algo_only" ? null : form.llm_credential_id,
          llm_model: form.llm_model,
        },
      });
      await api(`/api/bots/${bot.id}`, {
        method: "PATCH", body: { decision_mode: form.decision_mode },
      });
      await api(`/api/bots/${bot.id}/start`, { method: "POST" });
      router.push(`/bots/${bot.id}`);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Hata");
      setBusy(false);
    }
  }

  return (
    <Shell title="Yeni bot" subtitle="3 adımda çalışmaya hazır otonom strateji">
      <div className="panel">
        <div className="mb-6 flex gap-2.5">
          {["Piyasa & Parite", "Risk Profili", "Zeka & Başlat"].map((label, i) => (
            <div
              key={label}
              className={`flex-1 rounded-lg border bg-[#0e1622] p-3 ${
                step === i + 1 ? "border-emerald shadow-glow" : "border-white/10"
              }`}
            >
              <div className="font-mono text-[11px] text-emerald-mint">ADIM {i + 1}</div>
              <div className="mt-0.5 text-[13px] font-semibold">{label}</div>
            </div>
          ))}
        </div>

        {step === 1 && (
          <>
            <div className="mb-4 grid gap-3 md:grid-cols-3">
              {MARKETS.map((m) => (
                <button
                  key={m.id}
                  onClick={() =>
                    set({
                      market: m.id,
                      exchange: m.id === "crypto" ? "binance" : m.id === "stock" ? "yfinance" : "demo",
                      symbol: m.id === "stock" ? "AAPL" : "BTC/USDT",
                    })
                  }
                  className={`rounded-lg border bg-[#0e1622] p-3.5 text-left transition ${
                    form.market === m.id ? "border-emerald bg-emerald/5" : "border-white/10"
                  }`}
                >
                  <div className="text-[13px] font-semibold">{m.title}</div>
                  <div className="mt-1 text-[11.5px] text-slate-400">{m.desc}</div>
                </button>
              ))}
            </div>

            <div className="grid gap-3 md:grid-cols-3">
              <div className="space-y-1.5">
                <label className="label">Borsa</label>
                <select className="input" value={form.exchange}
                  onChange={(e) => set({ exchange: e.target.value })}>
                  {(form.market === "crypto" ? meta?.exchanges ?? ["binance"]
                    : form.market === "stock" ? ["yfinance"] : ["demo"]).map((x) => (
                    <option key={x}>{x}</option>
                  ))}
                </select>
              </div>
              <div className="space-y-1.5">
                <label className="label">Parite / Sembol</label>
                <input className="input font-mono" value={form.symbol} list="symbols"
                  onChange={(e) => set({ symbol: e.target.value.toUpperCase() })} />
                <datalist id="symbols">
                  {(form.market === "stock" ? meta?.popular.stock : meta?.popular.crypto)?.map((s) => (
                    <option key={s} value={s} />
                  ))}
                </datalist>
              </div>
              <div className="space-y-1.5">
                <label className="label">Zaman dilimi</label>
                <select className="input" value={form.timeframe}
                  onChange={(e) => set({ timeframe: e.target.value })}>
                  {(meta?.timeframes ?? ["1h"]).map((tf) => <option key={tf}>{tf}</option>)}
                </select>
                <p className="text-[11px] text-slate-400">
                  Yeni başlayanlar için <b>1h</b> veya <b>4h</b>: daha az gürültü, daha az komisyon.
                </p>
              </div>
            </div>

            <div className="mt-5 flex justify-end">
              <button className="btn btn-primary" onClick={() => setStep(2)}>Devam</button>
            </div>
          </>
        )}

        {step === 2 && (
          <>
            <div className="mb-4 grid gap-3 md:grid-cols-3">
              {catalog?.risk_presets.map((p) => (
                <button
                  key={p.id}
                  onClick={() => set({ risk_level: p.id })}
                  className={`rounded-lg border bg-[#0e1622] p-3.5 text-left transition ${
                    form.risk_level === p.id ? "border-emerald bg-emerald/5" : "border-white/10"
                  }`}
                >
                  <div className="text-[13px] font-semibold">{p.label}</div>
                  <div className="mt-1 space-y-0.5 text-[11.5px] text-slate-400">
                    <div>İşlem riski <b>%{p.risk_pct}</b> · Günlük limit <b>%{p.daily_loss_limit_pct}</b></div>
                    <div>Min. güven <b>%{(p.min_confidence * 100).toFixed(0)}</b> · Min. R/R <b>1:{p.min_rr}</b></div>
                    <div>Günde en fazla <b>{p.max_trades_per_day}</b> işlem</div>
                  </div>
                </button>
              ))}
            </div>

            <div className="max-w-xs space-y-1.5">
              <label className="label">Başlangıç Sermayesi (sanal)</label>
              <input className="input font-mono" type="number" value={form.initial_balance}
                onChange={(e) => set({ initial_balance: Number(e.target.value) })} />
            </div>

            {catalog && (
              <div className="mt-4 rounded-r-lg border-l-2 border-emerald bg-emerald/10 px-4 py-3 text-[12.5px] text-[#b7f5cf]">
                <b>Değiştirilemez sistem tavanları:</b> tek işlemde en fazla %
                {catalog.hard_limits.max_risk_pct} risk, günlük %
                {catalog.hard_limits.max_daily_loss_pct} kayıpta devre kesici +{" "}
                {catalog.hard_limits.circuit_breaker_lock_hours} saat kilit, minimum R/R 1:
                {catalog.hard_limits.min_rr}, minimum güven %
                {(catalog.hard_limits.min_confidence * 100).toFixed(0)}. Bu sınırların üstüne{" "}
                <b>hiçbir ayarla</b> çıkılamaz.
              </div>
            )}

            <div className="mt-5 flex justify-between">
              <button className="btn" onClick={() => setStep(1)}>Geri</button>
              <button className="btn btn-primary" onClick={() => setStep(3)}>Devam</button>
            </div>
          </>
        )}

        {step === 3 && (
          <>
            <div className="grid gap-5 md:grid-cols-2">
              <div>
                <div className="label mb-2">Karar Mimarisi</div>
                {catalog?.decision_modes.map((m) => {
                  const disabled = m.id !== "algo_only" && llmKeys.length === 0;
                  return (
                    <button
                      key={m.id}
                      disabled={disabled}
                      onClick={() => set({
                        decision_mode: m.id,
                        llm_credential_id: llmKeys[0]?.id ?? null,
                      })}
                      className={`mb-2.5 block w-full rounded-lg border bg-[#0e1622] p-3.5 text-left transition disabled:opacity-40 ${
                        form.decision_mode === m.id ? "border-emerald bg-emerald/5" : "border-white/10"
                      }`}
                    >
                      <div className="text-[13px] font-semibold">{m.label}</div>
                      <div className="mt-1 text-[11.5px] text-slate-400">{m.desc}</div>
                    </button>
                  );
                })}
              </div>

              <div>
                <div className="label mb-2">Yapay Zeka Modeli</div>
                {llmKeys.length ? (
                  <>
                    <select className="input mb-3" value={form.llm_credential_id ?? ""}
                      onChange={(e) => set({ llm_credential_id: Number(e.target.value) })}>
                      {llmKeys.map((k) => (
                        <option key={k.id} value={k.id}>
                          {k.label} · {k.provider} ({k.hint})
                        </option>
                      ))}
                    </select>
                    <input className="input font-mono" placeholder="model adı (örn. gemini-2.5-flash)"
                      value={form.llm_model} onChange={(e) => set({ llm_model: e.target.value })} />
                    <p className="mt-2 text-[11px] text-slate-400">
                      Hangi modeli takarsanız takın, sistem onu <b>profesyonel fon yöneticisi</b>{" "}
                      personası ve aynı JSON sözleşmesiyle çalıştırır.
                    </p>
                  </>
                ) : (
                  <div className="rounded-r-lg border-l-2 border-warn bg-warn/10 px-4 py-3 text-[12.5px] text-[#ffdca6]">
                    Kayıtlı yapay zeka anahtarınız yok. <b>Sadece Algoritma</b> modu ile hemen
                    başlayabilirsiniz (maliyet $0), ya da Kasa sayfasından ücretsiz bir Gemini
                    anahtarı ekleyin.
                  </div>
                )}
              </div>
            </div>

            <div className="mt-4 rounded-r-lg border-l-2 border-warn bg-warn/10 px-4 py-3 text-[12.5px] text-[#ffdca6]">
              <b>Özet:</b> {form.symbol} · {form.timeframe} · {form.exchange} · {form.risk_level}{" "}
              profil · {money(form.initial_balance)} sanal sermaye · <b>PAPER TRADING</b> (gerçek
              para kullanılmaz).
            </div>

            <div className="mt-5 flex justify-between">
              <button className="btn" onClick={() => setStep(2)}>Geri</button>
              <button className="btn btn-primary" disabled={busy} onClick={create}>
                {busy ? "Oluşturuluyor…" : "Botu oluştur ve başlat"}
              </button>
            </div>
          </>
        )}
      </div>
    </Shell>
  );
}
