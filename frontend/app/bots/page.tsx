"use client";

import Link from "next/link";
import useSWR from "swr";
import { Play, Square, RotateCw, Trash2, Sparkles } from "lucide-react";
import { Shell } from "@/components/Shell";
import { api, decisionModeLabel, fetcher, money, pct, toneOf, type Bot } from "@/lib/api";

export default function BotsPage() {
  const { data: bots, mutate, isLoading } = useSWR<Bot[]>("/api/bots", fetcher, {
    refreshInterval: 15000,
  });

  async function act(bot: Bot, action: "start" | "stop" | "run-once" | "unlock") {
    await api(`/api/bots/${bot.id}/${action}`, { method: "POST" });
    mutate();
  }

  async function remove(bot: Bot) {
    if (!confirm(`"${bot.name}" ve tüm işlem geçmişi kalıcı olarak silinecek. Emin misiniz?`)) return;
    await api(`/api/bots/${bot.id}`, { method: "DELETE" });
    mutate();
  }

  return (
    <Shell
      title="Botlar"
      subtitle="Tüm otonom stratejileriniz"
      actions={
        <Link href="/bots/new" className="btn btn-primary">
          <Sparkles size={15} /> Yeni Bot
        </Link>
      }
    >
      {isLoading ? (
        <div className="panel text-center text-slate-400">Yükleniyor…</div>
      ) : !bots?.length ? (
        <div className="panel py-14 text-center text-slate-400">
          Henüz bot oluşturmadınız.
          <div className="mt-3">
            <Link href="/bots/new" className="btn btn-primary">İlk Botumu Oluştur</Link>
          </div>
        </div>
      ) : (
        <div className="grid gap-4 xl:grid-cols-2">
          <div className="xl:col-span-2 text-[11.5px] text-slate-400">
            Paper moddaki sonuçlar sanaldır; gerçek kâr kanıtı değildir.
          </div>
          {bots.map((bot) => (
            <div key={bot.id} className="panel">
              <div className="mb-3 flex items-start justify-between gap-3">
                <div className="flex items-center gap-2.5">
                  <span
                    className={`h-2 w-2 rounded-full ${
                      bot.status === "running"
                        ? "animate-pulseDot bg-emerald"
                        : bot.status === "locked" ? "bg-danger" : "bg-slate-750"
                    }`}
                  />
                  <div>
                    <div className="text-sm font-semibold">{bot.name}</div>
                    <div className="text-[11.5px] text-slate-400">
                      {bot.symbol} · {bot.timeframe} · {bot.exchange}
                    </div>
                  </div>
                </div>
                <div className="flex gap-1.5">
                  <span className={`badge ${bot.mode === "paper" ? "badge-slate" : "badge-amber"}`}>
                    {bot.mode === "paper" ? "PAPER" : "CANLI"}
                  </span>
                  {bot.risk.recovery_mode && <span className="badge badge-amber">TOPARLANMA</span>}
                </div>
              </div>

              <div className="mb-3 grid grid-cols-3 gap-3">
                <div>
                  <div className="stat-label">Bakiye</div>
                  <div className="font-mono text-[17px]">{money(bot.capital.balance)}</div>
                </div>
                <div>
                  <div className="stat-label">Getiri</div>
                  <div className={`font-mono text-[17px] ${toneOf(bot.capital.total_return_pct)}`}>
                    {pct(bot.capital.total_return_pct)}
                  </div>
                </div>
                <div>
                  <div className="stat-label">İşlem</div>
                  <div className="font-mono text-[17px]">{bot.stats.total_trades}</div>
                </div>
              </div>

              <div className="mb-3 flex flex-wrap gap-1.5">
                <span className="badge badge-slate">{decisionModeLabel(bot.decision_mode)}</span>
                <span className="badge badge-slate">Risk %{bot.risk.effective_risk_pct}</span>
                <span className="badge badge-slate">{bot.stats.open_positions} açık</span>
              </div>

              {bot.risk.locked_until && (
                <div className="mb-3 rounded-r-lg border-l-2 border-danger bg-danger/10 px-3 py-2 text-[12px] text-[#ffc0cc]">
                  {bot.risk.lock_reason}
                  <button className="btn ml-2 px-2 py-0.5 text-[11px]" onClick={() => act(bot, "unlock")}>
                    Kilidi Aç
                  </button>
                </div>
              )}

              <div className="flex flex-wrap gap-2">
                <Link href={`/bots/${bot.id}`} className="btn px-3 py-1.5 text-xs">Detay</Link>
                {bot.status === "running" ? (
                  <button className="btn btn-danger px-3 py-1.5 text-xs" onClick={() => act(bot, "stop")}>
                    <Square size={12} /> Durdur
                  </button>
                ) : (
                  <button className="btn btn-primary px-3 py-1.5 text-xs" onClick={() => act(bot, "start")}>
                    <Play size={12} /> Başlat
                  </button>
                )}
                <button className="btn px-3 py-1.5 text-xs" onClick={() => act(bot, "run-once")}>
                  <RotateCw size={12} /> Tek Tur
                </button>
                <button className="btn ml-auto px-2.5 py-1.5 text-xs" onClick={() => remove(bot)}>
                  <Trash2 size={13} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </Shell>
  );
}
