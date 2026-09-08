"""
ARAŞTIRMA TEZGÂHI — veri → analiz → dağıtım → doğrulama → risk → rapor

Bir dil modeline "şu hisseye bakar mısın" demek ucuzdur ve sonucu güvenilmez.
Bu paket, finansal araştırmayı denetlenebilir bir üretim hattına çevirir:

    1. BRİF      kullanıcının cümlesi ölçülebilir bir araştırma sorusuna döner
    2. VERİ      her sayı kaynağıyla birlikte OLGU DEFTERİNE yazılır
    3. ANALİZ    uzman ajanlar kendi dilimlerinde paralel çalışır
    4. DOĞRULAMA her sayısal iddia deftere karşı BAĞIMSIZ yeniden hesaplanır
    5. RİSK      maruziyet, likidite ve en kötü senaryo kod ile ölçülür
    6. RAPOR     desteklenen, çelişen ve dayanaksız iddialar ayrı ayrı yazılır

Değişmez kural: modelin ürettiği hiçbir sayı doğrudan rapora girmez.
Deftere karşı doğrulanır; doğrulanamayan iddia silinmez, "DAYANAKSIZ" olarak
işaretlenir. Kullanıcı neyin ölçüldüğünü, neyin tahmin olduğunu her zaman
ayırt edebilmelidir.
"""
from .ledger import Fact, Ledger
from .pipeline import ResearchPipeline, Stage

__all__ = ["Fact", "Ledger", "ResearchPipeline", "Stage"]
