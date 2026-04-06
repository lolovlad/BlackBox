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

echo "[1/6] Installing dependencies with uv..."
uv sync

echo "[2/6] Exporting default environment variables..."
export BLACKBOX_DB_PATH="${BLACKBOX_DB_PATH:-instance/blackbox.db}"
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
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-5000}"
mkdir -p "$(dirname "$BLACKBOX_DB_PATH")"

echo "[3/5] Preparing Flask-Migrate repository..."
if [ ! -d "migrations" ] || [ ! -f "migrations/env.py" ]; then
  uv run flask db init
fi

echo "[4/6] Applying database migrations..."
uv run flask db upgrade

echo "[4.1/6] Verifying required tables..."
if ! uv run python - <<'PY'
import os
import sqlite3
import sys

db_path = os.getenv("BLACKBOX_DB_PATH", "instance/blackbox.db")
conn = sqlite3.connect(db_path)
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = {row[0] for row in cur.fetchall()}
conn.close()

required = {"analogs", "discretes", "alarms", "type_user", "user"}
missing = sorted(required - tables)
if missing:
    print("Missing tables:", ",".join(missing))
    sys.exit(1)
print("Schema check OK")
PY
then
  echo "Schema mismatch detected. Forcing migration replay (stamp base -> upgrade)..."
  uv run flask db stamp base
  uv run flask db upgrade
fi

echo "[4.2/6] Final schema check..."
uv run python - <<'PY'
import os
import sqlite3
import sys

db_path = os.getenv("BLACKBOX_DB_PATH", "instance/blackbox.db")
conn = sqlite3.connect(db_path)
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = {row[0] for row in cur.fetchall()}
conn.close()

required = {"analogs", "discretes", "alarms", "type_user", "user"}
missing = sorted(required - tables)
if missing:
    print(f"ERROR: required tables still missing in {db_path}: {','.join(missing)}")
    sys.exit(2)
print("Final schema check OK")
PY

echo "[5/6] Seeding initial users and roles..."
DISABLE_MODBUS_COLLECTOR=1 uv run python -m src.seed

echo "[6/6] Starting web application..."
PUBLIC_IP="${PUBLIC_IP:-10.109.114.106}"
echo "Open: http://${PUBLIC_IP}:${PORT}/"
echo "Uvicorn logs: access=on level=debug"
exec uv run uvicorn src.web_app:app --host "$HOST" --port "$PORT" --interface wsgi --log-level debug --access-log
