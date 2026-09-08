"""
ARAŞTIRMA UÇLARI
=================

Araştırma dakikalarca sürer (altı aşama, beş uzman, gerçek piyasa verisi).
Bu yüzden istek anında sonuç dönmez: iş arka planda başlar, ilerleme
WebSocket'ten akar, biten rapor diskten okunur.

Raporlar kullanıcının cihazında kalır. Hiçbir uç, raporu dışarıya göndermez.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.db import SessionLocal
from ..core.logging import get_logger
from ..models import User
from ..research import roles
from ..research.service import REPORT_DIR, run_research
from ..schemas import GenericOut
from .deps import current_user

log = get_logger("zumvia.api.research")
router = APIRouter(prefix="/api/research", tags=["Araştırma"])

#  Aynı anda çalışan araştırmalar. Bir kullanıcının aynı anda ikinci bir
#  araştırma başlatması engellenir: her araştırma onlarca sağlayıcı çağrısı
#  yapar ve üst üste binen işler yalnızca kotayı yakar.
_RUNNING: dict[int, str] = {}
_CANCEL: set[int] = set()


class ResearchIn(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    timeframe: str = Field(default="4h", max_length=8)
    with_backtest: bool = True
    session_id: int | None = None


@router.get("/roles")
def role_catalog() -> dict[str, Any]:
    """Araştırmada görev alan uzman rolleri ve görev tanımları."""
    return {"roles": roles.catalog(),
            "stages": [
                {"id": "brif", "label": "Soruyu netleştir"},
                {"id": "veri", "label": "Veri topla"},
                {"id": "analiz", "label": "Uzmanlar çalışsın"},
                {"id": "dogrulama", "label": "Sayıları doğrula"},
                {"id": "risk", "label": "Riski ölç"},
                {"id": "rapor", "label": "Raporu yaz"},
            ]}


@router.get("/status")
def research_status(user: User = Depends(current_user)) -> dict[str, Any]:
    """Bu kullanıcının araştırması çalışıyor mu."""
    return {"running": user.id in _RUNNING,
            "question": _RUNNING.get(user.id, "")}


@router.post("/start", response_model=GenericOut)
def start(payload: ResearchIn, background: BackgroundTasks,
          user: User = Depends(current_user)) -> GenericOut:
    """Araştırmayı arka planda başlatır; ilerleme WebSocket'ten akar."""
    if user.id in _RUNNING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Zaten bir araştırma çalışıyor. Bitmesini bekleyin ya da durdurun.")

    _RUNNING[user.id] = payload.question
    _CANCEL.discard(user.id)
    background.add_task(_run, user.id, payload.question, payload.timeframe,
                        payload.with_backtest, payload.session_id)
    return GenericOut(message="Araştırma başladı — adımlar canlı akacak.")


@router.post("/stop", response_model=GenericOut)
def stop(user: User = Depends(current_user)) -> GenericOut:
    """
    Çalışan araştırmayı durdurur.

    Boru hattı her aşama arasında durdurma isteğine bakar; o ana kadar
    toplanan ölçümler ve tamamlanan uzman görüşleri korunur.
    """
    if user.id not in _RUNNING:
        return GenericOut(message="Çalışan araştırma yok.")
    _CANCEL.add(user.id)
    return GenericOut(message="Durduruluyor — bulunduğu aşamayı bitirip duracak.")


@router.get("/reports")
def list_reports(user: User = Depends(current_user),
                 limit: int = Query(default=30, le=100)) -> list[dict[str, Any]]:
    """
    Bu kullanıcının araştırma raporları (en yeni önce).

    Yalnızca kendi dosyaları listelenir: dosya adı kullanıcı kimliğiyle
    başlar ve başka bir kullanıcının kimliğiyle başlayan dosyalar hiç
    okunmaz.
    """
    if not REPORT_DIR.exists():
        return []

    rows: list[dict[str, Any]] = []
    for path in sorted(REPORT_DIR.glob(f"{user.id}-*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — bozuk dosya listeyi bozmasın
            log.warning("rapor okunamadı (%s): %s", path.name, exc)
            continue
        rows.append({
            "id": path.stem,
            "question": data.get("question", ""),
            "trust_score": data.get("trust_score"),
            "facts": data.get("ledger", {}).get("count", 0),
            "sources": data.get("ledger", {}).get("sources", []),
            "warnings": len(data.get("warnings", [])),
            "duration_ms": data.get("duration_ms", 0),
            "started_at": data.get("started_at", ""),
        })
    return rows


@router.get("/reports/{report_id}")
def read_report(report_id: str, fmt: str = Query(default="json"),
                user: User = Depends(current_user)) -> Any:
    """Tek bir raporun tamamı (`fmt=md` okunabilir metin döndürür)."""
    path = _own_report(user.id, report_id)
    if fmt == "md":
        markdown = path.with_suffix(".md")
        if not markdown.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Metin raporu yok.")
        return {"id": report_id, "markdown": markdown.read_text(encoding="utf-8")}
    return json.loads(path.read_text(encoding="utf-8"))


@router.delete("/reports/{report_id}", response_model=GenericOut)
def delete_report(report_id: str, user: User = Depends(current_user)) -> GenericOut:
    """Raporu siler. Silme geri alınamaz."""
    path = _own_report(user.id, report_id)
    removed = 0
    for suffix in (".json", ".md"):
        candidate = path.with_suffix(suffix)
        if candidate.exists():
            candidate.unlink()
            removed += 1
    return GenericOut(message=f"{removed} dosya silindi.")


# --------------------------------------------------------------------------- #
#  İç işler
# --------------------------------------------------------------------------- #

def _own_report(user_id: int, report_id: str) -> Path:
    """
    Rapor dosyasını doğrular ve döndürür.

    İki ayrı kontrol yapılır ve ikisi de gereklidir: dosya adı kullanıcının
    kimliğiyle başlamalı VE çözümlenmiş yol rapor klasörünün içinde
    kalmalı. İkincisi olmadan `../../` içeren bir kimlik, klasör dışına
    çıkabilirdi.
    """
    if not report_id.startswith(f"{user_id}-"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rapor bulunamadı.")

    path = (REPORT_DIR / report_id).with_suffix(".json").resolve()
    if not str(path).startswith(str(REPORT_DIR.resolve())):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rapor bulunamadı.")
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rapor bulunamadı.")
    return path


def _run(user_id: int, question: str, timeframe: str,
         with_backtest: bool, session_id: int | None) -> None:
    """Araştırmayı kendi veritabanı oturumunda çalıştırır."""
    db: Session = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            return
        run_research(db, user, question, session_id=session_id,
                     timeframe=timeframe, with_backtest=with_backtest,
                     cancelled=lambda: user_id in _CANCEL)
    except Exception:  # noqa: BLE001 — araştırma çökse de platform ayakta kalır
        log.exception("araştırma hatası (kullanıcı %s)", user_id)
        from ..engine.hub import hub  # noqa: PLC0415
        hub.publish(user_id, {"type": "research_error",
                              "message": "Araştırma beklenmedik bir hatayla durdu."})
    finally:
        db.close()
        _RUNNING.pop(user_id, None)
        _CANCEL.discard(user_id)
