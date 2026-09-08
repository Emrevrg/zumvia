"""
ZAMANLAYICI — 7/24 Otonom Çalışma
==================================
Her bot kendi periyoduyla (`poll_seconds`) arka planda çalışır.
Sunucu yeniden başlasa bile `status=running` olan botlar otomatik geri yüklenir.
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from ..core.db import session_scope
from ..core.logging import get_logger
from ..models import Bot, BotStatus
from .orchestrator import emit, run_cycle

log = get_logger("zumvia.scheduler")

scheduler = BackgroundScheduler(
    executors={"default": ThreadPoolExecutor(8)},
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120},
    timezone="UTC",
)


def _job_id(bot_id: int) -> str:
    return f"bot-{bot_id}"


def _tick(bot_id: int) -> None:
    """Zamanlayıcının çağırdığı sarmalayıcı — hata botu asla çökertmez."""
    try:
        run_cycle(bot_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("Bot %s döngü hatası: %s", bot_id, exc)
        try:
            with session_scope() as db:
                bot = db.get(Bot, bot_id)
                if bot:
                    emit(db, bot, "error", "engine",
                         f"MOTOR HATASI · {type(exc).__name__}: {exc}", {})
        except Exception:  # noqa: BLE001
            # Hata bildirimi de başarısız olduysa (veritabanı erişilemez)
            # yapılabilecek bir şey yok; tur zaten günlüğe yazıldı.
            log.exception("bot hatası bildirilemedi (bot %s)", bot_id)


def start_bot_job(bot_id: int, seconds: int) -> None:
    """Botu zamanlayıcıya ekler (varsa değiştirir) ve hemen bir tur çalıştırır."""
    seconds = max(30, int(seconds))
    scheduler.add_job(
        _tick, "interval", seconds=seconds, args=[bot_id],
        id=_job_id(bot_id), replace_existing=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=2),
    )
    log.info("Bot %s zamanlandı (%s sn).", bot_id, seconds)


def stop_bot_job(bot_id: int) -> None:
    # İş zaten yoksa kaldırmak hata verir; bu beklenen bir durumdur.
    with contextlib.suppress(Exception):
        scheduler.remove_job(_job_id(bot_id))
        log.info("Bot %s zamanlayıcıdan çıkarıldı.", bot_id)


def is_scheduled(bot_id: int) -> bool:
    return scheduler.get_job(_job_id(bot_id)) is not None


def next_run(bot_id: int) -> str | None:
    job = scheduler.get_job(_job_id(bot_id))
    return job.next_run_time.isoformat() if job and job.next_run_time else None


def restore_jobs() -> int:
    """Açılışta çalışır durumda kalan botları geri yükler."""
    restored = 0
    with session_scope() as db:
        for bot in db.query(Bot).filter(Bot.status == BotStatus.RUNNING).all():
            start_bot_job(bot.id, bot.poll_seconds)
            restored += 1
    if restored:
        log.info("%s bot yeniden başlatıldı.", restored)
    return restored


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.start()
        log.info("Zamanlayıcı çalışıyor.")


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


# --------------------------------------------------------------------------- #
#  KOMUTA AJANI KALP ATISI (otonom denetim turlari)
# --------------------------------------------------------------------------- #

def _agent_job_id(session_id: int) -> str:
    return f"agent-{session_id}"


def _agent_tick(session_id: int) -> None:
    """Otonom oturum icin periyodik denetim turu."""
    from ..agent.controller import run_agent  # noqa: PLC0415
    from ..models import AgentSession, User  # noqa: PLC0415

    try:
        with session_scope() as db:
            session = db.get(AgentSession, session_id)
            if session is None or not session.autonomous:
                stop_agent_job(session_id)
                return
            if session.status in ("thinking", "running"):
                log.info("Ajan %s hala calisiyor, tur atlandi.", session_id)
                return
            user = db.get(User, session.user_id)
            if user is None:
                return
            run_agent(db, user, session, None, autonomous=True)
    except Exception as exc:  # noqa: BLE001
        log.exception("Ajan kalp atisi hatasi (%s): %s", session_id, exc)


def start_agent_job(session_id: int, seconds: int) -> None:
    seconds = max(120, int(seconds))
    scheduler.add_job(
        _agent_tick, "interval", seconds=seconds, args=[session_id],
        id=_agent_job_id(session_id), replace_existing=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=20),
    )
    log.info("Ajan oturumu %s otonom moda alindi (%s sn).", session_id, seconds)


def stop_agent_job(session_id: int) -> None:
    with contextlib.suppress(Exception):        # iş zaten yoksa sorun değil
        scheduler.remove_job(_agent_job_id(session_id))


def restore_agent_jobs() -> int:
    """Acilista otonom oturumlari geri yukler."""
    from ..models import AgentSession  # noqa: PLC0415

    restored = 0
    with session_scope() as db:
        for session in db.query(AgentSession).filter(AgentSession.autonomous.is_(True)).all():
            start_agent_job(session.id, session.heartbeat_seconds)
            restored += 1
    return restored


# --------------------------------------------------------------------------- #
#  PARÇALI EMİR YÜRÜTÜCÜ
#  Büyük emirler tek seferde değil, parça parça gönderilir. Bu iş hızlı ve
#  kısa çalışır: yalnızca vadesi gelmiş parçaları iletir.
# --------------------------------------------------------------------------- #

SLICE_TICK_SECONDS = 5


def _slice_worker() -> None:
    """Vadesi gelen çalışan emir parçalarını gönderir."""
    from .slicer import due_orders, execute_slice  # noqa: PLC0415

    try:
        with session_scope() as db:
            for order in due_orders(db):
                try:
                    execute_slice(db, order, emit)
                except Exception:  # noqa: BLE001 — bir emir diğerlerini durdurmaz
                    log.exception("parçalı emir hatası (id %s)", order.id)
    except Exception:  # noqa: BLE001
        log.exception("parçalı emir turu başarısız")


def start_slice_worker() -> None:
    """Parçalı emir yürütücüsünü başlatır (tekil iş)."""
    if scheduler.get_job("slice-worker") is None:
        scheduler.add_job(_slice_worker, "interval", seconds=SLICE_TICK_SECONDS,
                          id="slice-worker", max_instances=1,
                          coalesce=True, replace_existing=True)
        log.info("Parçalı emir yürütücüsü çalışıyor (%s sn).", SLICE_TICK_SECONDS)


# --------------------------------------------------------------------------- #
#  PERİYODİK MUTABAKAT
#  Borsadaki gerçeklik ile kaydı düzenli olarak eşitler. Açılıştaki tek
#  seferlik kontrol yetmez: platform çalışırken de borsa tarafında stop
#  tetiklenebilir ya da koruyucu emir düşebilir.
# --------------------------------------------------------------------------- #

RECONCILE_MINUTES = 10


def _reconcile_worker() -> None:
    from .orchestrator import emit  # noqa: PLC0415
    from .reconcile import run as run_reconcile  # noqa: PLC0415

    try:
        with session_scope() as db:
            run_reconcile(db, emit)
    except Exception:  # noqa: BLE001 — mutabakat hatası platformu durdurmaz
        log.exception("periyodik mutabakat başarısız")


# --------------------------------------------------------------------------- #
#  KULLANICI OTOMASYONLARI
#  Ajanın kendi kurduğu zamanlanmış işler. Tek bir yoklayıcı iş, zamanı
#  gelen otomasyonları çalıştırır — her otomasyon için ayrı zamanlayıcı işi
#  açmak yerine. Sebep: otomasyonlar sık değişir (kurulur, silinir,
#  düzenlenir) ve her değişiklikte zamanlayıcıyı senkron tutmak, sessizce
#  yetim kalmış işlere yol açar.
# --------------------------------------------------------------------------- #

AUTOMATION_TICK_MINUTES = 1


def _automation_worker() -> None:
    """Zamanı gelen otomasyonları çalıştırır."""
    from ..layers import automations  # noqa: PLC0415
    from ..models import User  # noqa: PLC0415

    try:
        with session_scope() as db:
            pending = automations.due(db)
            for row in pending:
                user = db.get(User, row.user_id)
                if user is None:
                    continue
                try:
                    automations.run_one(db, user, row)
                except Exception:  # noqa: BLE001 — biri çökse diğerleri sürer
                    log.exception("otomasyon çalıştırılamadı: %s", row.slug)
            if pending:
                log.info("%d otomasyon çalıştırıldı", len(pending))
    except Exception:  # noqa: BLE001 — otomasyon hatası platformu durdurmaz
        log.exception("otomasyon yoklayıcısı başarısız")


def start_automation_worker() -> None:
    if scheduler.get_job("automation-worker") is None:
        scheduler.add_job(_automation_worker, "interval",
                          minutes=AUTOMATION_TICK_MINUTES,
                          id="automation-worker", max_instances=1,
                          coalesce=True, replace_existing=True)
        log.info("Otomasyon yoklayıcısı çalışıyor (%s dk).",
                 AUTOMATION_TICK_MINUTES)


# --------------------------------------------------------------------------- #
#  DİSK BAKIMI
#  Sınırsız büyüyen hiçbir şey olmamalı. Bot olayları, sermaye eğrisi ve
#  raporlar saklama sınırlarına göre budanır; WAL sıkıştırılır.
#
#  Bu bir düzen meselesi değil: disk dolduğunda veritabanına yazılamaz, yani
#  AÇIK POZİSYON KAYDI tutulamaz. Borsada gerçek bir pozisyon durur ama
#  sistemin haberi olmaz.
# --------------------------------------------------------------------------- #

HOUSEKEEPING_MINUTES = 60


def _housekeeping_worker() -> None:
    from ..core import storage  # noqa: PLC0415

    try:
        state = storage.status()
        if state.level == "ok":
            # Yer boldur; yalnızca WAL şişmişse sıkıştır.
            with session_scope() as db:
                storage.checkpoint_wal(db)
            return

        with session_scope() as db:
            report = storage.housekeeping(db, aggressive=(state.level == "critical"))
            if state.level == "critical":
                storage.vacuum(db)
        log.warning("disk bakımı: %s — %s", state.message, report["removed"])
    except Exception:  # noqa: BLE001 — bakım hatası platformu durdurmaz
        log.exception("disk bakımı başarısız")


def start_housekeeping_worker() -> None:
    if scheduler.get_job("housekeeping-worker") is None:
        scheduler.add_job(_housekeeping_worker, "interval",
                          minutes=HOUSEKEEPING_MINUTES,
                          id="housekeeping-worker", max_instances=1,
                          coalesce=True, replace_existing=True)
        log.info("Disk bakımı çalışıyor (%s dk).", HOUSEKEEPING_MINUTES)


def start_reconcile_worker() -> None:
    if scheduler.get_job("reconcile-worker") is None:
        scheduler.add_job(_reconcile_worker, "interval", minutes=RECONCILE_MINUTES,
                          id="reconcile-worker", max_instances=1,
                          coalesce=True, replace_existing=True)
        log.info("Mutabakat işi çalışıyor (%s dk).", RECONCILE_MINUTES)


# --------------------------------------------------------------------------- #
#  Piyasa merkezi ısıtması
# --------------------------------------------------------------------------- #

def _warm_market_hub() -> None:
    """
    ZUMVIA Finance ekranının ilk açılışını hızlandırır.

    Ölçüldü: soğuk süreçte tahtanın ilk yüklenmesi 17 saniye sürüyor, ikinci
    kez 3 saniye. Aradaki fark tek seferlik ısınma — borsa piyasa listesi ve
    veri sağlayıcı oturumu. Bunu kullanıcının ilk tıklamasında ödetmek yerine
    açılışta, arka planda ödemek doğrudur.

    Başarısız olursa hiçbir şey bozulmaz: ekran ilk açılışta yavaş olur.
    """
    try:
        from ..layers import finance_hub  # noqa: PLC0415
        finance_hub.board()
        log.info("Piyasa merkezi ısıtıldı.")
    except Exception as exc:  # noqa: BLE001 — ısınma, uygulamayı ilgilendirmez
        log.info("piyasa merkezi ısıtılamadı: %s", str(exc)[:160])


def start_market_warmup() -> None:
    """Açılıştan 5 saniye sonra bir kez, sonra düzenli tazeleme."""
    if scheduler.get_job("market-warmup") is None:
        scheduler.add_job(_warm_market_hub, "interval", minutes=5,
                          id="market-warmup", max_instances=1, coalesce=True,
                          replace_existing=True,
                          next_run_time=datetime.now(UTC) + timedelta(seconds=5))
        log.info("Piyasa merkezi ısıtması planlandı.")
