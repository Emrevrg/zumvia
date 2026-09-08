"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
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

  useEffect(() => {
    if (!auth.token) {
      router.replace("/login");
      return;
    }
    setLive(true);
    return openEventStream(() => setLive(true));
  }, [router]);

  return (
    <div className="grid min-h-screen md:grid-cols-[250px_1fr]">
      <aside className="sticky top-0 hidden h-screen flex-col gap-1 border-r border-white/10 bg-panel/95 p-5 backdrop-blur-xl md:flex">
        <div className="mb-5 flex items-center gap-3 px-1">
          <div className="grid h-9 w-9 place-items-center rounded-xl bg-gradient-to-br from-emerald to-[#00b894] text-lg font-extrabold text-[#04120a] shadow-glow">
            V
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
                  ? "border-emerald/25 bg-gradient-to-r from-emerald/15 to-transparent text-emerald-mint"
                  : "border-transparent text-slate-400 hover:bg-white/5 hover:text-white"
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
          <div className="truncate">{auth.email}</div>
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

      <main className="w-full max-w-[1680px] px-4 pb-24 pt-5 md:px-7">
        <header className="mb-5 flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-[22px] font-bold tracking-tight">{title}</h1>
            {subtitle && <p className="text-[12.5px] text-slate-400">{subtitle}</p>}
          </div>
          <div className="flex flex-wrap gap-2">{actions}</div>
        </header>
        {children}
      </main>

      {/* Mobil alt gezinme */}
      <nav className="fixed inset-x-0 bottom-0 z-50 flex overflow-x-auto border-t border-white/10 bg-panel/95 p-2 backdrop-blur-xl md:hidden">
        {NAV.map(({ href, label, Icon }) => (
          <Link
            key={href}
            href={href}
            className="flex flex-1 flex-col items-center gap-1 px-3 py-1 text-[10px] text-slate-400"
          >
            <Icon size={17} />
            {label}
          </Link>
        ))}
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
    <div className="panel relative overflow-hidden">
      <span className="absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r from-emerald to-transparent opacity-70" />
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone}`}>{value}</div>
      {foot && <div className="mt-1 text-[11.5px] text-slate-400">{foot}</div>}
    </div>
  );
}
