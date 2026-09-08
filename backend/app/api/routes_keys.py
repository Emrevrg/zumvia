"""
API Anahtarı (Kasa) Uçları
===========================
Anahtarlar AES ile şifreli saklanır. Hiçbir uç, düz metin anahtar DÖNDÜRMEZ.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..core.creds import public_view, read_extra, read_secrets, save_credential
from ..core.db import SessionLocal, get_db
from ..core.logging import get_logger
from ..engine.hub import hub
from ..layers import model_verify
from ..layers.l3_llm_gateway import PROVIDERS, LLMGateway, provider_catalog
from ..layers.l5_execution import LiveBroker
from ..layers.model_discovery import discover
from ..layers.notifier import send_telegram
from ..models import Credential, CredentialKind, User
from ..schemas import CredentialIn, CredentialTestIn, GenericOut, ModelPickIn
from .deps import current_user

log = get_logger("zumvia.api.keys")
router = APIRouter(prefix="/api/keys", tags=["Kasa"])


@router.get("/providers")
def providers() -> dict:
    """Desteklenen yapay zeka sağlayıcıları ve model listeleri."""
    from ..layers.l3_llm_gateway import provider_health_snapshot
    return {"llm": provider_catalog(), "health": provider_health_snapshot()}


@router.get("/providers/health")
def providers_health() -> dict:
    from ..layers.l3_llm_gateway import provider_health_snapshot
    return provider_health_snapshot()


@router.get("/{cred_id}/models")
def credential_models(cred_id: int, db: Session = Depends(get_db),
                      user: User = Depends(current_user)) -> dict:
    """
    Bu anahtarın sağlayıcısındaki GÜNCEL model listesi.

    Liste sağlayıcıdan canlı çekilir; ulaşılamazsa koddaki bilinen liste döner
    ve `source` alanı hangisinin kullanıldığını açıkça söyler. Listede olmayan
    bir modeli kullanıcı elle yazabilir — sağlayıcı yarın yenisini çıkarabilir.
    """
    cred = (db.query(Credential)
            .filter(Credential.id == cred_id, Credential.user_id == user.id).first())
    if cred is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anahtar bulunamadı.")

    secrets = read_secrets(user, cred)
    extra = read_extra(cred)
    result = discover(cred.provider, secrets.get("api_key", ""), extra.get("base_url", ""))
    result["provider"] = cred.provider
    result["selected"] = extra.get("model", "")

    # Listede olmak, çağrılabilmek demek DEĞİLDİR. Daha önce gerçekten
    # denenmiş modellerin sonucu buraya iliştirilir; hiç denenmemişler
    # "bilinmiyor" olarak kalır — tahmin edilmez.
    checks = model_verify.cached(db, cred.id)
    result["checks"] = checks
    result["verified"] = [m for m in result.get("models", [])
                          if checks.get(m, {}).get("ok")]
    result["broken"] = [m for m in result.get("models", [])
                        if m in checks and not checks[m]["ok"]]
    result["unchecked"] = [m for m in result.get("models", []) if m not in checks]
    result["tool_capable"] = [m for m in result.get("models", [])
                              if checks.get(m, {}).get("ok")
                              and checks[m].get("supports_tools")]
    return result


def _verify_in_background(user_id: int, cred_id: int, force: bool) -> None:
    """
    Modelleri tek tek gerçekten çağırarak doğrular.

    Arka planda çalışır ve her adımı WebSocket'ten yayınlar: 38 modelli bir
    sağlayıcıda tarama dakikalar sürebilir, kullanıcı boş ekrana bakmamalı.
    """
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        cred = db.get(Credential, cred_id)
        if user is None or cred is None or cred.user_id != user_id:
            return

        secrets = read_secrets(user, cred)
        extra = read_extra(cred)
        api_key = secrets.get("api_key", "")
        base_url = extra.get("base_url", "")
        found = discover(cred.provider, api_key, base_url)
        models = found.get("models", [])

        def progress(index: int, total: int, verdict: model_verify.Verdict) -> None:
            hub.publish(user_id, {
                "type": "model_verify_progress", "credential_id": cred_id,
                "index": index, "total": total, "model": verdict.model,
                "ok": verdict.ok, "code": verdict.code,
                "supports_tools": verdict.supports_tools,
            })

        hub.publish(user_id, {"type": "model_verify_start",
                              "credential_id": cred_id, "total": len(models)})

        report = model_verify.verify_all(db, cred_id, cred.provider, api_key,
                                         base_url, models, force=force,
                                         on_progress=progress)

        # Seçili model çalışmıyorsa, çalışan bir modele geçilir. Kullanıcıyı
        # bozuk bir seçimle baş başa bırakmak, doğrulamanın amacını boşa
        # çıkarırdı.
        current = extra.get("model", "")
        replacement = ""
        if report.working and current not in report.working:
            replacement = model_verify.best_model(db, cred_id, report.working)
            if replacement:
                import json as _json  # noqa: PLC0415
                extra["model"] = replacement
                cred.extra_json = _json.dumps(extra)
                db.commit()

        hub.publish(user_id, {
            "type": "model_verify_done", "credential_id": cred_id,
            **report.to_dict(), "switched_to": replacement,
        })
        log.info("anahtar %s doğrulandı: %d çalışan model", cred_id, len(report.working))
    except Exception:  # noqa: BLE001 — doğrulama çökse de kasa çalışmaya devam eder
        log.exception("model doğrulama hatası (anahtar %s)", cred_id)
        hub.publish(user_id, {"type": "model_verify_error", "credential_id": cred_id})
    finally:
        db.close()


@router.post("/{cred_id}/models/verify", response_model=GenericOut)
def verify_models(cred_id: int, background: BackgroundTasks,
                  force: bool = False,
                  db: Session = Depends(get_db),
                  user: User = Depends(current_user)) -> GenericOut:
    """
    Sağlayıcının listelediği modelleri TEK TEK gerçekten çağırarak doğrular.

    Amaç, kullanıcının çalışmayan bir modeli seçip görevini kaybetmesini
    önlemektir. Her model için 4 tokenlık bir istek gönderilir; kotadan
    kayda değer bir şey yemez. Sonuçlar kalıcı saklanır, 24 saat boyunca
    yeniden sorgulanmaz.
    """
    cred = (db.query(Credential)
            .filter(Credential.id == cred_id, Credential.user_id == user.id).first())
    if cred is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anahtar bulunamadı.")
    if cred.kind != CredentialKind.LLM:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Yalnızca yapay zeka anahtarları doğrulanabilir.")

    background.add_task(_verify_in_background, user.id, cred_id, force)
    return GenericOut(message="Modeller doğrulanıyor — sonuçlar geldikçe listelenecek.")


@router.get("/{cred_id}/models/checks")
def model_checks(cred_id: int, db: Session = Depends(get_db),
                 user: User = Depends(current_user)) -> dict:
    """Bu anahtar için bilinen doğrulama sonuçları."""
    cred = (db.query(Credential)
            .filter(Credential.id == cred_id, Credential.user_id == user.id).first())
    if cred is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anahtar bulunamadı.")
    return {"credential_id": cred_id, "checks": model_verify.cached(db, cred_id)}


@router.delete("/{cred_id}/models/checks", response_model=GenericOut)
def clear_model_checks(cred_id: int, db: Session = Depends(get_db),
                       user: User = Depends(current_user)) -> GenericOut:
    """Doğrulama geçmişini siler; sonraki tarama her modeli yeniden dener."""
    cred = (db.query(Credential)
            .filter(Credential.id == cred_id, Credential.user_id == user.id).first())
    if cred is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anahtar bulunamadı.")
    removed = model_verify.forget(db, cred_id)
    return GenericOut(message=f"{removed} kayıt silindi. Yeniden doğrulayabilirsiniz.")


@router.post("/{cred_id}/models")
def set_credential_model(cred_id: int, payload: ModelPickIn,
                         db: Session = Depends(get_db),
                         user: User = Depends(current_user)) -> dict:
    """Anahtarın varsayılan modelini değiştirir (elle yazılan ad da kabul edilir)."""
    cred = (db.query(Credential)
            .filter(Credential.id == cred_id, Credential.user_id == user.id).first())
    if cred is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anahtar bulunamadı.")

    name = payload.model.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Model adı boş olamaz.")

    import json as _json  # noqa: PLC0415
    extra = read_extra(cred)
    extra["model"] = name[:120]
    cred.extra_json = _json.dumps(extra)
    db.commit()
    return {"ok": True, "model": extra["model"]}


@router.get("")
def list_credentials(db: Session = Depends(get_db),
                     user: User = Depends(current_user)) -> list[dict]:
    creds = db.query(Credential).filter(Credential.user_id == user.id).all()
    return [public_view(c) for c in creds]


@router.post("", response_model=GenericOut, status_code=status.HTTP_201_CREATED)
def upsert_credential(payload: CredentialIn, db: Session = Depends(get_db),
                      user: User = Depends(current_user)) -> GenericOut:
    """Anahtar kaydeder veya günceller (aynı etiket = güncelleme)."""
    kind = CredentialKind(payload.kind)
    secrets = {
        k: v for k, v in {
            "api_key": payload.api_key.strip(),
            "secret": payload.secret.strip(),
            "password": payload.password.strip(),
            "token": payload.token.strip(),
            "chat_id": payload.chat_id.strip(),
        }.items() if v
    }

    if kind == CredentialKind.LLM:
        spec = PROVIDERS.get(payload.provider)
        if spec and spec.needs_key and not secrets.get("api_key"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"{spec.label} için API anahtarı zorunludur.")
    elif kind == CredentialKind.EXCHANGE and not (secrets.get("api_key") and secrets.get("secret")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Borsa için hem API anahtarı hem gizli anahtar gereklidir.")
    elif kind == CredentialKind.SOCIAL and not (secrets.get("token")
                                               or secrets.get("api_key")):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "X/Twitter için Bearer Token zorunludur. developer.x.com adresinden "
            "bir uygulama oluşturup 'Bearer Token' alanını buraya yapıştırın.")
    elif kind == CredentialKind.TELEGRAM and not (secrets.get("token") and secrets.get("chat_id")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Telegram için bot token ve chat id gereklidir.")

    extra = {
        "base_url": payload.base_url.strip(),
        "model": payload.model.strip(),
        "temperature": payload.temperature,
        "sandbox": payload.sandbox,
    }
    cred = save_credential(db, user, kind, payload.provider, payload.label, secrets, extra)
    db.commit()
    return GenericOut(message=f"{payload.label} güvenli kasaya şifrelenerek kaydedildi.",
                      data={"id": cred.id})


@router.delete("/{cred_id}", response_model=GenericOut)
def delete_credential(cred_id: int, db: Session = Depends(get_db),
                      user: User = Depends(current_user)) -> GenericOut:
    cred = db.get(Credential, cred_id)
    if cred is None or cred.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Kayıt bulunamadı.")
    db.delete(cred)
    db.commit()
    return GenericOut(message="Anahtar silindi.")


@router.post("/test", response_model=GenericOut)
def test_credential(payload: CredentialTestIn, db: Session = Depends(get_db),
                    user: User = Depends(current_user)) -> GenericOut:
    """Kaydedilmiş anahtarın gerçekten çalıştığını doğrular."""
    cred = db.get(Credential, payload.credential_id)
    if cred is None or cred.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Kayıt bulunamadı.")

    secrets = read_secrets(user, cred)
    extra = read_extra(cred)

    if cred.kind == CredentialKind.LLM:
        try:
            gateway = LLMGateway(
                provider=cred.provider, api_key=secrets.get("api_key", ""),
                model=payload.model or extra.get("model", ""),
                base_url=extra.get("base_url", ""),
            )
        except ValueError as exc:
            detail = str(exc)
            if "Model" in detail:
                detail = ("Bu anahtarda model adı tanımlı değil. Anahtarı silip "
                          "model adıyla birlikte yeniden ekleyin.")
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail) from exc
        ok, message, latency = gateway.ping()
        return GenericOut(ok=ok, message=(f"Bağlantı başarılı ({latency} ms): " + message) if ok
                          else f"Bağlantı başarısız ({latency} ms): {message}",
                          data={"latency_ms": latency})

    if cred.kind == CredentialKind.EXCHANGE:
        broker = LiveBroker(cred.provider, secrets.get("api_key", ""),
                            secrets.get("secret", ""), secrets.get("password", ""),
                            bool(extra.get("sandbox", False)))
        ok, message = broker.test_connection()
        return GenericOut(ok=ok, message=message)

    ok, message = send_telegram(
        secrets.get("token", ""), secrets.get("chat_id", ""),
        "*ZUMVIA* \nBildirim kanalı başarıyla bağlandı.\n"
        "İşlem bildirimleri buraya gelecek.",
    )
    return GenericOut(ok=ok, message="Telegram test mesajı gönderildi." if ok else message)
