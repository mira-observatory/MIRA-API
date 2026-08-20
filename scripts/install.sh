#!/usr/bin/env sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON_BIN=${PYTHON:-python3}

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Error: no se encontro '$PYTHON_BIN'. Instala Python 3.11 o superior." >&2
    exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    echo "Error: se requiere Python 3.11 o superior." >&2
    exit 1
fi

echo "Creando el entorno virtual en $VENV_DIR..."
"$PYTHON_BIN" -m venv "$VENV_DIR"

echo "Instalando MIRA API y las dependencias de desarrollo..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -e "$PROJECT_DIR[dev]"

if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo "Se creo .env a partir de .env.example. Completa sus credenciales antes de ejecutar."
else
    echo "Se conservo el archivo .env existente."
fi

echo "Instalacion terminada. Ejecuta: ./scripts/run.sh"
