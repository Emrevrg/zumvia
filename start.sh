#!/usr/bin/env bash
# ============================================================
#  ZUMVIA — Linux / macOS tek komutla başlatma
# ============================================================
set -e
cd "$(dirname "$0")/backend"

if [ ! -d ".venv" ]; then
  echo "[1/4] Sanal ortam oluşturuluyor…"
  python3 -m venv .venv
fi

echo "[2/4] Bağımlılıklar kontrol ediliyor…"
./.venv/bin/python -m pip install -q -r requirements.txt

if [ ! -f ".env" ]; then
  echo "[3/4] Güvenlik anahtarları üretiliyor…"
  ./.venv/bin/python - <<'PY'
import base64, os, secrets
open(".env", "w").write(
    f"VQ_MASTER_KEY={base64.urlsafe_b64encode(os.urandom(32)).decode()}\n"
    f"VQ_JWT_SECRET={secrets.token_urlsafe(48)}\n"
    "VQ_DATABASE_URL=sqlite:///./zumvia.db\n"
    "VQ_LOG_LEVEL=INFO\n"
    "VQ_FORCE_PAPER_ONLY=false\n"
)
PY
  echo "    .env oluşturuldu — bu dosyayı kimseyle paylaşmayın."
else
  echo "[3/4] .env mevcut, atlanıyor."
fi

echo "[4/4] Sunucu başlatılıyor…"
echo
echo "    Tarayıcıdan açın:  http://localhost:8000"
echo "    Durdurmak için:    Ctrl+C"
echo
exec ./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
