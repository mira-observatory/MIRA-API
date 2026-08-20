#!/usr/bin/env sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "Error: el entorno virtual no existe. Ejecuta ./scripts/install.sh primero." >&2
    exit 1
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "Error: falta .env. Copia .env.example y completa sus valores." >&2
    exit 1
fi

HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8000}

cd "$PROJECT_DIR"
exec "$PYTHON_BIN" -m uvicorn mira_api.main:app \
    --host "$HOST" \
    --port "$PORT" \
    --reload \
    "$@"
