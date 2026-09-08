"""
ZUMVIA — Veritabanı Modelleri
"""
from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .core.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
#  Sabit listeler
# --------------------------------------------------------------------------- #
class CredentialKind(enum.StrEnum):
    EXCHANGE = "exchange"     # Binance, Bybit, ...
    LLM = "llm"               # Gemini, DeepSeek, Claude, OpenAI, Ollama...
    TELEGRAM = "telegram"     # Bildirim
    SOCIAL = "social"         # X/Twitter — hesap ve gonderi okuma


class TradingMode(enum.StrEnum):
    PAPER = "paper"           # Sanal bakiye — sıfır risk
    LIVE = "live"             # Gerçek para


class Autonomy(enum.StrEnum):
    MANUAL = "manual"         # Sinyal üretir, kullanıcı onaylar
    SEMI = "semi"             # Otomatik açar, kullanıcı da kapatabilir
    FULL = "full"             # Tam otonom: açar, yönetir, kapatır


class BotStatus(enum.StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    LOCKED = "locked"         # Devre kesici kilidi
    ERROR = "error"


class PositionStatus(enum.StrEnum):
    PENDING = "pending"       # Kullanıcı onayı bekliyor (MANUAL mod)
    OPEN = "open"
    CLOSED = "closed"
    REJECTED = "rejected"


class Side(enum.StrEnum):
    LONG = "long"
    SHORT = "short"


# --------------------------------------------------------------------------- #
#  Kullanıcı
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    vault_salt: Mapped[str] = mapped_column(String(64))       # HKDF salt
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # --- CANLI TİCARET YETKİSİ ---
    # Ajan bu yetkiyi KENDİSİ veremez; yalnızca kullanıcı arayüzden, açık
    # onayla, üst limit ve son kullanma tarihi belirterek verir.
    # GLOBAL RISK BUTCESI (%). Tum botlarin islem basina riski bunu ASAMAZ.
    # 0.0 verilirse sistem yeni pozisyon acmaz: yalnizca izler ve mevcut
    # pozisyonlari yonetir. Kullanicinin tek dugmeyle her seyi kismasi icin.
    risk_budget_pct: Mapped[float] = mapped_column(Float, default=0.5)
    # Arayuz dili ile yazilan metnin dili farkliysa, metni otomatik cevir.
    # Kapatilirsa metin oldugu gibi ajana gider. Varsayilan: acik.
    auto_translate_prompt: Mapped[bool] = mapped_column(Boolean, default=True)
    # Yeni botlarin baslangic modu. 'paper' guvenli varsayilan degil, KULLANICI
    # tercihidir: canli secilirse ve borsa anahtari varsa bot gercek parayla baslar.
    default_trading_mode: Mapped[str] = mapped_column(String(8), default="paper")
    # Yeni botlarin varsayilan kasa buyuklugu. Ust sinir yoktur.
    default_capital: Mapped[float] = mapped_column(Float, default=1000.0)
    # Coklu model stratejisi: single | same_provider | multi_provider
    model_strategy: Mapped[str] = mapped_column(String(16), default="single")
    live_authorized_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Suresiz yetki ACIKCA isaretlenir. Bunu cikarimla anlamak tehlikeli:
    # `live_authorized_until` alanini bosaltan herhangi bir kod, yetkisiz
    # kullaniciyi yanlislikla 'suresiz yetkili' yapardi.
    live_unlimited_time: Mapped[bool] = mapped_column(Boolean, default=False)
    live_max_capital: Mapped[float] = mapped_column(Float, default=0.0)
    live_authorized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    live_authorization_note: Mapped[str] = mapped_column(String(255), default="")

    credentials: Mapped[list[Credential]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    bots: Mapped[list[Bot]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Credential(Base):
    """Şifreli sır kaydı. payload_enc alanı AES şifreli JSON tutar."""
    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("user_id", "kind", "label", name="uq_cred_label"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[CredentialKind] = mapped_column(Enum(CredentialKind))
    provider: Mapped[str] = mapped_column(String(64))          # binance | deepseek | gemini ...
    label: Mapped[str] = mapped_column(String(64))             # Kullanıcı dostu ad
    payload_enc: Mapped[str] = mapped_column(Text)             # AES-şifreli JSON
    hint: Mapped[str] = mapped_column(String(64), default="")  # maskeli önizleme
    extra_json: Mapped[str] = mapped_column(Text, default="{}")  # sır olmayan meta (base_url, model)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="credentials")


# --------------------------------------------------------------------------- #
#  Bot
# --------------------------------------------------------------------------- #
class Bot(Base):
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))

    # --- Piyasa ---
    market: Mapped[str] = mapped_column(String(16), default="crypto")   # crypto | stock
    exchange: Mapped[str] = mapped_column(String(32), default="binance")
    symbol: Mapped[str] = mapped_column(String(32), default="BTC/USDT")
    timeframe: Mapped[str] = mapped_column(String(8), default="1h")
    quote_currency: Mapped[str] = mapped_column(String(12), default="USDT")

    # --- Çalışma ---
    mode: Mapped[TradingMode] = mapped_column(Enum(TradingMode), default=TradingMode.PAPER)
    autonomy: Mapped[Autonomy] = mapped_column(Enum(Autonomy), default=Autonomy.FULL)
    status: Mapped[BotStatus] = mapped_column(Enum(BotStatus), default=BotStatus.STOPPED)
    poll_seconds: Mapped[int] = mapped_column(Integer, default=300)
    allow_short: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Zeka ve bağlantılar ---
    llm_credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )
    llm_model: Mapped[str] = mapped_column(String(96), default="")
    # Karar mimarisi:
    #   ai_only   → yalnızca LLM
    #   algo_only → yalnızca klasik strateji motoru (AI gerekmez, $0 maliyet)
    #   hybrid    → LLM + algo mutabakatı zorunlu (en güvenli, varsayılan)
    #   ai_first  → LLM birincil; hata/kota durumunda algo devreye girer
    decision_mode: Mapped[str] = mapped_column(String(16), default="hybrid")
    strategies_json: Mapped[str] = mapped_column(Text, default="[]")   # aktif strateji listesi
    min_agree: Mapped[int] = mapped_column(Integer, default=2)         # konsensüs eşiği
    strategy_notes: Mapped[str] = mapped_column(Text, default="")      # kullanıcı strateji notları
    # --- Karar kalitesi modu ---
    #   solo    → tek model karar verir (hızlı, ucuz)
    #   council → birden çok model paralel bakar, ağırlıklı oy + medyan seviyeler
    #   strict  → konsey + risk eleştirmeni + hakem + algoritma mutabakatı (sağlamcı)
    council_mode: Mapped[str] = mapped_column(String(12), default="auto")
    council_json: Mapped[str] = mapped_column(Text, default="[]")      # konsey üyeleri
    exchange_credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )
    telegram_credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )

    # --- Risk (Katman 4) ---
    risk_pct: Mapped[float] = mapped_column(Float, default=1.0)               # işlem başı kasa riski %
    daily_loss_limit_pct: Mapped[float] = mapped_column(Float, default=3.0)   # devre kesici %
    min_confidence: Mapped[float] = mapped_column(Float, default=0.75)
    min_rr: Mapped[float] = mapped_column(Float, default=2.0)
    max_open_positions: Mapped[int] = mapped_column(Integer, default=1)
    max_trades_per_day: Mapped[int] = mapped_column(Integer, default=8)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=15.0)      # toplam DD tavanı
    trailing_stop: Mapped[bool] = mapped_column(Boolean, default=True)
    breakeven_at_r: Mapped[float] = mapped_column(Float, default=1.0)         # 1R sonrası SL girişe çekilir
    # --- Kısmi kâr alma (scale-out): beklentiyi yükselten profesyonel çıkış ---
    partial_tp_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Sistemin zayif yanini kapatan filtreler (app/layers/guards.py).
    # Girisin hemen oncesinde uygulanir; reddederse pozisyon acilmaz.
    guards_json: Mapped[str] = mapped_column(Text, default="{}")
    partial_tp_at_r: Mapped[float] = mapped_column(Float, default=1.5)        # kaç R'de kısmi kapatılır
    partial_tp_fraction: Mapped[float] = mapped_column(Float, default=0.5)    # pozisyonun ne kadarı
    # --- Portföy seviyesi risk ---
    max_portfolio_heat_pct: Mapped[float] = mapped_column(Float, default=3.0) # tüm açık riskin tavanı
    max_spread_pct: Mapped[float] = mapped_column(Float, default=0.15)        # likidite filtresi

    # --- Sermaye ---
    initial_balance: Mapped[float] = mapped_column(Float, default=1000.0)
    paper_balance: Mapped[float] = mapped_column(Float, default=1000.0)
    peak_equity: Mapped[float] = mapped_column(Float, default=1000.0)

    # --- Günlük risk durumu ---
    day_key: Mapped[str] = mapped_column(String(10), default="")              # YYYY-MM-DD (UTC)
    day_start_equity: Mapped[float] = mapped_column(Float, default=0.0)
    day_trades: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lock_reason: Mapped[str] = mapped_column(String(255), default="")

    # --- Toparlanma (Recovery) motoru ---
    recovery_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="bots")
    positions: Mapped[list[Position]] = relationship(
        back_populates="bot", cascade="all, delete-orphan"
    )


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[Side] = mapped_column(Enum(Side))
    status: Mapped[PositionStatus] = mapped_column(
        Enum(PositionStatus), default=PositionStatus.OPEN, index=True
    )
    mode: Mapped[TradingMode] = mapped_column(Enum(TradingMode), default=TradingMode.PAPER)

    qty: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    initial_stop: Mapped[float] = mapped_column(Float, default=0.0)
    risk_amount: Mapped[float] = mapped_column(Float, default=0.0)   # riske edilen para
    notional: Mapped[float] = mapped_column(Float, default=0.0)

    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    r_multiple: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)

    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    snapshot_json: Mapped[str] = mapped_column(Text, default="{}")   # karar anındaki göstergeler
    close_reason: Mapped[str] = mapped_column(String(64), default="")
    exchange_order_id: Mapped[str] = mapped_column(String(64), default="")
    # --- Kısmi kâr alma takibi ---
    original_qty: Mapped[float] = mapped_column(Float, default=0.0)
    # Kararı veren modeller — kapanışta sicile puan yazmak için
    decision_models_json: Mapped[str] = mapped_column(Text, default="[]")
    decision_id: Mapped[str] = mapped_column(String(40), default="")
    partial_taken: Mapped[bool] = mapped_column(Boolean, default=False)
    realized_partial_pnl: Mapped[float] = mapped_column(Float, default=0.0)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    bot: Mapped[Bot] = relationship(back_populates="positions")


