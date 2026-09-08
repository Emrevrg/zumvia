"""
YÜKSEK GÜÇLÜ SKILL KATALOĞU
===========================
Platformun “kas” hafızası. Her skill, ajan ve konseyin ihtiyaç duyduğunda
çağırabileceği, test edilmiş deterministik bir yetenektir.

Kural: skill’ler burada sabittir; model skill YAZMAZ, sadece uygun olanı SEÇER.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Skill:
    id: str
    label: str
    category: str  # risk | analiz | yürütme | veri | kurtarma | otomasyon
    icon: str
    level: str  # temel | ileri | uzman
    description: str
    when_used: str
    cost: str  # model maliyeti
    tags: tuple[str, ...] = ()

SKILLS: dict[str, Skill] = {
    # --- RİSK & KORUMA (en güçlü) ---
    "risk_shield": Skill("risk_shield", "Kırılmaz Risk Kalkanı", "risk", "shieldCheck", "uzman", "SL zorunlu, %1.5 tavan, 1:2 R/R, %3 devre kesici — kod seviyesinde veto.", "Her emir öncesi", "$0", ("zorunlu",)),
    "dynamic_lot": Skill("dynamic_lot", "Dinamik Lot Hesabı", "risk", "scales", "ileri", "Pozisyon = (Kasa×Risk)/|Giriş-SL| — kasa küçülürse lot küçülür.", "Boyutlandırma", "$0"),
    "recovery_engine": Skill("recovery_engine", "Toparlanma Motoru", "risk", "recovery", "uzman", "Drawdown büyüdükçe risk yarıya iner, güven eşiği yükselir.", "Zarardayken", "$0"),
    "portfolio_heat": Skill("portfolio_heat", "Portföy Isısı", "risk", "shield", "ileri", "Toplam açık risk %3 tavanı — korelasyon kalkanı aktif.", "Çok pozisyon", "$0"),
    "correlation_guard": Skill("correlation_guard", "Korelasyon Kalkanı", "risk", "scan", "ileri", "BTC/ETH aynı yön korelasyonu >0.7 ise 2. pozisyonu engeller.", "Sepet riski", "$0"),
    "spread_filter": Skill("spread_filter", "Spread Filtresi", "risk", "candles", "temel", "Likiditesi düşük, spreadi geniş pariteleri eler.", "Giriş öncesi", "$0"),
    "partial_tp": Skill("partial_tp", "Kısmi Kâr + Breakeven", "risk", "target", "ileri", "1.5R’de yarıyı kapat, stopu girişe çek — kalan risksiz büyür.", "Kar yönetimi", "$0"),
    "circuit_breaker": Skill("circuit_breaker", "Devre Kesici", "risk", "brake", "uzman", "Günlük %3 kayıpta tüm botlar 24 saat kilitlenir.", "Panik koruma", "$0"),
    "kill_switch": Skill("kill_switch", "Acil Fren", "risk", "brake", "uzman", "Tek tıkla tüm yeni pozisyonları durdurur.", "Acil durum", "$0"),
    "sl_validator": Skill("sl_validator", "Stop Doğrulayıcı", "risk", "shieldCheck", "ileri", "SL girişin yanlış tarafındaysa işlemi iptal eder.", "Her emir", "$0"),
    # --- ANALİZ ---
    "market_snapshot": Skill("market_snapshot", "Piyasa Fotoğrafı", "analiz", "candles", "temel", "OHLCV + 8 gösterge + rejim + destek/direnç — 0 halüsinasyon.", "Karar öncesi", "$0"),
    "strategy_engine": Skill("strategy_engine", "12 Strateji Konsensüsü", "analiz", "scales", "ileri", "Trend, kırılım, ortalamaya dönüş vb. ağırlıklı oy.", "Sinyal üretimi", "$0"),
    "regime_classifier": Skill("regime_classifier", "Rejim Sınıflandırıcı", "analiz", "analysis", "ileri", "Trend / yatay / sıkışma — ADX + EMA ile deterministik.", "Sistem seçimi", "$0"),
    "scanner": Skill("scanner", "24 Parite Tarayıcı", "analiz", "scan", "uzman", "Tüm piyasayı paralel tarar, fırsat skoru üretir.", "Aday bulma", "$0"),
    "validator": Skill("validator", "Walk-Forward Doğrulayıcı", "analiz", "shieldCheck", "uzman", "Geçmişi ezberleyen kurulumu overfit_gap ile reddeder.", "Kanıt toplama", "$0"),
    "optimizer": Skill("optimizer", "Setup Optimizasyonu", "analiz", "wrench", "uzman", "TF×strateji×eşik kombinasyonlarını dener, en iyiyi seçer.", "Kurulum seçimi", "$0"),
    "news_rss": Skill("news_rss", "Haber Tarayıcı", "analiz", "news", "temel", "Ücretsiz RSS + deterministik duygu skoru (keyword).", "Bağlam", "$0"),
    "vwap_engine": Skill("vwap_engine", "VWAP Motoru", "analiz", "candles", "ileri", "Seans içi adil fiyat sapması — gün içi dönüş sinyali.", "Gün içi", "$0"),
    "volume_profile": Skill("volume_profile", "Hacim Profili", "analiz", "portfolio", "ileri", "OBV thrust, hacim teyidi — sahte kırılımı eler.", "Teyit", "$0"),
    "backtest_engine": Skill("backtest_engine", "Bar-Bar Geri Test", "analiz", "backtest", "ileri", "Komisyon+spread+slippage dahil simülasyon.", "Kanıt", "$0"),
    # --- YÜRÜTME ---
    "paper_exec": Skill("paper_exec", "Paper Trading", "yürütme", "flask", "temel", "Sanal bakiye, gerçek risk yok — varsayılan mod.", "Simülasyon", "$0"),
    "live_exec": Skill("live_exec", "Canlı İcra", "yürütme", "live", "uzman", "Borsa API ile gerçek emir — limit/i limitli yetki ile.", "Canlı", "$0"),
    "order_preview": Skill("order_preview", "Emir Önizleme", "yürütme", "report", "ileri", "Brüt, komisyon, nakit etkisi ve veto gerekçesi — onay öncesi.", "Güvenli icra", "$0"),
    "telegram_notify": Skill("telegram_notify", "Telegram Bildirim", "yürütme", "bell", "temel", "Al/sat, SL tetik, devre kesici — markdown bildirim.", "Bildirim", "$0"),
    "position_manager": Skill("position_manager", "Pozisyon Yöneticisi", "yürütme", "target", "ileri", "Açık/kapalı/bekleyen pozisyon listesi + otomatik trailing.", "Yönetim", "$0"),
    # --- OTOMASYON ---
    "auto_heartbeat": Skill("auto_heartbeat", "7/24 Kalp Atışı", "otomasyon", "loop", "uzman", "Ajan belirli aralıklarla otonom denetim turu atar.", "Otonom", "$0"),
    "council": Skill("council", "Çoklu Model Konseyi", "otomasyon", "council", "uzman", "Paralel oy, medyan SL/TP, veto, hakem — halüsinasyon kalkanı.", "Karar", "model"),
    "playbook_selector": Skill("playbook_selector", "Sistem Seçici", "otomasyon", "brain", "ileri", "Rejim+volatilite+kurtarma fazından skorla en uygun botu kurar.", "Kurulum", "$0"),
    "model_scoreboard": Skill("model_scoreboard", "Model Sicili", "otomasyon", "scales", "ileri", "Her modelin R bazlı ağırlığı 10 işlemden sonra otomatik güncellenir.", "Kalibrasyon", "$0"),
    "scheduler": Skill("scheduler", "Piyasa Takvimi", "otomasyon", "clock", "temel", "Açılış öncesi tarama, kapanış sonrası mutabakat, haftalık rapor.", "Zamanlama", "$0"),
    # --- VERİ ---
    "ccxt_feed": Skill("ccxt_feed", "CCXT 100+ Borsa", "veri", "database", "temel", "Binance, Bybit, OKX dahil canlı OHLCV + emir defteri.", "Veri", "$0"),
    "yfinance_feed": Skill("yfinance_feed", "YFinance Hisse", "veri", "database", "temel", "NASDAQ/BIST hisse, endeks, emtia, döviz.", "Veri", "$0"),
    "demo_feed": Skill("demo_feed", "Demo Üreteci", "veri", "flask", "temel", "İnternet yokken bile sentetik veri ile sistem testi.", "Test", "$0"),
    # --- KURTARMA & GELİŞMİŞ ---
    "breakeven_calc": Skill("breakeven_calc", "Başabaş Hesabı", "kurtarma", "recovery", "ileri", "Zirveye dönüş için kaç R ve kaç işlem gerektiğini sayıyla verir.", "Toparlanma", "$0"),
    "drawdown_lock": Skill("drawdown_lock", "Düşüş Kilidi", "kurtarma", "brake", "uzman", "%18 DD’de minimum riske iner, strateji gözden geçirme ister.", "Koruma", "$0"),
    "algo_fallback": Skill("algo_fallback", "Algo Fallback", "otomasyon", "shieldCheck", "uzman", "Model kotası biterse 12 strateji devralır — $0 kesintisiz.", "Yedeklilik", "$0"),
    "multi_council_strict": Skill("multi_council_strict", "Sağlamcı Konsey", "otomasyon", "council", "uzman", "Konsey + risk eleştirmeni + hakem + algo mutabakatı.", "En yüksek kalite", "model"),
    "sub_model_delegate": Skill("sub_model_delegate", "Alt Model Delegesi", "otomasyon", "brain", "ileri", "İkinci görüş için sub-model’e danışır.", "Derin yorum", "model"),
    "health_monitor": Skill("health_monitor", "Sağlık İzleyici", "veri", "live", "ileri", "Provider sağlığı, devre kesici, soğuma takibi.", "Gözlem", "$0"),
    # --- FİNANS UZMANLIK ---
    "atr_stop": Skill("atr_stop", "ATR Stop", "analiz", "target", "ileri", "Volatiliteye göre stop mesafesi — gürültü filtresi.", "Risk", "$0"),
    "rr_engine": Skill("rr_engine", "R/R Motoru", "analiz", "scales", "temel", "Minimum 1:2 asimetri olmadan işlem yok.", "Filtre", "$0"),
    "confidence_gate": Skill("confidence_gate", "Güven Eşiği", "analiz", "shield", "temel", "Confidence <0.75 ise işlem açılmaz.", "Filtre", "$0"),
    "session_filter": Skill("session_filter", "Seans Filtresi", "analiz", "clock", "temel", "Piyasa kapalı/tatil kontrolü.", "Zaman", "$0"),
    "liquidity_check": Skill("liquidity_check", "Likidite Kontrolü", "analiz", "scan", "ileri", "Min hacim ve spread sınırı.", "Likidite", "$0"),
    "equity_curve": Skill("equity_curve", "Sermaye Eğrisi", "analiz", "portfolio", "temel", "Birleşik ve bot bazlı equity eğrisi.", "Rapor", "$0"),
    "report_engine": Skill("report_engine", "Rapor Motoru", "otomasyon", "report", "ileri", "JSON + Markdown rapor, audit izi.", "Rapor", "$0"),
    "mcp_bridge": Skill("mcp_bridge", "MCP Köprüsü", "otomasyon", "key", "uzman", "Claude Code / Cursor / OpenClaw ile dış kontrol.", "Entegrasyon", "$0"),
    "news_sentiment": Skill("news_sentiment", "Haber Duygu Skoru", "analiz", "news", "ileri", "Başlıklara deterministik duygu skoru — pomp/dump filtresi.", "Bağlam", "$0"),
    "multi_timeframe": Skill("multi_timeframe", "Çoklu Zaman Dilimi", "analiz", "analysis", "uzman", "1h sinyalini 4h trendle teyit — üst zaman filtresi.", "Teyit", "$0"),
    "risk_parity": Skill("risk_parity", "Risk Paritesi", "risk", "scales", "uzman", "Volatiliteye göre ağırlık — eşit risk, dengeli sepet.", "Sepet", "$0"),
    "trailing_atr": Skill("trailing_atr", "ATR Trailing", "yürütme", "target", "ileri", "Chandelier Exit ile kârı koruyarak sür.", "Yönetim", "$0"),
    "anomaly_guard": Skill("anomaly_guard", "Anomali Kalkanı", "risk", "alert", "uzman", "Anormal fiyat boşluğu/spike’da işlemi veto eder.", "Koruma", "$0"),
    "cost_analyzer": Skill("cost_analyzer", "Maliyet Analizörü", "analiz", "report", "temel", "Komisyon+spread+slippage’i beklenen R’den düşer.", "Maliyet", "$0"),
    "auto_rebalance": Skill("auto_rebalance", "Oto Rebalance", "otomasyon", "loop", "ileri", "Hedef ağırlık sapınca otomatik dengeleme.", "Portföy", "$0"),
    "signal_fusion": Skill("signal_fusion", "Sinyal Füzyonu", "analiz", "brain", "uzman", "12 strateji + konsey oyunu tek kararda birleştirir.", "Karar", "$0"),
}

def skill_catalog() -> list[dict[str, Any]]:
    return [
        {"id": s.id, "label": s.label, "category": s.category, "icon": s.icon, "level": s.level,
         "description": s.description, "when_used": s.when_used, "cost": s.cost, "tags": list(s.tags)}
        for s in SKILLS.values()
    ]

def count_by_category() -> dict[str, int]:
    from collections import Counter
    return dict(Counter(s.category for s in SKILLS.values()))
