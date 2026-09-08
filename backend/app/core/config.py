"""
ZUMVIA — Merkezi Yapılandırma
--------------------------------------
Tüm ayarlar ortam değişkenlerinden (.env) okunur. Hiçbir sır kod içine gömülmez.
"""
from __future__ import annotations

import base64
import contextlib
import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]  # backend/


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_prefix="VQ_",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Kimlik ---
    app_name: str = "ZUMVIA"
    tagline: str = "Zero-Hallucination Neuro-Symbolic Quantitative Trading Platform"
    version: str = "1.0.0"

    # --- Güvenlik ---
    master_key: str = ""          # Fernet (AES-256 zarf) ana anahtarı
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7

    # --- Sunucu ---
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # --- Veritabanı ---
    database_url: str = f"sqlite:///{(BASE_DIR / 'zumvia.db').as_posix()}"

    # --- İşletim ---
    force_paper_only: bool = False   # true => canlı ticaret sistem genelinde kapalı
    log_level: str = "INFO"

    # --- Sabit Risk Tavanları (kullanıcı bunların ÜSTÜNE çıkamaz) — ultra güvenli ---
    hard_max_risk_pct: float = 1.0          # tek işlemde kasa riski tavanı (%) — 1.5→1.0 düşürüldü
    hard_daily_loss_limit_pct: float = 2.2  # günlük devre kesici (%) — 3.0→2.2
    hard_min_confidence: float = 0.78       # minimum LLM güven eşiği — 0.75→0.78
    hard_min_rr_ratio: float = 2.2          # minimum risk/ödül oranı — 2.0→2.2
    circuit_breaker_lock_hours: int = 24

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


def _secrets_path() -> Path:
    """
    Otomatik üretilen anahtarların saklandığı dosya.

    Docker'da veritabanıyla aynı kalıcı birimde durur; böylece "docker compose up"
    tek komutla çalışır ama yeniden başlatmada oturumlar ve kasadaki anahtarlar
    ayakta kalır.
    """
    override = os.getenv("VQ_SECRETS_FILE")
    if override:
        return Path(override)
    db_url = os.getenv("VQ_DATABASE_URL", "")
    if db_url.startswith("sqlite:///"):
        db_file = Path(db_url.replace("sqlite:////", "/").replace("sqlite:///", ""))
        return db_file.parent / "secrets.env"
    return BASE_DIR / ".secrets.env"


def _autogenerate_secrets(s: Settings) -> None:
    """
    Eksik anahtarları üretir ve kalıcı dosyaya yazar.

    Amaç kurulumu sıfır adıma indirmek: kullanıcı hiçbir şey ayarlamadan
    başlatabilsin, ama anahtarlar rastgele ve KALICI olsun. Dosya yazılamıyorsa
    (salt-okunur disk) geçici anahtarla devam edilir ve uyarı verilir.
    """
    path = _secrets_path()
    stored: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                stored[key.strip()] = value.strip()

    created = False
    if not s.jwt_secret:
        s.jwt_secret = stored.get("VQ_JWT_SECRET") or secrets.token_urlsafe(48)
        stored["VQ_JWT_SECRET"] = s.jwt_secret
        created = True
    if not s.master_key:
        s.master_key = (stored.get("VQ_MASTER_KEY")
                        or base64.urlsafe_b64encode(os.urandom(32)).decode())
        stored["VQ_MASTER_KEY"] = s.master_key
        created = True

    if not created:
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# ZUMVIA - otomatik uretilen anahtarlar. GIZLI TUTUN.",
            "# Bu dosyayi kaybederseniz kasadaki API anahtarlari cozulemez.",
            *(f"{k}={v}" for k, v in stored.items()),
        ]
        path.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        print(f"[BILGI] Guvenlik anahtarlari uretildi ve saklandi: {path}")
    except OSError as exc:
        print(f"[UYARI] Anahtar dosyasi yazilamadi ({exc}). Gecici anahtarla "
              "devam ediliyor - yeniden baslatmada oturumlar duser.")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    _autogenerate_secrets(s)
    return s


settings = get_settings()
