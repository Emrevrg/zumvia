"use client";

import { useState } from "react";
import { Shell, StatCard } from "@/components/Shell";
import { api, money, pct, toneOf } from "@/lib/api";

type Report = {
  initial_balance: number;
  final_balance: number;
  verdict: string;
  metrics: Record<string, number | string>;
  trades: {
    entry_time: string; exit_time: string; side: string; entry: number; exit: number;
    pnl: number; r_multiple: number; reason: string; strategy: string;
  }[];
};

export default function BacktestPage() {
  const [form, setForm] = useState({
    market: "crypto", symbol: "BTC/USDT", timeframe: "1h", candles: 1000,
    initial_balance: 1000, risk_pct: 1.0, min_agree: 2, allow_short: false,
  });
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));

  async function run() {
    setBusy(true);
    setError("");
    try {
      setReport(await api<Report>("/api/market/backtest", {
        method: "POST",
        body: {
          ...form,
          exchange: form.market === "crypto" ? "binance"
            : form.market === "stock" ? "yfinance" : "demo",
        },
      }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Hata");
    } finally {
      setBusy(false);
    }
  }

  const m = report?.metrics ?? {};

  return (
    <Shell title="Geri test" subtitle="Canlıya geçmeden önce kanıt toplayın">
      <div className="panel mb-4">
        <div className="grid gap-3 md:grid-cols-4">
          <div className="space-y-1.5">
            <label className="label">Piyasa</label>
            <select className="input" value={form.market} onChange={(e) => set({ market: e.target.value })}>
              <option value="crypto">Kripto</option><option value="stock">Hisse</option>
              <option value="demo">Demo</option>
            </select>
          </div>
          <div className="space-y-1.5">
            <label className="label">Sembol</label>
            <input className="input font-mono" value={form.symbol}
              onChange={(e) => set({ symbol: e.target.value.toUpperCase() })} />
          </div>
          <div className="space-y-1.5">
            <label className="label">Zaman dilimi</label>
            <select className="input" value={form.timeframe} onChange={(e) => set({ timeframe: e.target.value })}>
              {["5m", "15m", "30m", "1h", "4h", "1d"].map((tf) => <option key={tf}>{tf}</option>)}
            </select>
          </div>
          <div className="space-y-1.5">
            <label className="label">Mum sayısı</label>
            <input className="input font-mono" type="number" min={300} max={1000} value={form.candles}
              onChange={(e) => set({ candles: Number(e.target.value) })} />
          </div>
          <div className="space-y-1.5">
            <label className="label">Sermaye</label>
            <input className="input font-mono" type="number" value={form.initial_balance}
              onChange={(e) => set({ initial_balance: Number(e.target.value) })} />
          </div>
          <div className="space-y-1.5">
            <label className="label">İşlem riski (%)</label>
            <input className="input font-mono" type="number" step={0.1} max={1.5} value={form.risk_pct}
              onChange={(e) => set({ risk_pct: Number(e.target.value) })} />
          </div>
          <div className="space-y-1.5">
            <label className="label">Konsensüs eşiği</label>
            <input className="input font-mono" type="number" min={1} max={8} value={form.min_agree}
              onChange={(e) => set({ min_agree: Number(e.target.value) })} />
          </div>
          <label className="flex items-end gap-2 pb-2.5 text-[12.5px]">
            <input type="checkbox" checked={form.allow_short}
              onChange={(e) => set({ allow_short: e.target.checked })} />
            Short dahil
          </label>
        </div>

        <button className="btn btn-primary mt-4" onClick={run} disabled={busy}>
          {busy ? "Simülasyon çalışıyor…" : "Geri testi çalıştır"}
        </button>
        <span className="ml-3 text-[11.5px] text-slate-400">
          Look-ahead yok · komisyon ve slipaj dahil · canlıyla aynı risk formülü
        </span>
      </div>

      {error && <div className="panel border-danger/40 text-[#ffc0cc]">{error}</div>}

      {report && (
        <>
          <div className={`panel mb-4 border-l-2 ${
            report.verdict.startsWith("HAZIR") || report.verdict.startsWith("SAĞLAM") ? "border-l-emerald"
              : report.verdict.startsWith("KABUL") || report.verdict.startsWith("SINIRDA") ? "border-l-warn" : "border-l-danger"
          }`}>
            {report.verdict}
          </div>

          {Number(m.trade_count ?? 0) === 0 && (
            <div className="panel mb-4 text-slate-400">
              {typeof m.note === "string" && m.note
                ? m.note
                : "Bu dönemde kurulum oluşmadı — işlem yapmamak da bir karardır."}
            </div>
          )}

          {Number(m.trade_count ?? 0) > 0 && (
            <>
              <div className="mb-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                <StatCard label="Toplam Getiri" value={pct(Number(m.total_return_pct))}
                  foot={`${money(report.initial_balance)} - ${money(report.final_balance)}`}
                  tone={toneOf(Number(m.total_return_pct))} />
                <StatCard label="Kazanma Oranı" value={`%${m.win_rate_pct}`}
                  foot={`${m.trade_count} işlem`}
                  tone={Number(m.win_rate_pct) >= 55 ? "text-emerald" : ""} />
                <StatCard label="Kâr Faktörü" value={String(m.profit_factor)}
                  foot={`Beklenti ${m.expectancy_R}R`}
                  tone={Number(m.profit_factor) >= 1.8 ? "text-emerald"
                    : Number(m.profit_factor) >= 1 ? "" : "text-danger"} />
                <StatCard label="Maks. Drawdown" value={`%${m.max_drawdown_pct}`}
                  foot={`Sharpe ${m.sharpe_ratio}`}
                  tone={Number(m.max_drawdown_pct) <= 15 ? "text-emerald" : "text-danger"} />
              </div>

              <div className="panel">
                <h2 className="mb-3 text-sm font-semibold">İşlemler</h2>
                <div className="max-h-[440px] overflow-auto">
                  <table className="w-full text-[12.5px]">
                    <thead className="sticky top-0 bg-panel text-[10.5px] uppercase tracking-wider text-slate-400">
                      <tr className="border-b border-white/10">
                        <th className="p-2 text-left">Giriş</th>
                        <th className="p-2 text-left">Çıkış</th>
                        <th className="p-2 text-left">Yön</th>
                        <th className="p-2 text-right">PnL</th>
                        <th className="p-2 text-right">R</th>
                        <th className="p-2 text-left">Strateji</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...report.trades].reverse().slice(0, 80).map((t, i) => (
                        <tr key={i} className="border-b border-white/5">
                          <td className="p-2 text-slate-400">{t.entry_time.slice(0, 16)}</td>
                          <td className="p-2 text-slate-400">{t.exit_time.slice(0, 16)}</td>
                          <td className="p-2">
                            <span className={`badge ${t.side === "long" ? "badge-green" : "badge-red"}`}>
                              {t.side.toUpperCase()}
                            </span>
                          </td>
                          <td className={`p-2 text-right font-mono ${toneOf(t.pnl)}`}>
                            {t.pnl >= 0 ? "+" : ""}{money(t.pnl)}
                          </td>
                          <td className={`p-2 text-right font-mono ${toneOf(t.r_multiple)}`}>
                            {(t.r_multiple ?? 0).toFixed(2)}R
                          </td>
                          <td className="p-2 text-slate-400">{t.strategy}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </>
      )}
    </Shell>
  );
}
