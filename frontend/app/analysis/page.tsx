"use client";

import { useState } from "react";
import { Shell, StatCard } from "@/components/Shell";
import { PriceChart } from "@/components/PriceChart";
import { api, toneOf } from "@/lib/api";

type Analysis = {
  snapshot: {
    symbol: string; price: number; regime: string;
    indicators: Record<string, number>;
    structure: Record<string, string | boolean>;
    support: number[]; resistance: number[];
  };
  technical_bias: { score: number; label: string; reasons: string[] };
  consensus: {
    action: string; confidence: number; summary: string;
    stop_loss: number; take_profit: number;
    signals: { name: string; action: string; confidence: number; weight: number; reason: string }[];
  };
  order_book: { available: boolean; pressure?: string; imbalance_pct?: number };
};

export default function AnalysisPage() {
  const [market, setMarket] = useState("crypto");
  const [symbol, setSymbol] = useState("BTC/USDT");
  const [timeframe, setTimeframe] = useState("1h");
  const [data, setData] = useState<Analysis | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const exchange = market === "crypto" ? "binance" : market === "stock" ? "yfinance" : "demo";

  async function run() {
    setBusy(true);
    setError("");
    try {
      setData(await api<Analysis>(
        `/api/market/analyze?market=${market}&exchange=${exchange}` +
          `&symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}`,
      ));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Hata");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell title="Piyasa Analizi" subtitle="Deterministik göstergeler ve strateji konsensüsü">
      <div className="panel mb-4">
        <div className="flex flex-wrap items-center gap-2.5">
          <select className="input max-w-[150px]" value={market} onChange={(e) => setMarket(e.target.value)}>
            <option value="crypto">Kripto</option>
            <option value="stock">Hisse</option>
            <option value="demo">Demo</option>
          </select>
          <input className="input max-w-[190px] font-mono" value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())} />
          <select className="input max-w-[110px]" value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
            {["1m", "5m", "15m", "30m", "1h", "4h", "1d"].map((tf) => <option key={tf}>{tf}</option>)}
          </select>
          <button className="btn btn-primary" onClick={run} disabled={busy}>
            {busy ? "Hesaplanıyor…" : "Analiz Et"}
          </button>
          <span className="text-[11.5px] text-slate-400">
            Bu analiz yapay zeka çağırmaz — anında ve ücretsizdir.
          </span>
        </div>
      </div>

      {error && (
        <div className="panel border-danger/40 text-[#ffc0cc]">{error}</div>
      )}

      {data && (
        <>
          <div className="mb-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <StatCard label="Fiyat" value={String(data.snapshot.price)} foot={data.snapshot.symbol} />
            <StatCard label="Rejim" value={data.snapshot.regime.replaceAll("_", " ")}
              foot={`ADX ${data.snapshot.indicators.adx_14?.toFixed(1) ?? "—"}`} />
            <StatCard label="Teknik Skor"
              value={`${data.technical_bias.score > 0 ? "+" : ""}${data.technical_bias.score}`}
              foot={data.technical_bias.label} tone={toneOf(data.technical_bias.score)} />
            <StatCard label="Konsensüs" value={data.consensus.action} foot={data.consensus.summary}
              tone={data.consensus.action === "BUY" ? "text-emerald"
                : data.consensus.action === "SELL" ? "text-danger" : ""} />
          </div>

          <div className="mb-4 grid gap-4 xl:grid-cols-2">
            <div className="panel">
              <h2 className="mb-3 text-sm font-semibold">
                Deterministik Göstergeler
                <span className="ml-2 text-[11px] font-normal text-slate-400">
                  Katman 2 · %0 halüsinasyon
                </span>
              </h2>
              <div className="grid grid-cols-2 gap-2 text-[12.5px]">
                {Object.entries(data.snapshot.indicators).map(([k, v]) => (
                  <div key={k} className="flex justify-between">
                    <span className="text-slate-400">{k}</span>
                    <b className="font-mono">{v}</b>
                  </div>
                ))}
              </div>
            </div>

            <div className="panel">
              <h2 className="mb-3 text-sm font-semibold">Piyasa Yapısı</h2>
              <div className="space-y-1.5 text-[12.5px]">
                {Object.entries(data.snapshot.structure).map(([k, v]) => (
                  <div key={k} className="flex justify-between">
                    <span className="text-slate-400">{k.replaceAll("_", " ")}</span>
                    <b>{typeof v === "boolean" ? (v ? "evet" : "hayır") : v}</b>
                  </div>
                ))}
              </div>
              <div className="mt-3 space-y-1 text-[12.5px] text-slate-400">
                <div>Direnç: <b className="font-mono text-danger">
                  {data.snapshot.resistance.join(" · ") || "—"}</b></div>
                <div>Destek: <b className="font-mono text-emerald">
                  {data.snapshot.support.join(" · ") || "—"}</b></div>
                {data.order_book.available && (
                  <div>Emir defteri: <b>{data.order_book.pressure}</b> ({data.order_book.imbalance_pct}%)</div>
                )}
              </div>
            </div>
          </div>

          <div className="panel mb-4">
            <h2 className="mb-3 text-sm font-semibold">Strateji Motoru Oylaması</h2>
            <div className="overflow-x-auto">
              <table className="w-full text-[12.5px]">
                <thead className="text-[10.5px] uppercase tracking-wider text-slate-400">
                  <tr className="border-b border-white/10">
                    <th className="p-2 text-left">Strateji</th>
                    <th className="p-2 text-left">Karar</th>
                    <th className="p-2 text-right">Güven</th>
                    <th className="p-2 text-right">Ağırlık</th>
                    <th className="p-2 text-left">Gerekçe</th>
                  </tr>
                </thead>
                <tbody>
                  {data.consensus.signals.map((s) => (
                    <tr key={s.name} className="border-b border-white/5">
                      <td className="p-2">{s.name}</td>
                      <td className="p-2">
                        <span className={`badge ${s.action === "BUY" ? "badge-green"
                          : s.action === "SELL" ? "badge-red" : "badge-slate"}`}>{s.action}</span>
                      </td>
                      <td className="p-2 text-right font-mono">
                        {s.confidence ? `%${(s.confidence * 100).toFixed(0)}` : "—"}
                      </td>
                      <td className="p-2 text-right font-mono">{s.weight}</td>
                      <td className="p-2 text-slate-400">{s.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="panel">
            <PriceChart market={market} exchange={exchange} symbol={symbol} timeframe={timeframe}
              levels={data.consensus.action !== "WAIT" ? [
                { price: data.snapshot.price, color: "#facc15", title: "GİRİŞ" },
                { price: data.consensus.stop_loss, color: "#ff4d6d", title: "STOP" },
                { price: data.consensus.take_profit, color: "#00e676", title: "HEDEF" },
              ] : []} />
          </div>
        </>
      )}
    </Shell>
  );
}
