"""
CANLI FORWARD GÖZLEM — gerçek borsa + sanal para (salt-okunur)
==============================================================
8 saat standardının koşucusu: gerçek Binance public verisiyle, sanal 100 USD
ile, tur tur ölçüm yapar ve her turu JSONL'ye yazar.

Güvenceler (pazarlığa kapalı):

    * YALNIZCA public veri okunur; API anahtarı kullanılmaz, EMİR İLETİLMEZ,
      veritabanına YAZILMAZ. Bu betik ne paper ne canlı pozisyon açar.
    * Pozisyon taşınmaz: her tur bağımsız ölçüm + dry-run boyutlandırmadır.
      Turlar arası "sanal kâr" biriktirilmez — biriktirilseydi, hiç açılmamış
      pozisyonların hayali PnL'si olurdu.
    * Kill-switch açıksa koşu durur (koruma, gözlemden önce gelir).
    * Kesinti satırı da loga yazılır; boşluklar raporda görünür, gizlenmez.

Kullanım (backend dizininden):

    python tools/forward_watch.py --minutes 480 --interval 300

Çıktı: reports/forward_watch.jsonl (git'e girmez; kanıt dosyasıdır).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_BALANCE = 100.0
DEFAULT_SYMBOL = "BTC/USDT"


def observe_once(*, market: str = "crypto", symbol: str = DEFAULT_SYMBOL,
                 timeframe: str = "1h", limit: int = 1000,
                 balance: float = DEFAULT_BALANCE,
                 now: datetime | None = None) -> dict[str, Any]:
    """
    Tek gözlem turu: canlı mum + kotasyon çekilir, paper değerlendirmesi
    enjekte sağlayıcıyla koşar. Ağ dışı her şey parametredir (testler ağı
    buradan kapatır); gerçek koşuda sağlayıcılar l1_market_data'dır.
    """
    from app.core.safety import kill_switch_active  # noqa: PLC0415
    from app.engine.paper_evaluation import run_paper_evaluation  # noqa: PLC0415
    from app.layers.l1_market_data import fetch_ohlcv, fetch_quote  # noqa: PLC0415

    ts = now or datetime.now(UTC)
    if kill_switch_active():
        return {"ts": ts.isoformat(), "mode": "paper", "balance": balance,
                "market": market, "symbol": symbol, "timeframe": timeframe,
                "stopped": True,
                "reason": "Acil fren açık; gözlem durdu (koruma önce gelir)."}

    try:
        df = fetch_ohlcv(market, "binance", symbol, timeframe, limit)
        quote = fetch_quote(market, "binance", symbol)
    except Exception as exc:  # noqa: BLE001 — kesinti de kanıttır
        return {"ts": ts.isoformat(), "mode": "paper", "balance": balance,
                "market": market, "symbol": symbol, "timeframe": timeframe,
                "stopped": False, "fetch_ok": False,
                "reason": f"Canlı veri alınamadı: {exc!s}"[:200]}

    price = float(quote.price) if quote.price else None
    report = run_paper_evaluation(
        market=market, symbol=symbol, timeframe=timeframe,
        starting_balance=balance,
        fetch_ohlcv=lambda m, s, tf: df,
        fetch_quote=lambda m, s: {"price": price, "bid": quote.bid,
                                  "ask": quote.ask, "ts": quote.ts},
        now=ts,
    )
    backtest = report.get("backtest") or {}
    metrics = backtest.get("metrics", backtest) if isinstance(backtest, dict) else {}
    order = report.get("paper_order") or {}
    return {
        "ts": ts.isoformat(), "mode": "paper", "balance": balance,
        "market": market, "symbol": symbol, "timeframe": timeframe,
        "stopped": False, "fetch_ok": True,
        "price": price, "spread_pct": quote.spread_pct,
        "status": report.get("status"), "available": report.get("available"),
        "steps_ok": report.get("steps_ok"), "steps_total": report.get("steps_total"),
        "profit_factor": metrics.get("profit_factor"),
        "profit_factor_ci95": metrics.get("profit_factor_ci95"),
        "win_rate_pct": metrics.get("win_rate_pct"),
        "trade_count": metrics.get("trade_count"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "verdict": str(report.get("backtest", {}).get("verdict", ""))[:160]
        if isinstance(report.get("backtest"), dict) else "",
        "order_ok": order.get("ok"),
        "reason": report.get("reason", "")[:200],
    }


def run(*, minutes: float, interval: float, out: Path,
        market: str = "crypto", symbol: str = DEFAULT_SYMBOL,
        timeframe: str = "1h", limit: int = 1000) -> dict[str, Any]:
    """Belirlenen pencere boyunca gözlem yapar; özet döner."""
    out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + minutes * 60.0
    cycles = ok = 0
    with out.open("a", encoding="utf-8") as log:
        while time.monotonic() < deadline:
            try:
                row = observe_once(market=market, symbol=symbol,
                                   timeframe=timeframe, limit=limit)
            except Exception:  # noqa: BLE001 — tur ölür, koşu ölmez
                row = {"ts": datetime.now(UTC).isoformat(), "mode": "paper",
                       "stopped": False, "fetch_ok": False,
                       "reason": traceback.format_exc().splitlines()[-1][:200]}
            log.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            log.flush()
            cycles += 1
            ok += 1 if row.get("available") else 0
            print(f"[forward] tur {cycles}: {row.get('status')} "
                  f"fiyat={row.get('price')} pf={row.get('profit_factor')}",
                  flush=True)
            if row.get("stopped"):
                break
            kalan = deadline - time.monotonic()
            if kalan <= 0:
                break
            time.sleep(min(interval, kalan))
    return {"cycles": cycles, "ok": ok, "log": str(out)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canlı forward gözlem (sanal para).")
    parser.add_argument("--minutes", type=float, default=480.0)
    parser.add_argument("--interval", type=float, default=300.0)
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--out", default="reports/forward_watch.jsonl")
    args = parser.parse_args(argv)
    print(f"[forward] {args.symbol} {args.timeframe} · {args.minutes} dk · "
          f"{args.interval} sn aralık · GERÇEK veri + SANAL para (emir yok)",
          flush=True)
    summary = run(minutes=args.minutes, interval=args.interval,
                  out=ROOT / args.out, symbol=args.symbol,
                  timeframe=args.timeframe, limit=args.limit)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
