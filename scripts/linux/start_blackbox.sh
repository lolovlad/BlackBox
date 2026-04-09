#!/usr/bin/env sh
set -eu

# Starts BlackBox web app with uv in foreground.
# Intended to be used by systemd service.

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv is not installed. https://docs.astral.sh/uv/"
  exit 1
fi

mkdir -p "$PROJECT_ROOT/instance" "$PROJECT_ROOT/settings"

export BLACKBOX_DB_PATH="${BLACKBOX_DB_PATH:-$PROJECT_ROOT/instance/blackbox.db}"
export MODBUS_PORT="${MODBUS_PORT:-/dev/ttyAMA0}"
export MODBUS_SLAVE="${MODBUS_SLAVE:-1}"
export MODBUS_BAUDRATE="${MODBUS_BAUDRATE:-9600}"
export MODBUS_TIMEOUT="${MODBUS_TIMEOUT:-0.35}"
export MODBUS_INTERVAL="${MODBUS_INTERVAL:-0.12}"
export MODBUS_ADDRESS_OFFSET="${MODBUS_ADDRESS_OFFSET:-1}"
export RAM_BATCH_SIZE="${RAM_BATCH_SIZE:-60}"
export APP_TIMEZONE="${APP_TIMEZONE:-Europe/Moscow}"
export SECRET_KEY="${SECRET_KEY:-change-me}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-5000}"
export FLASK_APP="${FLASK_APP:-src.web_app:app}"

if [ ! -f "$PROJECT_ROOT/settings/settings.json" ]; then
  : > "$PROJECT_ROOT/settings/settings.json"
fi

uv sync --frozen --no-dev
uv run flask db upgrade

exec uv run uvicorn src.web_app:app \
  --host "$HOST" \
  --port "$PORT" \
  --interface wsgi \
  --log-level info \
  --access-log
