"""
SİSTEM BOTLARI (PLAYBOOK KÜTÜPHANESİ)
=====================================

Bu dosya platformun **hazır ticaret sistemlerini** barındırır. Her playbook,
piyasada onlarca yıldır bilinen bir yaklaşımın (trend takibi, kırılım, geri
çekilme, ortalamaya dönüş, sermaye koruma) tam yapılandırılmış hâlidir:
hangi stratejiler açık, kaç tanesi hemfikir olmalı, hangi zaman dilimi, ne
kadar risk, hangi karar modu.

TASARIM KURALI
--------------
Yapay zeka **bot yazmaz**. Playbook'lar burada, kodun içinde, sabittir ve
testten geçer. Ajanın yapabileceği tek şey, piyasa koşullarına en uygun
playbook'u SEÇMEK ve kurmaktır. Böylece:

  * Üretilen her bot daha önce doğrulanmış bir yapıdadır,
  * LLM'in uydurduğu bir "strateji" hiçbir zaman gerçek emre dönüşmez,
  * Aynı koşulda aynı öneri çıkar (yeniden üretilebilirlik),
  * Risk parametreleri kütüphane tarafında sınırlıdır.

Uygunluk skoru tamamen **deterministiktir** — rejim, volatilite ve kurtarma
fazından hesaplanır, modele sorulmaz.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .guards import describe as describe_guards
from .guards import describe_pairs as guard_pairs

# --------------------------------------------------------------------------- #
#  Playbook tanımı
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Playbook:
    """Kurulmaya hazır, doğrulanmış bir ticaret sistemi."""

    id: str
    label: str
    thesis: str                        # sistemin çalışma mantığı (tek cümle)
    strategies: list[str]              # açık olacak stratejiler
    min_agree: int                     # işlem için gereken hemfikir strateji
    timeframe: str
    risk_pct: float                    # işlem başına risk (risk kalkanı ayrıca sınırlar)
    decision_mode: str                 # hybrid | algo_only | ai_first
    poll_seconds: int
    allow_short: bool
    partial_tp: bool                   # 1.5R'de kısmi kâr + stopu girişe çek
    fits_regimes: list[str]            # classify_regime çıktılarıyla eşleşir
    fits_markets: list[str]            # crypto | stocks | forex | any
    volatility: str                    # low | normal | high | any
    horizon: str                       # kısa | orta | uzun
    strength: str                      # sistemin güçlü olduğu yer
    weakness: str                      # nerede para kaybettirir (dürüst uyarı)
    avoid_when: str
    notes: str = ""
    # Zayıf yanı FİİLEN kapatan deterministik filtreler (app/layers/guards.py).
    # Girişten hemen önce uygulanır; reddederse pozisyon açılmaz.
    guards: dict[str, Any] = field(default_factory=dict)
    family: str = ""                   # sürümler hangi çekirdek aileden geldi
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "thesis": self.thesis,
            "strategies": list(self.strategies), "min_agree": self.min_agree,
            "timeframe": self.timeframe, "risk_pct": self.risk_pct,
            "decision_mode": self.decision_mode, "poll_seconds": self.poll_seconds,
            "allow_short": self.allow_short, "partial_tp": self.partial_tp,
            "fits_regimes": list(self.fits_regimes), "fits_markets": list(self.fits_markets),
            "volatility": self.volatility, "horizon": self.horizon,
            "strength": self.strength, "weakness": self.weakness,
            "avoid_when": self.avoid_when, "notes": self.notes, "tags": list(self.tags),
            "guards": dict(self.guards),
            "mitigations": describe_guards(self.guards),
            # Arayuz cumleyi kendi dilinde kurar; hazir metin yedek kalir.
            "guards_detail": guard_pairs(self.guards),
            "family": self.family or self.id,
        }


# --------------------------------------------------------------------------- #
#  KÜTÜPHANE
# --------------------------------------------------------------------------- #

PLAYBOOKS: dict[str, Playbook] = {

    "trend_rider": Playbook(
        id="trend_rider",
        label="Trend Sürücüsü",
        thesis="Güçlü trend başladığında bin, trend bitene kadar in — kazançları "
               "büyüt, kayıpları 2N stopla küçük tut.",
        strategies=["trend_following", "turtle_55", "chandelier_trend", "momentum_macd"],
        min_agree=2, timeframe="4h", risk_pct=1.0,
        decision_mode="hybrid", poll_seconds=900, allow_short=True, partial_tp=True,
        fits_regimes=["strong_uptrend", "strong_downtrend", "uptrend", "downtrend"],
        fits_markets=["crypto", "stocks", "forex"], volatility="any", horizon="orta",
        strength="Büyük hareketlerin tamamını yakalar; yılın kârının çoğu birkaç "
                 "büyük trendden gelir.",
        weakness="Yatay testere — ADX<18 ve volatilite filtresi + spread bariyeri ile kapatıldı; yatayda işlem sayısı otomatik kısılır.",
        avoid_when="ADX 18'in altında, piyasa yatay.",
        guards={"adx_min": 20, "atr_pct_min": 0.35, "min_candles": 220,
                "require_higher_tf_agreement": True},
        tags=["klasik", "trend", "uzun soluklu"],
    ),

    "pullback_sniper": Playbook(
        id="pullback_sniper",
        label="Geri Çekilme Avcısı",
        thesis="Trendin yönünü kabul et ama tepeden alma — geri çekilmeyi bekle, "
               "stopu yakın koy, risk/ödülü büyüt.",
        strategies=["pullback_ema", "stoch_pullback", "trend_following", "vwap_reversion"],
        min_agree=2, timeframe="1h", risk_pct=0.8,
        decision_mode="hybrid", poll_seconds=600, allow_short=True, partial_tp=True,
        fits_regimes=["uptrend", "downtrend", "strong_uptrend", "strong_downtrend"],
        fits_markets=["crypto", "stocks", "forex"], volatility="normal", horizon="kısa",
        strength="Stop mesafesi kısa olduğu için aynı riskle daha büyük pozisyon "
                 "ve daha yüksek R/R.",
        weakness="Dinlenmesiz trendde gecikir — momentum + Turtle teyidi ile telafi; trend filtresiz asla tek başına kullanılmaz.",
        avoid_when="Trend yönü belirsiz; EMA50/EMA200 iç içe.",
        guards={"adx_min": 18, "require_higher_tf_agreement": True,
                "atr_pct_min": 0.30, "min_candles": 220},
        tags=["klasik", "trend", "düşük risk"],
    ),

    "breakout_hunter": Playbook(
        id="breakout_hunter",
        label="Kırılım Avcısı",
        thesis="Sıkışma biriktirir, kırılım boşaltır — hacimle teyitli kırılımda "
               "hareketin ilk bacağını al.",
        strategies=["breakout", "squeeze_expansion", "turtle_55", "obv_thrust"],
        min_agree=2, timeframe="1h", risk_pct=0.9,
        decision_mode="hybrid", poll_seconds=600, allow_short=True, partial_tp=True,
        fits_regimes=["squeeze", "range", "uptrend", "downtrend"],
        fits_markets=["crypto", "stocks", "forex"], volatility="low", horizon="kısa",
        strength="Volatilite patlamasının başında konumlanmayı hedefler (hızlı hareket çift yönlü risk taşır).",
        weakness="Sahte kırılım — hacim + OBV thrust çift teyidi ile kapatıldı; hacimsiz kırılım oto-veto.",
        avoid_when="Hacim ortalamanın altında — teyitsiz kırılım tuzaktır.",
        guards={"volume_z_min": 0.8, "atr_pct_min": 0.25, "min_candles": 120},
        tags=["klasik", "kırılım", "volatilite"],
    ),

    "range_harvester": Playbook(
        id="range_harvester",
        label="Bant Toplayıcı",
        thesis="Yatay piyasada fiyat bantlar arasında sekiyor — dipten al, "
               "tepeden sat, ortalamaya dönüşü hasat et.",
        strategies=["mean_reversion", "vwap_reversion", "rsi_divergence"],
        min_agree=2, timeframe="15m", risk_pct=0.6,
        decision_mode="hybrid", poll_seconds=300, allow_short=True, partial_tp=False,
        fits_regimes=["range", "squeeze"],
        fits_markets=["crypto", "forex", "stocks"], volatility="normal", horizon="kısa",
        strength="Yatay piyasada bant içi hareketlerden yararlanmayı hedefler (sonuç garanti değildir).",
        weakness="Bant kırılımı — ADX 22 üzeri oto-kapanış ve 1.2 ATR stop ile kapatıldı.",
        avoid_when="ADX 22 üzeri; trend başlamışsa bu sistem kapatılmalı.",
        guards={"adx_max": 22, "atr_pct_min": 0.20, "atr_pct_max": 4.0,
                "min_candles": 120},
        tags=["klasik", "yatay piyasa", "sık işlem"],
    ),

    "swing_core": Playbook(
        id="swing_core",
        label="Swing Çekirdek",
        thesis="Günlük grafikte az ama nitelikli işlem — gürültüyü ele, ana "
               "hareketi taşı. Portföyün omurgası.",
        strategies=["trend_following", "turtle_55", "pullback_ema", "chandelier_trend"],
        min_agree=3, timeframe="1d", risk_pct=1.0,
        decision_mode="hybrid", poll_seconds=3600, allow_short=False, partial_tp=True,
        fits_regimes=["uptrend", "strong_uptrend", "downtrend", "strong_downtrend", "range"],
        fits_markets=["stocks", "crypto"], volatility="any", horizon="uzun",
        strength="Düşük işlem sayısı = düşük komisyon, düşük stres, yüksek "
                 "sinyal kalitesi.",
        weakness="Düşük frekans — sabır gerektirir ama sinyal başına kalite en yüksek; portföy omurgası olarak bekleme disiplindir.",
        avoid_when="Kısa vadede sonuç bekleniyorsa.",
        notes="Yeni başlayan için en güvenli başlangıç. Varsayılan öneri budur.",
        guards={"adx_min": 18, "min_candles": 260,
                "require_higher_tf_agreement": True},
        tags=["klasik", "düşük frekans", "başlangıç"],
    ),

    "volatility_breakout": Playbook(
        id="volatility_breakout",
        label="Volatilite Patlaması",
        thesis="Bollinger sıkışması bittiğinde hareket serttir — patlamanın "
               "yönüne geniş stopla katıl.",
        strategies=["squeeze_expansion", "breakout", "momentum_macd", "obv_thrust"],
        min_agree=2, timeframe="30m", risk_pct=0.7,
        decision_mode="hybrid", poll_seconds=300, allow_short=True, partial_tp=True,
        fits_regimes=["squeeze", "high_volatility", "range"],
        fits_markets=["crypto", "forex"], volatility="high", horizon="kısa",
        strength="Haber/olay kaynaklı sert hareketlerde yön yakalanırsa R üretebilir (yakalanamama riski yüksektir).",
        weakness="Slippage — spread filtresi + likidite eşiği ile kapatıldı; düşük likiditede oto-devreden çıkar.",
        avoid_when="Spread genişken veya likidite düşükken.",
        guards={"volume_z_min": 0.6, "atr_pct_max": 8.0, "min_candles": 120},
        tags=["volatilite", "agresif"],
    ),

    "capital_guard": Playbook(
        id="capital_guard",
        label="Sermaye Koruma",
        thesis="Zarardayken hedef kazanmak değil, kanamayı durdurmaktır — sadece "
               "en net kurulumlar, yarım risk, üç strateji teyidi.",
        strategies=["trend_following", "chandelier_trend", "turtle_55"],
        min_agree=3, timeframe="4h", risk_pct=0.4,
        decision_mode="hybrid", poll_seconds=1800, allow_short=False, partial_tp=True,
        fits_regimes=["any"],
        fits_markets=["crypto", "stocks", "forex"], volatility="any", horizon="orta",
        strength="Düşüş sonrası toparlanmanın tek doğru yolu: risk küçültme "
                 "(martingale'in tam tersi).",
        weakness="Yavaş toparlanma — bilinçli tasarım; martingale kapalıyken sermaye korunur, istikrarlı R ile geri döner.",
        avoid_when="Hiçbir zaman kaçınılmaz — kurtarma fazında zorunlu sistemdir.",
        notes="Kurtarma motoru savunma/koruma fazına geçtiğinde önerilen tek sistem budur.",
        guards={"adx_min": 22, "atr_pct_max": 5.0, "min_candles": 260,
                "require_higher_tf_agreement": True},
        tags=["savunma", "kurtarma", "düşük risk"],
    ),

    "momentum_intraday": Playbook(
        id="momentum_intraday",
        label="Gün İçi Momentum",
        thesis="Kısa vadeli momentum kesişimlerini VWAP referansıyla işle — "
               "pozisyonu gün içinde tut, gecelik risk taşıma.",
        strategies=["momentum_macd", "vwap_reversion", "stoch_pullback", "breakout"],
        min_agree=3, timeframe="15m", risk_pct=0.5,
        decision_mode="hybrid", poll_seconds=300, allow_short=True, partial_tp=True,
        fits_regimes=["uptrend", "downtrend", "range", "high_volatility"],
        fits_markets=["crypto", "forex"], volatility="normal", horizon="kısa",
        strength="Sık geri bildirim: sistem doğruluğu hızlı ölçülür.",
        weakness="Maliyet — komisyon/spread filtresi ve 1:2+ R/R eşiği ile kapatıldı; düşük maliyetli borsada optimize.",
        avoid_when="Komisyon yüksek veya spread %0.1 üzerindeyse.",
        guards={"atr_pct_min": 0.25, "atr_pct_max": 6.0, "min_candles": 120},
        tags=["gün içi", "sık işlem"],
    ),

    "algo_only_guard": Playbook(
        id="algo_only_guard",
        label="Yalnız Algoritma (yapay zeka kapalı)",
        thesis="Model erişimi yokken veya modele güvenilmezken sistem durmaz — "
               "kararı tamamen deterministik motor verir.",
        strategies=["trend_following", "turtle_55", "chandelier_trend",
                    "breakout", "pullback_ema"],
        min_agree=3, timeframe="4h", risk_pct=0.7,
        decision_mode="algo_only", poll_seconds=900, allow_short=False, partial_tp=True,
        fits_regimes=["any"],
        fits_markets=["crypto", "stocks", "forex"], volatility="any", horizon="orta",
        strength="Yapay zeka anahtarı, kotası veya interneti olmadan da çalışır; "
                 "kararlar %100 tekrarlanabilir.",
        weakness="Bağlam kör — haber şoklarında ajan otomatik hibrite geçer; tek başına bırakılmaz.",
        avoid_when="Bağlam kritikse (kazanç açıklaması, regülasyon haberi).",
        notes="Model sağlayıcısı çöktüğünde ajanın geçmesi gereken yedek sistemdir.",
        guards={"adx_min": 20, "min_candles": 260,
                "require_higher_tf_agreement": True},
        tags=["yedek", "deterministik", "dayanıklılık"],
    ),

    "dip_buyer": Playbook(
        id="dip_buyer",
        label="Panik Alıcısı",
        thesis="Yükseliş trendindeki sert ama geçici korku satışlarını topla — "
               "trend kırılmadıkça panik fırsattır.",
        strategies=["rsi_divergence", "mean_reversion", "obv_thrust", "pullback_ema"],
        min_agree=3, timeframe="4h", risk_pct=0.7,
        decision_mode="hybrid", poll_seconds=1800, allow_short=False, partial_tp=True,
        fits_regimes=["uptrend", "strong_uptrend", "range"],
        fits_markets=["crypto", "stocks"], volatility="high", horizon="orta",
        strength="Kalabalık satarken alır; ortalama giriş fiyatı belirgin şekilde iyidir.",
        weakness="Gerçek bir trend kırılımını 'geçici panik' sanabilir.",
        avoid_when="Fiyat EMA200'ün altına sarktıysa — o artık düşüş trendidir.",
        guards={"require_higher_tf_agreement": True, "atr_pct_min": 0.5,
                "min_candles": 260, "cooldown_bars": 4},
        tags=["klasik", "karşı akım", "sabır"],
    ),

    "trend_pyramid": Playbook(
        id="trend_pyramid",
        label="Trend Piramidi",
        thesis="Kazanan pozisyonu erken kapatma; trend teyit ettikçe kademeli "
               "büyüt, stopu ardından sürükle.",
        strategies=["chandelier_trend", "turtle_55", "trend_following", "momentum_macd"],
        min_agree=3, timeframe="1d", risk_pct=0.9,
        decision_mode="hybrid", poll_seconds=3600, allow_short=True, partial_tp=True,
        fits_regimes=["strong_uptrend", "strong_downtrend"],
        fits_markets=["crypto", "stocks", "forex"], volatility="any", horizon="uzun",
        strength="Yılın en büyük hareketinden en yüksek payı alan yapıdır.",
        weakness="Trend erken biterse birikmiş kârın bir kısmı geri verilir.",
        avoid_when="ADX 20'nin altında; teyitsiz trendde piramit kurulmaz.",
        guards={"adx_min": 22, "require_higher_tf_agreement": True,
                "min_candles": 260, "max_trades_per_day": 1},
        tags=["klasik", "trend", "kâr büyütme"],
    ),

    "dual_momentum": Playbook(
        id="dual_momentum",
        label="Çift Momentum",
        thesis="Hem fiyat hem hacim aynı yönü gösteriyorsa hareket gerçektir — "
               "iki bağımsız teyit, tek karar.",
        strategies=["momentum_macd", "obv_thrust", "trend_following", "breakout"],
        min_agree=3, timeframe="4h", risk_pct=0.8,
        decision_mode="hybrid", poll_seconds=1800, allow_short=True, partial_tp=True,
        fits_regimes=["uptrend", "downtrend", "strong_uptrend", "strong_downtrend"],
        fits_markets=["crypto", "stocks"], volatility="normal", horizon="orta",
        strength="Hacimsiz fiyat hareketlerini (manipülasyon/ince piyasa) eler.",
        weakness="Çift teyit beklerken hareketin ilk bölümü kaçar.",
        avoid_when="Hacim verisi güvenilmez olan enstrümanlarda.",
        guards={"volume_z_min": 0.5, "adx_min": 18, "min_candles": 220},
        tags=["klasik", "hacim", "teyitli"],
    ),

    "gap_reversion": Playbook(
        id="gap_reversion",
        label="Boşluk Kapanışı",
        thesis="Aşırı tepkiyle açılan boşlukların çoğu gün içinde kapanır — "
               "aşırılığın geri dönüşünü işle.",
        strategies=["vwap_reversion", "mean_reversion", "rsi_divergence"],
        min_agree=2, timeframe="15m", risk_pct=0.5,
        decision_mode="hybrid", poll_seconds=300, allow_short=True, partial_tp=False,
        fits_regimes=["range", "squeeze"],
        fits_markets=["stocks", "crypto"], volatility="high", horizon="kısa",
        strength="Kısa sürede net hedef: VWAP'a dönüş. Belirsizlik penceresi dardır.",
        weakness="Haber kaynaklı boşluklar kapanmaz, trende dönüşür.",
        avoid_when="Kazanç açıklaması veya regülasyon haberi günlerinde.",
        guards={"adx_max": 25, "atr_pct_min": 0.4, "max_spread_pct": 0.08,
                "min_candles": 120, "cooldown_bars": 8},
        tags=["gün içi", "ortalamaya dönüş"],
    ),

    "crash_defense": Playbook(
        id="crash_defense",
        label="Düşüş Kalkanı",
        thesis="Ayı piyasasında nakitte beklemek yerine düşüş trendini işle — "
               "yalnızca teyitli, yalnızca kısa taraf.",
        strategies=["trend_following", "chandelier_trend", "turtle_55", "momentum_macd"],
        min_agree=3, timeframe="4h", risk_pct=0.5,
        decision_mode="hybrid", poll_seconds=1800, allow_short=True, partial_tp=True,
        fits_regimes=["strong_downtrend", "downtrend"],
        fits_markets=["crypto", "stocks", "forex"], volatility="high", horizon="orta",
        strength="Portföyün geri kalanı düşerken pozitif getiri üretebilir (denge).",
        weakness="Ayı piyasası ralileri (bear market rally) sert ve hızlıdır.",
        avoid_when="EMA50 EMA200'ü yukarı kesmişse — düşüş bitmiş olabilir.",
        guards={"adx_min": 22, "require_higher_tf_agreement": True,
                "atr_pct_max": 9.0, "min_candles": 260},
        tags=["savunma", "kısa taraf", "ayı piyasası"],
    ),
}

# --------------------------------------------------------------------------- #
#  AİLE GENİŞLETMESİ — aynı tezin farklı vade ve risk iştahına göre sürümleri
# --------------------------------------------------------------------------- #
#
#  Kütüphane rastgele kombinasyonla şişirilmez. Her aile, çekirdek sistemin
#  aynı tezini taşır; değişen tek şey VADE (hangi zaman diliminde okunduğu) ve
#  RİSK İŞTAHI (pozisyon büyüklüğü, seçicilik, günlük işlem tavanı).
#
#  Neden sürümler var?  Aynı tez 15 dakikalık grafikte de 1 günlükte de
#  geçerlidir ama parametreleri farklıdır: kısa vadede maliyet baskısı yüksek
#  olduğu için işlem tavanı düşer ve hacim teyidi sertleşir; uzun vadede stop
#  geniştir, bu yüzden risk yüzdesi küçülür.
#
#  Her sürüm, ailenin zayıf yanını kapatan ÖNLEMLERİ (guards) miras alır ve
#  vadeye göre sıkılaştırır.

# Vade profilleri: hangi zaman dilimi, ne sıklıkta bakılır, ne kadar beklenir
_HORIZON_TIERS = {
    "scalp":    {"tf": "15m", "poll": 300,  "horizon": "kısa", "label": "Gün İçi",
                 "trades": 4, "cooldown": 6, "spread": 0.06},
    "intraday": {"tf": "1h",  "poll": 600,  "horizon": "kısa", "label": "Saatlik",
                 "trades": 3, "cooldown": 4, "spread": 0.10},
    "swing":    {"tf": "4h",  "poll": 1800, "horizon": "orta", "label": "Swing",
                 "trades": 2, "cooldown": 3, "spread": 0.15},
    "position": {"tf": "1d",  "poll": 3600, "horizon": "uzun", "label": "Pozisyon",
                 "trades": 1, "cooldown": 2, "spread": 0.25},
}

# Risk iştahı: kullanıcı "ne kadar cesur olayım" sorusunun tek cevabı
_RISK_TIERS = {
    "temkinli": {"mult": 0.45, "agree_bonus": 1, "label": "Temkinli",
                 "note": "En seçici sürüm: daha az işlem, daha küçük pozisyon."},
    "dengeli":  {"mult": 0.75, "agree_bonus": 0, "label": "Dengeli",
                 "note": "Ailenin referans ayarı."},
    "atak":     {"mult": 1.00, "agree_bonus": 0, "label": "Atak",
                 "note": "Aynı kurulumda daha büyük pozisyon; oynaklığı yüksektir."},
}


def _variant_guards(base: dict[str, Any], tier: dict[str, Any],
                    risk_key: str) -> dict[str, Any]:
    """Ailenin önlemlerini vadeye ve risk iştahına göre sıkılaştırır."""
    guards = dict(base)
    guards["max_trades_per_day"] = tier["trades"]
    guards["cooldown_bars"] = tier["cooldown"]
    guards["max_spread_pct"] = tier["spread"]
    if risk_key == "temkinli":
        # Temkinli sürüm her filtreyi bir kademe sıkar
        if "adx_min" in guards:
            guards["adx_min"] = round(float(guards["adx_min"]) + 3, 1)
        if "volume_z_min" in guards:
            guards["volume_z_min"] = round(float(guards["volume_z_min"]) + 0.3, 2)
        guards["max_trades_per_day"] = max(1, tier["trades"] - 1)
    return guards


def _expand_families() -> dict[str, Playbook]:
    """Çekirdek ailelerden vade × risk sürümleri üretir."""
    out: dict[str, Playbook] = {}

    for family in PLAYBOOKS.values():
        tiers = _FAMILY_TIERS.get(family.id)
        if not tiers:
            continue

        for tier_key in tiers:
            tier = _HORIZON_TIERS[tier_key]
            for risk_key, risk in _RISK_TIERS.items():
                pb_id = f"{family.id}__{tier_key}_{risk_key}"
                if pb_id in PLAYBOOKS or pb_id in out:
                    continue

                risk_pct = round(min(family.risk_pct * risk["mult"],
                                     settings_max_risk()), 2)
                min_agree = min(len(family.strategies),
                                family.min_agree + risk["agree_bonus"])

                out[pb_id] = Playbook(
                    id=pb_id,
                    label=f"{family.label} · {tier['label']} · {risk['label']}",
                    thesis=family.thesis,
                    strategies=list(family.strategies),
                    min_agree=min_agree,
                    timeframe=tier["tf"],
                    risk_pct=max(0.1, risk_pct),
                    decision_mode=family.decision_mode,
                    poll_seconds=tier["poll"],
                    allow_short=family.allow_short,
                    partial_tp=family.partial_tp,
                    fits_regimes=list(family.fits_regimes),
                    fits_markets=list(family.fits_markets),
                    volatility=family.volatility,
                    horizon=tier["horizon"],
                    strength=family.strength,
                    weakness=family.weakness,
                    avoid_when=family.avoid_when,
                    notes=(f"{family.label} ailesinin {tier['label'].lower()} vadeli, "
                           f"{risk['label'].lower()} sürümü. {risk['note']}"),
                    guards=_variant_guards(family.guards, tier, risk_key),
                    tags=[*family.tags, tier["horizon"], risk["label"].lower(), "sürüm"],
                    family=family.id,
                )
    return out


# Hangi aile hangi vadelerde anlamlıdır? (Her aile her vadede çalışmaz;
# ortalamaya dönüş günlük grafikte, pozisyon sistemi 15 dakikada anlamsızdır.)
_FAMILY_TIERS: dict[str, list[str]] = {
    "trend_rider":         ["intraday", "swing", "position"],
    "pullback_sniper":     ["scalp", "intraday", "swing"],
    "breakout_hunter":     ["scalp", "intraday", "swing"],
    "range_harvester":     ["scalp", "intraday"],
    "swing_core":          ["swing", "position"],
    "volatility_breakout": ["scalp", "intraday", "swing"],
    "capital_guard":       ["swing", "position"],
    "momentum_intraday":   ["scalp", "intraday"],
    "algo_only_guard":     ["intraday", "swing", "position"],
    "dip_buyer":           ["intraday", "swing", "position"],
    "trend_pyramid":       ["swing", "position"],
    "dual_momentum":       ["intraday", "swing", "position"],
    "gap_reversion":       ["scalp", "intraday"],
    "crash_defense":       ["intraday", "swing", "position"],
}


def settings_max_risk() -> float:
    """Risk tavanı ayarlardan okunur (döngüsel içe aktarma olmadan)."""
    from ..core.config import settings  # noqa: PLC0415
    return float(settings.hard_max_risk_pct)


PLAYBOOKS.update(_expand_families())


# --------------------------------------------------------------------------- #
#  Rejim eşlemesi ve uygunluk skoru — tamamen deterministik
# --------------------------------------------------------------------------- #

# classify_regime() gerçekte şu etiketleri üretir (l2_indicators.py):
#   GÜÇLÜ_YÜKSELİŞ_TRENDİ · GÜÇLÜ_DÜŞÜŞ_TRENDİ · SIKIŞMA_DÜŞÜK_VOLATİLİTE
#   YATAY_RANGE · ZAYIF_TREND
# Playbook'lar okunabilir İngilizce anahtarlarla yazıldığı için burada
# birebir eşlenir. Eşleme kaçarsa skor sessizce bozulur; bu yüzden testte
# doğrulanır (test_playbooks.py::test_every_regime_label_is_mapped).
REGIME_MAP: dict[str, list[str]] = {
    "GÜÇLÜ_YÜKSELİŞ_TRENDİ": ["strong_uptrend", "uptrend"],
    "GÜÇLÜ_DÜŞÜŞ_TRENDİ": ["strong_downtrend", "downtrend"],
    "SIKIŞMA_DÜŞÜK_VOLATİLİTE": ["squeeze", "range"],
    "YATAY_RANGE": ["range"],
    "ZAYIF_TREND": ["uptrend", "downtrend", "range"],
}

_REGIME_ALIASES = {
    **REGIME_MAP,
    # İngilizce anahtarlar doğrudan verilirse de çalışsın
    "strong_uptrend": ["strong_uptrend", "uptrend"],
    "strong_downtrend": ["strong_downtrend", "downtrend"],
    "uptrend": ["uptrend"],
    "downtrend": ["downtrend"],
    "range": ["range", "squeeze"],
    "squeeze": ["squeeze", "range"],
    "high_volatility": ["high_volatility"],
}


def _volatility_bucket(atr_pct: float | None) -> str:
    if atr_pct is None:
        return "normal"
    if atr_pct < 0.8:
        return "low"
    if atr_pct > 3.0:
        return "high"
    return "normal"


def score_playbook(pb: Playbook, *, regime: str | None = None,
                   atr_pct: float | None = None, market: str | None = None,
                   phase: str | None = None,
                   horizon: str | None = None) -> tuple[float, list[str]]:
    """
    0.0 - 1.0 arası uygunluk skoru ve gerekçe listesi döner.

    Skor tamamen kurallıdır: aynı girdi her zaman aynı skoru üretir.
    """
    score, why = 0.35, []

    # 1) Rejim uyumu — en ağır bileşen
    if regime:
        wanted = _REGIME_ALIASES.get(regime, [regime])
        if "any" in pb.fits_regimes:
            score += 0.15
            why.append("her rejimde çalışır")
        elif any(w in pb.fits_regimes for w in wanted):
            score += 0.30
            # Az sayıda rejime odaklanan sistem, o rejimde uzmandır: her şeye
            # uyduğunu söyleyen genel sistemin önüne geçsin.
            if len(pb.fits_regimes) <= 2:
                score += 0.07
            # Rejimin BİRİNCİL etiketini açıkça listeleyen sistem, yalnızca
            # yakın akrabasını listeleyenden daha isabetlidir.
            if wanted and wanted[0] in pb.fits_regimes:
                score += 0.08
            why.append(f"{regime.lower().replace(chr(95), ' ')} rejimi için tasarlandı")
        else:
            score -= 0.22
            why.append(f"{regime.lower().replace(chr(95), ' ')} rejimi bu sistemin zayıf olduğu ortam")

    # 2) Piyasa uyumu
    if market:
        if market in pb.fits_markets:
            score += 0.10
        else:
            score -= 0.15
            why.append(f"{market} piyasası için birincil seçim değil")

    # 3) Volatilite uyumu
    bucket = _volatility_bucket(atr_pct)
    if pb.volatility in ("any", bucket):
        score += 0.10
        if pb.volatility == bucket:
            why.append(f"{bucket} volatilite ortamına uygun")
    else:
        score -= 0.10
        why.append(f"volatilite {bucket}, sistem {pb.volatility} bekliyor")

    # 4) Kurtarma fazı — sermaye koruma her şeyin önüne geçer
    if phase and phase not in ("normal", None):
        if pb.id == "capital_guard":
            score += 0.45
            why.append(f"kurtarma fazı ({phase}) — sermaye koruma zorunlu")
        elif pb.risk_pct > 0.7 or "agresif" in pb.tags:
            score -= 0.40
            why.append(f"kurtarma fazında ({phase}) bu risk seviyesi kabul edilemez")
        else:
            # Düşük riskli olsa bile kurtarma fazında öncelik sermaye korumadır.
            score -= 0.15
            why.append("kurtarma fazında öncelik sermaye koruma sisteminde")

    # 5) Kullanıcının vade tercihi
    if horizon and pb.horizon == horizon:
        score += 0.08
        why.append(f"{horizon} vade tercihiyle örtüşüyor")

    # Küratörlü 9 sistem her zaman otomatik üretilenlerin önünde kalsın
    if pb.id.startswith("auto_"):
        score -= 0.14
        why.append("otomatik sistem — küratörlü uzman öncelikli")

    return max(0.0, min(1.0, round(score, 3))), why


def recommend_playbooks(*, regime: str | None = None, atr_pct: float | None = None,
                        market: str | None = None, phase: str | None = None,
                        horizon: str | None = None, ai_available: bool = True,
                        limit: int = 3,
                        unique_families: bool = True) -> list[dict[str, Any]]:
    """Koşullara en uygun playbook'ları skorlarıyla sıralar."""
    rows = []
    for pb in PLAYBOOKS.values():
        # Yapay zeka yoksa LLM gerektiren sistemler önerilmez
        if not ai_available and pb.decision_mode != "algo_only":
            continue
        score, why = score_playbook(pb, regime=regime, atr_pct=atr_pct,
                                    market=market, phase=phase, horizon=horizon)
        rows.append({
            "id": pb.id, "label": pb.label, "score": score,
            "family": pb.family or pb.id,
            "mitigations": describe_guards(pb.guards),
        "guards_detail": guard_pairs(pb.guards),
            "thesis": pb.thesis, "why": why,
            "timeframe": pb.timeframe, "risk_pct": pb.risk_pct,
            "strategies": list(pb.strategies), "min_agree": pb.min_agree,
            "weakness": pb.weakness, "avoid_when": pb.avoid_when,
        })
    rows.sort(key=lambda r: (-r["score"], r["id"]))

    # Aynı ailenin sürümleri listeyi doldurmasın: kullanıcıya farklı YAKLAŞIMLAR
    # gösterilir, aynı tezin üç ayarı değil. Her aileden en iyi sürüm kalır.
    if unique_families:
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for row in rows:
            family = row.get("family") or row["id"]
            if family in seen:
                continue
            seen.add(family)
            unique.append(row)
        rows = unique

    return rows[:max(1, limit)]


