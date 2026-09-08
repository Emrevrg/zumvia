"""
RAPOR — okunabilir, dipnotlu, dürüst
=====================================

Bir araştırma raporunun değeri, ne söylediği kadar NEYİ SÖYLEMEDİĞİNİ
göstermesindedir. Buradaki biçim üç ayrımı hiç bulanıklaştırmaz:

    ✓ ÖLÇÜLDÜ     defterde kaynağıyla birlikte var
    ⚠ ÇELİŞİYOR   model başka söylüyor, ölçüm başka
    → ÖNGÖRÜ      geleceğe dair beklenti — doğrulanamaz, çürütülemez
    ○ YORUM       ölçüm karşılığı yok — değersiz değil, ama ölçüm de değil

Kullanıcı raporu okurken hangi cümlenin hangi kategoride olduğunu tahmin
etmek zorunda kalmaz. Bu, sistemin en önemli özelliğidir: dil modelleri
ikna edici yazar, ikna ediciliğin dayanağı olup olmadığını görmek gerekir.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:                       # pragma: no cover
    from .pipeline import ResearchResult

#  Rapor başına gösterilecek en fazla dipnot. Fazlası okunmaz hâle getirir.
MAX_CITATIONS = 24

LIABILITY = (
    "Bu rapor yatırım tavsiyesi değildir. Ölçümler gerçek piyasa verisinden "
    "alınmıştır; yorumlar bir yapay zeka modeline aittir ve yanılabilir. "
    "Kararın sorumluluğu kullanıcıya aittir."
)


def render(result: ResearchResult) -> str:
    """Araştırma sonucunu okunabilir Markdown rapora çevirir."""
    lines: list[str] = []
    add = lines.append

    add(f"# Araştırma: {result.question.strip()}")
    add("")
    add(_headline(result))
    add("")

    if result.warnings:
        add("## Dikkat")
        for warning in result.warnings:
            add(f"- {warning}")
        add("")

    if result.report:
        add("## Sonuç")
        add(result.report.strip())
        add("")

    risk_block = _risk_section(result.risk)
    if risk_block:
        add("## Risk")
        add(risk_block)
        add("")

    add("## Uzman görüşleri")
    for output in result.outputs:
        if output.role == "synthesis":
            continue
        add("")
        add(f"### {output.label}")
        if not output.ok:
            add(f"_Bu uzman çalışamadı: {output.error}_")
            continue
        add(output.text.strip())
        if output.audit is not None:
            add("")
            add(f"> **Doğrulama:** {output.audit.summary()}")
            for claim in output.audit.contradicted:
                add(f"> - ⚠ **Çelişki:** “{claim.raw}” — {claim.note}")
            for claim in output.audit.projections[:4]:
                add(f"> - → Öngörü (ölçüm değil): “{claim.raw}”")
            for claim in output.audit.unsupported[:4]:
                add(f"> - ○ Ölçüm karşılığı yok: “{claim.raw}”")
            for source in output.audit.unknown_sources[:4]:
                add(f"> - ○ Doğrulanmamış atıf: {source}")

    add("")
    add(_measurements(result))
    add("")
    add("---")
    add(f"_{LIABILITY}_")
    return "\n".join(lines)


def _headline(result: ResearchResult) -> str:
    """Raporun en üstündeki tek satırlık güven özeti."""
    score = result.trust_score
    facts = len(result.ledger)
    sources = len(result.ledger.sources())
    experts = len([o for o in result.outputs if o.ok])

    if not result.has_analysis:
        # Ölçümler alındı ama hiçbir uzman konuşamadı. Bunu "yüksek dayanak"
        # diye sunmak, kullanıcıyı doğrudan yanıltmak olur.
        return (f"**ANALİZ YAPILAMADI** — {facts} ölçüm toplandı ama hiçbir "
                f"uzman model yanıt veremedi. Aşağıdaki ham ölçümler geçerli, "
                f"yorum yok.")

    if score >= 0.85:
        mark = "Yüksek dayanak"
    elif score >= 0.6:
        mark = "Orta dayanak"
    elif score >= 0.35:
        mark = "Zayıf dayanak"
    else:
        mark = "ÇOK ZAYIF dayanak — bu rapora dayanarak işlem açmayın"

    return (f"**{mark}** · sayısal iddiaların %{score * 100:.0f}'i ölçümle "
            f"doğrulandı · {facts} ölçüm, {sources} kaynak, {experts} uzman "
            f"· {result.duration_ms / 1000:.0f} sn")


def _risk_section(risk: dict[str, Any]) -> str:
    """Risk tablosu. Ölçülemeyen alan gizlenmez, 'ölçülemedi' yazılır."""
    if not risk:
        return ""

    lines: list[str] = []
    equity = risk.get("sermaye")
    lines.append(f"- Sermaye: {_num(equity)}")
    lines.append(f"- İşlem başına kasa riski: %{risk.get('kasa_riski_pct', '?')}")

    worst = risk.get("en_kotu_senaryo_tutar")
    worst_pct = risk.get("en_kotu_senaryo_pct")
    if worst is not None:
        lines.append(f"- En kötü makul senaryo: {_num(worst)} "
                     f"(%{worst_pct} sermaye)")
    else:
        lines.append("- En kötü makul senaryo: **ölçülemedi**")

    rows = risk.get("islem_basi") or []
    measured = [r for r in rows if r.get("pozisyon_tutari") is not None]
    if measured:
        lines.append("")
        lines.append("| Varlık | Fiyat | Stop mesafesi | Pozisyon | Riske atılan |")
        lines.append("|---|---|---|---|---|")
        for row in measured:
            lines.append(
                f"| {row['sembol']} | {_num(row.get('fiyat'))} | "
                f"{_num(row.get('stop_mesafesi'))} (%{row.get('stop_pct', '?')}) | "
                f"{_num(row.get('pozisyon_tutari'))} | {_num(row.get('risk_tutari'))} |")

    unmeasured = [r for r in rows if r.get("pozisyon_tutari") is None]
    for row in unmeasured:
        lines.append(f"- {row['sembol']}: {row.get('not', 'hesaplanamadı')}")

    for note in risk.get("notlar", []):
        lines.append(f"- ⚠ {note}")

    return "\n".join(lines)


def _measurements(result: ResearchResult) -> str:
    """
    Ölçüm dipnotları.

    Raporun sonundaki bu bölüm, her sayının nereden geldiğini gösterir.
    Onsuz rapor, kaynağı belirsiz sayılar listesine döner.
    """
    facts = result.ledger.all()
    if not facts:
        return "## Ölçümler\n_Bu araştırmada hiçbir ölçüm alınamadı._"

    lines = ["## Ölçümler",
             f"_{len(facts)} ölçüm, kaynaklar: {', '.join(result.ledger.sources())}_",
             ""]
    for fact in facts[:MAX_CITATIONS]:
        unit = f" {fact.unit}" if fact.unit else ""
        lines.append(f"- `{fact.key}` = **{fact.value}**{unit} {fact.cite()}")
    if len(facts) > MAX_CITATIONS:
        lines.append(f"- _… ve {len(facts) - MAX_CITATIONS} ölçüm daha_")
    return "\n".join(lines)


def _num(value: Any) -> str:
    """Sayıyı okunabilir yazar; yoksa 'ölçülemedi' der — 0 yazmaz."""
    if value is None:
        return "ölçülemedi"
    if isinstance(value, (int, float)):
        if abs(value) >= 1000:
            return f"{value:,.2f}".replace(",", " ")
        return f"{value:,.4f}".rstrip("0").rstrip(".").replace(",", " ")
    return str(value)
