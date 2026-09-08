"""
ZUMVIA — FastAPI Uygulaması
=====================================
Zero-Hallucination Neuro-Symbolic Quantitative Trading Platform

Çalıştırma:
    uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import (
    routes_agent,
    routes_auth,
    routes_bots,
    routes_finance,
    routes_keys,
    routes_market,
    routes_mcp,
    routes_portfolio,
    routes_research,
    routes_safety,
    routes_skills,
    routes_workshop,
    routes_ws,
)
from .core.config import settings
from .core.db import SessionLocal, init_db
from .core.logging import get_logger, setup_logging
from .core.upgrade import apply as apply_upgrade
from .engine import reconcile
from .engine.hub import hub
from .engine.scheduler import (
    restore_agent_jobs,
    restore_jobs,
    shutdown_scheduler,
    start_automation_worker,
    start_housekeeping_worker,
    start_market_warmup,
    start_reconcile_worker,
    start_scheduler,
    start_slice_worker,
)

setup_logging()
log = get_logger("zumvia.main")

BANNER = r"""
 __     _____ ____  ____    _    _   _ _____ _____  __
 \ \   / / __|  _ \|  _ \  / \  | \ | |_   _|_ _\ \/ /
  \ \ / /|  _|| |_) | | | |/ _ \ |  \| | | |  | | \  /
   \ V / | |__|  _ <| |_| / ___ \| |\  | | |  | | /  \
    \_/  |_____|_| \_\____/_/   \_\_| \_| |_| |___/_/\_\   Q U A N T
"""


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    print(BANNER)
    log.info("%s v%s başlatılıyor…", settings.app_name, settings.version)
    init_db()

    # Sürüm geçişi politikası: güncelleme sonrası çalışan sisteme ne olacağı
    # belirsiz bırakılmaz (bkz. core/upgrade.py). Açık pozisyonlara dokunulmaz.
    with SessionLocal() as db:
        report = apply_upgrade(db)
    if report.changed:
        log.warning("Sürüm geçişi %s → %s | duraklatılan bot: %d | sahipsiz: %d",
                    report.previous, report.current,
                    len(report.paused_bots), len(report.orphaned_bots))
        for note in report.notes:
            log.warning("  %s", note)

    # MUTABAKAT: platform kapalıyken piyasa durmadı. Kayıt ile borsanın
    # gerçeği eşitlenir; şüphedeyken hiçbir kayda dokunulmaz.
    with SessionLocal() as db:
        recon = reconcile.run(db)
    if recon.changed:
        log.warning("Mutabakat: %d pozisyon kapatıldı, %d koruyucu stop yenilendi.",
                    len(recon.closed_by_exchange) + len(recon.paper_settled),
                    len(recon.stops_replaced))

    hub.bind_loop(asyncio.get_running_loop())
    start_scheduler()
    start_slice_worker()
    start_reconcile_worker()
    start_automation_worker()
    start_housekeeping_worker()
    start_market_warmup()
    restored = restore_jobs()
    agents = restore_agent_jobs()
    log.info("Hazır. Yeniden başlatılan bot: %s · otonom ajan oturumu: %s", restored, agents)
    if settings.force_paper_only:
        log.warning("GLOBAL KİLİT: Canlı ticaret kapalı (VQ_FORCE_PAPER_ONLY=true).")
    try:
        yield
    finally:
        shutdown_scheduler()
        log.info("Kapatıldı.")


app = FastAPI(
    title=settings.app_name,
    description=settings.tagline,
    version=settings.version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (routes_auth, routes_keys, routes_bots, routes_market,
               routes_portfolio, routes_agent, routes_research, routes_safety,
               routes_skills, routes_workshop, routes_mcp, routes_ws,
               routes_finance):
    app.include_router(module.router)


def _disk_status() -> dict:
    """
    Diskin o anki durumu.

    Bir ticaret sisteminde disk dolması sıradan bir aksaklık değildir: o an
    veritabanına yazılamaz, yani açık pozisyon kaydı tutulamaz.
    """
    from .core import storage  # noqa: PLC0415

    return storage.status().to_dict()


def _route_paths(routes: list[object], prefix: str = "") -> list[str]:
    """FastAPI'nin düz ve gecikmeli router kayıtlarından gerçek yolları çıkarır."""
    paths: list[str] = []
    for route in routes:
        path = getattr(route, "path", "")
        if path:
            paths.append(prefix + path)

        # FastAPI'nin yeni sürümleri `include_router()` çağrılarını başlangıçta
        # düzleştirmek yerine `_IncludedRouter` olarak saklıyor. Yalnızca
        # `app.routes[*].path` okumak bu sürümlerde bütün API'yi görünmez kılar.
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            nested_prefix = prefix + str(getattr(context, "prefix", "") or "")
            paths.extend(_route_paths(list(getattr(original, "routes", [])), nested_prefix))
    return paths