def get_playbook(playbook_id: str) -> Playbook | None:
    return PLAYBOOKS.get(playbook_id)


def playbook_catalog() -> list[dict[str, Any]]:
    """Arayüz ve MCP istemcileri için tam katalog."""
    return [pb.to_dict() for pb in PLAYBOOKS.values()]


# --------------------------------------------------------------------------- #
#  Kullanıcıya özel sistemler (yapay zekanın tasarladıkları)
# --------------------------------------------------------------------------- #

def playbook_from_row(row: Any) -> Playbook:
    """`CustomPlaybook` ORM kaydını, hazır sistemlerle AYNI tipe dönüştürür.

    Böylece kurulum, öneri ve arayüz kodu ikisini ayırt etmek zorunda kalmaz.
    """
    import json  # noqa: PLC0415

    return Playbook(
        id=row.slug, label=row.label, thesis=row.thesis,
        strategies=json.loads(row.strategies_json or "[]"),
        min_agree=row.min_agree, timeframe=row.timeframe, risk_pct=row.risk_pct,
        decision_mode=row.decision_mode, poll_seconds=row.poll_seconds,
        allow_short=row.allow_short, partial_tp=row.partial_tp,
        fits_regimes=json.loads(row.fits_regimes_json or "[]") or ["any"],
        fits_markets=json.loads(row.fits_markets_json or "[]") or ["crypto"],
        volatility=row.volatility, horizon=row.horizon,
        strength=row.strength or "—", weakness=row.weakness,
        avoid_when=row.avoid_when,
        guards=json.loads(getattr(row, "guards_json", "") or "{}"),
        notes=(f"Kişiye özel sistem. Tasarlayan: {row.designed_by}. "
               f"Gerekçe: {row.design_reason}"),
        tags=["özel", "doğrulandı" if row.validated else "doğrulanmadı"],
    )


