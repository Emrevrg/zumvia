"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import { MessageSquare } from "lucide-react";
import { Shell, StatCard } from "@/components/Shell";
import { PriceChart } from "@/components/PriceChart";
import { FinanceAssistant } from "@/components/FinanceAssistant";
import { api, fetcher, money, pct, toneOf } from "@/lib/api";
import { agentApi } from "@/lib/agent";

type Tick = {
  symbol: string; label: string; market: string; exchange: string;
  group?: string; price: number | null; change_pct: number | null;
  change_abs?: number | null; day_high?: number | null; day_low?: number | null;
  volume?: number | null; spread_pct?: number | null;
  ok: boolean; error: string;
};

type Board = {
  at: string; pulse: Tick[]; watchlist: Tick[];
  gainers: Tick[]; losers: Tick[]; measured: number; failed_count: number;
};

type WatchItem = {
  id: number; symbol: string; market: string; exchange: string; label: string;
};

type Macro = { available: boolean; regime: string; reasons: string[]; reason?: string };
type News = {
  headlines: { title: string; source: string; mood?: string; url?: string }[];
};
type Fundamentals = {
  available: boolean; reason?: string; name?: string; sector?: string;
  [k: string]: number | string | boolean | null | undefined;
};

const FUND_FIELDS: [string, string][] = [
  ["name", "Şirket"], ["sector", "Sektör"], ["currency", "Para birimi"],
  ["market_cap", "Piyasa değeri"], ["trailing_pe", "F/K (geçmiş)"],
  ["forward_pe", "F/K (beklenti)"], ["price_to_book", "PD/DD"],
  ["ev_to_ebitda", "FD/FAVÖK"], ["dividend_yield", "Temettü verimi"],
  ["eps_trailing", "HBK"], ["roe", "ROE"], ["debt_to_equity", "Borç/Özsermaye"],
  ["gross_margin", "Brüt marj"], ["net_margin", "Net marj"],
];

