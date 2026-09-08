# Ajan Araçları Referansı

Komuta ajanının (ve MCP ile bağladığınız her aracın) kullanabileceği **27 araç**.
Tanımlar `backend/app/agent/tools.py` ve `tools_pro.py` içindedir; hiçbiri risk
kalkanını atlayamaz.

**Okuma:** durumu değiştirmez, güvenle çağrılır.
**Yazma:** sistemi değiştirir; chat'te turuncu nokta ile işaretlenir.

---

## 1. Piyasa ve analiz

### `get_market_snapshot` · okuma
Bir varlığın deterministik teknik fotoğrafı. **Her kararın başlangıç noktası.**

| Parametre | Tip | Zorunlu |
|---|---|---|
| `market` | `crypto` \| `stock` \| `demo` | |
| `symbol` | `BTC/USDT`, `AAPL`… | |
| `timeframe` | `1m`…`1d` | |
| `exchange` | borsa kimliği | — |

Döndürür: RSI(14), EMA20/50/200, MACD, Bollinger, ATR, ADX/DI, Stochastic,
Supertrend, hacim z-skoru, rejim sınıfı, destek/direnç seviyeleri, son 12 mum,
kural tabanlı teknik skor (−100…+100) ve emir defteri dengesizliği.

> Tüm sayılar Python ile hesaplanır. Model bu sayıları **yorumlar**, üretmez.

### `run_strategy_engine` · okuma
8 klasik stratejiyi çalıştırır, ağırlıklı konsensüs üretir. Yapay zeka
kullanmaz — bu yüzden ajanın kendi görüşü için **bağımsız çapraz doğrulamadır**.

Döndürür: `action` (BUY/SELL/WAIT), güven, stop/hedef, skor, kaç stratejinin
katıldığı ve her stratejinin ayrı gerekçesi.

### `get_news` · okuma
Ücretsiz RSS kaynaklarından başlıklar + deterministik anahtar-kelime duygu skoru
(−10…+10). Sert fiyat hareketlerinin sebebini anlamak için.

### `get_quote` · okuma
Anlık fiyat, alış/satış, spread.

### `run_backtest` · okuma
Strateji setini geçmiş veride bar-bar simüle eder. Look-ahead yoktur, komisyon
ve slipaj dahildir, pozisyon boyutu canlıdaki formülün aynısıdır.

Döndürür: işlem sayısı, kazanma oranı, kâr faktörü, maksimum drawdown, Sharpe,
beklenti (R), üst üste en fazla zarar ve **canlıya hazırlık kararı**.

---

## 2. Portföy ve bot denetimi

### `get_portfolio` · okuma
Tüm botların birleşik durumu: sermaye, getiri, kazanma oranı, kâr faktörü,
açık pozisyonlar, kilitli ve toparlanma modundaki botlar.

### `get_bot_detail` · okuma
Tek botun tam resmi: risk ayarları, açık pozisyonlar, son kapanan işlemler ve
son motor olayları (bot ne gördü, neden bekledi).

### `system_health` · okuma
Hata veren, kilitli veya **donmuş (stale)** botlar, son hatalar, kayıtlı
anahtarlar (maskeli) ve sistem risk tavanları. Otonom denetim turunun ilk adımı.

### `list_credentials` · okuma
Kayıtlı anahtarlar — yalnızca maskeli. Gerçek anahtar değeri **asla** dönmez.

### `create_bot` · yazma
Yeni bot kurar. **Her zaman sanal (paper) modda başlar.**
Risk yüzdesi sistem tavanına kırpılır.

### `update_bot` · yazma
Risk, karar mimarisi, stratejiler, periyot, alt model ve notları günceller.
`reason` alanı zorunludur — ajan neden değiştirdiğini yazmak zorundadır.

> `mode` (paper→live) bu aracın şemasında **yoktur**. Ajan gerçek paraya geçemez.

### `control_bot` · yazma
`start` / `stop` / `unlock`. Devre kesici kilidi ancak sebebi anlaşılıp
gerekli ayar yapıldıktan sonra kaldırılmalıdır.

### `run_bot_cycle` · yazma
Botun 5 katmanlı karar turunu hemen çalıştırır. Ajan böylece **botun kendi
gözüyle** ne gördüğünü görür ve kendi analiziyle karşılaştırır.

---

## 3. İşlem

### `open_position` · yazma
Ajanın kendi kararıyla pozisyon açması. **Katman 4 risk kalkanından geçer:**

