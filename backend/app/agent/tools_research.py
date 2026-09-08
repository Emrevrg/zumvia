"""
ARAŞTIRMA ARACI — ajanın derin araştırma başlatması
====================================================

Ajan tek başına da piyasa verisi çekip yorum yapabilir. Aradaki fark şudur:
tek başına yaptığında ürettiği sayılar DOĞRULANMAZ. Araştırma boru hattı
ise her sayıyı ölçüm defterine karşı sınar, farklı modellere farklı roller
dağıtır ve bir şüpheci görevlendirir.

Bu yüzden ajan, "şuna derinlemesine bak", "rapor hazırla", "karşılaştır"
türü isteklerde tek başına yorum yapmak yerine bu aracı çağırmalıdır.
"""
from __future__ import annotations

from typing import Any

from ..core.logging import get_logger
from .tools import ToolContext, _obj, tool

log = get_logger("zumvia.agent.research")

#  Rapor bağlama sığmaz (uzman görüşleri + ölçümler binlerce token). Ajana
#  özet ve dosya yolu verilir; ayrıntıyı kullanıcı yan panelden okur.
PREVIEW_CHARS = 1400


@tool(
    "deep_research",
    "DERİN ARAŞTIRMA başlatır: piyasa verisi toplanır, farklı modellerdeki uzman "
    "ajanlar (piyasa analisti, haber analisti, kanıt uzmanı, risk müdürü, şüpheci) "
    "paralel çalışır, ürettikleri HER SAYI ölçüm defterine karşı doğrulanır ve "
    "kaynaklı bir rapor yazılır. Kullanıcı derinlemesine analiz, karşılaştırma ya "
    "da rapor istediğinde KENDİ BAŞINA yorum yapmak yerine bunu çağır — buradaki "
    "sayılar doğrulanmıştır, seninkiler değil. Dakikalarca sürebilir.",
    _obj({
        "question": {"type": "string",
                     "description": "Araştırılacak soru. Kullanıcının cümlesini "
                                    "olduğu gibi ilet; varlık adı geçmiyorsa ekleme."},
        "timeframe": {"type": "string",
                      "description": "Zaman dilimi (15m, 1h, 4h, 1d). Varsayılan 4h."},
        "with_backtest": {"type": "boolean",
                          "description": "Geçmiş veride kanıt toplansın mı "
                                         "(yavaş ama tezi güçlendirir). Varsayılan true."},
    }, ["question"]),
)
def _deep_research(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..research.service import run_research  # noqa: PLC0415

    question = str(args.get("question", "")).strip()
    if len(question) < 3:
        return {"error": "Araştırma sorusu çok kısa."}

    result = run_research(
        ctx.db, ctx.user, question,
        session_id=ctx.session_id,
        timeframe=str(args.get("timeframe", "4h")),
        with_backtest=bool(args.get("with_backtest", True)),
    )

    experts = [
        {"rol": o.label, "model": f"{o.provider}/{o.model}" if o.model else "",
         "calisti": o.ok,
         "guven": round(o.audit.trust_score, 2) if o.audit else None,
         "celiski": len(o.audit.contradicted) if o.audit else 0}
        for o in result.outputs
    ]

    # Ajana verilen özet, raporun KENDİ değerlendirmesini içerir: kaç iddia
    # doğrulandı, kaçı ölçümle çelişti. Ajan bunu kullanıcıya aktarmalı,
    # çünkü raporun güvenilirliği içeriği kadar önemlidir.
    return {
        "soru": question,
        "guven_skoru": round(result.trust_score, 3),
        "olcum_sayisi": len(result.ledger),
        "kaynaklar": result.ledger.sources(),
        "uzmanlar": experts,
        "celiskili_iddia": sum(len(o.audit.contradicted)
                               for o in result.outputs if o.audit),
        "dayanaksiz_iddia": sum(len(o.audit.unsupported)
                                for o in result.outputs if o.audit),
        "risk": {k: v for k, v in (result.risk or {}).items()
                 if isinstance(v, (int, float, str)) or v is None},
        "uyarilar": result.warnings,
        "rapor_ozeti": result.report[:PREVIEW_CHARS] if result.report else "",
        "not": ("Tam rapor yan panelde açıldı. Ölçümler ve dipnotlar orada; "
                "kullanıcıya özet ver ve raporu incelemesini söyle."),
    }
