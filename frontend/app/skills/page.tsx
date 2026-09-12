"use client";

import { useState } from "react";
import useSWR from "swr";
import { Shell } from "@/components/Shell";
import { fetcher } from "@/lib/api";

type Skill = {
  id: string; label: string; category: string; level: string; cost: string;
  description: string; when_used: string;
};

const CATS: [string, string][] = [
  ["all", "Tümü"],
  ["risk", "Risk & Koruma"],
  ["analiz", "Analiz"],
  ["yürütme", "Yürütme"],
  ["otomasyon", "Otomasyon"],
  ["veri", "Veri"],
  ["kurtarma", "Kurtarma"],
];

export default function SkillsPage() {
  const { data } = useSWR<{ skills: Skill[]; count: number }>("/api/skills", fetcher);
  const [filter, setFilter] = useState("all");
  const skills = data?.skills ?? [];
  const shown = filter === "all" ? skills : skills.filter((s) => s.category === filter);

  return (
    <Shell title="Yetenekler" subtitle="Ajanın alet çantası — model yetenek yazmaz, uygun olanı seçer">
      <div className="panel mb-4 text-[12.5px] leading-relaxed text-slate-400">
        Sistemde <b className="text-white">{data?.count ?? skills.length} yetenek</b> hazır —
        model yetenek <b className="text-white">yazmaz</b>, yalnızca uygun olanı{" "}
        <b className="text-white">seçer</b>. Geri test, tarama, doğrulama dahil her şeyi
        ajan kendisi yapar; siz sadece izlersiniz.
      </div>

      <div className="mb-4 flex flex-wrap gap-2">
        {CATS.map(([id, label]) => (
          <button key={id} onClick={() => setFilter(id)}
            className={`btn px-3 py-1.5 text-xs ${filter === id ? "btn-primary" : ""}`}>
            {label}
          </button>
        ))}
      </div>

      {!data ? (
        <div className="panel text-center text-slate-400">Yükleniyor…</div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {shown.map((s) => (
            <div key={s.id} className="panel">
              <div className="mb-1.5 text-[13.5px] font-semibold">{s.label}</div>
              <div className="mb-2 flex flex-wrap items-center gap-1.5 text-[11px] text-slate-500">
                {s.category}
                <span className={`badge ${s.level === "uzman" ? "badge-green" : "badge-slate"}`}>
                  {s.level}
                </span>
                <span className="badge badge-slate">{s.cost}</span>
              </div>
              <p className="text-[12.5px] leading-relaxed text-slate-400">{s.description}</p>
              <p className="mt-2 text-[11.5px] text-slate-500">
                <span className="text-slate-400">Ne zaman:</span> {s.when_used}
              </p>
            </div>
          ))}
        </div>
      )}
    </Shell>
  );
}
