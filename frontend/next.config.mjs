/** @type {import('next').NextConfig} */
// ÖNEMLİ: Bu değer DERLEME anında gömülür. Backend farklı bir adreste
// çalışıyorsa `next build` komutunu o adresle çalıştırın:
//     NEXT_PUBLIC_API_URL=http://sunucu:8000 npm run build
// (veya proje kökündeki .env.local dosyasına yazıp yeniden derleyin)
const API = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: true,
  // Kök dizini açıkça belirt: üst klasörlerdeki lock dosyaları yanlışlıkla
  // proje kökü sanılmasın.
  turbopack: { root: import.meta.dirname },
  // Next'in otomatik ajan dosyaları üretmesini kapat (depoyu temiz tutar).
  agentRules: false,
  // Geliştirmede CORS derdi olmasın diye API çağrıları backend'e vekillenir.
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API}/api/:path*` },
    ];
  },
};

export default nextConfig;
