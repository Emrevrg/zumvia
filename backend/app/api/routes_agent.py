"""
KOMUTA AJANI UÇLARI (Chat + Otonom Denetim)
============================================
Kullanıcı tek cümle yazar; ajan arka planda çalışır ve her adımı WebSocket
üzerinden chat ekranına yayınlar.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..agent import modes as agent_modes
from ..agent import run_lock, work_mode
from ..agent.controller import (
    align_model_to_credential,
    cli_availability,
    run_agent,
)
from ..agent.tools import ToolContext, execute_tool, tool_manifest
from ..core.config import settings
from ..core.db import SessionLocal, get_db
from ..core.desktop import detect_all, launch
from ..core.logging import get_logger
from ..layers import attachments as attach
from ..layers.playbooks import PLAYBOOKS, playbook_catalog
from ..layers.strategies import STRATEGIES
from ..models import AgentMessage, AgentSession, Attachment, ControlTool, User
from ..schemas import GenericOut
from .deps import current_user

log = get_logger("zumvia.api.agent")
router = APIRouter(prefix="/api/agent", tags=["Komuta Ajanı"])


# --------------------------------------------------------------------------- #
#  Şemalar
# --------------------------------------------------------------------------- #
class SessionIn(BaseModel):
    title: str = Field(default="Yeni Oturum", max_length=120)
    control_tool: Literal["api", "claude_code", "codex", "gemini_cli"] = "api"
    control_credential_id: int | None = None
    control_model: str = ""
    sub_credential_id: int | None = None
    sub_model: str = ""
    council_mode: Literal["auto", "solo", "council", "strict"] = "auto"
    # Calisma modu: ask (oku/anlat) | plan (yaz, uygulama) | agent (yap + otonom)
    work_mode: Literal["ask", "plan", "agent"] = "ask"
    mandate: dict[str, Any] = Field(default_factory=dict)
    heartbeat_seconds: int = Field(default=900, ge=120, le=86400)


class SessionPatch(BaseModel):
    model_config = {"extra": "ignore"}
    title: str | None = None
    control_tool: Literal["api", "claude_code", "codex", "gemini_cli"] | None = None
    control_credential_id: int | None = None
    control_model: str | None = None
    sub_credential_id: int | None = None
    sub_model: str | None = None
    council_mode: Literal["auto", "solo", "council", "strict"] | None = None
    work_mode: Literal["ask", "plan", "agent"] | None = None
    mandate: dict[str, Any] | None = None
    heartbeat_seconds: int | None = Field(default=None, ge=120, le=86400)
    # `autonomous` ARTIK AYRI BIR ALAN DEGIL: calisma modundan turer.
    #
    # Ayri bir dugme oldugunda iki celiskili durum mumkundu — "Uygula"
    # modunda otonomu kapatmak (kurdugunu izlemeyen ajan) ve "Sor" modunda
    # otonomu acmak (hicbir sey yapamayan ama surekli uyanan ajan). Ikisi de
    # kullaniciya bir sey kazandirmiyordu.


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    # Arayuzun dili: ajan kullaniciya bu dilde yanit verir.
    language: Literal["tr", "en"] = "tr"
    # "+" menusunden secilen calisma modu ve o mesaja ilistirilen dosyalar.
    mode: str = agent_modes.DEFAULT_MODE
    attachment_ids: list[int] = Field(default_factory=list, max_length=12)


class ToolCallIn(BaseModel):
    """CLI ajanlarının (Claude Code / Codex) araç çağırdığı uç."""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
#  Yardımcılar
# --------------------------------------------------------------------------- #
def _session_or_404(db: Session, user: User, session_id: int) -> AgentSession:
    session = db.get(AgentSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Oturum bulunamadı.")
    return session


def serialize_session(session: AgentSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "title": session.title,
        "control_tool": session.control_tool.value,
        "control_credential_id": session.control_credential_id,
        "control_model": session.control_model,
        "sub_credential_id": session.sub_credential_id,
        "sub_model": session.sub_model,
        "council_mode": session.council_mode or "auto",
        "work_mode": session.work_mode or work_mode.DEFAULT,
        "mandate": json.loads(session.mandate_json or "{}"),
        "autonomous": session.autonomous,
        "heartbeat_seconds": session.heartbeat_seconds,
        "status": session.status,
        "last_error": session.last_error,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "last_active_at": session.last_active_at.isoformat() if session.last_active_at else None,
    }


def serialize_message(message: AgentMessage) -> dict[str, Any]:
    def parse(raw: str) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "tool_name": message.tool_name,
        "tool_args": parse(message.tool_args_json),
        "tool_result": parse(message.tool_result_json),
        "ok": message.ok,
        "duration_ms": message.duration_ms,
        "ts": message.ts.isoformat() if message.ts else None,
    }


def _run_in_background(session_id: int, user_id: int, content: str | None,
                       autonomous: bool = False, language: str = "tr",
                       mode: str = agent_modes.DEFAULT_MODE,
                       attachment_ids: list[int] | None = None) -> None:
    """Ajanı kendi veritabanı oturumunda çalıştırır (istek bloklanmaz)."""
    db = SessionLocal()
    try:
        session = db.get(AgentSession, session_id)
        user = db.get(User, user_id)
        if session and user:
            # Bağlam BURADA kurulur, istek görevinde değil: canlı piyasa
            # bağlamı ağ çağrısı yapar ve kullanıcı "Gönder"e bastığında
            # arayüzün bunu beklemesi gerekmez.
            block = ""
            try:
                block = agent_modes.build_context(db, user, mode, attachment_ids or [])
            except Exception:  # noqa: BLE001 — bağlam kurulamazsa mesaj yine gitsin
                log.exception("bağlam kurulamadı (oturum %s)", session_id)
            run_agent(db, user, session, content, autonomous, language=language,
                      context_block=block)
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("Arka plan ajan hatası (oturum %s)", session_id)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
#  Katalog
# --------------------------------------------------------------------------- #


@router.get("/catalog")
def catalog() -> dict[str, Any]:
    """Kontrol araçları, araç listesi ve hazır görev şablonları."""
    return {
        "control_tools": [
            {"id": "api", "label": "API (doğrudan model)", "available": True,
             "desc": "Seçtiğiniz modeli araç çağırmalı ajan döngüsünde çalıştırır. "
                     "Kurulum gerektirmez, en hızlı yol."},
            *[{**c,
               "desc": {"claude_code": "Yerel Claude Code CLI ajanını komutan olarak kullanır.",
                        "codex": "Yerel Codex CLI ajanını komutan olarak kullanır.",
                        "gemini_cli": "Yerel Gemini CLI ajanını komutan olarak kullanır."}[c["id"]]}
              for c in cli_availability()],
        ],
        "tools": tool_manifest(),
        "playbooks": playbook_catalog(),
        "capabilities": {
            "strategies": len(STRATEGIES),
            "playbooks": len(PLAYBOOKS),
            "tools": len(tool_manifest()),
            "max_risk_pct": settings.hard_max_risk_pct,
        },
        "quick_missions": [
            {"id": "crypto_manage",
             "label": "Kriptoda paramı yönet",
             "prompt": "Kriptoda 1000 dolarlık sanal kasamı yönetmeni istiyorum. "
                       "Uygun pariteyi sen seç, kanıtını geri testle topla, botu kur, "
                       "çalıştır ve bana kısa bir özet ver."},
            {"id": "audit",
             "label": "Sistemi denetle ve düzelt",
             "prompt": "Tüm sistemi denetle: kilitli, hatalı veya donmuş bot var mı, "
                       "açık pozisyonların tezi hâlâ geçerli mi? Sorunları kendin düzelt "
                       "ve bana ne yaptığını anlat."},
            {"id": "opportunity",
             "label": "Bugün fırsat var mı?",
             "prompt": "Bugün piyasada işlem açmaya değer net bir kurulum var mı? "
                       "Veriyi ve algoritmik konsensüsü kontrol et, haberlere bak. "
                       "Yoksa açıkça 'yok' de — zorlama işlem isteme."},
            {"id": "explain",
             "label": "Durumumu sade anlat",
             "prompt": "Portföyümün durumunu finans bilmeyen birine anlatır gibi özetle: "
                       "ne kadar param var, ne kazandım/kaybettim, şu an ne yapıyorsun?"},
        ],
    }


# --------------------------------------------------------------------------- #
#  Oturumlar
# --------------------------------------------------------------------------- #
@router.get("/desktop-tools")
def desktop_tools() -> dict[str, Any]:
    """Bu makinede kurulu ajan/editör araçları (Claude Desktop, Codex, Cursor…)."""
    rows = detect_all()
    return {
        "tools": rows,
        "installed": sum(1 for r in rows if r["available"]),
        "mcp_note": "MCP anahtarınızı Kontrol merkezi → Bağlantılar bölümünden alın; "
                    "işaretli araçlar platforma doğrudan bağlanabilir.",
    }


@router.post("/desktop-tools/{tool_id}/launch")
def launch_desktop_tool(tool_id: str,
                        user: User = Depends(current_user)) -> dict[str, Any]:  # noqa: ARG001
    """Kurulu bir aracı başlatır (yalnızca kayıt defterindekiler)."""
    result = launch(tool_id)
    if not result.get("launched"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, result.get("error", "Başlatılamadı"))
    return result


@router.get("/sessions")
def list_sessions(db: Session = Depends(get_db),
                  user: User = Depends(current_user)) -> list[dict[str, Any]]:
    rows = (db.query(AgentSession)
            .filter(AgentSession.user_id == user.id)
            .order_by(desc(AgentSession.id)).all())
    return [serialize_session(s) for s in rows]


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
def create_session(payload: SessionIn, db: Session = Depends(get_db),
                   user: User = Depends(current_user)) -> dict[str, Any]:
    session = AgentSession(
        user_id=user.id, title=payload.title,
        control_tool=ControlTool(payload.control_tool),
        control_credential_id=payload.control_credential_id,
        control_model=payload.control_model,
        sub_credential_id=payload.sub_credential_id,
        sub_model=payload.sub_model,
        council_mode=payload.council_mode,
        work_mode=payload.work_mode,
        mandate_json=json.dumps(payload.mandate, ensure_ascii=False),
        # Otonomluk moddan TÜRER, ayrıca istenmez: "Uygula" modundaki bir ajan
        # kurduğunu izler, "Sor" modundaki bir ajanın uyanmasının anlamı yok.
        autonomous=work_mode.resolve(payload.work_mode).autonomous,
        heartbeat_seconds=payload.heartbeat_seconds,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    if session.autonomous:
        from ..engine import scheduler as sched  # noqa: PLC0415
        sched.start_agent_job(session.id, session.heartbeat_seconds)
    return serialize_session(session)


@router.get("/sessions/{session_id}")
def get_session(session_id: int, db: Session = Depends(get_db),
                user: User = Depends(current_user)) -> dict[str, Any]:
    return serialize_session(_session_or_404(db, user, session_id))


@router.patch("/sessions/{session_id}")
def update_session(session_id: int, payload: SessionPatch,
                   db: Session = Depends(get_db),
                   user: User = Depends(current_user)) -> dict[str, Any]:
    from ..engine import scheduler as sched  # noqa: PLC0415

    session = _session_or_404(db, user, session_id)
    data = payload.model_dump(exclude_unset=True)

    if "mandate" in data and data["mandate"] is not None:
        session.mandate_json = json.dumps(data.pop("mandate"), ensure_ascii=False)
    if data.get("control_tool"):
        session.control_tool = ControlTool(data.pop("control_tool"))

    credential_changed = ("control_credential_id" in data
                          and data["control_credential_id"] != session.control_credential_id)
    model_given = bool(data.get("control_model"))

    for key, value in data.items():
        if value is not None:
            setattr(session, key, value)

    # Sağlayıcı değiştiyse ve model ayrıca verilmediyse, model yeni sağlayıcının
    # modeline çekilir. Aksi hâlde oturum, o sağlayıcıda var olmayan bir modeli
    # çağırıp her turda hata verirdi.
    if credential_changed:
        align_model_to_credential(db, user, session, model_given)

    # Mod değiştiyse otonomluk onunla birlikte gelir/gider.
    session.autonomous = work_mode.resolve(session.work_mode).autonomous

    db.commit()
    db.refresh(session)

    if session.autonomous:
        sched.start_agent_job(session.id, session.heartbeat_seconds)
    else:
        sched.stop_agent_job(session.id)
    return serialize_session(session)


@router.delete("/sessions/{session_id}", response_model=GenericOut)
def delete_session(session_id: int, db: Session = Depends(get_db),
                   user: User = Depends(current_user)) -> GenericOut:
    from ..engine import scheduler as sched  # noqa: PLC0415

    session = _session_or_404(db, user, session_id)
    sched.stop_agent_job(session.id)
    db.delete(session)
    db.commit()
    return GenericOut(message="Oturum silindi.")


# --------------------------------------------------------------------------- #
#  Sohbet
# --------------------------------------------------------------------------- #
@router.get("/sessions/{session_id}/messages")
def messages(session_id: int, db: Session = Depends(get_db),
             user: User = Depends(current_user),
             limit: int = Query(default=200, le=1000)) -> list[dict[str, Any]]:
    _session_or_404(db, user, session_id)
    rows = (db.query(AgentMessage)
            .filter(AgentMessage.session_id == session_id)
            .order_by(desc(AgentMessage.id)).limit(limit).all())
    return [serialize_message(m) for m in reversed(rows)]


@router.post("/sessions/{session_id}/messages", response_model=GenericOut)
def send_message(session_id: int, payload: MessageIn, background: BackgroundTasks,
                 db: Session = Depends(get_db),
                 user: User = Depends(current_user)) -> GenericOut:
    """Mesajı alır ve ajanı arka planda çalıştırır; adımlar WebSocket'ten akar."""
    session = _session_or_404(db, user, session_id)
    # Veritabanındaki "çalışıyor" bayrağına DEĞİL, gerçekten çalışan tura bak.
    # Süreç çöktüğünde bayrak takılı kalır ve oturum bir daha hiç mesaj kabul
    # etmezdi; kilit ise süreçle birlikte gider, yeniden başlatma iyileştirir.
    if run_lock.is_running(session.id):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Bu görev şu anda çalışıyor. Bitmesini bekleyin "
                            "ya da Durdur'a basın.")
    background.add_task(_run_in_background, session.id, user.id, payload.content,
                        False, payload.language, payload.mode,
                        list(payload.attachment_ids))
    return GenericOut(message="Ajan çalışmaya başladı.")