function DetailPanel({ detail, fund, market }: {
  detail: Record<string, any>; fund: Fundamentals | null; market: string;
}) {
  const q = detail.quote ?? {};
  const c = detail.consensus ?? {};
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <div>
        <div className="grid grid-cols-2 gap-2 text-[12.5px]">
          {[
            ["Fiyat", q.price],
            ["Değişim %", q.change_pct],
            ["Gün aralığı", q.day_low != null ? `${q.day_low} – ${q.day_high}` : null],
            ["Hacim", q.volume],
            ["Spread %", q.spread_pct],
            ["Rejim", detail.snapshot?.regime?.replaceAll?.("_", " ")],
          ].map(([k, v]) => (
            <div key={k as string} className="flex justify-between gap-2">
              <span className="text-slate-400">{k}</span>
              <b className="font-mono">{v == null ? "ölçülemedi" : String(v)}</b>
            </div>
          ))}
        </div>
        {(detail.error || (!q.price && q.error)) && (
          <div className="mt-2 text-[12px] text-[#ffc0cc]">{detail.error || q.error}</div>
        )}
        {c.action && (
          <div className="mt-3 rounded-lg bg-white/[0.03] p-2.5 text-[12.5px]">
            <span className="text-slate-400">Algoritmik oy:</span>{" "}
            <b className={c.action === "BUY" ? "text-emerald" : c.action === "SELL" ? "text-danger" : ""}>
              {c.action}
            </b>
            {c.confidence != null && (
              <span className="font-mono"> · %{Math.round(c.confidence * 100)}</span>
            )}
            {c.summary && <div className="mt-1 text-slate-400">{c.summary}</div>}
          </div>
        )}
        {Array.isArray(detail.news) && detail.news.length > 0 && (
          <div className="mt-3">
            <div className="stat-label mb-1">İlgili haberler</div>
            <div className="space-y-1.5 text-[12px]">
              {detail.news.slice(0, 5).map((n: any, i: number) => (
                <div key={i}>
                  <div>{n.title}</div>
                  <div className="text-[11px] text-slate-500">
                    {n.source}{n.mood ? ` · hava: ${n.mood}` : ""}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
      <div>
        <div className="stat-label mb-1">
          Temel oranlar {fund && !fund.available ? "— ölçülemedi" : ""}
        </div>
        {fund && !fund.available && (
          <div className="text-[12px] text-slate-400">{fund.reason}</div>
        )}
        {fund?.available && (
          <div className="grid grid-cols-2 gap-2 text-[12.5px]">
            {FUND_FIELDS.map(([k, label]) => (
              <div key={k} className="flex justify-between gap-2">
                <span className="text-slate-400">{label}</span>
                <b className="font-mono">{fund[k] == null ? "—" : String(fund[k])}</b>
              </div>
            ))}
          </div>
        )}
        {!fund && market === "stock" && (
          <div className="text-slate-400">Temel veriler yükleniyor…</div>
        )}
        {market !== "stock" && (
          <div className="text-slate-400">Temel oranlar hisse senetleri içindir.</div>
        )}
      </div>
    </div>
  );
}

function TickCard({ t, onOpen }: { t: Tick; onOpen: (t: Tick) => void }) {
  return (
    <button onClick={() => onOpen(t)} className="panel panel-lift p-3 text-left">
      <div className="text-[11px] text-slate-400">{t.label}</div>
      <div className="font-mono text-[17px]">
        {t.price == null ? "ölçülemedi" : money(t.price, t.price < 10 ? 4 : 2)}
      </div>
      <div className={`font-mono text-[12px] ${toneOf(t.change_pct ?? 0)}`}>
        {t.change_pct == null ? (t.error || "—") : pct(t.change_pct)}
      </div>
      <div className="mt-1 font-mono text-[10.5px] text-slate-500">{t.symbol}</div>
    </button>
  );
}

function TickRows({ rows, onOpen }: { rows: Tick[]; onOpen: (t: Tick) => void }) {  if (!rows.length) return <div className="text-slate-400">Ölçülen hareket yok.</div>;
  return (
    <div className="space-y-1.5">
      {rows.map((t) => (
        <button key={t.symbol} onClick={() => onOpen(t)}
          className="flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-left hover:bg-white/5">
          <span>
            <span className="text-[13px] font-semibold">{t.label}</span>
            <span className="ml-2 font-mono text-[11px] text-slate-500">{t.symbol}</span>
          </span>
          <span className={`font-mono text-[13px] ${toneOf(t.change_pct ?? 0)}`}>
            {t.change_pct == null ? "—" : pct(t.change_pct)}
          </span>
        </button>
      ))}
    </div>
  );
}

const EXCHANGES = ["binance", "bybit", "okx", "kraken", "coinbase", "kucoin", "bitstamp", "mexc"];

/** Aynı paritenin 8 borsadaki canlı fiyatı — medyana göre sapma. */
function ExchangeStrip({ symbol }: { symbol: string }) {
  const [rows, setRows] = useState<{ ex: string; price: number | null }[]>([]);
  useEffect(() => {
    if (!symbol.includes("/")) return;
    let dead = false;
    async function load() {
      const out = await Promise.all(EXCHANGES.map(async (ex) => {
        try {
          const r = await api<{ price: number | null }>(
            `/api/finance/quote?symbol=${encodeURIComponent(symbol)}&market=crypto&exchange=${ex}`);
          return { ex, price: typeof r.price === "number" ? r.price : null };
        } catch {
          return { ex, price: null };
        }
      }));
      if (!dead) setRows(out);
    }
    load();
    const t = setInterval(load, 30000);
    return () => { dead = true; clearInterval(t); };
  }, [symbol]);

  if (!symbol.includes("/")) return null;
  const oks = rows.filter((r) => r.price != null).map((r) => r.price as number);
  const med = oks.length ? [...oks].sort((a, b) => a - b)[Math.floor(oks.length / 2)] : null;
  return (
    <div className="panel mb-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold">Borsalar Canlı — {symbol}</h2>
        <span className="text-[11.5px] text-slate-400">
          {oks.length}/{EXCHANGES.length} borsa · 30 sn'de bir yenilenir
        </span>
      </div>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {rows.map((r) => (
          <div key={r.ex} className="rounded-lg bg-white/[0.03] px-3 py-2">
            <div className="text-[11px] uppercase tracking-wide text-slate-400">{r.ex}</div>
            <div className="font-mono text-[15px]">
              {r.price == null ? "ölçülemedi" : money(r.price, 2)}
            </div>
            <div className="font-mono text-[11.5px] text-slate-400">
              {r.price == null || med == null ? "—"
                : `medyan ${((r.price - med) / med * 100) >= 0 ? "+" : ""}${((r.price - med) / med * 100).toFixed(3)}%`}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function FinancePage() {
  const router = useRouter();
  const { data: board, error: boardError, mutate: mutateBoard } = useSWR<Board>("/api/finance/board", fetcher, { refreshInterval: 60000 });
  const { data: watch, mutate: mutateWatch } = useSWR<{ items: WatchItem[] }>("/api/finance/watchlist", fetcher);
  const { data: macro } = useSWR<Macro>("/api/finance/macro", fetcher);
  const { data: news } = useSWR<News>("/api/finance/news?limit=12", fetcher);

  const [q, setQ] = useState("");
  const [results, setResults] = useState<Tick[]>([]);
  const [searching, setSearching] = useState(false);
  const [sel, setSel] = useState<Tick | null>(null);
  const [detail, setDetail] = useState<Record<string, any> | null>(null);
  const [fund, setFund] = useState<Fundamentals | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [fx, setFx] = useState({ base: "USD", quote: "TRY", amount: "1", out: "" });
  const [notice, setNotice] = useState("");
  const [stripSymbol, setStripSymbol] = useState("BTC/USDT");
  const [assistant, setAssistant] = useState<Tick | null>(null);
  const [taskBusy, setTaskBusy] = useState(false);
  const [groupFilter, setGroupFilter] = useState("Tümü");

  async function createTask(title: string, prompt: string): Promise<number | null> {
    setTaskBusy(true);
    try {
      const keys = await api<{ id: number; kind: string }[]>("/api/keys").catch(() => []);
      const llm = (keys ?? []).find((k) => k.kind === "llm");
      const created = await agentApi.createSession({
        title,
        control_tool: "api",
        control_credential_id: llm?.id ?? null,
        control_model: "",
        council_mode: "auto",
        work_mode: "agent",
        heartbeat_seconds: 900,
      });
      await agentApi.send(created.id, prompt);
      router.push(`/command?session=${created.id}`);
      return created.id;
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Görev oluşturulamadı");
      setTaskBusy(false);
      return null;
    }
  }

  async function search() {
    if (!q.trim()) return;
    setSearching(true);
    try {
      const r = await api<{ results: Tick[] }>(`/api/finance/search?q=${encodeURIComponent(q)}`);
      setResults(r.results ?? []);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Arama hatası");
    } finally {
      setSearching(false);
    }
  }

  async function addWatch(t: Tick) {
    try {
      await api("/api/finance/watchlist", {
        method: "POST",
        body: { symbol: t.symbol, market: t.market, exchange: t.exchange },
      });
      mutateWatch();
      mutateBoard();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Ekleme hatası");
    }
  }

  async function removeWatch(id: number) {
    await api(`/api/finance/watchlist/${id}`, { method: "DELETE" });
    mutateWatch();
    mutateBoard();
  }

  async function refresh() {
    await api("/api/finance/refresh", { method: "POST" });
    mutateBoard();
  }

  async function openDetail(t: Tick) {
    setSel(t);
    setDetailBusy(true);
    setDetail(null);
    setFund(null);
    try {
      const qs = `symbol=${encodeURIComponent(t.symbol)}&market=${t.market}&exchange=${t.exchange}`;
      const [d, f] = await Promise.all([
        api<Record<string, any>>(`/api/finance/detail?${qs}`),
        t.market === "stock"
          ? api<Fundamentals>(`/api/finance/fundamentals?${qs}`)
          : Promise.resolve(null),
      ]);
      setDetail(d);
      setFund(f);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Ayrıntı alınamadı");
    } finally {
      setDetailBusy(false);
    }
  }

  async function sendToCommand(t: Tick) {
    const ctx = `[Finans: ${t.symbol} @ ${t.price ?? "ölçülemedi"}${
      t.change_pct == null ? "" : ` (${t.change_pct >= 0 ? "+" : ""}${t.change_pct.toFixed(2)}%)`
    } · ${t.market}/${t.exchange}]`;
    await createTask(`Finans görevi · ${t.symbol}`,
      `${ctx} Bunu analiz et: teknik görünüm nedir, uygun sistem botu var mı? `
      + `Geri testle doğrula ve uygunsa SANAL modda kurup çalıştır. Adımlarını göster.`);
  }

  async function startAutopilot() {
    await createTask("Otopilot · tüm piyasa (sanal)",
      `[Finans Otopilotu] Tüm piyasayı tara (kripto + hisse + endeks): en güçlü 1-2 kurulumu `
      + `walk-forward ile doğrula, geçenleri SADECE SANAL modda kur ve çalıştır. Gerçek para YOK. `
      + `Risk kalkanı tavanlarını aşma (stop-loss zorunlu, işlem başına ≤%1 risk). `
      + `Kurulum yoksa kurma — "yok" de. Sonunda 5 cümlelik rapor ver.`);
  }

  async function convertFx() {
    try {
      const r = await api<{ available: boolean; converted: number; reason?: string }>(
        `/api/finance/fx?base=${encodeURIComponent(fx.base)}&quote=${encodeURIComponent(fx.quote)}&amount=${encodeURIComponent(fx.amount)}`,
      );
      setFx({ ...fx, out: r.available ? money(r.converted) : (r.reason || "ölçülemedi") });
    } catch (err) {
      setFx({ ...fx, out: err instanceof Error ? err.message : "Hata" });
    }
  }

  return (
    <Shell title="Finans" subtitle="Piyasa nabzı, izleme listesi ve temel veriler — sayılar ajanın gördüğüyle aynı kaynaktan"
      actions={
        <button className="btn btn-primary" onClick={() => setAssistant(
          sel ?? { symbol: "BTC/USDT", label: "Bitcoin", market: "crypto", exchange: "binance", price: null, change_pct: null, ok: false, error: "" },
        )}>
          <MessageSquare size={15} /> AI Asistan
        </button>
      }>
      <div className="mb-4 text-[11.5px] text-slate-400">
        Paper moddaki sonuçlar sanaldır; gerçek kâr kanıtı değildir. Ölçülemeyen veri uydurulmaz — kartında sebebi yazar.
      </div>

      {notice && (
        <div className="panel mb-4 border-danger/40 text-[#ffc0cc]">
          {notice}
          <button className="btn ml-3 px-2 py-0.5 text-[11px]" onClick={() => setNotice("")}>Kapat</button>
        </div>
      )}

      {!board && !boardError ? (
        <div aria-live="polite" aria-busy="true">
          <div className="mb-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="panel space-y-3 overflow-hidden">
                <div className="skeleton h-3 w-2/5 rounded" />
                <div className="skeleton h-8 w-3/5 rounded-lg" />
                <div className="skeleton h-3 w-1/2 rounded" />
              </div>
            ))}
          </div>
          <div className="panel flex items-center gap-3 text-slate-400">
            <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-emerald shadow-glow" />
            <div>
              <div className="text-sm font-semibold text-white">Piyasa tahtası hazırlanıyor</div>
              <div className="mt-1 text-[11.5px]">Borsalar, makro göstergeler ve izleme listesi aynı anda ölçülüyor…</div>
            </div>
          </div>
        </div>
      ) : boardError ? (
        <div className="panel border-danger/30 text-[#ffc0cc]">
          <div className="font-semibold">Finans tahtası yüklenemedi.</div>
          <div className="mt-1 text-[12px] text-slate-400">{boardError instanceof Error ? boardError.message : "Bağlantıyı kontrol edip yeniden deneyin."}</div>
          <button className="btn mt-3" onClick={() => mutateBoard()}>Yeniden dene</button>
        </div>
      ) : board ? (
        <>
          <div className="mb-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <StatCard label="Ölçülen enstrüman" value={String(board.measured)}
              foot={board.failed_count ? `${board.failed_count} ölçülemedi` : "Tümü ölçüldü"} />
            <StatCard label="Makro rejim" value={macro?.available ? (macro?.regime ?? "—") : "ölçülemedi"}
              foot={macro?.available ? (macro?.reasons?.[0] ?? "") : (macro?.reason ?? "")} />
            <StatCard label="En çok yükselen" value={board.gainers[0]?.label ?? "—"}
              foot={board.gainers[0]?.change_pct != null ? pct(board.gainers[0].change_pct as number) : ""}
              tone="text-emerald" />
            <StatCard label="En çok düşen" value={board.losers[0]?.label ?? "—"}
              foot={board.losers[0]?.change_pct != null ? pct(board.losers[0].change_pct as number) : ""}
              tone="text-danger" />
          </div>

          <div className="mb-4 flex flex-wrap items-center gap-2">
            <span className="text-[12.5px] text-slate-400">Borsa karşılaştırma:</span>
            <input className="input max-w-[170px] font-mono" value={stripSymbol}
              onChange={(e) => setStripSymbol(e.target.value.toUpperCase())} />
          </div>
          <ExchangeStrip symbol={stripSymbol.trim() || "BTC/USDT"} />

          <div className="panel mb-4 border-emerald/25 bg-gradient-to-r from-emerald/[0.07] via-transparent to-transparent">
            <div className="flex flex-wrap items-center gap-3">
              <div className="min-w-[220px] flex-1">
                <h2 className="text-sm font-semibold">Otopilot — tek tıkla AI yönetimi (sanal)</h2>
                <p className="mt-1 text-[12px] leading-relaxed text-slate-400">
                  Taramayı, doğrulamayı, kurulumu ve denetimi ajan üstlenir; sen yalnızca izlersin.
                  Stop-loss zorunlu, işlem başına ≤%1 risk, acil fren her an elinde.
                  Kâr garantisi yoktur — sistem disiplini otomatikleştirir.
                </p>
              </div>
              <button className="btn btn-primary px-4 py-2" onClick={startAutopilot} disabled={taskBusy}>
                {taskBusy ? "Başlatılıyor…" : "Otopilotu başlat"}
              </button>
            </div>
          </div>

          <div className="panel mb-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-semibold">Piyasa Nabzı</h2>
              <div className="flex gap-1.5">
                {(["Tümü", ...Array.from(new Set(board.pulse.map((t) => t.group ?? "diğer")))] as string[]).map((g) => (
                  <button key={g} onClick={() => setGroupFilter(g)}
                    className={`rounded-full px-2.5 py-1 text-[11.5px] transition ${
                      groupFilter === g ? "bg-emerald/15 text-emerald-mint" : "text-slate-400 hover:text-white"
                    }`}>
                    {g}
                  </button>
                ))}
                <button className="btn px-2.5 py-1 text-[12px]" onClick={refresh}>Şimdi yenile</button>
              </div>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {board.pulse
                .filter((t) => groupFilter === "Tümü" || (t.group ?? "diğer") === groupFilter)
                .map((t) => <TickCard key={t.symbol} t={t} onOpen={openDetail} />)}
            </div>
          </div>

          <div className="mb-4 grid gap-4 xl:grid-cols-3">
            <div className="panel xl:col-span-2">
              <h2 className="mb-3 text-sm font-semibold">İzleme Listesi</h2>
              <div className="mb-3 flex flex-wrap gap-2">
                <input className="input max-w-[240px]" placeholder="Enstrüman ara: bitcoin, apple, altın"
                  value={q} onChange={(e) => setQ(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") search(); }} />
                <button className="btn btn-primary" onClick={search} disabled={searching}>
                  {searching ? "Aranıyor…" : "Ara"}
                </button>
              </div>
              {results.length > 0 && (
                <div className="mb-3 space-y-1.5">
                  {results.map((t) => (
                    <div key={`${t.market}:${t.symbol}`} className="flex items-center justify-between gap-2 rounded-lg bg-white/[0.03] px-2.5 py-1.5">
                      <span className="text-[13px]">
                        <b>{t.label}</b>
                        <span className="ml-2 font-mono text-[11px] text-slate-500">{t.symbol} · {t.market}</span>
                      </span>
                      <button className="btn px-2 py-0.5 text-[11px]" onClick={() => addWatch(t)}>+ İzle</button>
                    </div>
                  ))}
                </div>
              )}
              <div className="grid gap-3 sm:grid-cols-2">
                {board.watchlist.map((t) => <TickCard key={t.symbol} t={t} onOpen={openDetail} />)}
              </div>
              {(watch?.items?.length ?? 0) > 0 && (
                <div className="mt-3 space-y-1 border-t border-white/10 pt-2">
                  {watch!.items.map((w) => (
                    <div key={w.id} className="flex items-center justify-between text-[12.5px]">
                      <span className="font-mono">{w.symbol} <span className="text-slate-500">· {w.market}</span></span>
                      <button className="btn px-2 py-0.5 text-[11px]" onClick={() => removeWatch(w.id)}>Çıkar</button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="space-y-4">
              <div className="panel">
                <h2 className="mb-3 text-sm font-semibold">Döviz Çevirici</h2>
                <div className="flex flex-wrap gap-2">
                  <input className="input max-w-[90px] font-mono" value={fx.amount}
                    onChange={(e) => setFx({ ...fx, amount: e.target.value })} />
                  <input className="input max-w-[80px] font-mono" value={fx.base}
                    onChange={(e) => setFx({ ...fx, base: e.target.value.toUpperCase() })} />
                  <span className="self-center text-slate-400">→</span>
                  <input className="input max-w-[80px] font-mono" value={fx.quote}
                    onChange={(e) => setFx({ ...fx, quote: e.target.value.toUpperCase() })} />
                  <button className="btn btn-primary" onClick={convertFx}>Çevir</button>
                </div>
                {fx.out && <div className="mt-2 font-mono text-[15px]">{fx.out} {fx.quote}</div>}
              </div>

              <div className="panel">
                <h2 className="mb-2 text-sm font-semibold">Hareket Edenler</h2>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <div className="stat-label mb-1">Yükselenler</div>
                    <TickRows rows={board.gainers} onOpen={openDetail} />
                  </div>
                  <div>
                    <div className="stat-label mb-1">Düşenler</div>
                    <TickRows rows={board.losers} onOpen={openDetail} />
                  </div>
                </div>
              </div>

              <div className="panel">
                <h2 className="mb-2 text-sm font-semibold">Finans Haberleri</h2>
                <div className="space-y-2 text-[12.5px]">
                  {(news?.headlines ?? []).slice(0, 8).map((n, i) => (
                    <div key={i}>
                      <div>{n.title}</div>
                      <div className="text-[11px] text-slate-500">{n.source}{n.mood ? ` · hava: ${n.mood}` : ""}</div>
                    </div>
                  ))}
                  {!news && <div className="text-slate-400">Yükleniyor…</div>}
                </div>
                <div className="mt-2 text-[11px] text-slate-500">
                  Duygu skoru sabit sözlükle hesaplanır; yatırım tavsiyesi değildir.
                </div>
              </div>
            </div>
          </div>

          <div className="panel mb-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-semibold">
                Enstrüman Ayrıntısı {sel ? `— ${sel.label} (${sel.symbol})` : ""}
              </h2>
              {sel && (
                <div className="flex gap-2">
                  <button className="btn px-2.5 py-1 text-[12px]" onClick={() => setAssistant(sel)}>
                    AI&apos;ya sor
                  </button>
                  <button className="btn btn-primary px-2.5 py-1 text-[12px]"
                    onClick={() => sendToCommand(sel)} disabled={taskBusy}>
                    {taskBusy ? "Gönderiliyor…" : "Komuta'ya görev olarak gönder"}
                  </button>
                </div>
              )}
            </div>
            {!sel && <div className="text-slate-400">Ayrıntı için bir karta tıklayın.</div>}
            {detailBusy && <div className="text-slate-400">Yükleniyor…</div>}
            {detail && (
              <>
                <DetailPanel detail={detail} fund={fund} market={sel?.market ?? ""} />
                {sel && (
                  <div className="mt-4">
                    <PriceChart market={sel.market} exchange={sel.exchange}
                      symbol={sel.symbol} timeframe="1h" height={300} />
                  </div>
                )}
              </>
            )}
          </div>
        </>
      ) : null}
      {assistant && (
        <FinanceAssistant symbol={assistant.symbol} market={assistant.market}
          exchange={assistant.exchange} price={assistant.price}
          changePct={assistant.change_pct} onClose={() => setAssistant(null)} />
      )}
    </Shell>
  );
}
