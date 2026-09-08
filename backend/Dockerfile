# ============================================================================
#  VERDANTIX QUANT — Backend imajı
#  İki aşamalı: bağımlılıklar ayrı katmanda derlenir, çalışma imajı ince kalır.
# ============================================================================

FROM python:3.12-slim AS deps

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt


# --------------------------------------------------------------------------- #

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC \
    VQ_DATABASE_URL=sqlite:////app/data/verdantix.db \
    VQ_SECRETS_FILE=/app/data/secrets.env

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=deps /install /usr/local
COPY . .

# Kök olmayan kullanıcı + kalıcı veri dizini.
# /app/data'nın sahipliği imajda ayarlanır; Docker adlandırılmış birimi ilk
# oluştururken bu sahipliği devralır, böylece izin hatası yaşanmaz.
RUN useradd -m -u 1000 verdantix \
 && mkdir -p /app/data \
 && chown -R verdantix:verdantix /app

USER verdantix

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
