"use client";

import Link from "next/link";
import useSWR from "swr";
import { Sparkles } from "lucide-react";
import { Shell, StatCard } from "@/components/Shell";
import { LogTerminal } from "@/components/LogTerminal";
import { decisionModeLabel, fetcher, money, pct, toneOf } from "@/lib/api";

type Overview = {
  bot_count: number;
  running: number;
  capital: {
    total_balance: number; total_initial: number; net_pnl: number;
    return_pct: number; drawdown_pct: number; peak_equity: number;
  };
  performance: {
    total_trades: number; wins: number; losses: number; win_rate_pct: number;
    profit_factor: number; avg_r: number; open_positions: number;
  };
  bots: {
    id: number; name: string; symbol: string; status: string; mode: string;
    decision_mode: string; balance: number; return_pct: number; recovery_mode: boolean;
  }[];
};

export default function DashboardPage() {
  const { data, isLoading } = useSWR<Overview>("/api/portfolio/overview", fetcher, {
    refreshInterval: 20000,
  });

  return (
    <Shell
      title="Portföy"
      subtitle="Portföyünüzün canlı durumu"
      actions={
        <Link href="/bots/new" className="btn btn-primary">
          <Sparkles size={15} /> Yeni Bot Oluştur
        </Link>
      }
    >
      {isLoading || !data ? (
        <div className="panel text-center text-slate-400">Yükleniyor…</div>
      ) : (
        <>
          <div className="mb-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <StatCard
              label="Toplam Sermaye"
              value={money(data.capital.total_balance)}
              foot={`Başlangıç: ${money(data.capital.total_initial)}`}
            />
            <StatCard
              label="Net Kâr / Zarar"
              value={`${data.capital.net_pnl >= 0 ? "+" : ""}${money(data.capital.net_pnl)}`}
              foot={`Getiri ${pct(data.capital.return_pct)}`}
              tone={toneOf(data.capital.net_pnl)}
            />
            <StatCard
              label="Kazanma Oranı"
              value={`%${data.performance.win_rate_pct.toFixed(1)}`}
              foot={`${data.performance.wins}K / ${data.performance.losses}Z · ${data.performance.total_trades} işlem`}
            />
            <StatCard
              label="Kâr Faktörü"
              value={data.performance.profit_factor >= 999 ? "∞" : data.performance.profit_factor.toFixed(2)}
              foot={`Ort. ${data.performance.avg_r.toFixed(2)}R · DD %${data.capital.drawdown_pct.toFixed(2)}`}
              tone={data.performance.profit_factor >= 1.5 ? "text-emerald" : "text-slate-400"}
            />
          </div>

          <div className="mb-4 panel">
            <div className="mb-1 text-[11.5px] text-slate-400">
              Paper moddaki sonuçlar sanaldır; gerçek kâr kanıtı değildir.
            </div>
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-semibold">Bot Filosu</h2>
              <span className="text-[11.5px] text-slate-400">
                {data.running} çalışıyor · {data.bot_count} toplam
              </span>
            </div>

            {data.bots.length === 0 ? (
              <div className="py-10 text-center text-slate-400">
                Henüz bot yok.{" "}
                <Link href="/bots/new" className="text-emerald-mint">
                  3 adımda ilkini oluşturun.
                </Link>
              </div>
            ) : (
              <div className="grid gap-2.5 md:grid-cols-2">
                {data.bots.map((bot) => (
                  <Link
                    key={bot.id}
                    href={`/bots/${bot.id}`}
                    className="rounded-lg border border-white/10 bg-[#0e1622] p-3.5 transition hover:border-emerald/40"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span
                        className={`h-2 w-2 rounded-full ${
                          bot.status === "running"
                            ? "animate-pulseDot bg-emerald"
                            : bot.status === "locked"
                              ? "bg-danger"
                              : "bg-slate-750"
                        }`}
                      />
                      <b>{bot.name}</b>
                      <span className="badge badge-slate">{bot.symbol}</span>
                      <span className={`badge ${bot.mode === "paper" ? "badge-slate" : "badge-amber"}`}>
                        {bot.mode === "paper" ? "PAPER" : "CANLI"}
                      </span>
                      {bot.recovery_mode && <span className="badge badge-amber">TOPARLANMA</span>}
                      <span className={`ml-auto font-mono ${toneOf(bot.return_pct)}`}>
                        {pct(bot.return_pct)}
                      </span>
                    </div>
                    <div className="mt-1.5 text-[11.5px] text-slate-400">
                      Bakiye {money(bot.balance)} · {decisionModeLabel(bot.decision_mode)}
                    </div>
                  </Link>
                ))}
              </div>
            )}
          </div>

          <div className="panel">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-semibold">Canlı karar terminali</h2>
              <span className="text-[11.5px] text-slate-400">
                Veri - Matematik - Algoritma - Yapay Zeka - Risk - İcra
              </span>
            </div>
            <LogTerminal />
          </div>
        </>
      )}
    </Shell>
  );
}