1. Güven eşiği kontrolü (< %75 → ret)
2. Stop-loss zorunluluğu ve mantık kontrolü (alışta SL ≥ giriş → ret)
3. Stop mesafesi kontrolü (%0.1–%12 dışı → ret)
4. Risk/Ödül kontrolü (< 1:2 → ret)
5. Dinamik lot: `(Kasa × Risk%) / |Giriş − Stop|`
6. Notional tavanı (kasanın tamamı tek pozisyona giremez)

Reddedilirse `blocked_by` ve `reason` döner. **Ret bir arıza değil, korumadır.**

### `close_position` · yazma
Açık pozisyonu güncel fiyattan kapatır. Tez bozulduğunda kullanılır.

---

## 4. Delegasyon ve bildirim

### `ask_sub_model` · okuma
Alt modele (analist) soru delege eder. Alt model araç çağıramaz; gereken tüm
sayısal veri `context` içinde verilmelidir.

### `send_notification` · yazma
Kullanıcının Telegram kanalına mesaj gönderir.

---

## MCP üzerinden kullanım

```bash
claude mcp add zumvia -- python /yol/backend/mcp_server.py
```

Araç adları ve şemaları birebir aynıdır. `readOnlyHint` / `destructiveHint`
açıklamaları MCP istemcisine iletilir, böylece istemci yazma işlemleri için
onay isteyebilir.

Örnek istek:

> `get_portfolio` ile duruma bak, sonra BTC/USDT için `get_market_snapshot` ve
> `run_strategy_engine` çalıştır. İkisi aynı yönü gösteriyorsa `run_backtest` ile
> kanıt topla; kâr faktörü 1.3 üstündeyse `create_bot` ile kur ve `control_bot`
> ile başlat. Sonucu bana özetle.


---

## 5. Fırsat tarama ve doğrulama (profesyonel set)

### `scan_markets` · okuma
24 enstrümanı **paralel** tarar ve deterministik fırsat skoruyla sıralar.
Skor bileşenleri: konsensüs gücü, trend kalitesi (ADX), teknik uyum, volatilite
sağlığı (çok ölü / çok çılgın olmamalı), hacim teyidi, likidite cezası.

> Tek pariteye takılıp beklemek yerine **her turda önce bunu çağır.**

### `validate_strategy` · okuma
Walk-forward doğrulama: veri ardışık eğitim/test pencerelerine bölünür,
yapılandırma yalnızca eğitimde seçilir, performans **görülmemiş** veride ölçülür.

`overfit_gap` = eğitim PF − test PF. **0.6 üzerindeyse kullanma** — o
yapılandırma geçmişi ezberlemiştir.

### `optimize_setup` · okuma
Zaman dilimi × strateji seti × konsensüs eşiği kombinasyonlarını dener,
en iyisini **yalnızca görülmemiş veri performansına göre** seçer.

---

## 6. Portföy ve toparlanma

### `get_portfolio_risk` · okuma
Portföy ısısı (tüm açık risklerin özkaynağa oranı), korelasyon kümeleri,
long/short dengesi, risksiz hale gelmiş pozisyon sayısı.

### `get_recovery_plan` · okuma
Zararda ne yapılacağının **sayısal** cevabı: drawdown, başabaş için gereken
kazanç yüzdesi, kaç R, tahmini kaç işlem, hangi aşamadasın ve hangi kurallar
uygulanıyor.

> Kaybı telafi etmek için riski artırmak kod seviyesinde engellidir.

---

## 7. Güvenlik kapıları

### `get_safety_status` · okuma
Acil fren açık mı, canlı yetki var mı, hangi limit ve süreyle.

### `enable_live_trading` · yazma · çok kapılı
Botu gerçek para moduna alır. **Tüm kapılar** açık olmalı:

| Kapı | Kontrol |
|---|---|
| Kullanıcı yetkisi | Panelden verilmiş, süresi dolmamış |
| Sermaye limiti | İstenen ≤ kullanıcının belirlediği tavan |
| Acil fren | Kapalı olmalı |
| Borsa anahtarı | Bota tanımlı olmalı |
| Kanıt | ≥20 sanal işlem **ve** kâr faktörü ≥1.3 |

Herhangi biri sağlanmazsa `enabled: false` ve gerekçe döner.

### `disable_live_trading` · yazma
Sanal moda alır. **Her zaman serbesttir** — güvenli tarafa geçmek için izin gerekmez.

### `activate_kill_switch` · yazma
Acil fren: tüm botlarda yeni pozisyon durur, mevcutlar izlenmeye devam eder.
Kapatma yetkisi yalnızca kullanıcıdadır.

### `get_model_scoreboard` · okuma
Konseydeki her modelin gerçek sicili: karar sayısı, işlem, kazanma oranı,
toplam R, beklenti ve güncel oy ağırlığı.
