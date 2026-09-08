"""
GÜVENLİK VE RAPOR UÇLARI
=========================
Gerçek para yetkisi, kill switch, denetim izi ve raporlar.

**Kritik:** Canlı ticaret yetkisi burada, yalnızca oturum açmış kullanıcının
açık isteğiyle verilir. Ajanın bu uçlara erişimi yoktur; ajan yalnızca
`enable_live_trading` aracıyla, verilmiş bir yetkinin sınırları içinde
hareket edebilir.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..core import emergency
from ..core.config import settings
from ..core.db import get_db
from ..core.mcp_auth import create_token, list_tokens, revoke_token
from ..core.safety import (
    DEFAULT_LIVE_AUTH_HOURS,
    MAX_LIVE_AUTH_HOURS,
    grant_live_authorization,
    revoke_live_authorization,
    safety_snapshot,
)
from ..engine import reporting
from ..models import AuditLog, Bot, DecisionRecord, ModelScore, TradingMode, User
from ..schemas import GenericOut
from .deps import current_user


class RateLimitIn(BaseModel):
    """Sağlayıcı hız sınırı (kullanıcının katmanına göre)."""
    rpm: int | None = Field(default=None, ge=1, le=100_000)
    tpm: int | None = Field(default=None, ge=0)
    concurrent: int | None = Field(default=None, ge=0, le=64)


class PreferencesIn(BaseModel):
    """Kullanıcı tercihleri."""
    auto_translate_prompt: bool | None = None
    default_trading_mode: Literal["paper", "live"] | None = None
    default_capital: float | None = Field(default=None, ge=0)
    model_strategy: Literal["single", "same_provider", "multi_provider"] | None = None


class RiskBudgetIn(BaseModel):
    """Global risk tavanı (0 = yeni işlem yok)."""
    risk_budget_pct: float = Field(default=0.5, ge=0, le=1.5)


router = APIRouter(prefix="/api/safety", tags=["Güvenlik"])


# --------------------------------------------------------------------------- #
#  Şemalar
# --------------------------------------------------------------------------- #
class LiveGrantIn(BaseModel):
    """Gerçek para yetkisi. Onay metni birebir yazılmadan işlem kabul edilmez."""
    max_capital: float = Field(gt=0, description="Bu yetkiyle riske atılabilecek üst sınır")
    hours: int = Field(default=DEFAULT_LIVE_AUTH_HOURS, ge=1, le=MAX_LIVE_AUTH_HOURS)
    confirmation: str = Field(description="Birebir 'GERCEK PARA ONAYLIYORUM' yazılmalı")
    note: str = Field(default="", max_length=255)


class McpTokenIn(BaseModel):
    """Dış araçlar için adlandırılmış erişim anahtarı."""
    name: str = Field(default="MCP istemcisi", max_length=64)
    days: int | None = Field(default=None, ge=1, le=365,
                             description="Boş bırakılırsa süresiz (iptal edilene kadar)")


class KillSwitchIn(BaseModel):
    active: bool
    note: str = Field(default="", max_length=255)


CONFIRMATION_PHRASE = "GERCEK PARA ONAYLIYORUM"


# --------------------------------------------------------------------------- #
#  Durum
# --------------------------------------------------------------------------- #
@router.get("/rate-limits")
def rate_limits() -> dict:
    """Sağlayıcı hız sınırları ve gerçek kullanım (son 60 sn)."""
    from ..layers.rate_limit import PROVIDER_LIMITS, snapshot  # noqa: PLC0415

    return {
        "usage": snapshot(),
        "known": {k: {"rpm": v.rpm, "tpm": v.tpm, "concurrent": v.concurrent,
                      "note": v.note}
                  for k, v in sorted(PROVIDER_LIMITS.items())},
    }


@router.post("/rate-limits/{provider}")
def set_rate_limit(provider: str, payload: RateLimitIn,
                   user: User = Depends(current_user)) -> dict:  # noqa: ARG001
    """
    Sağlayıcı sınırını günceller.

    Ücretli katmanda sınır 10 kat yüksek olabiliyor; kullanıcı parasını verdiği
    kapasiteyi kullanabilmelidir. Düşürmek de mümkündür (daha temkinli olmak için).
    """
    from ..layers.rate_limit import configure  # noqa: PLC0415

    return configure(provider, rpm=payload.rpm, tpm=payload.tpm,
                     concurrent=payload.concurrent)


@router.get("/model-pool")
def model_pool(strategy: str = "single", size: int = 3,
               db: Session = Depends(get_db),
               user: User = Depends(current_user)) -> dict:
    """Seçilen stratejiye göre model havuzu ve gerçek kapasitesi."""
    from ..agent.model_pool import STRATEGIES, build, describe  # noqa: PLC0415

    result = build(db, user, strategy, size=size)
    result["available_strategies"] = [
        {"id": s, "label": describe(s)} for s in STRATEGIES
    ]
    return result


@router.post("/preferences")
def set_preferences(payload: PreferencesIn, db: Session = Depends(get_db),
                    user: User = Depends(current_user)) -> dict:
    """Kullanıcı tercihleri (şu an: istem çevirisi)."""
    if payload.auto_translate_prompt is not None:
        user.auto_translate_prompt = bool(payload.auto_translate_prompt)
    if payload.default_trading_mode is not None:
        user.default_trading_mode = payload.default_trading_mode
    if payload.default_capital is not None:
        user.default_capital = float(payload.default_capital)
    if payload.model_strategy is not None:
        user.model_strategy = payload.model_strategy
    db.commit()

    from ..core.trading_mode import evaluate  # noqa: PLC0415
    mode = evaluate(db, user)
    return {
        "ok": True,
        "auto_translate_prompt": user.auto_translate_prompt,
        "default_trading_mode": user.default_trading_mode,
        "default_capital": user.default_capital,
        "model_strategy": user.model_strategy,
        "effective_mode": mode["mode"],
        "mode_reason": mode["reason"],
    }


@router.post("/risk-budget")
def set_risk_budget(payload: RiskBudgetIn, db: Session = Depends(get_db),
                    user: User = Depends(current_user)) -> dict:
    """
    Tüm sistem için işlem başına risk tavanı.

    Botların kendi ayarı ne olursa olsun bunun üstüne çıkamaz. 0 verilirse
    sistem yeni pozisyon açmaz; yalnızca izler ve açık pozisyonları yönetir.
    """
    value = max(0.0, min(float(payload.risk_budget_pct), settings.hard_max_risk_pct))
    from ..core.safety import audit  # noqa: PLC0415

    user.risk_budget_pct = value
    db.commit()
    audit(db, user, "risk_budget", f"Risk bütçesi %{value} olarak ayarlandı")
    db.commit()
    return {
        "ok": True,
        "risk_budget_pct": value,
        "message": ("Yeni pozisyon açılmayacak; sistem yalnızca izliyor."
                    if value == 0 else
                    f"İşlem başına risk tavanı %{value} olarak uygulanıyor."),
    }


@router.get("/status")
def status_endpoint(db: Session = Depends(get_db),
                    user: User = Depends(current_user)) -> dict[str, Any]:
    """Kill switch, canlı yetki ve sistem sınırlarının tek bakışta durumu."""
    snapshot = safety_snapshot(db, user)
    live_bots = (db.query(Bot)
                 .filter(Bot.user_id == user.id, Bot.mode == TradingMode.LIVE).all())
    return {
        **snapshot,
        "live_bots": [{"id": b.id, "name": b.name, "symbol": b.symbol,
                       "balance": round(b.paper_balance, 2)} for b in live_bots],
        "confirmation_phrase": CONFIRMATION_PHRASE,
        "max_authorization_hours": MAX_LIVE_AUTH_HOURS,
        "preferences": {
            "auto_translate_prompt": bool(user.auto_translate_prompt),
            "risk_budget_pct": float(user.risk_budget_pct),
            "default_trading_mode": user.default_trading_mode,
            "default_capital": float(user.default_capital or 0.0),
            "model_strategy": user.model_strategy,
        },
    }


# --------------------------------------------------------------------------- #
#  Canlı ticaret yetkisi
# --------------------------------------------------------------------------- #
@router.post("/live-authorization")
def grant_live(payload: LiveGrantIn, db: Session = Depends(get_db),
               user: User = Depends(current_user)) -> dict[str, Any]:
    """
    Gerçek para yetkisi verir.

    Üç koruma: (1) birebir onay metni, (2) üst sermaye limiti,
    (3) otomatik süre sonu. Yetki her an iptal edilebilir.
    """
    if payload.confirmation.strip().upper() != CONFIRMATION_PHRASE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Onay metni hatalı. Devam etmek için birebir '{CONFIRMATION_PHRASE}' yazın.",
        )
    try:
        result = grant_live_authorization(
            db, user, max_capital=payload.max_capital,
            hours=payload.hours, note=payload.note,
        )
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    db.commit()
    return {"ok": True, "authorization": result,
            "message": (f"Gerçek para yetkisi verildi: üst limit "
                        f"{payload.max_capital:.2f}, süre {payload.hours} saat. "
                        "Dilediğiniz an iptal edebilirsiniz.")}


@router.delete("/live-authorization")
def revoke_live(db: Session = Depends(get_db),
                user: User = Depends(current_user)) -> dict[str, Any]:
    """Yetkiyi anında iptal eder ve canlı botları sanal moda alır."""
    result = revoke_live_authorization(db, user, "Kullanıcı iptal etti.")

    switched = 0
    for bot in db.query(Bot).filter(Bot.user_id == user.id,
                                    Bot.mode == TradingMode.LIVE).all():
        bot.mode = TradingMode.PAPER
        switched += 1
    db.commit()

    return {"ok": True, "authorization": result, "bots_switched_to_paper": switched,
            "message": (f"Yetki iptal edildi. {switched} bot sanal moda alındı — "
                        "gerçek para riski kalmadı.")}


# --------------------------------------------------------------------------- #
#  Kill switch
# --------------------------------------------------------------------------- #
@router.post("/kill-switch", response_model=GenericOut)
def kill_switch(payload: KillSwitchIn, db: Session = Depends(get_db),
                user: User = Depends(current_user)) -> GenericOut:
    """
    ACİL FREN: durmadan ÖNCE her şeyi kapatır.

    Eskiden bu uç yalnızca yeni emirleri reddediyordu; açık pozisyonlar
    piyasada kalıyor ve botlar durduğu için stopları da izlenmiyordu. Frenin
    ne zaman kalkacağı bilinmediğinden bu, kullanıcıyı sınırsız süreyle
    açıkta bırakmak demekti.

    Artık sıra şudur: parçalı emirler iptal → tüm pozisyonlar kapatılır →
    botlar durdurulur → anahtar çevrilir → sonuç (kapanamayanlar dahil)
    denetim izine yazılır.
    """
    if payload.active:
        result = emergency.engage(db, user, payload.note)
        db.commit()
        return GenericOut(
            ok=True,
            message=result["kullaniciya_soyle"] + (
                " " + result["uyari"] if result["uyari"] else ""),
            data=result,
        )

    result = emergency.release(db, user, payload.note)
    db.commit()
    return GenericOut(ok=True, message=result["kullaniciya_soyle"], data=result)


@router.post("/flatten", response_model=GenericOut)
def flatten(db: Session = Depends(get_db),
            user: User = Depends(current_user)) -> GenericOut:
    """
    "Her şeyi kapat" — fren çekmeden tüm açık riski sıfırlar.

    Acil frenden farkı: sistem çalışmaya devam eder. Kullanıcı riskten
    çıkmak isteyip sistemi kapatmak istemeyebilir; bu ikisini aynı düğmeye
    bağlamak, birini isteyeni diğerine mahkûm eder.
    """
    result = emergency.flatten_all(db, user, stop_bots=True)
    db.commit()
    return GenericOut(
        ok=result.clean, message=result.headline(), data=result.to_dict())


# --------------------------------------------------------------------------- #
#  Denetim izi ve model sicili
# --------------------------------------------------------------------------- #
@router.get("/audit")
def audit_trail(db: Session = Depends(get_db), user: User = Depends(current_user),
                limit: int = Query(default=80, le=500)) -> list[dict[str, Any]]:
    rows = (db.query(AuditLog).filter(AuditLog.user_id == user.id)
            .order_by(desc(AuditLog.id)).limit(limit).all())
    return [{"id": a.id, "ts": a.ts.isoformat() if a.ts else None,
             "action": a.action, "actor": a.actor, "detail": a.detail}
            for a in rows]


@router.get("/decisions")
def decisions(db: Session = Depends(get_db), user: User = Depends(current_user),
              limit: int = Query(default=50, le=200)) -> list[dict[str, Any]]:
    """Konsey kararları — hangi model ne dedi, neden uygulandı/uygulanmadı."""
    import json  # noqa: PLC0415

    rows = (db.query(DecisionRecord).filter(DecisionRecord.user_id == user.id)
            .order_by(desc(DecisionRecord.id)).limit(limit).all())
    out = []
    for record in rows:
        try:
            council = json.loads(record.council_json or "{}")
        except json.JSONDecodeError:
            council = {}
        out.append({
            "decision_id": record.decision_id, "run_id": record.run_id,
            "ts": record.ts.isoformat() if record.ts else None,
            "symbol": record.symbol, "timeframe": record.timeframe,
            "action": record.action, "confidence": round(record.confidence, 3),
            "executed": record.executed, "veto_reason": record.veto_reason,
            "agreement_pct": council.get("agreement_pct"),
            "opinions": council.get("opinions", []),
            "critique": council.get("critique"),
        })
    return out


@router.get("/model-scores")
def model_scores(db: Session = Depends(get_db),
                 user: User = Depends(current_user)) -> list[dict[str, Any]]:
    """Modellerin gerçek sicili ve güncel oy ağırlıkları."""
    from ..agent.council import member_weight  # noqa: PLC0415

    rows = (db.query(ModelScore).filter(ModelScore.user_id == user.id)
            .order_by(desc(ModelScore.total_r)).all())
    return [{
        "provider": r.provider, "model": r.model, "decisions": r.decisions,
        "trades": r.trades, "wins": r.wins, "losses": r.losses,
        "win_rate_pct": round(r.wins / r.trades * 100, 1) if r.trades else 0.0,
        "total_r": round(r.total_r, 2),
        "expectancy_r": round(r.total_r / r.trades, 3) if r.trades else 0.0,
        "vetoes": r.vetoes, "schema_failures": r.schema_failures,
        "avg_latency_ms": int(r.avg_latency_ms),
        "weight": member_weight(db, user.id, r.provider, r.model),
    } for r in rows]


# --------------------------------------------------------------------------- #
#  Raporlar
# --------------------------------------------------------------------------- #
@router.post("/reports")
def create_report(db: Session = Depends(get_db), user: User = Depends(current_user),
                  period_days: int = Query(default=7, ge=1, le=365)) -> dict[str, Any]:
    """Yeni rapor üretir (JSON + Markdown) ve diske kaydeder."""
    result = reporting.generate(db, user, period_days=period_days)
    return {"ok": True, "files": result["files"], "markdown": result["markdown"],
            "report": result["report"]}


@router.get("/reports")
def list_reports() -> list[dict[str, Any]]:
    return reporting.list_reports()


@router.get("/reports/{date}/{run_id}")
def get_report(date: str, run_id: str) -> dict[str, Any]:
    payload = reporting.read_report(date, run_id)
    if payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rapor bulunamadı.")
    return payload


# --------------------------------------------------------------------------- #
#  MCP erişim anahtarları (Claude Code / Desktop / Cursor bağlantısı)
# --------------------------------------------------------------------------- #
@router.get("/mcp-tokens")
def mcp_tokens(db: Session = Depends(get_db),
               user: User = Depends(current_user)) -> list[dict[str, Any]]:
    """Kayıtlı MCP anahtarları (düz metin ASLA dönmez)."""
    return list_tokens(db, user)


@router.post("/mcp-tokens", status_code=status.HTTP_201_CREATED)
def create_mcp_token(payload: McpTokenIn, db: Session = Depends(get_db),
                     user: User = Depends(current_user)) -> dict[str, Any]:
    """
    Yeni MCP anahtarı üretir.

    Düz metin YALNIZCA bu yanıtta döner — kaydedilmez, tekrar gösterilemez.
    Kaybederseniz iptal edip yenisini üretin.
    """
    try:
        record, plain = create_token(db, user, payload.name, payload.days)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    from ..core.safety import audit  # noqa: PLC0415

    audit(db, user, "MCP_TOKEN_CREATE", "user", f"MCP anahtarı üretildi: {record.name}")
    db.commit()
    return {
        "ok": True,
        "id": record.id,
        "name": record.name,
        "token": plain,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "uyari": ("Bu anahtar bir daha gösterilmeyecek. Şimdi kopyalayın. "
                  "Hesabınıza tam araç erişimi verir; kimseyle paylaşmayın."),
    }


@router.delete("/mcp-tokens/{token_id}", response_model=GenericOut)
def delete_mcp_token(token_id: int, db: Session = Depends(get_db),
                     user: User = Depends(current_user)) -> GenericOut:
    """Anahtarı iptal eder — bağlı araç anında erişimini kaybeder."""
    if not revoke_token(db, user, token_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anahtar bulunamadı.")

    from ..core.safety import audit  # noqa: PLC0415

    audit(db, user, "MCP_TOKEN_REVOKE", "user", f"MCP anahtarı iptal edildi (#{token_id}).")
    db.commit()
    return GenericOut(message="Anahtar iptal edildi. Bağlı araç artık erişemez.")
