#!/usr/bin/env bash
# Lance l'API FastAPI en production avec uvicorn.
# Usage : bash api/start.sh [port]

set -e

PORT="${1:-8000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Charge le .env si présent
if [ -f "$ROOT/.env" ]; then
  set -a
  source "$ROOT/.env"
  set +a
fi

# Vérifie que uvicorn est disponible
if ! command -v uvicorn &>/dev/null; then
  echo "[ERROR] uvicorn introuvable. Lancez : pip install fastapi uvicorn asyncpg python-dotenv"
  exit 1
fi

echo "========================================"
echo "  GeoLeaad API — port $PORT"
echo "  Répertoire : $ROOT"
echo "========================================"

cd "$ROOT"

exec uvicorn api.main:app \
  --host 127.0.0.1 \
  --port "$PORT" \
  --workers 1 \
  --log-level info \
  --access-log
