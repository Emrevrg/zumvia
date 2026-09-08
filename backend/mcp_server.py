#!/usr/bin/env python
"""
ZUMVIA — MCP SUNUCUSU
===============================
Bu sunucu, platformun tüm ticaret araçlarını **Model Context Protocol** üzerinden
dışarı açar. Claude Code, Codex, Cursor veya MCP destekleyen herhangi bir ajana
bağladığınızda o ajan bu platformun komutanına dönüşür — kendi arayüzümüzdeki
komuta ajanıyla birebir aynı yetkilere ve **aynı risk sınırlarına** sahip olur.

Kurulum (Claude Code):
    claude mcp add zumvia -- python C:/.../backend/mcp_server.py

veya projenizin `.mcp.json` dosyasına:
    {
      "mcpServers": {
        "zumvia": {
          "command": "python",
          "args": ["C:/.../backend/mcp_server.py"],
          "env": { "VQ_MCP_USER": "siz@ornek.com" }
        }
      }
    }

Notlar:
  * Taşıma katmanı stdio, protokol JSON-RPC 2.0'dır; harici bağımlılık yoktur.
  * Kullanıcı `VQ_MCP_USER` ile seçilir; boşsa veritabanındaki ilk kullanıcı.
  * Risk kalkanı, tavanlar ve paper→live yasağı burada da geçerlidir:
    MCP istemcisi bunları AŞAMAZ (bkz. app/agent/tools.py, app/layers/l4_risk.py).
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.agent.tools import REGISTRY, ToolContext, execute_tool  # noqa: E402
from app.core.db import SessionLocal, init_db  # noqa: E402
from app.models import User  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "zumvia", "version": "1.0.0"}

INSTRUCTIONS = """ZUMVIA — otonom ticaret komuta araçları.

Kullanıcının sermayesini yöneten bir portföy yöneticisi gibi davran:
1. `get_portfolio` ve `system_health` ile durumu oku.
2. `get_market_snapshot` ile KESİN sayıları al (asla gösterge/fiyat uydurma).
3. `run_strategy_engine` ile bağımsız algoritmik oyu al ve kendi görüşünle
   ÇAPRAZ DOĞRULA. Çelişki varsa işlem açma.
4. Sert hareket varsa `get_news` ile sebebini ara.
5. Yeni kurulum için `run_backtest` ile kanıt topla (PF < 1.3 ise kullanma).
6. `create_bot` / `control_bot` ile kur ve çalıştır; `run_bot_cycle` ile denetle.

Değiştirilemez sınırlar (kodda zorlanır):
- Tek işlemde kasa riski en fazla %{max_risk}, stop-loss zorunlu,
  R/R en az 1:{min_rr}.
- Günlük %{max_daily} kayıpta devre kesici botu kilitler.
- Kasa, başlangıç sermayesinin %{floor}'inin altına inerse sistem her şeyi
  kapatır: buradan sonrası toparlanma değil, erimedir.
- Stop hiç çalışmasa bile kullanıcı BORÇLANAMAZ: her pozisyon sert bir ters
  hareket varsayımıyla sınanır, geçemezse açılmaz.
