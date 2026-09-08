# MCP Kurulumu — Claude Code, Claude Desktop, Cursor

ZUMVIA bir **MCP (Model Context Protocol) sunucusudur**. Bağladığınız
araç, platformun 27 ticaret aracını doğrudan kullanmaya başlar: piyasa tarar,
geri test yapar, bot kurar, pozisyon açar — hepsi **aynı risk kalkanının**
altında.

İki taşıma desteklenir:

| Taşıma | Kimler için | Sunucu gerekli mi |
|---|---|---|
| **HTTP** (`/mcp`) | Masaüstü uygulamaları, uzak bağlantı | Evet — platform çalışıyor olmalı |
| **stdio** (`mcp_server.py`) | Terminal ajanları, çevrimdışı kullanım | Hayır — süreç doğrudan başlar |

---

## 1. Erişim anahtarı alın

Panelde: **profil → Kontrol merkezi → Bağlantılar**

1. Anahtara bir ad verin (örn. *"Claude Desktop — ev bilgisayarı"*).
2. İsterseniz gün cinsinden süre girin (boş = iptal edilene kadar geçerli).
3. **Yeni anahtar üret**e basın.

Anahtar `vq_` ile başlar ve **yalnızca bir kez** gösterilir. Veritabanında
sadece SHA-256 özeti saklanır; kaybederseniz iptal edip yenisini üretirsiniz.

> Tarayıcı oturum anahtarınızı (JWT) kullanmayın: kısa ömürlüdür, iptal
> edilemez ve hangi aracın bağlı olduğu anlaşılmaz.

Aynı sayfa, anahtarınız gömülü hâlde hazır komutları da gösterir ve kopyalar.

---

## 2. Claude Code (terminal veya masaüstü)

### En hızlı yol — proje dosyası zaten hazır

Depo kökündeki `.mcp.json` her iki taşımayı da tanımlar ve anahtarı
`VQ_MCP_TOKEN` ortam değişkeninden okur (anahtar dosyaya yazılmaz):

```bash
export VQ_MCP_TOKEN="vq_ANAHTARINIZ"
```

Windows PowerShell:

```powershell
$env:VQ_MCP_TOKEN = "vq_ANAHTARINIZ"
```

Ardından bu klasörde `claude` başlatın; Claude Code proje MCP sunucularını
onayınıza sunar. Onayladıktan sonra 30 araç kullanıma hazırdır.

### Elle eklemek isterseniz

```bash
claude mcp add --transport http zumvia http://127.0.0.1:8000/mcp   --header "Authorization: Bearer vq_ANAHTARINIZ"
```

Doğrulama:

```bash
claude mcp list
```

Ardından Claude Code içinde şöyle konuşabilirsiniz:

> Portföyüme bak, BTC/USDT için hangi sistem botunun uygun olduğunu hesapla,
> geçmiş veride doğrula ve uygunsa sanal modda kur.

Claude'un bu iş için kullanacağı sıra: `get_portfolio` → `recommend_playbook`
→ `validate_strategy` → `deploy_playbook`. Sistem botunu **Claude yazmaz**,
kütüphaneden seçer; risk kalkanı her durumda son sözü söyler.

## 3. Claude Desktop / Cursor / diğer masaüstü uygulamaları

**Ayarlar → Connectors (veya MCP) → Add custom server** bölümüne:

```
URL:     http://127.0.0.1:8000/mcp
Header:  Authorization: Bearer vq_ANAHTARINIZ
```

Uygulama yapılandırma dosyası istiyorsa:

```json
{
  "mcpServers": {
    "zumvia": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer vq_ANAHTARINIZ" }
    }
  }
}
```

Başlık gönderemeyen istemciler için sorgu parametresi de kabul edilir:

```
http://127.0.0.1:8000/mcp?token=vq_ANAHTARINIZ
```

---

## 4. Terminal ajanları (stdio — ağ gerekmez)

```bash
claude mcp add zumvia -- python /tam/yol/backend/mcp_server.py
```

Ortam değişkenleriyle kullanıcı seçimi:

| Değişken | Anlamı |
|---|---|
| `VQ_MCP_TOKEN` | Panelden alınan `vq_…` anahtarı (**önerilen**) |
| `VQ_MCP_USER` | E-posta ile kullanıcı seçimi (tek kullanıcılı yerel kurulum) |
| — | Hiçbiri yoksa veritabanındaki ilk kullanıcı |

Proje kökündeki `.mcp.json` her iki taşımayı da hazır içerir; anahtarı yapıştırıp
kullanabilirsiniz.

---

## 5. Bağlantıyı doğrulama

Sunucu bilgisi (kimlik doğrulama gerektirmez):

```bash
curl http://127.0.0.1:8000/mcp
```

El sıkışma ve araç listesi:

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer vq_ANAHTARINIZ" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Gerçek bir araç çağrısı:

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer vq_ANAHTARINIZ" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_safety_status","arguments":{}}}'
```

---

## 6. Güvenlik: bağlı araç neyi yapamaz

Bağladığınız araç sizin adınıza çalışır ama **sınırların dışına çıkamaz**:

| Kural | Nerede zorlanır |
|---|---|
| Tek işlemde en fazla %1.5 risk | `app/layers/l4_risk.py` |
| Stop-loss zorunlu, R/R en az 1:2 | `app/layers/l4_risk.py` |
| Portföy ısısı ve korelasyon tavanı | `app/layers/portfolio_risk.py` |
| Gerçek para yetkisi **verilemez** | Araç kayıt defterinde böyle bir araç yok |
| Gerçek paraya geçiş | Yalnızca kullanıcı yetkisi + kanıt kapısıyla |
| Acil fren açıkken yeni pozisyon | Engellenir |

Araçlar `readOnlyHint` / `destructiveHint` etiketleriyle sunulur; iyi istemciler
yazma işlemleri için size onay penceresi gösterir.

**Anahtar sızarsa:** Kontrol merkezi → Bağlantılar → ilgili anahtarın yanındaki
*İptal et*. Bağlı araç anında erişimini kaybeder; diğer anahtarlar etkilenmez.

---

## 7. Sık karşılaşılan sorunlar

| Belirti | Sebep | Çözüm |
|---|---|---|
| `Kimlik doğrulanamadı` | Anahtar yok, yanlış veya iptal edilmiş | Yeni anahtar üretin |
| `connect ECONNREFUSED` | Platform çalışmıyor | `start.bat` / `start.sh` ile başlatın |
| Araçlar görünmüyor | İstemci `tools/list` çağırmadı | İstemciyi yeniden başlatın |
| `Bilinmeyen araç` | Sürüm uyuşmazlığı | Platformu güncelleyip istemciyi yeniden bağlayın |
| Uzak makineden erişim | Sunucu yalnızca 127.0.0.1 dinliyor | `--host 0.0.0.0` ile başlatın ve **mutlaka** güvenlik duvarı/VPN kullanın |

> Uzak erişimde platformu doğrudan internete açmayın. Ters vekil (nginx/Caddy)
> arkasında HTTPS ile veya VPN üzerinden yayınlayın.