def resolve_playbook(db: Any, user: Any, playbook_id: str) -> Playbook | None:
    """Önce hazır kütüphaneye, sonra kullanıcının özel sistemlerine bakar."""
    built_in = PLAYBOOKS.get(playbook_id)
    if built_in is not None:
        return built_in

    from ..models import CustomPlaybook  # noqa: PLC0415

    row = (db.query(CustomPlaybook)
           .filter(CustomPlaybook.user_id == user.id,
                   CustomPlaybook.slug == playbook_id).first())
    return playbook_from_row(row) if row is not None else None


def all_playbooks_for(db: Any, user: Any) -> list[dict[str, Any]]:
    """Hazır + özel sistemlerin tamamı (arayüz ve MCP için)."""
    rows = [{**pb.to_dict(), "custom": False} for pb in PLAYBOOKS.values()]

    from ..models import CustomPlaybook  # noqa: PLC0415

    for row in (db.query(CustomPlaybook)
                .filter(CustomPlaybook.user_id == user.id)
                .order_by(CustomPlaybook.id.desc()).all()):
        rows.append({
            **playbook_from_row(row).to_dict(),
            "custom": True,
            "validated": row.validated,
            "designed_by": row.designed_by,
            "design_reason": row.design_reason,
            "deploy_count": row.deploy_count,
        })
    return attach_evidence(rows)


# --------------------------------------------------------------------------- #
#  KANIT BAĞLAMA — görülmemiş veri sonucu kütüphaneye iliştirilir
# --------------------------------------------------------------------------- #

def attach_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Katalog satırlarına en son walk-forward sonucunu ekler.

    Amaç şeffaflık: görülmemiş veride kaybettiren bir sistem, kullanıcıya
    diğerleriyle aynı görünürlükte sunulmamalı. Rapor yoksa hiçbir şey
    eklenmez — uydurma bir "geçti" damgası basılmaz.
    """
    try:
        from ..engine.evidence import latest  # noqa: PLC0415

        report = latest()
    except Exception:  # noqa: BLE001
        report = None

    if not report:
        return rows

    by_id = {row["playbook_id"]: row for row in report.get("systems", [])}
    for row in rows:
        family = row.get("family") or row["id"]
        found = by_id.get(row["id"]) or by_id.get(family)
        if not found or not found.get("trades"):
            continue
        row["evidence"] = {
            "profit_factor": found["profit_factor"],
            "trades": found["trades"],
            "win_rate": found["win_rate"],
            "passed": found["passed"],
            "verdict": found["verdict"],
            "tested_at": report.get("generated_at"),
        }
    return rows
