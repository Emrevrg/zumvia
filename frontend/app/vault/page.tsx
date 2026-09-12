"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { Search, Trash2, Zap, Shield, Server } from "lucide-react";
import { Shell } from "@/components/Shell";
import { api, fetcher } from "@/lib/api";

type Provider = {
  id: string; label: string; base_url: string; models: string[];
  needs_key: boolean; note: string; style?: string;
};
type Credential = {
  id: number; kind: string; provider: string; label: string; hint: string;
};
type Meta = { exchanges: string[] };
type Health = Record<string, { healthy: boolean; fails: number; ok: number; last_latency: number; cooldown_secs_left: number }>;

export default function VaultPage() {
  const { data: provData } = useSWR<{ llm: Provider[]; health: Health }>("/api/keys/providers", fetcher);
  const providers = provData?.llm ?? [];
  const health = provData?.health ?? {};
  const { data: credentials, mutate } = useSWR<Credential[]>("/api/keys", fetcher);
  const { data: meta } = useSWR<Meta>("/api/market/meta", fetcher);

  const [tab, setTab] = useState<"llm" | "exchange" | "telegram">("llm");
  const [q, setQ] = useState("");
  const [message, setMessage] = useState("");
  const [messageOk, setMessageOk] = useState<boolean | null>(null);
  const [testingId, setTestingId] = useState<number | null>(null);
  const [testResult, setTestResult] = useState<Record<number, { ok: boolean; msg: string }>>({});
  const [form, setForm] = useState<Record<string, string | boolean>>({
    provider: "gemini", label: "", api_key: "", model: "", base_url: "",
    secret: "", password: "", token: "", chat_id: "", sandbox: true,
  });

  const setF = (patch: Record<string, string | boolean>) => setForm((f) => ({ ...f, ...patch }));
  const provider = providers.find((p) => p.id === form.provider);

  const filtered = useMemo(() => {
    if (!q.trim()) return providers;
    const s = q.toLowerCase();
    return providers.filter(p => p.label.toLowerCase().includes(s) || p.id.toLowerCase().includes(s) || (p.models ?? []).some(m => m.toLowerCase().includes(s)));
  }, [providers, q]);

  async function save() {
    try {
      // validation: custom needs base_url, others need model if strict
      if (form.provider === "custom" && !String(form.base_url).trim()) {
        setMessage("Özel sağlayıcı için Base URL zorunludur (örn. https://api.example.com/v1)."); setMessageOk(false); return;
      }
      await api("/api/keys", {
        method: "POST",
        body: {
          kind: tab,
          provider: tab === "telegram" ? "telegram" : String(form.provider),
          label: String(form.label || form.provider),
          api_key: String(form.api_key || ""),
          secret: String(form.secret || ""),
          password: String(form.password || ""),
          token: String(form.token || ""),
          chat_id: String(form.chat_id || ""),
          base_url: String(form.base_url || ""),
          model: String(form.model || ""),
          sandbox: Boolean(form.sandbox),
        },
      });
      setMessage("Anahtar AES-256 ile şifrelenerek kaydedildi."); setMessageOk(true);
      mutate();
      setF({ api_key: "", secret: "", password: "", token: "", chat_id: "" });
    } catch (err) {
      setMessage(`${err instanceof Error ? err.message : "Hata"}`); setMessageOk(false);
    }
  }

  async function test(id: number) {
    setTestingId(id); setMessage("Test ediliyor…"); setMessageOk(null);
    try {
      const r = await api<{ ok: boolean; message: string; data?: { latency_ms: number } }>("/api/keys/test", {
        method: "POST", body: { credential_id: id },
      });
      const msg = r.message + (r.data?.latency_ms ? ` · ${r.data.latency_ms} ms` : "");
      setTestResult(s => ({ ...s, [id]: { ok: r.ok, msg } }));
      setMessage(msg); setMessageOk(r.ok);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Hata";
      setTestResult(s => ({ ...s, [id]: { ok: false, msg } }));
      setMessage(msg); setMessageOk(false);
    } finally { setTestingId(null); }
  }

  async function remove(id: number) {
    if (!confirm("Anahtar silinecek. Emin misiniz?")) return;
    await api(`/api/keys/${id}`, { method: "DELETE" });
    mutate();
  }

  const healthyCount = Object.values(health).filter(h => h.healthy).length;

  return (
    <Shell title="Anahtar Kasası" subtitle={`${providers.length} sağlayıcı · ${credentials?.length ?? 0} kayıtlı anahtar · ${healthyCount} sağlıklı`}>
      <div className="mb-4 flex flex-wrap gap-2">
        <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald/25 bg-emerald/10 px-3 py-1 text-[11px] text-emerald"><Shield size={12}/>AES-256 şifreli</span>
        <span className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/5 px-3 py-1 text-[11px] text-slate-300"><Zap size={12}/>Tek tıkla bağlantı testi</span>
        <span className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/5 px-3 py-1 text-[11px] text-slate-300"><Server size={12}/>Yerel Ollama/vLLM · $0</span>
      </div>

      <div className="mb-4 rounded-r-lg border-l-2 border-emerald bg-emerald/10 px-4 py-3 text-[12.5px] leading-relaxed text-[#b7f5cf]">
        <b>{providers.length} sağlayıcı</b> tek çatı altında. Moonshot Kimi K3, Gemini, DeepSeek, Claude, OpenAI, Groq, Mistral, xAI, Perplexity, Cohere, Together, Fireworks, Zhipu, ve herhangi bir <code className="rounded bg-black/30 px-1">OpenAI-uyumlu</code> uç nokta. Dayanıklılık katmanı: 3 ardışık hata → sağlayıcı 5 dk soğumaya alınır, konsey kalanlarla devam eder.
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.05fr_0.95fr]">
        {/* SOL: ekleme formu */}
        <div className="panel">
          <div className="mb-4 flex gap-1 rounded-lg bg-[#0e1622] p-1">
            {( [["llm", "Yapay zeka"], ["exchange", "Borsa"], ["telegram", "Telegram"]] as const)
              .map(([id, label]) => (
                <button key={id} onClick={() => setTab(id)}
                  className={`flex-1 rounded-md py-2 text-[13px] font-semibold transition ${
                    tab === id ? "bg-emerald/15 text-emerald-mint border border-emerald/20" : "text-slate-400"
                  }`}>
                  {label}
                </button>
              ))}
          </div>

          <div className="space-y-3">
            {tab === "llm" && (
              <>
                <div className="relative">
                  <Search size={14} className="pointer-events-none absolute left-3 top-3 text-slate-500"/>
                  <input className="input pl-9" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Sağlayıcı veya model ara (örn. kimi, grok, qwen)…" />
                  {q && <button className="absolute right-2 top-1.5 rounded px-2 py-1 text-[11px] text-slate-400 hover:text-white" onClick={() => setQ("")}>Temizle</button>}
                </div>

                <div className="space-y-1.5">
                  <label className="label">Sağlayıcı — {filtered.length}/{providers.length}</label>
                  <select className="input" value={String(form.provider)}
                    onChange={(e) => setF({ provider: e.target.value, base_url: "", model: "" })}>
                    {filtered.map((p) => {
                      const h = health[p.id];
                      const badge = h ? (h.healthy ? " ●" : ` ● ${h.cooldown_secs_left}s soğuma`) : "";
                      return <option key={p.id} value={p.id}>{p.label}{badge} — {(p.models ?? [])[0] ?? ""}</option>;
                    })}
                  </select>
                  {provider?.note && <p className="text-[11px] text-slate-400">{provider.note}</p>}
                  {health[provider?.id ?? ""] && !health[provider!.id].healthy && (
                    <p className="rounded border border-amber-500/20 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-300">Bu sağlayıcı geçici soğumada — {health[provider!.id].cooldown_secs_left}s kaldı. Kayıt yapabilirsiniz, çağrılar otomatik kuyrukta.</p>
                  )}
                  <p className="text-[11px] text-slate-500">Varsayılan URL: <code className="font-mono text-[11px]">{provider?.base_url || "— özel girin —"}</code></p>
                </div>

                {provider?.needs_key !== false && (
                  <div className="space-y-1.5">
                    <label className="label">API Anahtarı</label>
                    <input className="input font-mono" type="password" value={String(form.api_key)}
                      onChange={(e) => setF({ api_key: e.target.value })} placeholder="sk-… / AIza… / kimi_…" />
                    <p className="text-[10.5px] text-slate-500">Anahtar AES-256 ile şifrelenir, loga yazılmaz.</p>
                  </div>
                )}

                <div className="space-y-1.5">
                  <label className="label">Model</label>
                  <div className="flex gap-2">
                    <select className="input flex-1" value={String(form.model)}
                      onChange={(e) => setF({ model: e.target.value })}>
                      <option value="">— seçin veya elle yazın —</option>
                      {(provider?.models ?? []).map((m) => <option key={m} value={m}>{m}</option>)}
                    </select>
                  </div>
                  <input className="input font-mono text-[12px]" value={String(form.model)}
                    onChange={(e) => setF({ model: e.target.value })} placeholder="Elle model adı (örn. kimi-k3-preview)" />
                </div>

                <div className="space-y-1.5">
                  <label className="label">Base URL {form.provider === "custom" ? "(zorunlu)" : "(opsiyonel — varsayılanı ezer)"}</label>
                  <input className="input font-mono" value={String(form.base_url) || provider?.base_url || ""}
                    onChange={(e) => setF({ base_url: e.target.value })}
                    placeholder={provider?.base_url || "https://api.example.com/v1"} />
                  {form.provider !== "custom" && <p className="text-[11px] text-slate-500">Boş bırakırsanız sağlayıcının varsayılan URL’i kullanılır.</p>}
                </div>
              </>
            )}

            {tab === "exchange" && (
              <>
                <div className="space-y-1.5">
                  <label className="label">Borsa</label>
                  <select className="input" value={String(form.provider)}
                    onChange={(e) => setF({ provider: e.target.value })}>
                    {(meta?.exchanges ?? ["binance"]).map((x) => <option key={x}>{x}</option>)}
                  </select>
                </div>
                <div className="space-y-1.5">
                  <label className="label">API Key</label>
                  <input className="input font-mono" type="password" value={String(form.api_key)}
                    onChange={(e) => setF({ api_key: e.target.value })} />
                </div>
                <div className="space-y-1.5">
                  <label className="label">Secret</label>
                  <input className="input font-mono" type="password" value={String(form.secret)}
                    onChange={(e) => setF({ secret: e.target.value })} />
                </div>
                <div className="space-y-1.5">
                  <label className="label">Passphrase (OKX / KuCoin)</label>
                  <input className="input font-mono" type="password" value={String(form.password)}
                    onChange={(e) => setF({ password: e.target.value })} />
                </div>
                <label className="flex items-center gap-2 text-[12.5px]">
                  <input type="checkbox" checked={Boolean(form.sandbox)}
                    onChange={(e) => setF({ sandbox: e.target.checked })} />
                  Testnet (önerilir)
                </label>
                <div className="rounded-r-lg border-l-2 border-danger bg-danger/10 px-3 py-2 text-[12px] text-[#ffc0cc]">
                  Borsa anahtarını oluştururken <b>PARA ÇEKME yetkisini KAPALI</b> bırakın.
                </div>
              </>
            )}

            {tab === "telegram" && (
              <>
                <div className="space-y-1.5">
                  <label className="label">Bot Token</label>
                  <input className="input font-mono" type="password" value={String(form.token)}
                    onChange={(e) => setF({ token: e.target.value })} placeholder="123456:ABC-DEF…" />
                </div>
                <div className="space-y-1.5">
                  <label className="label">Chat ID</label>
                  <input className="input font-mono" value={String(form.chat_id)}
                    onChange={(e) => setF({ chat_id: e.target.value })} placeholder="123456789" />
                </div>
                <p className="text-[11px] text-slate-400"> @BotFather ile bot oluşturun, @userinfobot ile chat id&apos;nizi öğrenin.</p>
              </>
            )}

            <div className="space-y-1.5">
              <label className="label">Etiket</label>
              <input className="input" value={String(form.label)}
                onChange={(e) => setF({ label: e.target.value })} placeholder="örn. Kimi K3 Konsey, Gemini Ücretsiz" />
            </div>

            <button className="btn btn-primary w-full" onClick={save}>Şifrele ve Kaydet</button>
            {message && <p className={`rounded px-3 py-2 text-center text-[12.5px] ${messageOk ? "bg-emerald/10 text-emerald border border-emerald/20" : messageOk === false ? "bg-danger/10 text-danger border border-danger/20" : "text-slate-400"}`}>{message}</p>}
          </div>
        </div>

        {/* SAĞ: kayıtlı anahtarlar + test */}
        <div className="panel">
          <h2 className="mb-3 text-sm font-semibold">
            Kayıtlı Anahtarlar
            <span className="ml-2 text-[11.5px] font-normal text-slate-400">
              {credentials?.length ?? 0} kayıt · tıkla test et
            </span>
          </h2>

          {!credentials?.length ? (
            <div className="rounded-lg border border-dashed border-white/10 bg-white/[0.03] py-10 text-center text-[12.5px] text-slate-400">
              Henüz anahtar eklenmedi.<br/>Yapay zeka olmadan da <b>Sadece Algoritma</b> modunda çalışabilirsiniz.<br/>
              <span className="text-[11px]">Öneri: Gemini (ücretsiz) + DeepSeek + Kimi ekleyip konsey kurun.</span>
            </div>
          ) : (
            credentials.map((cred) => {
              const res = testResult[cred.id];
              return (
              <div key={cred.id}
                className="mb-2.5 rounded-lg border border-white/10 bg-[#0e1622] p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="rounded bg-white/10 px-1.5 py-0.5 text-[11px] font-bold">{{ llm: "AI", exchange: "BR", telegram: "TG" }[cred.kind] ?? "K"}</span>
                  <b className="text-[13px]">{cred.label}</b>
                  <span className="badge badge-slate">{cred.provider}</span>
                  <span className="font-mono text-[11px] text-slate-400">{cred.hint}</span>
                  {cred.kind === "llm" && health[cred.provider] && (
                    <span className={`ml-auto inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] ${health[cred.provider].healthy ? "bg-emerald/15 text-emerald border border-emerald/20" : "bg-amber-500/15 text-amber-300 border border-amber-500/20"}`}>
                      <span className={`h-1.5 w-1.5 rounded-full ${health[cred.provider].healthy ? "bg-emerald" : "bg-amber-400"}`}/> {health[cred.provider].healthy ? "sağlıklı" : `${health[cred.provider].cooldown_secs_left}s soğuma`}
                    </span>
                  )}
                </div>
                {res && (
                  <div className={`mt-2 rounded px-2 py-1 text-[11.5px] ${res.ok ? "bg-emerald/10 text-emerald border border-emerald/20" : "bg-danger/10 text-danger border border-danger/20"}`}>{res.msg}</div>
                )}
                <div className="mt-2 flex gap-2">
                  <button className="btn flex-1 py-1.5 text-[11px]" onClick={() => test(cred.id)} disabled={testingId === cred.id}>
                    {testingId === cred.id ? "Test ediliyor…" : "Bağlantıyı Test Et"}
                  </button>
                  <button className="btn px-3 py-1.5 text-[11px]" onClick={() => remove(cred.id)}>
                    <Trash2 size={12} />
                  </button>
                </div>
              </div>
            );})
          )}

          <div className="mt-4 rounded-lg border border-white/5 bg-white/[0.02] p-3 text-[11.5px] leading-relaxed text-slate-400">
            <b className="text-slate-200">Dayanıklılık:</b> Sağlayıcı hata verirse konsey kalan modellerle karar verir; 3 ardışık hata alan sağlayıcı 5 dk pasif kalır. Hiç model yoksa sistem <code>Sadece Algoritma</code> moduna düşer — maliyet $0, kesinti yok.
          </div>
        </div>
      </div>
    </Shell>
  );
}
