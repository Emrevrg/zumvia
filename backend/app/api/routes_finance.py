"""
ZUMVIA FINANCE UÇLARI

Ekranın veri yolu. Tek kural: burada dönen her sayı `finance_hub` üzerinden
ölçülür — arayüz için ayrı bir "hızlı ama yaklaşık" yol açılmaz. Açılsaydı
kullanıcı ekranda bir fiyat, ajandan başka bir fiyat duyardı ve hangisinin
doğru olduğunu bilemezdi.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.db import get_db
from ..core.logging import get_logger
from ..layers import finance_hub as hub
from ..models import User, WatchItem
from .deps import current_user

log = get_logger("zumvia.api.finance")

router = APIRouter(prefix="/api/finance", tags=["finance"])


# --------------------------------------------------------------------------- #
#  İzleme listesi
# --------------------------------------------------------------------------- #

class WatchIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=48)
    market: str = ""
    exchange: str = ""
    label: str = ""


def _watchlist(db: Session, user: User) -> list[hub.Instrument]:
    """Kullanıcının listesi; boşsa varsayılan tahta."""
    rows = (db.query(WatchItem)
            .filter(WatchItem.user_id == user.id)
            .order_by(WatchItem.position, WatchItem.id).all())
    if not rows:
        return list(hub.DEFAULT_BOARD)
    return [hub.Instrument(symbol=r.symbol, market=r.market, exchange=r.exchange,
                           label=r.label or r.symbol,
                           group="kripto" if r.market == "crypto" else "hisse")
            for r in rows]


@router.get("/watchlist")
def get_watchlist(db: Session = Depends(get_db),
                  user: User = Depends(current_user)) -> dict:
    rows = (db.query(WatchItem)
            .filter(WatchItem.user_id == user.id)
            .order_by(WatchItem.position, WatchItem.id).all())
    return {
        "items": [{"id": r.id, "symbol": r.symbol, "market": r.market,
                   "exchange": r.exchange, "label": r.label,
                   "key": f"{r.market}:{r.exchange}:{r.symbol}"} for r in rows],
        "using_default": not rows,
        "max": hub.MAX_WATCH,
    }


@router.post("/watchlist", status_code=status.HTTP_201_CREATED)
def add_watch(payload: WatchIn, db: Session = Depends(get_db),
              user: User = Depends(current_user)) -> dict:
    try:
        inst = hub.resolve(payload.symbol, payload.market, payload.exchange)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    count = db.query(WatchItem).filter(WatchItem.user_id == user.id).count()
    if count >= hub.MAX_WATCH:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"İzleme listesi en fazla {hub.MAX_WATCH} enstrüman tutar. "
            f"Yenisini eklemek için birini çıkarın.")

    existing = (db.query(WatchItem)
                .filter(WatchItem.user_id == user.id,
                        WatchItem.market == inst.market,
                        WatchItem.exchange == inst.exchange,
                        WatchItem.symbol == inst.symbol).first())
    if existing is not None:
        return {"added": False, "id": existing.id, "not": "Zaten listede."}

    row = WatchItem(user_id=user.id, symbol=inst.symbol, market=inst.market,
                    exchange=inst.exchange, label=payload.label or inst.label,
                    position=count)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"added": True, "id": row.id, "symbol": row.symbol,
            "market": row.market, "exchange": row.exchange}


@router.delete("/watchlist/{item_id}")
def remove_watch(item_id: int, db: Session = Depends(get_db),
                 user: User = Depends(current_user)) -> dict:
    row = (db.query(WatchItem)
           .filter(WatchItem.id == item_id, WatchItem.user_id == user.id).first())
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bu enstrüman listede yok.")
    db.delete(row)
    db.commit()
    return {"removed": True}


# --------------------------------------------------------------------------- #
#  Tahta ve ayrıntı
# --------------------------------------------------------------------------- #

@router.get("/board")
def get_board(db: Session = Depends(get_db),
              user: User = Depends(current_user)) -> dict:
    """Ana ekran: nabız + izleme listesi + yükselen/düşenler."""
    return hub.board(_watchlist(db, user))


@router.get("/quote")
def get_quote(symbol: str = Query(min_length=1), market: str = "", exchange: str = "",
              _: User = Depends(current_user)) -> dict:
    try:
        inst = hub.resolve(symbol, market, exchange)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return hub.tick(inst).to_dict()


@router.get("/detail")
def get_detail(symbol: str = Query(min_length=1), market: str = "", exchange: str = "",
               timeframe: str = "1h", news: bool = True,
               _: User = Depends(current_user)) -> dict:
    """Tek enstrümanın tam dosyası: mumlar, göstergeler, algoritmik oy, haber."""
    try:
        inst = hub.resolve(symbol, market, exchange)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return hub.detail(inst, timeframe, with_news=news)


@router.get("/search")
def search_instruments(q: str = "", limit: int = 12,
                       _: User = Depends(current_user)) -> dict:
    return {"query": q, "results": hub.search(q, max(1, min(limit, 40)))}


# --------------------------------------------------------------------------- #
#  Haber
# --------------------------------------------------------------------------- #

@router.get("/news")
def get_news(scope: str = "all", limit: int = 40,
             _: User = Depends(current_user)) -> dict:
    """
    Canlı finans haber akışı.

    Duygu skoru deterministiktir ve YATIRIM TAVSİYESİ DEĞİLDİR — yanıtın
    içinde bu açıkça yazar, çünkü skorlu bir başlık listesi kolayca tavsiye
    gibi okunur.
    """
    if scope not in ("all", "crypto", "macro", "stock", "stocks"):
        scope = "all"
    return hub.news_stream(scope, max(5, min(limit, 80)))


@router.post("/refresh")
def refresh(_: User = Depends(current_user)) -> dict:
    """Önbelleği boşaltır: kullanıcı 'şimdi yenile' dediğinde gerçekten yeniler."""
    hub.clear_cache()
    return {"refreshed": True,
            "not": "Önbellek boşaltıldı; sıradaki istek doğrudan borsadan ölçer."}


# --------------------------------------------------------------------------- #
#  Ajan bağlamı
# --------------------------------------------------------------------------- #

@router.get("/context")
def get_context(db: Session = Depends(get_db),
                user: User = Depends(current_user)) -> dict:
    """
    Sohbete iliştirilecek canlı piyasa özeti.

    Ekrandaki sayıların aynısı. Ajan bu bağlamla konuştuğunda kullanıcının
    gördüğüyle çelişemez.
    """
    return hub.agent_context(_watchlist(db, user))