def _capabilities() -> list[str]:
    """
    Kayıtlı uç gruplarını CANLI olarak çıkarır.

    Elle yazılmış bir liste OLMAZ: eski bir listeyi güncellemeyi unutmak,
    tam olarak önlemeye çalıştığımız yalanı üretir — "bu özellik var" deyip
    olmaması. Burada uygulamanın kendi rota tablosu okunur.
    """
    groups: set[str] = set()
    for path in _route_paths(list(app.routes)):
        if not path.startswith("/api/"):
            continue
        parts = path.split("/")
        if len(parts) > 2 and parts[2]:
            groups.add(parts[2])
    return sorted(groups)


@app.get("/api/health", tags=["Sistem"])
def health() -> JSONResponse:
    """Sağlık kontrolü — VPS izleme araçları için."""
    return JSONResponse({
        "ok": True,
        "app": settings.app_name,
        "version": settings.version,
        "tagline": settings.tagline,
        "paper_only": settings.force_paper_only,
        # Disk durumu burada görünür. Sessizce çalışmayı sürdürüp bir gün
        # kayıt tutamamak, uyarmaktan çok daha kötüdür.
        "disk": _disk_status(),
        # Bu sürecin GERÇEKTEN kayıtlı olan uç grupları.
        #
        # Arayüz bunu okuyup beklediğiyle karşılaştırır. Sebebi ölçüldü:
        # uvicorn statik dosyaları diskten taze okur ama Python rotalarını
        # AÇILIŞTA kaydeder. Yeni bir rota eklenip süreç yeniden
        # başlatılmazsa `finance.js` yüklenir, çağırdığı uç 404 döner ve
        # kullanıcı "not found" görür — kodda hata yokken bozuk bir ürün.
        "capabilities": _capabilities(),
        "hard_limits": {
            "max_risk_pct": settings.hard_max_risk_pct,
            "max_daily_loss_pct": settings.hard_daily_loss_limit_pct,
            "min_confidence": settings.hard_min_confidence,
            "min_rr": settings.hard_min_rr_ratio,
        },
    })


@app.get("/api", tags=["Sistem"])
def api_root() -> dict:
    return {
        "name": settings.app_name,
        "tagline": settings.tagline,
        "docs": "/docs",
        "health": "/api/health",
        "uyari": "Bu yazılım finansal tavsiye vermez. Ticaret sermaye kaybı riski taşır. "
                 "Gerçek paraya geçmeden önce en az 30 gün paper trading yapın.",
    }


# --------------------------------------------------------------------------- #
#  Yerleşik Arayüz
#  Node.js / npm kurulumu gerektirmeyen, tek başına çalışan panel.
#  (İsteğe bağlı Next.js arayüzü için ../frontend dizinine bakın.)
# --------------------------------------------------------------------------- #
STATIC_DIR = Path(__file__).parent / "static"

if STATIC_DIR.is_dir():

    class NoCacheStatic(StaticFiles):
        """
        Arayüz dosyalarını önbelleğe almaz.

        Sürüm yükseltmelerinde kullanıcının tarayıcısında eski JavaScript kalması
        en sinsi hata kaynağıdır: arka uç yeni, arayüz eski olur. Dosyalar küçük
        olduğu için önbelleği kapatmak doğru dengedir.
        """

        def is_not_modified(self, response_headers, request_headers) -> bool:  # noqa: ARG002
            return False

        async def get_response(self, path: str, scope):
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            return response

    # Giriş rotaları statik mount'tan ÖNCE kaydedilmelidir. Starlette rotaları
    # sırayla eşler; mount önce olursa `/ui` ve `/ui/` isteğini kendi içinde
    # arayıp 404 döndürür ve alttaki uygulama rotasına hiç ulaşmaz. Tarayıcı
    # bu adrese yenilendiğinde kullanıcının gördüğü şey boş/beyaz yüzey olur.
    @app.get("/", include_in_schema=False)
    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    def ui_index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    app.mount("/ui", NoCacheStatic(directory=STATIC_DIR), name="ui")