class EquityPoint(Base):
    """Sermaye eğrisi noktası (grafik ve drawdown hesabı için)."""
    __tablename__ = "equity_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    equity: Mapped[float] = mapped_column(Float)
    balance: Mapped[float] = mapped_column(Float, default=0.0)


class BotEvent(Base):
    """Şeffaf işlem logu terminali için olay kaydı."""
    __tablename__ = "bot_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(12), default="info")       # info|success|warn|error|trade
    category: Mapped[str] = mapped_column(String(24), default="engine")  # data|math|ai|risk|exec|engine
    message: Mapped[str] = mapped_column(Text)
    data_json: Mapped[str] = mapped_column(Text, default="{}")


# --------------------------------------------------------------------------- #
#  KOMUTA AJANI (Chat + Otonom Denetleyici)
# --------------------------------------------------------------------------- #
class ControlTool(enum.StrEnum):
    API = "api"                  # Dogrudan LLM saglayicisi (tool-calling)
    CLAUDE_CODE = "claude_code"  # Yerel `claude` CLI ajani
    CODEX = "codex"              # Yerel `codex` CLI ajani
    GEMINI_CLI = "gemini_cli"    # Yerel `gemini` CLI ajani


class AgentSession(Base):
    """Bir komuta oturumu: kullanicinin ajanla konustugu kanal."""
    __tablename__ = "agent_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(120), default="Yeni Oturum")

    control_tool: Mapped[ControlTool] = mapped_column(Enum(ControlTool), default=ControlTool.API)
    control_credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )
    control_model: Mapped[str] = mapped_column(String(96), default="")   # ana (komuta) model
    sub_credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )
    sub_model: Mapped[str] = mapped_column(String(96), default="")       # alt (analist) model

    # Kullanicinin sectigi karar kalitesi modu — kurdugu tum botlara uygulanir
    #   solo    → tek model karar verir (hizli, ucuz)
    #   council → birden cok model paralel bakar, agirlikli oy + medyan seviyeler
    #   strict  → konsey + risk elestirmeni + hakem + algoritma mutabakati
    council_mode: Mapped[str] = mapped_column(String(12), default="auto")

    # CALISMA MODU — ajan bu turda ne YAPABILIR
    #   ask   → okur, olcer, anlatir; hicbir seyi degistirmez
    #   plan  → ne yapacagini yazar, uygulamaz
    #   agent → yapar ve yaptigini 7/24 kendi denetler (otonom)
    #
    # Varsayilan `ask`: bir aracin en guvenli hali varsayilan olmalidir.
    # Kullanici yaptirmak istediginde bunu acikca soyler.
    work_mode: Mapped[str] = mapped_column(String(8), default="ask")

    # Kullanicinin ajana verdigi yetki cercevesi
    mandate_json: Mapped[str] = mapped_column(Text, default="{}")
    autonomous: Mapped[bool] = mapped_column(Boolean, default=False)
    heartbeat_seconds: Mapped[int] = mapped_column(Integer, default=900)
    status: Mapped[str] = mapped_column(String(16), default="idle")  # idle|thinking|running|error
    last_error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    messages: Mapped[list[AgentMessage]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class AgentMessage(Base):
    """Chat ekranindaki her satir: kullanici, ajan, arac cagrisi veya sistem olayi."""
    __tablename__ = "agent_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))          # user|assistant|tool|system|thinking
    content: Mapped[str] = mapped_column(Text, default="")
    tool_name: Mapped[str] = mapped_column(String(64), default="")
    tool_args_json: Mapped[str] = mapped_column(Text, default="")
    tool_result_json: Mapped[str] = mapped_column(Text, default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    session: Mapped[AgentSession] = relationship(back_populates="messages")


class AuditLog(Base):
    """
    Hesap seviyesi denetim izi — canli yetki verme/iptal, mod degisimi gibi
    geri donusu olan kritik olaylar burada kalici olarak saklanir.
    """
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(48))          # LIVE_GRANT | LIVE_REVOKE | BOT_LIVE ...
    actor: Mapped[str] = mapped_column(String(16), default="user")  # user | agent | system
    detail: Mapped[str] = mapped_column(Text, default="")
    data_json: Mapped[str] = mapped_column(Text, default="{}")


class ModelScore(Base):
    """
    MODEL SICILI (track record)
    Her modelin gercek performansi burada birikir. Konseyde oy agirligi
    bu sicilden hesaplanir: dogru karar veren model zamanla daha cok soz sahibi
    olur, surekli yanilan modelin agirligi duser. Tamamen deterministiktir.
    """
    __tablename__ = "model_scores"
    __table_args__ = (UniqueConstraint("user_id", "provider", "model", name="uq_model_score"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(96))

    decisions: Mapped[int] = mapped_column(Integer, default=0)      # kac karara katildi
    trades: Mapped[int] = mapped_column(Integer, default=0)         # kac islem acildi
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    total_r: Mapped[float] = mapped_column(Float, default=0.0)      # toplam R
    vetoes: Mapped[int] = mapped_column(Integer, default=0)         # risk elestirmeni olarak veto
    schema_failures: Mapped[int] = mapped_column(Integer, default=0)  # bozuk JSON sayisi
    avg_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelCheck(Base):
    """
    MODEL DOĞRULAMA SİCİLİ

    Sağlayıcıların `/models` uç noktası, hesabın gerçekte ÇAĞIRAMAYACAĞI
    modelleri de listeler: emekliye ayrılmış (410), hesaba kapalı (404) ya da
    ayrı abonelik isteyen modeller. Kullanıcı listeden birini seçtiğinde
    görev, ilk turda hatayla ölür.

    Bu tablo "listede var" ile "gerçekten çalışıyor" arasındaki farkı tutar.
    Yalnızca gerçek bir çağrı ile öğrenilir; tahmin yapılmaz.
    """
    __tablename__ = "model_checks"
    __table_args__ = (UniqueConstraint("credential_id", "model", name="uq_model_check"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    credential_id: Mapped[int] = mapped_column(
        ForeignKey("credentials.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(160))

    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    code: Mapped[str] = mapped_column(String(32), default="")      # RETIRED, NOT_FOUND…
    detail: Mapped[str] = mapped_column(String(400), default="")
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=False)
    # Araçsız (düz sohbet) çağrıda çalışıyor mu. Bazı modeller YALNIZCA araç
    # şemasıyla kabul edilir; araştırma boru hattı düz sohbet kullandığı için
    # bu ayrı bir bilgi olmak zorunda.
    works_plain: Mapped[bool] = mapped_column(Boolean, default=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DecisionRecord(Base):
    """
    KARAR DEFTERI (audit)
    Her konsey karari; run_id, katilan modeller, oylar, deterministik veri
    ozeti ve nihai sonuc ile kalici olarak saklanir. Rapor ve denetim bunu okur.
    """
    __tablename__ = "decision_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    bot_id: Mapped[int | None] = mapped_column(
        ForeignKey("bots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    decision_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    symbol: Mapped[str] = mapped_column(String(32), default="")
    timeframe: Mapped[str] = mapped_column(String(8), default="")
    action: Mapped[str] = mapped_column(String(12), default="WAIT")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    veto_reason: Mapped[str] = mapped_column(Text, default="")

    council_json: Mapped[str] = mapped_column(Text, default="{}")     # tum oylar + gerekceler
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")    # deterministik veri + kaynaklar


class McpToken(Base):
    """
    MCP ERISIM ANAHTARI
    Claude Code, Claude Desktop, Cursor gibi araclarin platforma baglanmak icin
    kullandigi adlandirilmis, iptal edilebilir anahtar.

    Tarayici oturum anahtari (JWT) yerine bunun kullanilmasi onemlidir:
      * Suresi uzundur ama istenildigi an TEK TIKLA iptal edilebilir,
      * Hangi aracin bagli oldugu isimden bellidir,
      * Veritabaninda yalnizca SHA-256 ozeti saklanir; duz metin bir kez gosterilir.
    """
    __tablename__ = "mcp_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))              # "Claude Desktop", "Ofis PC"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))            # gosterim icin ilk karakterler
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    calls: Mapped[int] = mapped_column(Integer, default=0)


class CustomPlaybook(Base):
    """
    KISIYE OZEL SISTEM BOTU (yapay zekanin tasarladigi)

    Kutuphanedeki hazir sistemler (app/layers/playbooks.py) kodun icinde,
    sabit ve testlidir. Ancak kullanicinin durumu hicbirine uymuyorsa ajan
    KENDI sistemini tasarlayabilir -- ama serbestce kod yazarak degil,
    platformun dogrulanmis yapi taslarini birlestirerek:

        * yalnizca var olan stratejiler secilebilir,
        * hemfikirlik esigi strateji sayisini asamaz,
        * risk sistem tavaninin uzerine cikamaz,
        * tez, zayif yan ve kacinma kosulu YAZILMAK ZORUNDADIR,
        * kurulmadan once gecmis veride dogrulanir ve sonuc burada saklanir.

    Boylece "yapay zeka bot yazabilir" ile "yapay zeka uydurma strateji
    calistiramaz" ayni anda dogru olur.
    """
    __tablename__ = "custom_playbooks"
    __table_args__ = (UniqueConstraint("user_id", "slug", name="uq_custom_playbook"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(64), index=True)     # "ozel_btc_swing"
    label: Mapped[str] = mapped_column(String(96))

    thesis: Mapped[str] = mapped_column(Text, default="")
    strength: Mapped[str] = mapped_column(Text, default="")
    weakness: Mapped[str] = mapped_column(Text, default="")       # zorunlu: durustluk kurali
    avoid_when: Mapped[str] = mapped_column(Text, default="")

    strategies_json: Mapped[str] = mapped_column(Text, default="[]")
    min_agree: Mapped[int] = mapped_column(Integer, default=2)
    timeframe: Mapped[str] = mapped_column(String(8), default="4h")
    risk_pct: Mapped[float] = mapped_column(Float, default=0.7)
    decision_mode: Mapped[str] = mapped_column(String(16), default="hybrid")
    poll_seconds: Mapped[int] = mapped_column(Integer, default=900)
    allow_short: Mapped[bool] = mapped_column(Boolean, default=False)
    partial_tp: Mapped[bool] = mapped_column(Boolean, default=True)

    fits_regimes_json: Mapped[str] = mapped_column(Text, default="[]")
    fits_markets_json: Mapped[str] = mapped_column(Text, default="[]")
    volatility: Mapped[str] = mapped_column(String(8), default="any")
    horizon: Mapped[str] = mapped_column(String(8), default="orta")

    # Kim tasarladi ve kanit ne?
    forked_from: Mapped[str] = mapped_column(String(64), default="")  # temel alinan sistem
    guards_json: Mapped[str] = mapped_column(Text, default="{}")
    designed_by: Mapped[str] = mapped_column(String(96), default="")   # model adi
    design_reason: Mapped[str] = mapped_column(Text, default="")       # neden hazir sistem yetmedi
    validation_json: Mapped[str] = mapped_column(Text, default="{}")   # geri test / walk-forward
    validated: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deploy_count: Mapped[int] = mapped_column(Integer, default=0)


class CustomSkill(Base):
    """
    YAPAY ZEKANIN YAZDIGI BECERI

    Katalogdaki beceriler (app/layers/skills.py) kodun icinde sabittir:
    model onlari SECER, yazamaz. Ancak kullanicinin ihtiyaci tekrar eden bir
    is akisiysa -- "her sabah 5 pariteyi tara, kanit topla, en iyisini rapor
    et" gibi -- bunu her seferinde elle kurmak hem yorucu hem hataya aciktir.

    Bu tablo ajanin KENDI becerisini yazmasina izin verir. Ama serbest kod
    yazarak degil, DOGRULANMIS ARAC KUTUSUNU birlestirerek:

        * yalnizca kayitli araclar cagrilabilir (uydurma arac yok),
        * her adimin ciktisi bir sonrakine ad ile aktarilir,
        * risk ve emir araclari kendi korumalarindan gecmeye devam eder,
        * "ne zaman kullanilir" yazilmak ZORUNDADIR,
        * calisma sicili tutulur: kac kez calisti, kac kez hata verdi.

    Boylece "yapay zeka beceri yazabilir" ile "yapay zeka keyfi kod
    calistiramaz" ayni anda dogru olur.
    """
    __tablename__ = "custom_skills"
    __table_args__ = (UniqueConstraint("user_id", "slug", name="uq_custom_skill"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(96))
    category: Mapped[str] = mapped_column(String(32), default="otomasyon")

    description: Mapped[str] = mapped_column(Text, default="")
    when_used: Mapped[str] = mapped_column(Text, default="")     # zorunlu
    steps_json: Mapped[str] = mapped_column(Text, default="[]")
    inputs_json: Mapped[str] = mapped_column(Text, default="[]")

    # Koken: hangi beceriden turetildi, kacinci surum
    parent_slug: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    author_model: Mapped[str] = mapped_column(String(120), default="")

    # Sicil -- bir beceri "calisiyor" diye degil, CALISTIGI OLCULDUGU icin
    # guvenilir sayilir.
    runs: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(400), default="")
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CustomAutomation(Base):
    """
    AJANIN KENDI YAZDIGI OTOMASYON

    Beceri "nasil yapilacagini" bilir; otomasyon "ne zaman yapilacagini".
    Kullanici "her sabah 9'da portfoyumu kontrol et ve bir sey degisirse
    haber ver" dediginde, bunu her gun elle tetiklemek gerekmemeli.

    Bir otomasyon iki seyden birini calistirir:

        BECERI   yazilmis bir beceri (adim adim, deterministik)
        GOREV    ajana verilen serbest metin (yorum gerektiren isler)

    Guvenlik ayni ilkeye dayanir: otomasyon YENI yetki vermez. Calistirdigi
    beceri hangi kisitlara tabiyse otomasyon da aynisina tabidir; ajana
    verilen gorev de normal bir tur gibi risk kalkanindan gecer. Otomasyon
    yalnizca ZAMANLAMAYI otomatiklestirir, izni degil.
    """
    __tablename__ = "custom_automations"
    __table_args__ = (UniqueConstraint("user_id", "slug", name="uq_custom_automation"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(96))
    purpose: Mapped[str] = mapped_column(Text, default="")       # zorunlu

    # Ne calisacak
    action: Mapped[str] = mapped_column(String(16), default="skill")  # skill | task
    skill_slug: Mapped[str] = mapped_column(String(64), default="")
    task_prompt: Mapped[str] = mapped_column(Text, default="")
    inputs_json: Mapped[str] = mapped_column(Text, default="{}")

    # Ne zaman calisacak
    schedule: Mapped[str] = mapped_column(String(16), default="interval")  # interval | daily
    every_minutes: Mapped[int] = mapped_column(Integer, default=60)
    at_hour: Mapped[int] = mapped_column(Integer, default=9)      # daily icin
    at_minute: Mapped[int] = mapped_column(Integer, default=0)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notify: Mapped[bool] = mapped_column(Boolean, default=False)  # Telegram'a yaz

    # Sicil
    runs: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(400), default="")
    last_summary: Mapped[str] = mapped_column(Text, default="")
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None)

    author_model: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SystemState(Base):
    """
    SISTEM DURUMU (anahtar/deger)

    Kuruluma ait, kullaniciya bagli olmayan kucuk durum bilgileri: en son
    calisan surum, son bakim zamani gibi. Surum gecisi politikasi (bkz.
    core/upgrade.py) buradaki `app_version` kaydini okur.
    """
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class WorkingOrder(Base):
    """
    CALISAN EMIR (parent order) — buyuk emrin parcali yurutulmesi

    Buyuk bir emri tek seferde piyasaya vermek, kendi emrinize karsi islem
    yapmaktir: fiyati itersiniz ve ortalama giris fiyatiniz bozulur. Bu tablo
    emri "calisan emir" olarak tutar; zamanlayici her araligda BIR PARCA
    (child order) gonderir.

    Neden veritabaninda? Cunku surec yeniden baslasa bile yarim kalmis bir
    emrin ne kadarinin dolduğu bilinmelidir. Bellekte tutulan plan, cokme
    aninda pozisyonu belirsiz birakir.

    Iptal kosullari (hepsi kodda zorlanir):
      * Fiyat aleyhte `max_drift_pct` kadar kaydiysa kalan parcalar iptal,
      * Acil fren acildiysa kalan parcalar beklemede,
      * Sure asildiysa (`expires_at`) kalan parcalar iptal.
    Her durumda DOLAN kisim korunur ve pozisyon o kadarla yasar.
    """
    __tablename__ = "working_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), index=True)
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("positions.id", ondelete="SET NULL"), nullable=True)

    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[Side] = mapped_column(Enum(Side))
    status: Mapped[str] = mapped_column(String(16), default="working", index=True)
    # working | done | cancelled | failed

    total_qty: Mapped[float] = mapped_column(Float)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    slices: Mapped[int] = mapped_column(Integer, default=1)
    slices_done: Mapped[int] = mapped_column(Integer, default=0)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=0)

    reference_price: Mapped[float] = mapped_column(Float)       # plan yapildigindaki fiyat
    avg_fill_price: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    max_drift_pct: Mapped[float] = mapped_column(Float, default=0.6)

    stop_loss: Mapped[float] = mapped_column(Float, default=0.0)
    take_profit: Mapped[float] = mapped_column(Float, default=0.0)
    risk_amount: Mapped[float] = mapped_column(Float, default=0.0)
    decision_json: Mapped[str] = mapped_column(Text, default="{}")
    snapshot_json: Mapped[str] = mapped_column(Text, default="{}")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    next_slice_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")


class WatchItem(Base):
    """
    IZLEME LISTESI — ZUMVIA Finance ekraninin kullanici tarafi

    Kullanicinin takip ettigi enstrumanlar. Ayri bir tablo olmasinin sebebi,
    bu listenin bir botla ya da bir pozisyonla ilgisi olmamasi: kullanici
    henuz islem acmadigi bir varligi da izlemek ister ve izlemek, pozisyon
    almak degildir.

    `market` + `exchange` + `symbol` birlikte tekildir; ayni parite iki farkli
    borsada farkli fiyatlanabilir ve ikisini ayni satir saymak, hangi fiyatin
    gosterildigini belirsiz birakir.
    """
    __tablename__ = "watch_items"
    __table_args__ = (
        UniqueConstraint("user_id", "market", "exchange", "symbol",
                         name="uq_watch_user_instrument"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         index=True)
    symbol: Mapped[str] = mapped_column(String(48))
    market: Mapped[str] = mapped_column(String(16), default="crypto")
    exchange: Mapped[str] = mapped_column(String(32), default="binance")
    label: Mapped[str] = mapped_column(String(64), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)   # siralama
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Attachment(Base):
    """
    SOHBET EKI — kullanicinin ajana verdigi dosya

    Ham dosya DISKTE tutulmaz: cikarilan METIN veritabaninda durur. Sebebi
    iki tane. Birincisi, ajanin kullanabildigi sey zaten metindir; ikincisi,
    kullanicinin yukledigi bir hesap ekstresi diskte serbest bir dosya olarak
    durmamalidir.

    `chars` alani cikarilan metnin uzunlugudur ve KIRPILMADAN ONCEKI degeri
    tutar: ajanin gordugu metin kirpilmissa kullanici bunu bilmelidir.
    Kirpildigini soylemeden ozet vermek, dosyanin tamamini okumus gibi
    davranmaktir.
    """
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         index=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=True, index=True)

    filename: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(24), default="text")
    # text | csv | json | markdown | pdf
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    chars: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    content: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
