"""
KATMAN 5 — BİLDİRİM MOTORU (Telegram)
======================================
Her açılış, kapanış, stop tetiklenmesi ve devre kesici olayında telefona
zengin Markdown bildirimi gider. Bildirim hatası ASLA ticareti etkilemez.
"""
from __future__ import annotations

from typing import Any

import httpx

from ..core.logging import get_logger

log = get_logger("zumvia.notifier")

_API = "https://api.telegram.org/bot{token}/sendMessage"


def _esc(text: Any) -> str:
    """Telegram MarkdownV1 için kaçış (basit ve güvenli)."""
    return str(text).replace("_", " ").replace("*", "").replace("`", "'").replace("[", "(")


def send_telegram(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """Mesaj gönderir. Başarısızlık sessizce raporlanır, istisna fırlatmaz."""
    if not token or not chat_id:
        return False, "Telegram yapılandırılmamış."
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                _API.format(token=token),
                json={
                    "chat_id": chat_id,
                    "text": text[:4000],
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:160]}"
        return True, "Gönderildi."
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram bildirimi gönderilemedi: %s", exc)
        return False, str(exc)[:200]


# --------------------------------------------------------------------------- #
#  Mesaj şablonları
# --------------------------------------------------------------------------- #


def fmt_open(bot_name: str, symbol: str, side: str, mode: str, entry: float,
             sl: float, tp: float, qty: float, risk_amount: float, rr: float,
             confidence: float, reasoning: str, source: str) -> str:
    arrow = "LONG" if side == "long" else "SHORT"
    badge = "SANAL" if mode == "paper" else "CANLI"
    return (
        f"*ZUMVIA* · {badge}\n"
        f"*{arrow} POZİSYON AÇILDI*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Bot: `{_esc(bot_name)}`\n"
        f"Parite: `{_esc(symbol)}`\n"
        f"Giriş: `{entry:.6g}`\n"
        f"Stop-Loss: `{sl:.6g}`\n"
        f"Take-Profit: `{tp:.6g}`\n"
        f"Miktar: `{qty:.8g}`\n"
        f"Risk: `{risk_amount:.2f}` · R/R `1:{rr:.2f}`\n"
        f"Güven: `%{confidence * 100:.0f}` · Kaynak: `{_esc(source)}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{_esc(reasoning)[:400]}"
    )


def fmt_close(bot_name: str, symbol: str, side: str, mode: str, entry: float,
              exit_price: float, pnl: float, pnl_pct: float, r_multiple: float,
              reason: str, balance: float) -> str:
    win = pnl >= 0
    head = "KÂR ALINDI" if win else "ZARAR KESİLDİ"
    badge = "SANAL" if mode == "paper" else "CANLI"
    reason_tr = {
        "TAKE_PROFIT": "Hedef fiyata ulaşıldı",
        "STOP_LOSS": "Stop-loss tetiklendi",
        "AI_CLOSE": "Yapay zeka tezi bozuldu, erken çıkış",
        "CIRCUIT_BREAKER": "Devre kesici — acil kapanış",
        "MANUAL": "Kullanıcı tarafından kapatıldı",
        "TRAILING_STOP": "İz süren stop tetiklendi",
    }.get(reason, reason)
    return (
        f"*ZUMVIA* · {badge}\n"
        f"*{head}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Bot: `{_esc(bot_name)}`\n"
        f"Parite: `{_esc(symbol)}` ({side.upper()})\n"
        f"Giriş - Çıkış: `{entry:.6g}` - `{exit_price:.6g}`\n"
        f"PnL: `{pnl:+.2f}` (`{pnl_pct:+.2f}%`) · `{r_multiple:+.2f}R`\n"
        f"Yeni Bakiye: `{balance:.2f}`\n"
        f"Sebep: {_esc(reason_tr)}"
    )


def fmt_circuit_breaker(bot_name: str, message: str, equity: float, hours: int) -> str:
    return (
        f"*ZUMVIA* · *DEVRE KESİCİ*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Bot: `{_esc(bot_name)}`\n"
        f"{_esc(message)}\n"
        f"Güncel Kasa: `{equity:.2f}`\n"
        f"⏳ Kilit süresi: `{hours} saat`\n\n"
        f"_Sermaye koruma kuralı devreye girdi. Bu, sistemin doğru çalıştığının işaretidir._"
    )


def fmt_recovery(bot_name: str, message: str) -> str:
    return (
        f"*ZUMVIA* · *TOPARLANMA MODU*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Bot: `{_esc(bot_name)}`\n"
        f"{_esc(message)}"
    )


def fmt_signal(bot_name: str, symbol: str, action: str, confidence: float,
               reasoning: str) -> str:
    return (
        f"*ZUMVIA* · *ONAY BEKLEYEN SİNYAL*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Bot: `{_esc(bot_name)}` · `{_esc(symbol)}`\n"
        f"Karar: *{_esc(action)}* · Güven `%{confidence * 100:.0f}`\n"
        f"{_esc(reasoning)[:400]}\n\n"
        f"_Panelden onaylayana kadar işlem açılmayacak._"
    )
