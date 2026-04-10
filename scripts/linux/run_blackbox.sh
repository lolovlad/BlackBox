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

# Под systemd PATH часто не содержит каталог, куда поставили uv (типично ~/.local/bin).
HOME="${HOME:-$(getent passwd "$(id -un)" 2>/dev/null | cut -d: -f6)}"
HOME="${HOME:-/opt/blackbox}"
export PATH="${PROJECT_ROOT}/.local/bin:${HOME}/.local/bin:/usr/local/bin:/usr/local/sbin:${PATH:-/usr/bin:/bin}"

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

UV_BIN="${UV_BINARY:-}"
if [ -z "$UV_BIN" ]; then
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
  elif [ -x /usr/local/bin/uv ]; then
    UV_BIN=/usr/local/bin/uv
  elif [ -x "${HOME}/.local/bin/uv" ]; then
    UV_BIN="${HOME}/.local/bin/uv"
  elif [ -x "${PROJECT_ROOT}/.local/bin/uv" ]; then
    UV_BIN="${PROJECT_ROOT}/.local/bin/uv"
  fi
fi

if [ -z "$UV_BIN" ] || [ ! -x "$UV_BIN" ]; then
  echo "ERROR: uv не найден в PATH и в типичных каталогах."
  echo "Установите uv для пользователя службы (часто: HOME=/opt/blackbox → ~/.local/bin) или в /usr/local/bin."
  echo "Инструкция: https://docs.astral.sh/uv/getting-started/installation/"
  echo "Либо задайте полный путь: в /etc/default/blackbox добавьте строку UV_BINARY=/полный/путь/к/uv"
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

"$UV_BIN" sync --frozen --no-dev
"$UV_BIN" run flask db upgrade

exec "$UV_BIN" run uvicorn src.web_app:app --host "$HOST" --port "$PORT" --interface wsgi --log-level info --access-log
