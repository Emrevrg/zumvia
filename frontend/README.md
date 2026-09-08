# Next.js Arayüzü (opsiyonel)

Bu dizin, klasik panel ekranlarının **React + Tailwind** sürümüdür:
portföy, botlar, bot detayı (lightweight-charts ile grafik), yeni bot sihirbazı,
analiz, geri test ve anahtar kasası.

## Bilmeniz gerekenler

- **Zorunlu değildir.** Backend kendi arayüzünü `http://localhost:8000` adresinde
  sunar; hiçbir Node.js kurulumu gerektirmez ve **Komuta Merkezi (ajan sohbeti)
  yalnızca o arayüzde bulunur.**
- Bu sürüm **Komuta Merkezi dahil** tüm ekranları kapsar: ajan sohbeti, adım
  adım araç kartları, karar kalitesi modu (Tek Model / Konsey / Sağlamcı),
  otonom anahtarı ve canlı olay akışı.
- Kurulum için ~500 MB boş disk gerekir (`node_modules`). Diskiniz doluysa
  yerleşik arayüz zaten aynı işi Node.js olmadan yapar.

## Çalıştırma

```bash
cd frontend
npm install
npm run dev
```

`http://localhost:3000` — API çağrıları `next.config.mjs` içindeki rewrite ile
backend'e (`http://127.0.0.1:8000`) vekillenir.

Farklı bir backend adresi için — bu değer **derleme anında gömülür**, bu yüzden
build komutuna vermeniz gerekir:

```bash
NEXT_PUBLIC_API_URL=http://sunucu-adresi:8000 npm run build
npm start
```

Geliştirme modunda da aynı şekilde:

```bash
NEXT_PUBLIC_API_URL=http://sunucu-adresi:8000 npm run dev
```

Adres yanlışsa API çağrıları `ECONNREFUSED` ile 500 döner — sunucu loglarında
hedef port açıkça görünür.

## Yapı

```
app/
├── command/page.tsx    # Komuta Merkezi (ajan sohbeti + araç adımları)
├── page.tsx            # Portföy paneli
├── login/              # Giriş / kayıt
├── bots/               # Bot listesi, detay, yeni bot sihirbazı
├── analysis/           # Piyasa analizi
├── backtest/           # Geri test
└── vault/              # Anahtar kasası
components/
├── Shell.tsx           # Kabuk + istatistik kartı
├── PriceChart.tsx      # lightweight-charts mum grafiği
└── LogTerminal.tsx     # Canlı olay terminali (WebSocket)
lib/api.ts              # API istemcisi, tipler, biçimleyiciler
lib/agent.ts            # Komuta ajanı istemcisi + araç özetleyiciler
```
