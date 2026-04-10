#!/usr/bin/env sh
set -eu

# Запуск веб-приложения на переднем плане (логи в этот терминал).
# Остановка: Ctrl+C
#
# Перед первым запуском нужен .env:
#   sh scripts/linux/create_env.sh
#
# Для автозапуска при загрузке устройства используйте systemd отдельно
# (см. DEPLOY_ON_DEVICE_RU.md и scripts/linux/install_systemd_service.sh).

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

ENV_FILE="$PROJECT_ROOT/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: нет файла .env: $ENV_FILE"
  echo "Создайте его командой:  sh scripts/linux/create_env.sh"
  exit 1
fi

set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv is not installed. https://docs.astral.sh/uv/"
  exit 1
fi

mkdir -p "$PROJECT_ROOT/instance" "$PROJECT_ROOT/settings"

export BLACKBOX_DB_PATH="${BLACKBOX_DB_PATH:-$PROJECT_ROOT/instance/blackbox.db}"
export SECRET_KEY="${SECRET_KEY:-change-me}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-5000}"
export FLASK_APP="${FLASK_APP:-src.web_app:app}"

if [ ! -f "$PROJECT_ROOT/settings/settings.json" ]; then
  printf '%s\n' '{"requests":[{"name":"hr","fc":3,"address":0,"count":1}],"fields":[{"name":"r0","type":"uint16","source":"hr","address":0}]}' \
    > "$PROJECT_ROOT/settings/settings.json"
fi

uv sync --frozen --no-dev
uv run flask db upgrade

exec uv run uvicorn src.web_app:app --host "$HOST" --port "$PORT" --interface wsgi --log-level info --access-log
