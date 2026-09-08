"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
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
    <div className="grid min-h-screen place-items-center p-6">
      <div className="w-full max-w-[430px]">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 grid h-16 w-16 place-items-center rounded-2xl bg-gradient-to-br from-emerald to-[#00b894] text-3xl font-extrabold text-[#04120a] shadow-glow">
            V
          </div>
          <div className="text-lg font-extrabold tracking-[0.14em]">ZUMVIA</div>
          <div className="mt-1 text-[12px] text-slate-400">
            Otonom yapay zeka portföy komutanı
          </div>
        </div>

        <div className="panel">
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
      </div>
    </div>
  );
}