@router.post("/sessions/{session_id}/stop", response_model=GenericOut)
def stop_session(session_id: int, db: Session = Depends(get_db),
                 user: User = Depends(current_user)) -> GenericOut:
    """
    Çalışan ajan turunu durdurur.

    Bu YALNIZCA ajanın düşünme/araç turunu keser. Açık pozisyonlara,
    çalışan botlara ve zamanlayıcıya dokunmaz — onları durdurmak için
    acil fren (kill switch) ya da botu durdurmak gerekir.
    """
    session = _session_or_404(db, user, session_id)

    # Asıl durdurma burada olur: döngü her adımda bu bayrağa bakar ve turu
    # temiz kapatır. Yalnızca durum alanını değiştirmek yetmezdi — çalışan
    # döngü bir sonraki adımda üzerine yazardı.
    was_running = run_lock.cancel(session.id)

    if not was_running:
        # Çalışan tur yok; takılı kalmış bir durum bayrağı varsa temizle.
        session.status = "idle"
        session.last_error = ""
        db.commit()
        return GenericOut(message="Çalışan tur yoktu; durum sıfırlandı.")

    return GenericOut(message="Durduruluyor — ajan bulunduğu adımı bitirip duracak.")


@router.post("/sessions/{session_id}/tick", response_model=GenericOut)
def manual_tick(session_id: int, background: BackgroundTasks,
                db: Session = Depends(get_db),
                user: User = Depends(current_user)) -> GenericOut:
    """Otonom denetim turunu elle tetikler."""
    session = _session_or_404(db, user, session_id)
    if run_lock.is_running(session.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Bu görev şu anda çalışıyor.")
    background.add_task(_run_in_background, session.id, user.id, None, True)
    return GenericOut(message="Denetim turu başlatıldı.")


# --------------------------------------------------------------------------- #
#  Doğrudan araç çağrısı (CLI ajanları için)
# --------------------------------------------------------------------------- #
@router.post("/tool")
def call_tool(payload: ToolCallIn, db: Session = Depends(get_db),
              user: User = Depends(current_user)) -> dict[str, Any]:
    """
    Yerel CLI ajanlarının (Claude Code, Codex, Gemini CLI) platform araçlarını
    HTTP üzerinden çağırdığı uç. Aynı risk kalkanı ve tavanlar geçerlidir.
    """
    ctx = ToolContext(db=db, user=user)
    result = execute_tool(ctx, payload.name, payload.arguments)
    return {"tool": payload.name, "result": result}


# --------------------------------------------------------------------------- #
#  "+" menüsü: modlar
# --------------------------------------------------------------------------- #

@router.get("/modes")
def list_modes() -> dict[str, Any]:
    """
    Yazı kutusundaki '+' menüsü ve çalışma modu seçicisi.

    İki ayrı liste döner ve ayrım bilinçlidir:
      `work_modes` — ajan ne YAPABİLİR (Sor / Planla / Uygula)
      `modes`      — bu mesaj hangi İŞ için (portföy, araştırma, dosya…)
    """
    return {
        "work_modes": work_mode.catalog(),
        "work_default": work_mode.DEFAULT,
        "modes": agent_modes.catalog(),
        "default": agent_modes.DEFAULT_MODE,
        "upload": {
            "max_bytes": attach.MAX_BYTES,
            "max_chars": attach.MAX_CHARS,
            "max_per_session": attach.MAX_PER_SESSION,
            "extensions": sorted(attach.KINDS),
        },
    }


# --------------------------------------------------------------------------- #
#  Dosya ekleri
# --------------------------------------------------------------------------- #

@router.get("/sessions/{session_id}/attachments")
def list_attachments(session_id: int, db: Session = Depends(get_db),
                     user: User = Depends(current_user)) -> dict[str, Any]:
    _session_or_404(db, user, session_id)
    rows = (db.query(Attachment)
            .filter(Attachment.session_id == session_id,
                    Attachment.user_id == user.id)
            .order_by(Attachment.id).all())
    return {"items": [_serialize_attachment(r) for r in rows], "count": len(rows)}


def _serialize_attachment(row: Attachment) -> dict[str, Any]:
    """Ham içerik DÖNMEZ: ekranın ihtiyacı önizleme, dosyanın tamamı değil."""
    return {
        "id": row.id, "filename": row.filename, "kind": row.kind,
        "size_bytes": row.size_bytes, "chars": row.chars,
        "truncated": row.truncated, "summary": row.summary,
        "preview": attach.preview(row.content),
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


@router.post("/sessions/{session_id}/attachments",
             status_code=status.HTTP_201_CREATED)
async def upload_attachment(session_id: int, file: UploadFile = File(...),
                            db: Session = Depends(get_db),
                            user: User = Depends(current_user)) -> dict[str, Any]:
    """
    Dosyayı okur, METNİNİ çıkarır ve kaydeder.

    Ham dosya diske yazılmaz — ajanın kullanabildiği şey metindir ve
    kullanıcının hesap ekstresi sunucuda serbest bir dosya olarak durmamalı.
    """
    _session_or_404(db, user, session_id)

    count = (db.query(Attachment)
             .filter(Attachment.session_id == session_id,
                     Attachment.user_id == user.id).count())
    if count >= attach.MAX_PER_SESSION:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Bir görevde en fazla {attach.MAX_PER_SESSION} dosya olabilir.")

    raw = await file.read()
    try:
        extracted = attach.extract(file.filename or "", raw)
    except attach.AttachmentError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    row = Attachment(
        user_id=user.id, session_id=session_id,
        filename=(file.filename or "adsiz")[:200], kind=extracted.kind,
        size_bytes=len(raw), chars=extracted.chars,
        truncated=extracted.truncated, content=extracted.text,
        summary=extracted.summary,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    log.info("ek yüklendi: %s (%s, %d karakter)", row.filename, row.kind, row.chars)
    return {**_serialize_attachment(row), "warnings": extracted.warnings}


@router.delete("/attachments/{attachment_id}", response_model=GenericOut)
def delete_attachment(attachment_id: int, db: Session = Depends(get_db),
                      user: User = Depends(current_user)) -> GenericOut:
    row = (db.query(Attachment)
           .filter(Attachment.id == attachment_id,
                   Attachment.user_id == user.id).first())
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ek bulunamadı.")
    db.delete(row)
    db.commit()
    return GenericOut(message="Ek silindi.")
