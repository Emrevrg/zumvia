# ============================================================================
#  ZUMVIA — Next.js arayüzü (isteğe bağlı)
#  Yerleşik arayüz zaten backend :8000 üzerinde çalışır; bu imaj yalnızca
#  ayrı bir web katmanı isteyenler içindir:  docker compose --profile web up -d
# ============================================================================

FROM node:22-alpine AS build

WORKDIR /app

# API adresi DERLEME anında gömülür (Next.js NEXT_PUBLIC_* davranışı).
ARG NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY . .
RUN npm run build


# --------------------------------------------------------------------------- #

FROM node:22-alpine

ENV NODE_ENV=production
WORKDIR /app

COPY --from=build /app/package.json ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next
COPY --from=build /app/public ./public

RUN addgroup -g 1001 web && adduser -D -u 1001 -G web web && chown -R web:web /app
USER web

EXPOSE 3000
CMD ["npm", "start"]
