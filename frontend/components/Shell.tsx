"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type MouseEvent } from "react";
import {
  Activity, BarChart3, Boxes, BrainCircuit, LineChart, LogOut, Lock,
  MessageSquare, Sparkles,
} from "lucide-react";
import { auth, openEventStream } from "@/lib/api";

const NAV = [
  { href: "/command", label: "Komuta", Icon: MessageSquare },
  { href: "/", label: "Portföy", Icon: Activity },
  { href: "/bots", label: "Botlar", Icon: Boxes },
  { href: "/systems", label: "Sistem botları", Icon: BrainCircuit },
  { href: "/bots/new", label: "Yeni bot", Icon: Sparkles },
  { href: "/analysis", label: "Piyasa analizi", Icon: LineChart },
  { href: "/backtest", label: "Geri test", Icon: BarChart3 },
  { href: "/vault", label: "Anahtarlar", Icon: Lock },
];

export function Shell({
  title,
  subtitle,
  actions,
  children,
}: {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const [live, setLive] = useState(false);
  const [email, setEmail] = useState("");

  useEffect(() => {
    if (!auth.token) {
      router.replace("/login");
      return;
    }
    setEmail(auth.email);
    setLive(true);
    return openEventStream(() => setLive(true));
  }, [router]);

  const moveSpotlight = (event: MouseEvent<HTMLDivElement>) => {
    const x = `${Math.round((event.clientX / window.innerWidth) * 100)}%`;
    const y = `${Math.round((event.clientY / window.innerHeight) * 100)}%`;
    document.documentElement.style.setProperty("--spot-x", x);
    document.documentElement.style.setProperty("--spot-y", y);
  };

  return (
    <div className="grid min-h-screen md:grid-cols-[266px_1fr]" onMouseMove={moveSpotlight}>
      <aside className="sticky top-0 hidden h-screen flex-col gap-1 border-r border-white/10 bg-[#08101a]/90 p-5 backdrop-blur-2xl md:flex">
        <div className="glass-line absolute inset-x-0 top-0 h-px" />
        <div className="mb-5 flex items-center gap-3 px-1">
          <div className="relative grid h-11 w-11 place-items-center overflow-hidden rounded-2xl border border-emerald/25 bg-gradient-to-br from-emerald/25 via-info/10 to-purple-500/20 shadow-glow">
            <img src="/logo.png" alt="" className="h-8 w-8 object-contain" />
            <span className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-white/70 to-transparent" />
          </div>
          <div>
            <div className="text-[15px] font-bold tracking-widest">ZUMVIA</div>
            <div className="text-[10px] uppercase tracking-widest text-slate-400">
              Quant Platform
            </div>
          </div>
        </div>

        {NAV.map(({ href, label, Icon }) => {
          const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              className={`flex items-center gap-3 rounded-lg border px-3 py-2.5 text-sm font-medium transition ${
                active
                  ? "translate-x-1 border-emerald/25 bg-gradient-to-r from-emerald/15 via-info/5 to-transparent text-emerald-mint shadow-[inset_3px_0_0_#00e676]"
                  : "border-transparent text-slate-400 hover:translate-x-1 hover:bg-white/5 hover:text-white"
              }`}
            >
              <Icon size={17} />
              {label}
            </Link>
          );
        })}

        <div className="mt-auto space-y-2 text-[11px] text-slate-400">
          <div className="flex items-center gap-2">
            <span
              className={`h-2 w-2 rounded-full ${
                live ? "animate-pulseDot bg-emerald" : "bg-slate-750"
              }`}
            />
            {live ? "Canlı bağlantı" : "Bağlanıyor…"}
          </div>
          <div className="truncate">{email}</div>
          <button
            className="btn w-full justify-center py-1.5 text-xs"
            onClick={() => {
              auth.clear();
              router.replace("/login");
            }}
          >
            <LogOut size={13} /> Çıkış
          </button>
        </div>
      </aside>

      <main className="w-full max-w-[1680px] px-4 pb-24 pt-5 md:px-8 md:pt-7">
        <header className="mb-6 flex animate-fadeUp flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="bg-gradient-to-r from-white via-[#dfffee] to-info bg-clip-text text-[25px] font-extrabold tracking-tight text-transparent">{title}</h1>
            {subtitle && <p className="text-[12.5px] text-slate-400">{subtitle}</p>}
          </div>
          <div className="flex flex-wrap gap-2">{actions}</div>
        </header>
        {children}
      </main>

      {/* Mobil alt gezinme */}
      <nav className="fixed inset-x-2 bottom-2 z-50 flex overflow-x-auto rounded-2xl border border-white/10 bg-[#08101a]/90 p-2 shadow-panel backdrop-blur-2xl md:hidden">
        {NAV.map(({ href, label, Icon }) => {
          const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              className={`flex min-w-[66px] flex-1 flex-col items-center gap-1 rounded-xl px-3 py-1.5 text-[10px] transition ${active ? "bg-emerald/10 text-emerald-mint" : "text-slate-400"}`}
            >
              <Icon size={17} />
              {label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}

export function StatCard({
  label,
  value,
  foot,
  tone = "",
}: {
  label: string;
  value: string;
  foot?: string;
  tone?: string;
}) {
  return (
    <div className="panel panel-lift group relative overflow-hidden">
      <span className="glass-line absolute inset-x-0 top-0 h-0.5 opacity-80" />
      <span className="absolute -right-7 -top-7 h-24 w-24 rounded-full bg-emerald/5 blur-2xl transition group-hover:bg-info/10" />
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone}`}>{value}</div>
      {foot && <div className="mt-1 text-[11.5px] text-slate-400">{foot}</div>}
    </div>
  );
}
