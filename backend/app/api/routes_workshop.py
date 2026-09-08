"""
ATÖLYE UÇLARI — ajanın yazdıkları kullanıcıya görünür olsun
============================================================

Ajan kendi becerisini yazabiliyor ve kendi otomasyonunu kurabiliyor. Bunlar
kullanıcının adına ve onun cihazında çalışan şeylerdir; görülemiyorlarsa
kontrol edilemezler.

Bu uçlar üç soruya cevap verir:

    Ajan ne yazdı?          (beceriler, tarifleriyle)
    Ne zaman çalışıyor?     (otomasyonlar, sicilleriyle)
    Gerçekten işe yarıyor mu? (çalışma/hata sayıları)

Son soru en önemlisidir: bir beceri kaydedilmiş olduğu için değil,
çalıştığı ÖLÇÜLDÜĞÜ için güvenilirdir.

Silme ve durdurma da buradadır: ajanın kurduğu bir şeyi kullanıcı her zaman
kapatabilmelidir — otonomi, geri alınamazlık demek değildir.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..core.db import get_db
from ..core.logging import get_logger
from ..layers import automations, custom_skills
from ..layers.automations import AutomationError
from ..models import User
from ..schemas import GenericOut
from .deps import current_user

log = get_logger("zumvia.api.workshop")
router = APIRouter(prefix="/api/workshop", tags=["Atölye"])


@router.get("/skills")
def list_skills(db: Session = Depends(get_db),
                user: User = Depends(current_user)) -> dict[str, Any]:
    """Ajanın yazdığı beceriler, tarifleri ve sicilleriyle."""
    rows = custom_skills.listing(db, user)
    return {"skills": [custom_skills.describe(r) for r in rows],
            "count": len(rows)}


@router.get("/skills/{slug}")
def read_skill(slug: str, db: Session = Depends(get_db),
               user: User = Depends(current_user)) -> dict[str, Any]:
    row = custom_skills.find(db, user, slug)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Beceri bulunamadı.")
    return custom_skills.describe(row)


@router.delete("/skills/{slug}", response_model=GenericOut)
def delete_skill(slug: str, db: Session = Depends(get_db),
                 user: User = Depends(current_user)) -> GenericOut:
    """
    Beceriyi siler.

    Kullanıcı, ajanın yazdığı her şeyi kaldırabilmelidir: otonomi
    geri alınamazlık demek değildir.
    """
    if not custom_skills.remove(db, user, slug):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Beceri bulunamadı.")
    return GenericOut(message=f"'{slug}' silindi.")


@router.get("/automations")
def list_automations(db: Session = Depends(get_db),
                     user: User = Depends(current_user)) -> dict[str, Any]:
    """Kurulmuş otomasyonlar, zamanlamaları ve sicilleriyle."""
    rows = automations.listing(db, user)
    return {"automations": [automations.describe(r) for r in rows],
            "count": len(rows), "limit": automations.MAX_PER_USER}


@router.post("/automations/{slug}/toggle", response_model=GenericOut)
def toggle_automation(slug: str, db: Session = Depends(get_db),
                      user: User = Depends(current_user)) -> GenericOut:
    """
    Otomasyonu durdurur ya da yeniden başlatır.

    Silmek yerine durdurmak sicili korur: "bu işe yarıyor muydu" sorusu
    daha sonra da cevaplanabilsin.
    """
    row = automations.find(db, user, slug)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Otomasyon bulunamadı.")
    try:
        automations.edit(db, user, slug, {"enabled": not row.enabled})
    except AutomationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return GenericOut(message="Durduruldu." if row.enabled else "Yeniden başlatıldı.")


@router.post("/automations/{slug}/run", response_model=GenericOut)
def run_automation(slug: str, db: Session = Depends(get_db),
                   user: User = Depends(current_user)) -> GenericOut:
    """Otomasyonu zamanını beklemeden bir kez çalıştırır."""
    row = automations.find(db, user, slug)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Otomasyon bulunamadı.")
    result = automations.run_one(db, user, row)
    ok = bool(result.get("ok"))
    return GenericOut(
        ok=ok,
        message=(str(result.get("ozet") or "Çalıştı.")[:300] if ok
                 else f"Hata: {str(result.get('hata') or '')[:280]}"))


@router.delete("/automations/{slug}", response_model=GenericOut)
def delete_automation(slug: str, db: Session = Depends(get_db),
                      user: User = Depends(current_user)) -> GenericOut:
    if not automations.remove(db, user, slug):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Otomasyon bulunamadı.")
    return GenericOut(message=f"'{slug}' silindi.")


@router.get("/summary")
def workshop_summary(db: Session = Depends(get_db),
                     user: User = Depends(current_user)) -> dict[str, Any]:
    """
    Atölyenin tek bakışta özeti.

    Toplam sayı değil, ÇALIŞAN sayı gösterilir: on beceri yazıp hiçbirini
    denememiş olmak, bir beceri yazıp onu doğrulamış olmaktan daha kötüdür.
    """
    skills = custom_skills.listing(db, user)
    autos = automations.listing(db, user)
    tried = [s for s in skills if (s.runs or 0) > 0]
    working = [s for s in tried if (s.runs or 0) > (s.failures or 0)]

    return {
        "skills": {
            "total": len(skills),
            "tried": len(tried),
            "working": len(working),
            "untested": len(skills) - len(tried),
        },
        "automations": {
            "total": len(autos),
            "enabled": len([a for a in autos if a.enabled]),
            "failing": len([a for a in autos
                            if (a.runs or 0) and (a.failures or 0) * 2 > (a.runs or 0)]),
        },
        "note": ("Denenmemiş beceri, çalıştığı bilinmeyen beceridir. "
                 "Kaydedilmiş olması çalıştığını göstermez."),
    }
