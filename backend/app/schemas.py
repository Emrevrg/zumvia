"""
ZUMVIA — API Şemaları (Pydantic v2)
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from .core.config import settings


# --------------------------------------------------------------------------- #
#  Kimlik
# --------------------------------------------------------------------------- #
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str
    user_id: int


# --------------------------------------------------------------------------- #
#  Kimlik bilgileri (API anahtarları)
# --------------------------------------------------------------------------- #
class CredentialIn(BaseModel):
    kind: Literal["exchange", "llm", "telegram", "social"]
    provider: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=64)
    api_key: str = ""
    secret: str = ""
    password: str = ""          # bazı borsalar (OKX, KuCoin) passphrase ister
    token: str = ""             # telegram bot token
    chat_id: str = ""           # telegram sohbet kimliği
    base_url: str = ""          # özel/yerel LLM uç noktası
    model: str = ""
    temperature: float = 0.2
    sandbox: bool = False       # borsa testnet

    @field_validator("provider", "label")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class CredentialTestIn(BaseModel):
    credential_id: int
    model: str = ""


# --------------------------------------------------------------------------- #
#  Bot
# --------------------------------------------------------------------------- #
class BotIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    market: Literal["crypto", "stock", "demo"] = "crypto"
    exchange: str = "binance"
    symbol: str = "BTC/USDT"
    timeframe: Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"] = "1h"
    quote_currency: str = "USDT"

    mode: Literal["paper", "live"] = "paper"
    autonomy: Literal["manual", "semi", "full"] = "full"
    decision_mode: Literal["algo_only", "ai_only", "hybrid", "ai_first"] = "hybrid"
    poll_seconds: int = Field(default=300, ge=30, le=86400)
    allow_short: bool = False

    llm_credential_id: int | None = None
    llm_model: str = ""
    exchange_credential_id: int | None = None
    telegram_credential_id: int | None = None

    strategies: list[str] = Field(default_factory=list)
    min_agree: int = Field(default=2, ge=1, le=8)
    strategy_notes: str = Field(default="", max_length=2000)

    risk_pct: float = Field(default=0.5, ge=0)
    daily_loss_limit_pct: float = Field(default=3.0, gt=0)
    min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    min_rr: float = Field(default=2.0, gt=0)
    max_open_positions: int = Field(default=1, ge=1, le=10)
    max_trades_per_day: int = Field(default=8, ge=1, le=200)
    max_drawdown_pct: float = Field(default=15.0, gt=0, le=90)
    trailing_stop: bool = True
    breakeven_at_r: float = Field(default=1.0, ge=0, le=10)
    initial_balance: float = Field(default=1000.0, gt=0)

    # --- Sert tavanlar: kullanıcı bunların üstüne çıkamaz ---
    @field_validator("risk_pct")
    @classmethod
    def _cap_risk(cls, v: float) -> float:
        return min(v, settings.hard_max_risk_pct)

    @field_validator("daily_loss_limit_pct")
    @classmethod
    def _cap_daily(cls, v: float) -> float:
        return min(v, settings.hard_daily_loss_limit_pct)

    @field_validator("min_confidence")
    @classmethod
    def _floor_conf(cls, v: float) -> float:
        return max(v, settings.hard_min_confidence)

    @field_validator("min_rr")
    @classmethod
    def _floor_rr(cls, v: float) -> float:
        return max(v, settings.hard_min_rr_ratio)


class BotPatch(BaseModel):
    """Kısmi güncelleme — yalnızca gönderilen alanlar değişir."""
    model_config = {"extra": "ignore"}

    name: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    mode: Literal["paper", "live"] | None = None
    autonomy: Literal["manual", "semi", "full"] | None = None
    decision_mode: Literal["algo_only", "ai_only", "hybrid", "ai_first"] | None = None
    poll_seconds: int | None = Field(default=None, ge=30, le=86400)
    allow_short: bool | None = None
    llm_credential_id: int | None = None
    llm_model: str | None = None
    exchange_credential_id: int | None = None
    telegram_credential_id: int | None = None
    strategies: list[str] | None = None
    min_agree: int | None = Field(default=None, ge=1, le=8)
    strategy_notes: str | None = None
    risk_pct: float | None = None
    daily_loss_limit_pct: float | None = None
    min_confidence: float | None = None
    min_rr: float | None = None
    max_open_positions: int | None = None
    max_trades_per_day: int | None = None
    max_drawdown_pct: float | None = None
    trailing_stop: bool | None = None
    breakeven_at_r: float | None = None
    initial_balance: float | None = None


class QuickStartIn(BaseModel):
    """3 adımlık hızlı kurulum sihirbazı."""
    symbol: str = "BTC/USDT"
    risk_level: Literal["korumaci", "dengeli", "agresif"] = "dengeli"
    llm_credential_id: int | None = None
    llm_model: str = ""
    market: Literal["crypto", "stock", "demo"] = "crypto"
    exchange: str = "binance"
    timeframe: str = "1h"
    initial_balance: float = Field(default=1000.0, gt=0)


class ModelPickIn(BaseModel):
    """Anahtarın varsayılan modeli (listede olmayan ad da yazılabilir)."""
    model: str = Field(min_length=1, max_length=120)


class PlaybookDeployIn(BaseModel):
    """Hazır sistem (playbook) kurulumu — kullanıcı sadece parite ve kasa seçer."""
    playbook_id: str
    symbol: str = "BTC/USDT"
    market: Literal["crypto", "stock", "demo"] = "crypto"
    exchange: str = ""
    timeframe: str = ""
    name: str = ""
    initial_balance: float = Field(default=1000.0, gt=0)


class BacktestIn(BaseModel):
    market: Literal["crypto", "stock", "demo"] = "crypto"
    exchange: str = "binance"
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    candles: int = Field(default=1000, ge=300, le=1000)
    initial_balance: float = Field(default=1000.0, gt=0)
    risk_pct: float = Field(default=0.5, ge=0, le=1.5)
    min_rr: float = Field(default=2.0, ge=1.0)
    min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    allow_short: bool = False
    strategies: list[str] = Field(default_factory=list)
    min_agree: int = Field(default=2, ge=1, le=8)


class ManualTradeIn(BaseModel):
    position_id: int
    approve: bool = True


class ClosePositionIn(BaseModel):
    position_id: int


class GenericOut(BaseModel):
    ok: bool = True
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
