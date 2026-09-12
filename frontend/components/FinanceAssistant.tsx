"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Send, Square, X, ExternalLink } from "lucide-react";
import { api } from "@/lib/api";
import {
  agentApi, TOOL_TITLE, toolSummary,
  type AgentMessage, type AgentSession,
} from "@/lib/agent";

type Credential = { id: number; kind: string };

/**
 * Finans yan AI paneli — sayfadaki sayılarla konuşur.
 * Seçili enstrümanın anlık fiyatı her soruya iliştirilir; model sayı
 * uydurmaz, Python'un ölçtüğünü yorumlar. Salt-okunur "Sor" modunda açılır.
 */
export function FinanceAssistant({
  symbol, market, exchange, price, changePct, onClose,
}: {
  symbol: string; market: string; exchange: string;
  price: number | null; changePct: number | null;
  onClose: () => void;
}) {
  const router = useRouter();
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [status, setStatus] = useState<AgentSession["status"]>("idle");
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  const contextLine = `[Finans: ${symbol} @ ${
    price == null ? "ölçülemedi" : price
  }${changePct == null ? "" : ` (${changePct >= 0 ? "+" : ""}${changePct.toFixed(2)}%)`} · ${market}/${exchange}]`;

  async function ensureSession(): Promise<number> {
    if (sessionId) return sessionId;
    const keys = await api<Credential[]>("/api/keys").catch(() => []);
    const llm = (keys ?? []).find((k) => k.kind === "llm");
    const created = await agentApi.createSession({
      title: `Finans · ${symbol}`,
      control_tool: "api",
      control_credential_id: llm?.id ?? null,
      control_model: "",
      council_mode: "auto",
      work_mode: "ask",
      heartbeat_seconds: 900,
    });
    setSessionId(created.id);
    return created.id;
  }

  async function refresh(id: number) {
    try {
      const [msgs, sess] = await Promise.all([
        agentApi.messages(id),
        agentApi.get(id),
      ]);
      setMessages(msgs);
      setStatus(sess.status);
      return sess.status;
    } catch {
      return status;
    }
  }

  async function ask(text?: string) {
    const content = (text ?? input).trim();
    if (!content || busy) return;
    setBusy(true);
    try {
      const id = await ensureSession();
      setMessages((prev) => [...prev, { role: "user", content, ts: new Date().toISOString() }]);
      setInput("");
      setStatus("thinking");
      await agentApi.send(id, `${contextLine} ${content}`);
      await refresh(id);
    } catch (err) {
      setStatus("idle");
      setMessages((prev) => [...prev, {
        role: "system",
        content: err instanceof Error ? err.message : "Soru gönderilemedi.",
      }]);
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    if (!sessionId) return;
    await agentApi.stop(sessionId).catch(() => {});
    refresh(sessionId);
  }

  async function openInCommand() {
    const id = await ensureSession();
    router.push(`/command?session=${id}`);
  }

  useEffect(() => {
    if (!sessionId || (status !== "thinking" && status !== "running")) return;
    const timer = setInterval(() => { refresh(sessionId); }, 2500);
    return () => clearInterval(timer);
  }, [sessionId, status]);

  useEffect(() => {
    boxRef.current?.scrollTo({ top: boxRef.current.scrollHeight });
  }, [messages, status]);

  return (
    <div className="fixed inset-y-0 right-0 z-50 flex w-full max-w-md flex-col border-l border-white/10 bg-[#0a121c]">
      <div className="flex items-center gap-2 border-b border-white/10 px-4 py-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold">Finans Asistanı</div>
          <div className="truncate font-mono text-[11px] text-slate-400">{symbol}</div>
        </div>
        <span className="text-[11px] text-slate-400">
          {status === "idle" ? "hazır" : status === "thinking" ? "düşünüyor…" : "çalışıyor…"}
        </span>
        {(status === "thinking" || status === "running") && (
          <button className="btn px-2 py-1 text-[11px]" onClick={stop} title="Durdur" aria-label="Asistanı durdur">
            <Square size={12} />
          </button>
        )}
        <button className="btn px-2 py-1 text-[11px]" onClick={openInCommand} title="Komuta'da görev olarak aç" aria-label="Komuta ekranında aç">
          <ExternalLink size={12} />
        </button>
        <button className="btn px-2 py-1 text-[11px]" onClick={onClose} title="Kapat" aria-label="Finans asistanını kapat">
          <X size={13} />
        </button>
      </div>

      <div ref={boxRef} className="flex-1 space-y-2 overflow-y-auto px-4 py-3">
        {messages.length === 0 && (
          <div className="text-[12.5px] text-slate-400">
            Bu enstrümanın canlı sayılarıyla soru sor — fiyat, değişim ve piyasa
            otomatik iliştirilir.
            <div className="mt-3 flex flex-wrap gap-2">
              {["Bunu analiz et", "Uygun sistem botu var mı?", "Riskleri say"].map((p) => (
                <button key={p} onClick={() => ask(p)}
                  className="rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5 text-[12px] hover:border-emerald/40 hover:text-emerald-mint">
                  {p}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={m.id ?? i}>
            {m.role === "user" && (
              <div className="mb-2 ml-auto w-fit max-w-[90%] whitespace-pre-wrap rounded-2xl bg-white/[0.07] px-3.5 py-2 text-[13px]">
                {m.content}
              </div>
            )}
            {m.role === "system" && (
              <div className="mb-2 text-center text-[12px] text-slate-400">{m.content}</div>
            )}
            {m.role === "tool" && (
              <div className="mb-1.5 flex items-center gap-2 text-[12px] text-slate-400">
                <span className={`h-1.5 w-1.5 rounded-full ${!m.ok || m.tool_result?.error ? "bg-danger" : "bg-info"}`} />
                <b className="font-medium text-white">{TOOL_TITLE[m.tool_name ?? ""] ?? m.tool_name}</b>
                <span className="flex-1 truncate">{toolSummary(m)}</span>
              </div>
            )}
            {m.role === "assistant" && (
              <div className="mb-2 whitespace-pre-wrap text-[13px] leading-relaxed">{m.content}</div>
            )}
          </div>
        ))}
      </div>

      <div className="border-t border-white/10 p-3">
        <div className="flex gap-2">
          <input className="input flex-1" value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Sor…"
            onKeyDown={(e) => { if (e.key === "Enter") ask(); }} />
          <button className="btn btn-primary px-3" onClick={() => ask()} disabled={busy || !input.trim()} aria-label="Soruyu gönder">
            <Send size={15} />
          </button>
        </div>
        <div className="mt-1.5 text-[10.5px] text-slate-500">
          Salt-okunur mod — işlem açmaz, yalnızca yorumlar. Görev için sağ üstteki dışa-aktar düğmesi.
        </div>
      </div>
    </div>
  );
}
