"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { Play, RotateCw, Send, Trash2 } from "lucide-react";
import { BrandMark } from "@/components/BrandMark";
import { Shell } from "@/components/Shell";
import { api, fetcher, openEventStream } from "@/lib/api";
import {
  agentApi, AgentCatalog, AgentMessage, AgentSession, CouncilMode, MODE_HELP,
  MODE_LABEL, MUTATING_TOOLS, TOOL_TITLE, toolSummary, WORK_MODE_HELP, WORK_MODE_LABEL, WorkMode,
} from "@/lib/agent";

type Credential = { id: number; kind: string; provider: string; label: string; hint: string };

export default function CommandPage() {
  const { data: catalog } = useSWR<AgentCatalog>("agent-catalog", agentApi.catalog);
  const capabilities = catalog?.capabilities;
  const { data: credentials } = useSWR<Credential[]>("/api/keys", fetcher);
  const { data: sessions, mutate: mutateSessions } =
    useSWR<AgentSession[]>("agent-sessions", agentApi.sessions);

  const [sessionId, setSessionId] = useState<number | null>(null);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [status, setStatus] = useState<AgentSession["status"]>("idle");
  const [input, setInput] = useState("");
  const [mode, setMode] = useState<CouncilMode>("auto");
  const [workMode, setWorkMode] = useState<WorkMode>("ask");
  const streamRef = useRef<HTMLDivElement>(null);

  const llmKeys = (credentials ?? []).filter((c) => c.kind === "llm");
  const session = sessions?.find((s) => s.id === sessionId) ?? null;

  /* --- ilk oturumu seç (veya ?session= ile gelen görevi aç) --- */
  useEffect(() => {
    const q = new URLSearchParams(window.location.search).get("session");
    const wanted = Number(q);
    if (q && wanted) {
      setSessionId(wanted);
      return;
    }
    if (!sessionId && sessions?.length) setSessionId(sessions[0].id);
  }, [sessions, sessionId]);

  /* --- mesajları yükle --- */
  useEffect(() => {
    if (!sessionId) return;
    agentApi.messages(sessionId).then(setMessages).catch(() => setMessages([]));
    setMode(session?.council_mode ?? "auto");
    setWorkMode(session?.work_mode ?? "ask");
    setStatus(session?.status ?? "idle");
  }, [sessionId, session?.council_mode, session?.status, session?.work_mode]);

  /* --- canlı akış --- */
  useEffect(() => {
    return openEventStream((event) => {
      if (event.type === "agent" && event.session_id === sessionId) {
        if (event.message?.role === "user") return;
        if (event.message) setMessages((prev) => [...prev, event.message]);
      }
      if (event.type === "agent_status" && event.session_id === sessionId) {
        setStatus(event.status);
      }
    });
  }, [sessionId]);

  useEffect(() => {
    streamRef.current?.scrollTo({ top: streamRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, status]);

  const createSession = useCallback(async () => {
    const cred = llmKeys[0];
    const created = await agentApi.createSession({
      title: "Yeni Görev",
      control_tool: "api",
      control_credential_id: cred?.id ?? null,
      control_model: "",
      sub_credential_id: cred?.id ?? null,
      council_mode: mode,
      work_mode: workMode,
      heartbeat_seconds: 900,
    });
    await mutateSessions();
    setSessionId(created.id);
    return created;
  }, [llmKeys, mode, workMode, mutateSessions]);

  async function send(text?: string) {
    const content = (text ?? input).trim();
    if (!content) return;
    if (!llmKeys.length) {
      alert("Önce Kasa sayfasından bir yapay zeka anahtarı ekleyin.");
      return;
    }
    let id = sessionId;
    if (!id) id = (await createSession()).id;

    setMessages((prev) => [...prev, { role: "user", content, ts: new Date().toISOString() }]);
    setInput("");
    setStatus("thinking");
    try {
      await agentApi.send(id, content);
    } catch (err) {
      setStatus("idle");
      alert(err instanceof Error ? err.message : "Hata");
    }
  }

  async function patch(body: Record<string, unknown>) {
    if (!sessionId) return;
    await agentApi.patchSession(sessionId, body);
    mutateSessions();
  }

  return (
    <Shell
      title="Komuta"
      subtitle="Ne istediğinizi yazın — gerisini ajan halleder"
      actions={
        <>
          <button className="btn px-3 py-1.5 text-xs" onClick={createSession}>
            <Play size={13} /> Yeni Görev
          </button>
          {sessionId && (
            <>
              <button className="btn px-3 py-1.5 text-xs"
                onClick={() => agentApi.tick(sessionId)}>
                <RotateCw size={13} /> Denetim turu
              </button>
              <button className="btn btn-danger px-2.5 py-1.5 text-xs"
                onClick={async () => {
                  if (!confirm("Görev ve tüm konuşma silinecek.")) return;
                  await agentApi.deleteSession(sessionId);
                  setSessionId(null);
                  setMessages([]);
                  mutateSessions();
                }}>
                <Trash2 size={13} />
              </button>
            </>
          )}
        </>
      }
    >
      <div className="flex h-[calc(100vh-160px)] min-h-[520px] flex-col">
        {/* --------------------------- akış --------------------------- */}
        <div ref={streamRef} className="flex-1 overflow-y-auto pr-1">
          <div className="mx-auto max-w-3xl">
            {messages.length === 0 && (
              <div className="mx-auto mt-14 max-w-2xl text-center">
                <div className="mx-auto mb-5 w-fit">
                  <BrandMark size={60} />
                </div>
                <h2 className="text-[28px] font-semibold tracking-tight">
                  Paranızı yönetmeye hazırım
                </h2>
                <p className="mt-2.5 text-slate-400">
                  Ne yapmak istediğinizi tek cümleyle yazın; gerisini ben hallederim.
                </p>

                <div className="mt-9 grid grid-cols-2 gap-4 border-t border-white/5 pt-7 sm:grid-cols-4">
                  {[
                    [String(capabilities?.strategies ?? 12), "hazır strateji"],
                    [String(capabilities?.playbooks ?? 9), "sistem botu"],
                    [String(capabilities?.tools ?? 30), "ajan aracı"],
                    [`%${capabilities?.max_risk_pct ?? 1.5}`, "işlem başına risk tavanı"],
                  ].map(([value, label]) => (
                    <div key={label}>
                      <div className="text-[22px] font-bold leading-tight tracking-tight text-emerald-mint">
                        {value}
                      </div>
                      <div className="mt-1 text-[11.5px] leading-snug text-slate-500">
                        {label}
                      </div>
                    </div>
                  ))}
                </div>

                {!llmKeys.length && (
                  <div className="mt-6 rounded-lg border border-emerald/25 bg-emerald/5 px-4 py-3 text-left text-[12.5px] leading-relaxed text-slate-300">
                    <b className="text-white">Başlamak için tek adım kaldı:</b> bir yapay
                    zeka anahtarı ekleyin. Google Gemini ücretsiz kotasıyla maliyetsiz
                    başlayabilirsiniz.
                  </div>
                )}
              </div>
            )}

            {messages.map((message, index) => (
              <MessageRow key={message.id ?? `live-${index}`} message={message} />
            ))}

            {(status === "thinking" || status === "running") && (
              <div className="mb-4 ml-10 flex items-center gap-2 text-[13px] text-emerald-mint">
                <span className="flex h-3 items-end gap-0.5">
                  {[0, 1, 2].map((i) => (
                    <i key={i}
                      className="h-3 w-[3px] animate-pulse rounded-sm bg-emerald"
                      style={{ animationDelay: `${i * 0.15}s` }} />
                  ))}
                </span>
                {status === "thinking" ? "düşünüyor…" : "araç çalıştırıyor…"}
              </div>
            )}
          </div>
        </div>

        {/* ------------------------- hazır görevler ------------------------- */}
        <div className="mx-auto mb-2 flex max-w-3xl flex-wrap gap-2">
          {catalog?.quick_missions.map((m) => (
            <button key={m.id} onClick={() => send(m.prompt)}
              className="rounded-full border border-white/10 bg-raise px-3 py-1.5 text-[12.5px] text-slate-400 transition hover:border-emerald/40 hover:text-emerald-mint">
              {m.label}
            </button>
          ))}
        </div>

        {/* --------------------------- besteci --------------------------- */}
        <div className="mx-auto w-full max-w-3xl rounded-2xl border border-white/15 bg-raise focus-within:border-emerald/40">
          <textarea
            className="max-h-52 w-full resize-none bg-transparent px-4 pt-3.5 text-[14.5px] outline-none"
            rows={1}
            value={input}
            placeholder="Ne yapmamı istersiniz? Örn: Kriptoda 1000 dolarımı yönet, gerisini sen hallet."
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
          />
          <div className="flex flex-wrap items-center gap-2 px-3 pb-2.5 text-[12px] text-slate-400">
            <select
              className="cursor-pointer rounded bg-transparent px-1 py-0.5 hover:bg-white/5"
              value={workMode}
              title={WORK_MODE_HELP[workMode]}
              onChange={(e) => {
                const value = e.target.value as WorkMode;
                setWorkMode(value);
                patch({ work_mode: value });
              }}
            >
              {(Object.keys(WORK_MODE_LABEL) as WorkMode[]).map((key) => (
                <option key={key} value={key}>{WORK_MODE_LABEL[key]}</option>
              ))}
            </select>
            <select
              className="cursor-pointer rounded bg-transparent px-1 py-0.5 hover:bg-white/5"
              value={mode}
              title={MODE_HELP[mode]}
              onChange={(e) => {
                const value = e.target.value as CouncilMode;
                setMode(value);
                patch({ council_mode: value });
              }}
            >
              {(Object.keys(MODE_LABEL) as CouncilMode[]).map((key) => (
                <option key={key} value={key}>{MODE_LABEL[key]}</option>
              ))}
            </select>

            {session && (
              <span
                className={`rounded-full border px-2.5 py-0.5 text-[11.5px] ${
                  session.autonomous
                    ? "border-emerald/30 bg-emerald/10 text-emerald-mint"
                    : "border-white/15"
                }`}
                title={session.autonomous ? "Uygula modunda — 7/24 denetler" : "Sor/Planla modunda — yalnızca istek üzerine çalışır"}
              >
                {session.autonomous ? "Otonom · 7/24" : "Elle çalışır"}
              </span>
            )}

            <span className="ml-auto">{status === "idle" ? "hazır" : status}</span>
            <button
              className="grid h-8 w-8 place-items-center rounded-lg bg-emerald text-[#06170d] disabled:opacity-40"
              onClick={() => send()}
              disabled={!input.trim()}
            >
              <Send size={15} />
            </button>
          </div>
        </div>

        <p className="mx-auto mt-2 max-w-3xl text-[11.5px] text-slate-400">
          Risk kalkanı ajanı da bağlar: işlem başına en fazla {`%${capabilities?.max_risk_pct ?? 1}`} risk, stop-loss
          zorunlu, portföy ısısı tavanlı. <b>Gerçek para yetkisini yalnızca siz verirsiniz.</b>
        </p>
      </div>
    </Shell>
  );
}

/* --------------------------------- satırlar -------------------------------- */

function MessageRow({ message }: { message: AgentMessage }) {
  const [open, setOpen] = useState(false);

  if (message.role === "user") {
    return (
      <div className="mb-4 ml-auto w-fit max-w-[85%] whitespace-pre-wrap rounded-2xl bg-raise px-4 py-2.5">
        {message.content}
      </div>
    );
  }

  if (message.role === "system") {
    return <div className="mb-3 text-center text-[12px] text-slate-400">{message.content}</div>;
  }

  if (message.role === "tool") {
    const failed = !message.ok || Boolean(message.tool_result?.error);
    const mutating = MUTATING_TOOLS.has(message.tool_name ?? "");
    return (
      <div className="mb-2 ml-10 max-w-[720px]">
        <button
          onClick={() => setOpen((v) => !v)}
          className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[12.5px] text-slate-400 hover:bg-white/5"
        >
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${
            failed ? "bg-danger" : mutating ? "bg-warn" : "bg-info"}`} />
          <span className="font-medium text-white">
            {TOOL_TITLE[message.tool_name ?? ""] ?? message.tool_name}
          </span>
          <span className="flex-1 truncate">{toolSummary(message)}</span>
          {message.duration_ms ? (
            <span className="text-[11px] text-slate-500">{message.duration_ms}ms</span>
          ) : null}
          <span className="text-[10px]">{open ? "" : ""}</span>
        </button>
        {open && (
          <pre className="ml-4 mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-r-lg border-l-2 border-white/15 bg-raise p-3 font-mono text-[11.5px] text-slate-400">
            {JSON.stringify({ girdi: message.tool_args, sonuç: message.tool_result }, null, 2)}
          </pre>
        )}
      </div>
    );
  }

  return (
    <div className="mb-4 flex gap-3">
      <div className="mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-lg bg-gradient-to-br from-emerald to-[#00b268] text-[12px] font-extrabold text-[#06170d]">
        V
      </div>
      <div className="min-w-0 flex-1 whitespace-pre-wrap leading-relaxed">
        {message.content}
        {message.ts && (
          <div className="mt-1.5 text-[10.5px] text-slate-500">
            {new Date(message.ts).toLocaleTimeString("tr-TR", { hour12: false })}
          </div>
        )}
      </div>
    </div>
  );
}
