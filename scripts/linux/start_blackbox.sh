#!/usr/bin/env sh
set -eu

# Единый скрипт запуска BlackBox: при отсутствии .env — мастер (интерактивно или
# только дефолты без TTY), затем uv sync, миграции, uvicorn.

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

ENV_FILE="$PROJECT_ROOT/.env"

if [ ! -f "$ENV_FILE" ]; then
  ENV_TMP="$(mktemp)"
  trap 'rm -f "$ENV_TMP"' EXIT

  DEFAULTS_ONLY=1
  if [ -t 0 ] 2>/dev/null; then
    DEFAULTS_ONLY=0
  fi

  add_kv() {
    _k="$1"
    _v="$2"
    printf '%s=%s\n' "$_k" "$_v" >> "$ENV_TMP"
  }

  prompt_kv() {
    _block="$1"
    _title="$2"
    _help="$3"
    _key="$4"
    _default="$5"

    if [ "$DEFAULTS_ONLY" -eq 1 ]; then
      add_kv "$_key" "$_default"
      return
    fi

    printf '\n'
    printf '══════════════════════════════════════════════════════════════\n'
    printf '  %s\n' "$_block"
    printf '══════════════════════════════════════════════════════════════\n'
    printf '%s\n' "$_title"
    printf '%s\n' "$_help"
    printf '\n'
    printf '  Переменная:     %s\n' "$_key"
    printf '  По умолчанию:   %s\n' "$_default"
    printf '\n'
    printf 'Введите новое значение или нажмите Enter, чтобы оставить по умолчанию.\n'
    printf '> '
    _val=""
    if ! read -r _val; then
      _val=""
    fi
    if [ -z "$_val" ]; then
      _val="$_default"
    fi
    add_kv "$_key" "$_val"
  }

  gen_secret() {
    if command -v openssl >/dev/null 2>&1; then
      openssl rand -hex 24
    else
      printf 'change-me-%s' "$(date +%s)"
    fi
  }

  if [ "$DEFAULTS_ONLY" -eq 0 ]; then
    printf '\n'
    printf '╔══════════════════════════════════════════════════════════════╗\n'
    printf '║         BlackBox — первичная настройка файла .env            ║\n'
    printf '╚══════════════════════════════════════════════════════════════╝\n'
    printf '\n'
    printf 'Сейчас будет создан файл:\n  %s\n' "$ENV_FILE"
    printf 'Настройки сгруппированы по блокам; в каждом шаге можно принять значение по умолчанию (Enter).\n'
    printf '\n'
    printf 'Нажмите Enter, чтобы начать...\n'
    read -r _ || true
  fi

  _DEFAULT_SECRET="$(gen_secret)"

  prompt_kv "Блок 1. Веб-интерфейс и безопасность" "SECRET_KEY" "Секретный ключ Flask (сессии, CSRF). В продакшене должен быть длинным и случайным." "SECRET_KEY" "$_DEFAULT_SECRET"
  prompt_kv "Блок 1. Веб-интерфейс и безопасность" "Адрес привязки HTTP-сервера" "0.0.0.0 — слушать на всех интерфейсах; 127.0.0.1 — только локально." "HOST" "0.0.0.0"
  prompt_kv "Блок 1. Веб-интерфейс и безопасность" "Порт HTTP" "Порт, на котором будет доступен веб-интерфейс." "PORT" "5000"
  prompt_kv "Блок 1. Веб-интерфейс и безопасность" "Точка входа Flask для CLI (миграции)" "Обычно не меняют." "FLASK_APP" "src.web_app:app"
  prompt_kv "Блок 1. Веб-интерфейс и безопасность" "HTTPS-only cookie (SESSION_COOKIE_SECURE)" "1 — только при работе за HTTPS-прокси; 0 — для обычного HTTP." "SESSION_COOKIE_SECURE" "0"

  prompt_kv "Блок 2. База и обслуживание" "Файл базы SQLite" "Путь к БД относительно каталога запуска или абсолютный." "BLACKBOX_DB_PATH" "instance/blackbox.db"
  prompt_kv "Блок 2. База и обслуживание" "Интервал фоновой очистки БД (минуты)" "Как часто планировщик проверяет устаревшие записи." "DB_CLEANUP_INTERVAL_MINUTES" "60"
  prompt_kv "Блок 2. База и обслуживание" "Хранить данные не дольше (дней)" "Записи старше этого срока могут быть удалены задачей обслуживания." "DB_RETENTION_DAYS" "30"

  prompt_kv "Блок 3. Видео (опционально)" "Каталог хранения видео" "Пусто — если видео не используется." "VIDEO_STORAGE_DIR" ""
  prompt_kv "Блок 3. Видео (опционально)" "Интервал сборки мусора видео (дней)" "Удаление старых файлов из VIDEO_STORAGE_DIR." "VIDEO_GC_INTERVAL_DAYS" "10"

  prompt_kv "Блок 4. Начальные пользователи (seed)" "Имя администратора" "Создаётся при первом запуске seed (если настроено)." "SEED_ADMIN_USERNAME" "admin"
  prompt_kv "Блок 4. Начальные пользователи (seed)" "Пароль администратора" "" "SEED_ADMIN_PASSWORD" "admin"
  prompt_kv "Блок 4. Начальные пользователи (seed)" "Имя обычного пользователя" "" "SEED_USER_USERNAME" "user"
  prompt_kv "Блок 4. Начальные пользователи (seed)" "Пароль обычного пользователя" "" "SEED_USER_PASSWORD" "user"

  sort -o "$ENV_FILE" "$ENV_TMP"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  trap - EXIT
  rm -f "$ENV_TMP"

  if [ "$DEFAULTS_ONLY" -eq 0 ]; then
    printf '\nГотово. Файл сохранён:\n  %s\n' "$ENV_FILE"
  else
    printf 'Создан .env со значениями по умолчанию: %s\n' "$ENV_FILE"
  fi
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: .env was not created: $ENV_FILE"
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
  : > "$PROJECT_ROOT/settings/settings.json"
fi

uv sync --frozen --no-dev
uv run flask db upgrade

exec uv run uvicorn src.web_app:app --host "$HOST" --port "$PORT" --interface wsgi --log-level info --access-log