- Paper (sanal) moddan gerçek paraya geçişi YALNIZCA kullanıcı panelden yapar.
- Zarar sonrası riski artırma; telafi daha büyük risk değil, daha seçici olmaktır.
"""

# Sınırlar TEK KAYNAKTAN okunur.
#
# Eskiden bu metin sabit sayılar içeriyordu (%1.5 risk, %3 günlük) ve
# yapılandırma sıkılaştırıldığında geride kaldı: MCP istemcisi gerçekte
# uygulanandan gevşek sınırlar duyuyordu. Bir modele yanlış sınır söylemek,
# ona o sınıra kadar gidebileceğini söylemektir.
def _instructions() -> str:
    from app.core.config import settings  # noqa: PLC0415
    from app.layers.solvency import ACCOUNT_FLOOR_PCT  # noqa: PLC0415

    return INSTRUCTIONS.format(
        max_risk=settings.hard_max_risk_pct,
        min_rr=settings.hard_min_rr_ratio,
        max_daily=settings.hard_daily_loss_limit_pct,
        floor=ACCOUNT_FLOOR_PCT,
    )


def _resolve_user(db) -> User | None:
    """Kullanici cozumleme: MCP anahtari > e-posta > tek kullanici."""
    token = os.environ.get("VQ_MCP_TOKEN", "").strip()
    if token:
        from app.core.mcp_auth import resolve_token  # noqa: PLC0415

        user = resolve_token(db, token)
        if user is not None:
            db.commit()
            return user

    email = os.environ.get("VQ_MCP_USER", "").strip().lower()
    if email:
        return db.query(User).filter(User.email == email).first()
    return db.query(User).order_by(User.id).first()


def _mcp_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.parameters,
            "annotations": {
                "readOnlyHint": not tool.mutating,
                "destructiveHint": tool.name in ("close_position", "control_bot"),
            },
        }
        for tool in REGISTRY.values()
    ]


def _text_content(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False, indent=1, default=str)}]}


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    """Tek bir JSON-RPC isteğini işler. Bildirimler için None döner."""
    method = request.get("method", "")
    request_id = request.get("id")
    params = request.get("params") or {}

    def ok(result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def err(code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": _instructions(),
        })

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return ok({})

    if method == "tools/list":
        return ok({"tools": _mcp_tools()})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if name not in REGISTRY:
            return err(-32602, f"Bilinmeyen araç: {name}")

        db = SessionLocal()
        try:
            user = _resolve_user(db)
            if user is None:
                return ok({
                    **_text_content({
                        "error": "Kullanıcı bulunamadı. Önce web arayüzünden bir hesap "
                                 "oluşturun veya VQ_MCP_USER ortam değişkenini ayarlayın.",
                    }),
                    "isError": True,
                })
            result = execute_tool(ToolContext(db=db, user=user), name, arguments)
            payload = _text_content(result)
            if isinstance(result, dict) and "error" in result:
                payload["isError"] = True
            return ok(payload)
        except Exception as exc:  # noqa: BLE001
            return ok({**_text_content({"error": f"{type(exc).__name__}: {exc}"}),
                       "isError": True})
        finally:
            db.close()

    if method in ("resources/list", "prompts/list"):
        return ok({"resources": [], "prompts": []})

    return err(-32601, f"Desteklenmeyen metot: {method}")


def _force_utf8() -> None:
    """
    Boru hattini UTF-8'e sabitler.

    OLCULDU: Windows'ta Python'in stdout kodlamasi sistem kod sayfasidir
    (Turkce kurulumda cp1254). Arac aciklamalari ok isareti ve Turkce harf
    iceriyor; `tools/list` yaniti yazilirken UnicodeEncodeError ile SUREC
    COKUYORDU. Yani Claude Code Desktop ya da Codex Desktop bu sunucuya
    baglandigi anda, daha ilk arac listesinde kopuyordu.

    MCP protokolu zaten UTF-8 zorunlu kilar; burada yapilan, yerel
    varsayilanin protokolu bozmasini onlemek.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            # Yeniden yapilandirilamayan akis (test kosumu, yonlendirme):
            # `_write` icindeki ASCII kacis yedegi devreye girer.
            pass


def main() -> None:
    _force_utf8()
    init_db()
    print(f"[zumvia-mcp] hazır · {len(REGISTRY)} araç", file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        try:
            response = handle(request)
        except Exception:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            response = {"jsonrpc": "2.0", "id": request.get("id"),
                        "error": {"code": -32603, "message": "İç sunucu hatası"}}

        if response is not None:
            _write(response)


def _write(response: dict) -> None:
    """
    Yanıtı yazar; kodlama tutmazsa ASCII kaçışına düşer.

    Bir bağlantıyı kodlama yüzünden kaybetmektense Türkçe harfleri ASCII
    kaçışıyla göndermek yeğdir: JSON tarafında ikisi de aynı dizedir ve
    istemci doğru metni görür.
    """
    try:
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    except UnicodeEncodeError:
        sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
