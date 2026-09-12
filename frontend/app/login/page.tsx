"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Activity, ShieldCheck, Sparkles } from "lucide-react";
import { BrandMark } from "@/components/BrandMark";
import { api, auth } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const data = await api<{ access_token: string; email: string }>(
        `/api/auth/${mode}`,
        { method: "POST", body: { email, password }, auth: false },
      );
      auth.set(data.access_token, data.email);
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Bilinmeyen hata");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="relative grid min-h-screen place-items-center overflow-hidden p-6 lg:grid-cols-[1.08fr_.92fr] lg:gap-12 lg:px-[8vw]">
      <section className="relative hidden max-w-2xl lg:block">
        <div className="eyebrow mb-6"><Sparkles size={13} /> ZUMVIA QUANT PLATFORM</div>
        <h1 className="max-w-xl text-5xl font-black leading-[1.03] tracking-[-.04em] xl:text-6xl">
          Piyasayı ölçer. Riski sınırlar. Kararı{' '}
          <span className="bg-gradient-to-r from-emerald-mint via-emerald to-info bg-clip-text text-transparent">
            görünür kılar.
          </span>
        </h1>
        <p className="mt-6 max-w-lg text-[15px] leading-7 text-slate-400">
          ZUMVIA; piyasa verisini, deterministik stratejileri ve yapay zekâ yorumunu
          tek bir denetlenebilir çalışma alanında birleştirir.
        </p>
        <div className="mt-9 grid max-w-xl grid-cols-3 gap-3">
          {[
            [Activity, 'Canlı veri', 'Ölçülen piyasa akışı'],
            [ShieldCheck, 'Risk kalkanı', 'Stop-loss ve limitler'],
            [Sparkles, 'Akıllı komuta', 'İzlenebilir ajan adımları'],
          ].map(([Icon, title, detail]) => {
            const Mark = Icon as typeof Activity;
            return (
              <div key={String(title)} className="panel panel-lift p-4">
                <Mark size={18} className="mb-3 text-emerald" />
                <div className="text-[12.5px] font-semibold">{String(title)}</div>
                <div className="mt-1 text-[10.5px] leading-relaxed text-slate-500">{String(detail)}</div>
              </div>
            );
          })}
        </div>
      </section>

      <section className="w-full max-w-[450px] animate-fadeUp">
        <div className="mb-6 text-center">
          <div className="relative mx-auto mb-3 grid h-24 w-24 place-items-center">
            <span className="absolute inset-1 animate-[spin_14s_linear_infinite] rounded-[30px] border border-emerald/25 shadow-glow" />
            <span className="absolute inset-0 animate-pulse rounded-[34px] bg-emerald/10 blur-xl" />
            <BrandMark size={76} />
          </div>
          <div className="text-xl font-extrabold tracking-[0.18em]">ZUMVIA</div>
          <div className="mt-1 text-[12px] text-slate-400">
            Otonom yapay zeka portföy komutanı
          </div>
        </div>

        <div className="panel overflow-hidden">
          <span className="glass-line absolute inset-x-0 top-0 h-px" />
          <div className="mb-4 flex gap-1 rounded-lg bg-[#0e1622] p-1">
            {(["login", "register"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`flex-1 rounded-md py-2 text-sm font-semibold transition ${
                  mode === m ? "bg-emerald/15 text-emerald-mint" : "text-slate-400"
                }`}
              >
                {m === "login" ? "Giriş yap" : "Hesap oluştur"}
              </button>
            ))}
          </div>

          <form onSubmit={submit} className="space-y-3">
            <div className="space-y-1.5">
              <label className="label">E-posta</label>
              <input
                className="input" type="email" required value={email}
                onChange={(e) => setEmail(e.target.value)} placeholder="siz@ornek.com"
              />
            </div>
            <div className="space-y-1.5">
              <label className="label">Parola</label>
              <input
                className="input" type="password" required minLength={8} value={password}
                onChange={(e) => setPassword(e.target.value)} placeholder="En az 8 karakter"
              />
              <p className="text-[11px] text-slate-400">
                Parolanız scrypt ile saltlanarak saklanır; sunucuda düz metin tutulmaz.
              </p>
            </div>

            {error && (
              <div className="rounded-r-lg border-l-2 border-danger bg-danger/10 px-3 py-2 text-[12.5px] text-[#ffc0cc]">
                {error}
              </div>
            )}

            <button className="btn btn-primary w-full" disabled={busy}>
              {busy ? "İşleniyor…" : mode === "login" ? "Giriş yap" : "Hesap oluştur"}
            </button>
          </form>

          <div className="mt-4 rounded-r-lg border-l-2 border-warn bg-warn/10 px-3 py-2 text-[12px] text-[#ffdca6]">
            <b>Uyarı:</b> Bu yazılım finansal tavsiye vermez. Ticaret sermaye kaybı riski
            taşır. Gerçek paraya geçmeden önce en az 30 gün paper trading yapın.
          </div>
        </div>
      </section>
    </main>
  );
}
