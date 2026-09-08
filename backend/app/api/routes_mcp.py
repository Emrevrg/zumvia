"""
MCP HTTP SUNUCUSU (Streamable HTTP)
====================================
`mcp_server.py` stdio üzerinden çalışır ve terminal tabanlı ajanlar (Claude Code
CLI, Codex) için idealdir. Masaüstü uygulamaları ise genellikle **URL ile**
bağlanır — bu modül tam olarak onun içindir.

Bağlantı:
    http://127.0.0.1:8000/mcp        (Authorization: Bearer <token>)

Claude Code / Claude Desktop / Cursor gibi istemciler bu adresi "remote MCP
server" olarak ekleyebilir. Protokol JSON-RPC 2.0'dır; aynı 27 araç, aynı risk
kalkanı, aynı sınırlar geçerlidir.

Kimlik doğrulama üç yoldan biriyle yapılır:
  1. `Authorization: Bearer <jwt>`  — panelden alınan erişim anahtarı
  2. `?token=<jwt>` sorgu parametresi (bazı istemciler başlık gönderemez)
  3. Yerel ağdan tek kullanıcılı kurulumda `VQ_MCP_USER` ortam değişkeni
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..agent.tools import REGISTRY, ToolContext, execute_tool
from ..core.db import SessionLocal
from ..core.logging import get_logger
from ..core.mcp_auth import resolve_token
from ..core.security import decode_access_token
from ..models import User

log = get_logger("zumvia.mcp.http")
router = APIRouter(tags=["MCP"])

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "zumvia", "version": "1.0.0"}

INSTRUCTIONS = """ZUMVIA — otonom ticaret komuta araçları.

Kullanıcının sermayesini yöneten bir portföy yöneticisi gibi davran:
1. `get_portfolio`, `get_portfolio_risk` ve `get_safety_status` ile durumu oku.
2. `scan_markets` ile onlarca pariteyi aynı anda tara, en net kurulumu bul.
3. `get_market_snapshot` ile KESİN sayıları al (asla gösterge/fiyat uydurma).
4. `run_strategy_engine` ile bağımsız algoritmik oyu al ve ÇAPRAZ DOĞRULA.
   Çelişki varsa işlem açma.
5. `optimize_setup` + `validate_strategy` ile görülmemiş veride kanıt topla.
   Aşırı uyum farkı 0.6 üzerindeyse o yapılandırmayı kullanma.
6. `create_bot` / `control_bot` ile kur ve çalıştır; `run_bot_cycle` ile denetle.

Değiştirilemez sınırlar (kodda zorlanır):
- Tek işlemde kasa riski en fazla %1.5, stop-loss zorunlu, R/R en az 1:2.
- Portföy ısısı tavanı ve korelasyon kalkanı aşılamaz.
- Günlük %3 kayıpta devre kesici botu 24 saat kilitler.
- Gerçek para yetkisini yalnızca kullanıcı panelden verir; sen veremezsin.
- Zarar sonrası riski artırma; sistem drawdown'da riski kendisi küçültür."""


def _resolve_user(request: Request, db: Session) -> User | None:
    """Bearer başlığı, sorgu parametresi veya yerel ortam değişkeniyle kullanıcı."""
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    token = token or request.query_params.get("token", "")

    if token:
        # 1) Kalıcı MCP anahtarı (önerilen yol — iptal edilebilir)
        user = resolve_token(db, token)
        if user is not None:
            db.commit()
            return user

        # 2) Tarayıcı oturum anahtarı (geçici çözüm)
        try:
            payload = decode_access_token(token)
            user = db.get(User, int(payload.get("sub", 0)))
            if user and user.is_active:
                return user
        except Exception:  # noqa: BLE001 — geçersiz anahtar sessizce reddedilir
            return None

    email = os.environ.get("VQ_MCP_USER", "").strip().lower()
    if email:
        return db.query(User).filter(User.email == email).first()
    return None


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.parameters,
            "annotations": {
                "readOnlyHint": not tool.mutating,
                "destructiveHint": tool.name in ("close_position", "control_bot",
                                                 "activate_kill_switch"),
            },
        }
        for tool in REGISTRY.values()
    ]


def _text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False,
                                            indent=1, default=str)}]}


def _handle(request_body: dict[str, Any], request: Request) -> dict[str, Any] | None:
    """Tek bir JSON-RPC isteğini işler. Bildirimler için None döner."""
    method = request_body.get("method", "")
    request_id = request_body.get("id")
    params = request_body.get("params") or {}

    def ok(result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def err(code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })

    if method.startswith("notifications/"):
        return None

    if method == "ping":
        return ok({})

    if method == "tools/list":
        return ok({"tools": _tools()})

    if method in ("resources/list", "prompts/list"):
        return ok({"resources": [], "prompts": []})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if name not in REGISTRY:
            return err(-32602, f"Bilinmeyen araç: {name}")

        db = SessionLocal()
        try:
            user = _resolve_user(request, db)
            if user is None:
                return ok({**_text({
                    "error": "Kimlik doğrulanamadı. İstemcinize panelden aldığınız "
                             "erişim anahtarını 'Authorization: Bearer <token>' "
                             "başlığıyla ekleyin.",
                }), "isError": True})

            result = execute_tool(ToolContext(db=db, user=user), name, arguments)
            payload = _text(result)
            if isinstance(result, dict) and "error" in result:
                payload["isError"] = True
            return ok(payload)
        except Exception as exc:  # noqa: BLE001
            log.exception("MCP HTTP araç hatası: %s", name)
            return ok({**_text({"error": f"{type(exc).__name__}: {exc}"}),
                       "isError": True})
        finally:
            db.close()

    return err(-32601, f"Desteklenmeyen metot: {method}")


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    """
    MCP JSON-RPC uç noktası (Streamable HTTP).
    Tek istek veya toplu (batch) istek kabul eder.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "Geçersiz JSON"}},
            status_code=400,
        )

    if isinstance(body, list):
        responses = [r for r in (_handle(item, request) for item in body) if r is not None]
        return JSONResponse(responses) if responses else Response(status_code=202)

    response = _handle(body, request)
    if response is None:
        return Response(status_code=202)          # bildirim: gövde yok
    return JSONResponse(response)


@router.get("/mcp")
async def mcp_info(request: Request) -> JSONResponse:
    """Tarayıcıdan açıldığında bağlantı talimatı gösterir."""
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "server": SERVER_INFO,
        "protocol": PROTOCOL_VERSION,
        "transport": "streamable-http",
        "endpoint": f"{base}/mcp",
        "stdio_path": (Path(__file__).resolve().parents[2] / "mcp_server.py").as_posix(),
        "tools": len(REGISTRY),
        "authentication": "Authorization: Bearer vq_… (Kontrol merkezi → Bağlantılar)",
        "nasil_baglanir": {
            "claude_code_cli": f"claude mcp add --transport http zumvia {base}/mcp "
                               "--header \"Authorization: Bearer <TOKEN>\"",
            "masaustu_uygulamalari": "Ayarlar → Connectors / MCP → Add custom server → "
                                     f"{base}/mcp",
            "stdio_alternatifi": (
                f'claude mcp add zumvia -- python "'
                f'{(Path(__file__).resolve().parents[2] / "mcp_server.py").as_posix()}"'
            ),
        },
        "not": "Aynı 27 araç, aynı risk kalkanı. Gerçek para yetkisi yalnızca "
               "panelden verilir.",
    })
