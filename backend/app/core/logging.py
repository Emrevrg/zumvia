"""
ZUMVIA — Sir Sizdirmayan Log Altyapisi
-----------------------------------------------
Loglara API anahtari, secret veya token yazilmasi otomatik engellenir.
"""
from __future__ import annotations

import logging
import re
import sys

from .config import settings

# API anahtari benzeri desenleri yakalayan filtreler
_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(AIza[0-9A-Za-z_\-]{10,})"),
    re.compile(r"((?i:api[_\-]?key|secret|password|token|bearer)\W{1,3})([A-Za-z0-9_\-\.]{8,})"),
]


def redact(text: str) -> str:
    """Metindeki olasi sirlari maskeler."""
    out = text
    out = _PATTERNS[0].sub(lambda m: m.group(1)[:5] + "***REDACTED***", out)
    out = _PATTERNS[1].sub(lambda m: m.group(1)[:6] + "***REDACTED***", out)
    out = _PATTERNS[2].sub(lambda m: m.group(1) + "***REDACTED***", out)
    return out


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        return redact(super().format(record))


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        RedactingFormatter("%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    for noisy in ("httpx", "ccxt", "apscheduler.executors.default", "yfinance"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
