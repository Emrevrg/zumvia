"use client";

import { useParams } from "next/navigation";
import useSWR from "swr";
import { Play, RotateCw, Square } from "lucide-react";
import { Shell, StatCard } from "@/components/Shell";
import { LogTerminal } from "@/components/LogTerminal";
import { PriceChart } from "@/components/PriceChart";
import {
  api, decisionModeLabel, fetcher, money, pct, toneOf,
  type Bot, type BotEvent, type Position,
} from "@/lib/api";

type Positions = { price: number | null; open: Position[]; pending: Position[]; closed: Position[] };

const CLOSE_REASON: Record<string, string> = {
  TAKE_PROFIT: "Hedefe ulaştı", STOP_LOSS: "Stop-loss", AI_CLOSE: "Yapay zeka çıkışı",
  CIRCUIT_BREAKER: "Devre kesici", MANUAL: "Manuel kapanış", TRAILING_STOP: "İz süren stop",
};

export default function BotDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { data: bot, mutate } = useSWR<Bot>(`/api/bots/${id}`, fetcher, { refreshInterval: 20000 });
  const { data: positions, mutate: mutatePositions } = useSWR<Positions>(
    `/api/bots/${id}/positions`, fetcher, { refreshInterval: 20000 },
  );
  const { data: events } = useSWR<BotEvent[]>(`/api/bots/${id}/events?limit=150`, fetcher);

  if (!bot) return <Shell title="Bot"><div className="panel text-slate-400">Yükleniyor…</div></Shell>;

  const openPosition = positions?.open[0];
  const levels = openPosition
    ? [
        { price: openPosition.entry_price, color: "#facc15", title: "GİRİŞ" },
        { price: openPosition.stop_loss, color: "#ff4d6d", title: "STOP" },
        { price: openPosition.take_profit, color: "#00e676", title: "HEDEF" },
      ]
    : [];

  async function act(action: string) {
    await api(`/api/bots/${id}/${action}`, { method: "POST" });
    mutate();
    mutatePositions();
  }

  async function closePosition(positionId: number) {
    if (!confirm("Pozisyon güncel piyasa fiyatından kapatılacak. Onaylıyor musunuz?")) return;
    await api(`/api/bots/${id}/positions/close`, {
      method: "POST", body: { position_id: positionId },
    });
    mutate();
    mutatePositions();
  }

  return (
    <Shell
      title={bot.name}
      subtitle={`${bot.symbol} · ${bot.timeframe} · ${decisionModeLabel(bot.decision_mode)}`}
      actions={
        <>
          <button className="btn px-3 py-1.5 text-xs" onClick={() => act("run-once")}>
            <RotateCw size={13} /> Tek Tur Çalıştır
          </button>
          {bot.status === "running" ? (
            <button className="btn btn-danger px-3 py-1.5 text-xs" onClick={() => act("stop")}>
              <Square size={13} /> Durdur
            </button>
          ) : (
            <button className="btn btn-primary px-3 py-1.5 text-xs" onClick={() => act("start")}>
              <Play size={13} /> Başlat
            </button>
          )}
        </>
      }
    >
      {bot.risk.locked_until && (
        <div className="mb-4 rounded-r-lg border-l-2 border-danger bg-danger/10 px-4 py-3 text-[13px] text-[#ffc0cc]">
          <b>Devre kesici aktif:</b> {bot.risk.lock_reason}
          <button className="btn ml-3 px-3 py-1 text-xs" onClick={() => act("unlock")}>
            Kilidi Kaldır
          </button>
        </div>
      )}

      {bot.risk.recovery_mode && (
        <div className="mb-4 rounded-r-lg border-l-2 border-warn bg-warn/10 px-4 py-3 text-[13px] text-[#ffdca6]">
          <b>Toparlanma modu aktif.</b> Risk %{bot.risk.effective_risk_pct}&apos;e indirildi, güven
          eşiği %{(bot.risk.effective_min_confidence * 100).toFixed(0)}&apos;e yükseltildi. Sistem
          yalnızca A+ kurulumları alacak — kaybı telafi etmek için risk <b>artırılmaz</b>.
        </div>
      )}

      <div className="mb-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Bakiye" value={money(bot.capital.balance)}
          foot={`Zirve ${money(bot.capital.peak_equity)}`} />
        <StatCard label="Getiri" value={pct(bot.capital.total_return_pct)}
          foot={`Gerçekleşen ${money(bot.capital.realized_pnl)}`}
          tone={toneOf(bot.capital.total_return_pct)} />
        <StatCard label="Kazanma Oranı" value={`%${bot.stats.win_rate_pct.toFixed(1)}`}
          foot={`${bot.stats.wins}K / ${bot.stats.losses}Z`} />
        <StatCard
          label="Durum"
          value={bot.status === "running" ? "ÇALIŞIYOR" : bot.status === "locked" ? "KİLİTLİ" : "DURDU"}
          foot={bot.next_run_at ? `Sonraki tur ${new Date(bot.next_run_at).toLocaleTimeString("tr-TR")}` : "—"}
          tone={bot.status === "running" ? "text-emerald" : bot.status === "locked" ? "text-danger" : ""}
        />
      </div>

      <div className="panel mb-4">
        <div className="mb-3 flex items-center justify-between text-[11.5px] text-slate-400">
          <b className="text-sm text-white">{bot.symbol} · {bot.timeframe}</b>
          <span>EMA50 · EMA200 · Supertrend {openPosition ? "· Giriş/Stop/Hedef" : ""}</span>
        </div>
        <PriceChart
          market={bot.market} exchange={bot.exchange} symbol={bot.symbol}
          timeframe={bot.timeframe} levels={levels}
        />
      </div>

      <div className="panel mb-4">
        <h2 className="mb-3 text-sm font-semibold">
          Açık Pozisyonlar
          <span className="ml-2 text-[11.5px] font-normal text-slate-400">
            {positions?.open.length ?? 0} açık · {positions?.pending.length ?? 0} onay bekliyor
          </span>
        </h2>

        {!positions?.open.length && !positions?.pending.length ? (
          <div className="py-8 text-center text-slate-400">
            Açık pozisyon yok. Bot uygun kurulum bekliyor — işlem yapmamak da bir karardır.
          </div>
        ) : (
          [...(positions?.pending ?? []), ...(positions?.open ?? [])].map((p) => (
            <div key={p.id} className="mb-2.5 rounded-lg border border-white/10 bg-[#0e1622] p-3.5">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <span className={`badge ${p.side === "long" ? "badge-green" : "badge-red"}`}>
                  {p.side === "long" ? "LONG" : "SHORT"}
                </span>
                <b>{p.symbol}</b>
                {p.status === "pending" && <span className="badge badge-amber">ONAY BEKLİYOR</span>}
                {p.unrealized_pnl !== null && (
                  <b className={`ml-auto font-mono ${toneOf(p.unrealized_pnl)}`}>
                    {p.unrealized_pnl >= 0 ? "+" : ""}{money(p.unrealized_pnl)}
                  </b>
                )}
              </div>
              <div className="grid grid-cols-3 gap-2 font-mono text-[12px]">
                <div><span className="text-slate-400">Giriş</span><br />{p.entry_price}</div>
                <div><span className="text-slate-400">Stop</span><br /><span className="text-danger">{p.stop_loss}</span></div>
                <div><span className="text-slate-400">Hedef</span><br /><span className="text-emerald">{p.take_profit}</span></div>
              </div>
              {p.reasoning && <p className="mt-2 text-[12px] text-slate-400">{p.reasoning}</p>}
              {p.status === "open" && (
                <button className="btn btn-danger mt-3 px-3 py-1.5 text-xs"
                  onClick={() => closePosition(p.id)}>
                  Pozisyonu Kapat
                </button>
              )}
            </div>
          ))
        )}
      </div>

      <div className="panel mb-4">
        <h2 className="mb-3 text-sm font-semibold">Karar terminali</h2>
        <LogTerminal initial={events ?? []} botId={Number(id)} />
      </div>

      <div className="panel">
        <h2 className="mb-3 text-sm font-semibold">İşlem Geçmişi</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-[12.5px]">
            <thead className="text-[10.5px] uppercase tracking-wider text-slate-400">
              <tr className="border-b border-white/10">
                <th className="p-2 text-left">Zaman</th>
                <th className="p-2 text-left">Yön</th>
                <th className="p-2 text-right">Giriş</th>
                <th className="p-2 text-right">Çıkış</th>
                <th className="p-2 text-right">PnL</th>
                <th className="p-2 text-right">R</th>
                <th className="p-2 text-left">Sebep</th>
              </tr>
            </thead>
            <tbody>
              {!positions?.closed.length && (
                <tr><td colSpan={7} className="p-6 text-center text-slate-400">Kapanmış işlem yok.</td></tr>
              )}
              {positions?.closed.map((p) => (
                <tr key={p.id} className="border-b border-white/5 hover:bg-white/[.02]">
                  <td className="p-2 text-slate-400">
                    {p.closed_at ? new Date(p.closed_at).toLocaleString("tr-TR") : "—"}
                  </td>
                  <td className="p-2">
                    <span className={`badge ${p.side === "long" ? "badge-green" : "badge-red"}`}>
                      {p.side.toUpperCase()}
                    </span>
                  </td>
                  <td className="p-2 text-right font-mono">{p.entry_price}</td>
                  <td className="p-2 text-right font-mono">{p.exit_price ?? "—"}</td>
                  <td className={`p-2 text-right font-mono ${toneOf(p.pnl)}`}>
                    {p.pnl >= 0 ? "+" : ""}{money(p.pnl)}
                  </td>
                  <td className={`p-2 text-right font-mono ${toneOf(p.r_multiple)}`}>
                    {p.r_multiple.toFixed(2)}R
                  </td>
                  <td className="p-2">{CLOSE_REASON[p.close_reason] ?? p.close_reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </Shell>
  );
}
