"use client";

/**
 * SİSTEM BOTLARI — hazır, doğrulanmış ticaret sistemleri kütüphanesi.
 *
 * Ürünün temel ilkesi burada görünür: yapay zeka bot YAZMAZ. Sistemler kodun
 * içinde sabittir ve testten geçer; ajan yalnızca koşullara uyanı seçer.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import { Shell } from "@/components/Shell";
import { api } from "@/lib/api";
import { agentApi, AgentCatalog, Playbook } from "@/lib/agent";

export default function SystemsPage() {
  const { data: catalog } = useSWR<AgentCatalog>("agent-catalog", agentApi.catalog);
  const [selected, setSelected] = useState<Playbook | null>(null);

  const playbooks = catalog?.playbooks ?? [];
  const cap = catalog?.capabilities;

  return (
    <Shell title="Sistem botları" subtitle="kütüphane">
      <div className="mb-5 rounded-lg border border-white/5 bg-white/[0.02] px-4 py-3 text-[12.5px] leading-relaxed text-slate-400">
        Buradaki sistemlerin tamamı platformun içinde, testten geçmiş hâlde durur.
        Yapay zeka <b className="text-white">yeni bot yazmaz</b>; koşullara uyanı
        seçer, kurar ve denetler. Uygunluk skoru piyasa rejimi ve volatiliteden{" "}
        <b className="text-white">kodla</b> hesaplanır.
        <div className="mt-1.5 text-[11.5px] text-slate-500">
          {cap?.playbooks ?? playbooks.length} sistem · {cap?.strategies ?? "—"} strateji ·
          işlem başına risk tavanı %{cap?.max_risk_pct ?? 1.5}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {playbooks.map((pb) => (
          <article key={pb.id} className="panel flex flex-col gap-3">
            <header className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h3 className="text-[13.5px] font-semibold">{pb.label}</h3>
                <p className="mt-0.5 text-[11.5px] text-slate-500">
                  {pb.timeframe} · risk %{pb.risk_pct} · {pb.min_agree} strateji teyidi ·{" "}
                  {pb.horizon} vade
                </p>
              </div>
              <button className="btn btn-primary shrink-0 px-3 py-1.5 text-xs"
                      onClick={() => setSelected(pb)}>
                Kur
              </button>
            </header>

            <p className="border-b border-white/5 pb-3 text-[12.5px] leading-relaxed text-slate-400">
              {pb.thesis}
            </p>

            <Row label="Güçlü yanı" value={pb.strength} />
            <Row label="Zayıf yanı" value={pb.weakness} danger />
            <Row label="Kaçın" value={pb.avoid_when} />

            <div className="flex flex-wrap gap-1.5">
              {pb.strategies.map((s) => (
                <span key={s}
                      className="rounded bg-white/5 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">
                  {s}
                </span>
              ))}
            </div>
          </article>
        ))}
      </div>

      {selected && <DeployDialog playbook={selected} onClose={() => setSelected(null)} />}
    </Shell>
  );
}

function Row({ label, value, danger }: { label: string; value: string; danger?: boolean }) {
  return (
    <div className="flex gap-3 text-[11.5px] leading-relaxed">
      <span className={`w-[70px] shrink-0 pt-px text-[10px] uppercase tracking-wide ${
        danger ? "text-danger" : "text-slate-600"}`}>
        {label}
      </span>
      <span className="min-w-0 text-slate-400">{value}</span>
    </div>
  );
}

function DeployDialog({ playbook, onClose }: { playbook: Playbook; onClose: () => void }) {
  const router = useRouter();
  const [symbol, setSymbol] = useState("BTC/USDT");
  const [market, setMarket] = useState("crypto");
  const [balance, setBalance] = useState(1000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function deploy() {
    setBusy(true);
    setError("");
    try {
      const res = await api<{ bot_id: number }>("/api/bots/from-playbook", {
        method: "POST",
        body: {
          playbook_id: playbook.id,
          symbol: symbol.trim().toUpperCase(),
          market,
          initial_balance: balance,
        },
      });
      router.push(`/bots/${res.bot_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Kurulum başarısız");
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4"
         onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="panel w-full max-w-lg">
        <h3 className="text-[13.5px] font-semibold">{playbook.label} kur</h3>
        <p className="mt-1 text-[12px] leading-relaxed text-slate-400">{playbook.thesis}</p>

        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="label">Piyasa</span>
            <select className="input" value={market} onChange={(e) => setMarket(e.target.value)}>
              <option value="crypto">Kripto</option>
              <option value="stock">Hisse</option>
              <option value="demo">Demo</option>
            </select>
          </label>
          <label className="block">
            <span className="label">Parite / sembol</span>
            <input className="input" value={symbol}
                   onChange={(e) => setSymbol(e.target.value)} />
          </label>
        </div>

        <label className="mt-3 block">
          <span className="label">Başlangıç kasası (sanal)</span>
          <input className="input font-mono" type="number" min={100} step={100}
                 value={balance} onChange={(e) => setBalance(Number(e.target.value))} />
        </label>

        <div className="mt-4 rounded-lg border border-white/5 bg-white/[0.02] px-3.5 py-3 text-[11.5px] leading-relaxed text-slate-400">
          Bot <b className="text-white">sanal (paper)</b> modda kurulur. Zaman dilimi,
          strateji seti ve risk sistemin doğrulanmış değerlerinden gelir:{" "}
          {playbook.timeframe} · {playbook.strategies.length} strateji · %{playbook.risk_pct} risk.
          <div className="mt-1.5">
            <b className="text-white">Zayıf yanı:</b> {playbook.weakness}
          </div>
        </div>

        {error && (
          <div className="mt-3 rounded-lg border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[12px] text-danger">
            {error}
          </div>
        )}

        <div className="mt-4 flex justify-end gap-2">
          <button className="btn px-3 py-1.5 text-xs" onClick={onClose} disabled={busy}>
            Vazgeç
          </button>
          <button className="btn btn-primary px-3 py-1.5 text-xs" onClick={deploy} disabled={busy}>
            {busy ? "Kuruluyor…" : "Sanal modda kur"}
          </button>
        </div>
      </div>
    </div>
  );
}
