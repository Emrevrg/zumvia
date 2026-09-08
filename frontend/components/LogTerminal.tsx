"use client";

import { useEffect, useRef, useState } from "react";
import { openEventStream, type BotEvent } from "@/lib/api";

const LEVEL_STYLE: Record<string, string> = {
  info: "border-l-slate-400/40 text-slate-300",
  success: "border-l-emerald text-[#b7f5cf]",
  warn: "border-l-warn text-[#ffdca6]",
  error: "border-l-danger text-[#ffc0cc]",
  trade: "border-l-emerald-mint text-emerald-mint font-semibold",
};

type LiveEvent = BotEvent & { bot_id?: number; bot_name?: string; type?: string };

/**
 * Şeffaf işlem logu terminali.
 * `botId` verilirse yalnızca o botun olayları gösterilir.
 */
export function LogTerminal({
  initial = [],
  botId,
}: {
  initial?: LiveEvent[];
  botId?: number;
}) {
  const [events, setEvents] = useState<LiveEvent[]>(initial);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => setEvents(initial), [initial]);

  useEffect(() => {
    return openEventStream((event) => {
      if (event.type !== "event") return;
      if (botId && event.bot_id !== botId) return;
      setEvents((prev) => [...prev.slice(-350), event as LiveEvent]);
    });
  }, [botId]);

  useEffect(() => {
    boxRef.current?.scrollTo({ top: boxRef.current.scrollHeight, behavior: "smooth" });
  }, [events]);

  return (
    <div className="terminal" ref={boxRef}>
      {events.length === 0 && (
        <div className="text-slate-400">
          Bot çalıştığında kararlar burada canlı akacak…
        </div>
      )}
      {events.map((event, i) => (
        <div
          key={`${event.id ?? "live"}-${i}`}
          className={`flex gap-2.5 border-l-2 py-px pl-2 hover:bg-white/[.02] ${
            LEVEL_STYLE[event.level] ?? LEVEL_STYLE.info
          }`}
        >
          <span className="shrink-0 text-[#48606f]">
            {event.ts ? new Date(event.ts).toLocaleTimeString("tr-TR", { hour12: false }) : "--:--:--"}
          </span>
          <span className="w-14 shrink-0 text-[10px] uppercase opacity-80">
            {event.category}
          </span>
          <span className="flex-1 break-words">
            {!botId && event.bot_name ? `[${event.bot_name}] ` : ""}
            {event.message}
          </span>
        </div>
      ))}
    </div>
  );
}
