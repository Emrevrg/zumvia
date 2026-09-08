"""
Test ortamı hazırlığı.

Testler temiz bir klondan (veritabanı dosyası olmadan) da çalışmalıdır:
şema, herhangi bir fixture veritabanına dokunmadan **önce** oluşturulur.
"""
from __future__ import annotations

import os
import pathlib

import pytest

# Testler kendi veritabanında çalışır: geliştirme veritabanına test kullanıcısı,
# botu veya anahtarı sızmasın. Ayarlar okunmadan ÖNCE tanımlanmalıdır.
_TEST_DB = pathlib.Path(__file__).resolve().parent / "test_zumvia.db"
os.environ.setdefault("VQ_DATABASE_URL", f"sqlite:///{_TEST_DB.as_posix()}")

# Testler asla gerçek para moduna geçemesin — güvenlik ağı
os.environ.setdefault("VQ_FORCE_PAPER_ONLY", "false")
os.environ.pop("VQ_KILL_SWITCH", None)

from app.core.db import init_db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _prepare_database() -> None:
    """Tüm testlerden önce şemayı oluşturur ve eksik kolonları göç ettirir."""
    init_db()


@pytest.fixture(scope="session", autouse=True)
def _clear_kill_switch() -> None:
    """Önceki bir testten kalan acil fren dosyasını temizler."""
    from app.core.safety import KILL_SWITCH_FILE  # noqa: PLC0415

    KILL_SWITCH_FILE.unlink(missing_ok=True)
    yield
    KILL_SWITCH_FILE.unlink(missing_ok=True)
