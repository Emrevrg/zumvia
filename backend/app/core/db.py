"""
ZUMVIA — Veritabani Baglantisi (SQLite / PostgreSQL)
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=8000")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Arka plan gorevleri icin transactional kapsam."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _auto_migrate() -> list[str]:
    """
    Hafif, YALNIZCA EKLEYİCİ şema göçü.

    Sürüm yükseltmelerinde modele yeni kolon eklendiğinde mevcut veritabanı
    bozulmasın diye eksik kolonlar `ALTER TABLE ... ADD COLUMN` ile eklenir.
    Kolon silme, tip değiştirme veya veri taşıma ASLA yapılmaz — veri kaybı
    riski olan hiçbir işlem otomatik çalıştırılmaz.
    """
    from sqlalchemy import inspect, text  # noqa: PLC0415

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    applied: list[str] = []

    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue

                column_type = column.type.compile(dialect=engine.dialect)
                clause = f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {column_type}'

                default = column.default.arg if column.default is not None else None
                if callable(default):
                    default = None
                if default is not None and not isinstance(default, (list, dict)):
                    literal = f"'{default}'" if isinstance(default, str) else (
                        str(int(default)) if isinstance(default, bool) else str(default)
                    )
                    clause += f" DEFAULT {literal}"

                try:
                    connection.execute(text(clause))
                    applied.append(f"{table.name}.{column.name}")
                except Exception as exc:  # noqa: BLE001 — göç hatası uygulamayı durdurmaz
                    print(f"[göç] {table.name}.{column.name} eklenemedi: {exc}")

    if applied:
        print(f"[göç] {len(applied)} yeni kolon eklendi: {', '.join(applied[:12])}"
              + (" …" if len(applied) > 12 else ""))
    return applied


def init_db() -> None:
    from .. import models  # noqa: F401  (tablolarin kaydi icin)
    Base.metadata.create_all(bind=engine)
    _auto_migrate()
