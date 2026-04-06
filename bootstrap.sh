#!/usr/bin/env sh
set -eu

# Bootstrap script for full BlackBox startup from zero:
# 1) install dependencies
# 2) initialize/apply DB migrations
# 3) run app with Uvicorn

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$PROJECT_ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' is not installed. Install uv first: https://docs.astral.sh/uv/"
  exit 1
fi

echo "[1/5] Installing dependencies with uv..."
uv sync

echo "[2/5] Exporting default environment variables..."
export BLACKBOX_DB_PATH="${BLACKBOX_DB_PATH:-blackbox.db}"
export MODBUS_PORT="${MODBUS_PORT:-/dev/ttyAMA0}"
export MODBUS_SLAVE="${MODBUS_SLAVE:-1}"
export MODBUS_BAUDRATE="${MODBUS_BAUDRATE:-9600}"
export MODBUS_TIMEOUT="${MODBUS_TIMEOUT:-0.35}"
export MODBUS_INTERVAL="${MODBUS_INTERVAL:-0.12}"
export MODBUS_ADDRESS_OFFSET="${MODBUS_ADDRESS_OFFSET:-1}"
export RAM_BATCH_SIZE="${RAM_BATCH_SIZE:-60}"
export APP_USERNAME="${APP_USERNAME:-admin}"
export APP_PASSWORD="${APP_PASSWORD:-admin}"
export SECRET_KEY="${SECRET_KEY:-change-me}"
export FLASK_APP="${FLASK_APP:-src.web_app:app}"
export HOST="${HOST:-10.109.114.106}"
export PORT="${PORT:-5000}"

echo "[3/5] Preparing Flask-Migrate repository..."
if [ ! -d "migrations" ] || [ ! -f "migrations/env.py" ]; then
  uv run flask db init
fi

echo "[4/5] Applying database migrations..."
uv run flask db upgrade

echo "[5/5] Starting web application..."
echo "Open: http://${HOST}:${PORT}/"
echo "Uvicorn logs: access=on level=debug"
exec uv run uvicorn src.web_app:app --host "$HOST" --port "$PORT" --interface wsgi --log-level debug --access-log
