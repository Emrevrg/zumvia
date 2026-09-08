"""
ZUMVIA — AES-256 Sir Kasasi (Guvenlik Katmani)
-------------------------------------------------------
Borsa ve LLM API anahtarlari veritabanina ASLA duz metin yazilmaz.

Sema:
  1) VQ_MASTER_KEY  : 32 baytlik ana anahtar (base64url) - .env icinde saklanir.
  2) Kullanici salt : Her kullanici icin rastgele 16 bayt (DB'de saklanir).
  3) Turetilmis key : HKDF-SHA256(master, salt, info="zumvia-user-vault")
  4) Sifreleme      : Fernet (AES-CBC + HMAC-SHA256 zarfi)

Boylece DB calinsa dahi .env olmadan anahtarlar cozulemez.
"""
from __future__ import annotations

import base64
import os
import sys

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import settings

_INFO = b"zumvia-user-vault"


class VaultError(RuntimeError):
    """Sifreleme/cozme hatasi."""


def generate_master_key() -> str:
    """Yeni bir ana anahtar uretir (base64url, 32 bayt)."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def new_salt() -> str:
    """Kullaniciya ozel yeni salt uretir."""
    return base64.urlsafe_b64encode(os.urandom(16)).decode()


def _master_bytes() -> bytes:
    key = settings.master_key
    if not key:
        raise VaultError(
            "VQ_MASTER_KEY tanimli degil. Uretmek icin:\n"
            "  python -m app.core.crypto --generate-master-key\n"
            "ve ciktiyi backend/.env icine VQ_MASTER_KEY= olarak yazin."
        )
    try:
        raw = base64.urlsafe_b64decode(key.encode())
    except Exception as exc:  # noqa: BLE001
        raise VaultError("VQ_MASTER_KEY gecerli base64url degil.") from exc
    if len(raw) < 32:
        raise VaultError("VQ_MASTER_KEY en az 32 bayt olmalidir.")
    return raw


def _fernet_for(salt: str) -> Fernet:
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=base64.urlsafe_b64decode(salt.encode()),
        info=_INFO,
    ).derive(_master_bytes())
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(plaintext: str, salt: str) -> str:
    """Duz metni kullanici salt'i ile sifreler."""
    if plaintext is None:
        raise VaultError("Sifrelenecek deger bos olamaz.")
    return _fernet_for(salt).encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str, salt: str) -> str:
    """Sifreli metni cozer."""
    try:
        return _fernet_for(salt).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise VaultError(
            "Anahtar cozulemedi. VQ_MASTER_KEY degismis olabilir; "
            "API anahtarlarini yeniden kaydedin."
        ) from exc


def mask(secret: str, keep: int = 4) -> str:
    """Arayuz/log icin maskeler: sk-abcdefgh -> sk-a......efgh"""
    if not secret:
        return ""
    if len(secret) <= keep * 2:
        return "*" * len(secret)
    return f"{secret[:keep]}{'*' * 6}{secret[-keep:]}"


if __name__ == "__main__":  # pragma: no cover
    if "--generate-master-key" in sys.argv:
        print(generate_master_key())
    else:
        print("Kullanim: python -m app.core.crypto --generate-master-key")
