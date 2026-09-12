"use client";

import Link from "next/link";
import useSWR from "swr";
import { Activity, Bot, Radio, ShieldCheck, Sparkles, Zap } from "lucide-react";
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
          <section className="panel mb-4 overflow-hidden border-emerald/15 !p-0 animate-fadeUp">
            <div className="relative grid gap-5 overflow-hidden px-5 py-5 md:grid-cols-[1fr_auto] md:px-7">
              <div className="absolute -right-20 -top-28 h-64 w-64 animate-floatSoft rounded-full bg-info/10 blur-3xl" />
              <div className="absolute -bottom-28 left-1/3 h-56 w-56 rounded-full bg-purple-500/10 blur-3xl" />
              <div className="relative">
                <div className="eyebrow"><Radio size={12} className="animate-pulse" /> Sistem nabzı</div>
                <h2 className="mt-3 text-xl font-bold text-white md:text-2xl">
                  Portföy kontrolü sizde, risk kalkanı devrede.
                </h2>
                <p className="mt-1 max-w-2xl text-sm leading-6 text-slate-400">
                  {data.running} aktif bot, {data.performance.open_positions} açık pozisyon ve {data.performance.total_trades} tamamlanan işlem tek ekranda izleniyor.
                </p>
              </div>
              <div className="relative flex items-center gap-3 md:justify-end">
                <div className="rounded-2xl border border-emerald/20 bg-emerald/10 p-3 text-emerald-mint shadow-glow">
                  <ShieldCheck size={24} />
                </div>
                <div>
                  <div className="text-xs uppercase tracking-widest text-slate-400">Durum</div>
                  <div className="mt-0.5 flex items-center gap-2 font-semibold text-white"><span className="h-2 w-2 animate-pulseDot rounded-full bg-emerald" /> Canlı izleme</div>
                </div>
              </div>
            </div>
            <div className="glass-line h-px" />
            <div className="grid grid-cols-3 divide-x divide-white/10 bg-black/10 px-2 py-3 text-center text-xs text-slate-400">
              <span className="flex items-center justify-center gap-1.5"><Zap size={13} className="text-warn" /> Anlık karar akışı</span>
              <span className="flex items-center justify-center gap-1.5"><Bot size={13} className="text-info" /> {data.bot_count} bot</span>
              <span className="flex items-center justify-center gap-1.5"><Activity size={13} className="text-emerald" /> %{(data.performance.win_rate_pct ?? 0).toFixed(1)} başarı</span>
            </div>
          </section>

          <div className="mb-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4 [&>*:nth-child(1)]:animate-fadeUp [&>*:nth-child(2)]:animate-fadeUp [&>*:nth-child(3)]:animate-fadeUp [&>*:nth-child(4)]:animate-fadeUp [&>*:nth-child(2)]:[animation-delay:80ms] [&>*:nth-child(3)]:[animation-delay:160ms] [&>*:nth-child(4)]:[animation-delay:240ms]">
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
              value={`%${(data.performance.win_rate_pct ?? 0).toFixed(1)}`}
              foot={`${data.performance.wins}K / ${data.performance.losses}Z · ${data.performance.total_trades} işlem`}
            />
            <StatCard
              label="Kâr Faktörü"
              value={(data.performance.profit_factor ?? 0) >= 999 ? "∞" : (data.performance.profit_factor ?? 0).toFixed(2)}
              foot={`Ort. ${(data.performance.avg_r ?? 0).toFixed(2)}R · DD %${(data.capital.drawdown_pct ?? 0).toFixed(2)}`}
              tone={data.performance.profit_factor >= 1.5 ? "text-emerald" : "text-slate-400"}
            />
          </div>

          <div className="mb-4 panel animate-fadeUp [animation-delay:280ms]">
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
                    className="group rounded-xl border border-white/10 bg-gradient-to-br from-[#101b2a]/90 to-[#09121d]/80 p-3.5 transition duration-300 hover:-translate-y-0.5 hover:border-emerald/40 hover:shadow-[0_14px_30px_rgba(0,0,0,.28)]"
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

          <div className="panel animate-fadeUp [animation-delay:360ms]">
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
