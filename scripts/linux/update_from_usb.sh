#!/usr/bin/env bash
set -euo pipefail

# Обновление BlackBox с USB-флешки через rsync.
# Скрипт интерактивный:
# 1) ищет смонтированные съемные носители
# 2) просит выбрать источник
# 3) просит указать путь проекта и сервис
# 4) останавливает сервис, обновляет файлы, запускает сервис

if ! command -v rsync >/dev/null 2>&1; then
  echo "ERROR: rsync не установлен. Установите и повторите."
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите с root-правами:"
  echo "  sudo bash scripts/linux/update_from_usb.sh"
  exit 1
fi

_trim() {
  local x="${1:-}"
  x="${x#"${x%%[![:space:]]*}"}"
  x="${x%"${x##*[![:space:]]}"}"
  printf "%s" "$x"
}

_detect_usb_mounts() {
  local out=()
  if command -v lsblk >/dev/null 2>&1; then
    while IFS= read -r line; do
      [ -n "$line" ] && out+=("$line")
    done < <(lsblk -rpo RM,MOUNTPOINT | awk '$1=="1" && $2!="" {print $2}')
  fi

  # Fallback: типичные каталоги монтирования.
  if [ "${#out[@]}" -eq 0 ]; then
    local p
    for p in /media/* /media/*/* /mnt/* /run/media/*/*; do
      [ -d "$p" ] && out+=("$p")
    done
  fi

  # uniq
  local uniq=()
  local item
  for item in "${out[@]}"; do
    local seen=0
    local u
    for u in "${uniq[@]:-}"; do
      if [ "$u" = "$item" ]; then
        seen=1
        break
      fi
    done
    [ "$seen" -eq 0 ] && uniq+=("$item")
  done
  printf "%s\n" "${uniq[@]:-}"
}

echo "=== Обновление BlackBox с USB (rsync) ==="
echo

mapfile -t CANDIDATES < <(_detect_usb_mounts || true)
if [ "${#CANDIDATES[@]}" -eq 0 ]; then
  echo "Не удалось автоматически найти флешку."
  echo "Подключите флешку и проверьте точки монтирования (например, lsblk)."
  read -r -p "Введите путь к флешке вручную: " USB_MOUNT
  USB_MOUNT="$(_trim "$USB_MOUNT")"
else
  echo "Найденные точки монтирования съемных носителей:"
  i=1
  for m in "${CANDIDATES[@]}"; do
    echo "  [$i] $m"
    i=$((i + 1))
  done
  echo "  [0] Ввести путь вручную"
  read -r -p "Выберите номер: " PICK
  PICK="$(_trim "$PICK")"
  if [ "${PICK:-}" = "0" ] || [ -z "${PICK:-}" ]; then
    read -r -p "Введите путь к флешке вручную: " USB_MOUNT
    USB_MOUNT="$(_trim "$USB_MOUNT")"
  else
    if ! [[ "$PICK" =~ ^[0-9]+$ ]] || [ "$PICK" -lt 1 ] || [ "$PICK" -gt "${#CANDIDATES[@]}" ]; then
      echo "Некорректный выбор."
      exit 1
    fi
    USB_MOUNT="${CANDIDATES[$((PICK - 1))]}"
  fi
fi

if [ ! -d "${USB_MOUNT:-}" ]; then
  echo "ERROR: путь флешки не существует или не каталог: ${USB_MOUNT:-<empty>}"
  exit 1
fi

read -r -p "Путь к проекту на флешке [по умолчанию: $USB_MOUNT/BlackBox]: " SRC_DIR
SRC_DIR="$(_trim "$SRC_DIR")"
if [ -z "$SRC_DIR" ]; then
  SRC_DIR="$USB_MOUNT/BlackBox"
fi
if [ ! -d "$SRC_DIR" ]; then
  echo "ERROR: каталог проекта на флешке не найден: $SRC_DIR"
  exit 1
fi

read -r -p "Путь установленного проекта на устройстве [/opt/blackbox]: " DST_DIR
DST_DIR="$(_trim "$DST_DIR")"
[ -z "$DST_DIR" ] && DST_DIR="/opt/blackbox"
if [ ! -d "$DST_DIR" ]; then
  echo "ERROR: каталог назначения не найден: $DST_DIR"
  exit 1
fi

read -r -p "Имя systemd-сервиса [blackbox.service]: " SERVICE_NAME
SERVICE_NAME="$(_trim "$SERVICE_NAME")"
[ -z "$SERVICE_NAME" ] && SERVICE_NAME="blackbox.service"

echo
echo "Источник:     $SRC_DIR"
echo "Назначение:   $DST_DIR"
echo "Сервис:       $SERVICE_NAME"
echo
echo "По умолчанию сохраняются локальные runtime-файлы:"
echo "  - .env"
echo "  - .venv/"
echo "  - instance/"
echo "  - settings/app_runtime.json"
echo
read -r -p "Продолжить обновление? [y/N]: " CONFIRM
CONFIRM="$(_trim "$CONFIRM")"
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
  echo "Отменено."
  exit 0
fi

echo
echo "⛔ Остановка сервиса $SERVICE_NAME ..."
systemctl stop "$SERVICE_NAME" || true

echo "📂 Обновление файлов rsync ..."
rsync -a --delete \
  --exclude '.env' \
  --exclude '.venv/' \
  --exclude 'instance/' \
  --exclude 'settings/app_runtime.json' \
  "$SRC_DIR"/ "$DST_DIR"/

echo "✅ Права на скрипты запуска ..."
chmod +x "$DST_DIR/scripts/linux/run_blackbox.sh" "$DST_DIR/scripts/linux/create_env.sh" || true

echo "🚀 Запуск сервиса $SERVICE_NAME ..."
systemctl start "$SERVICE_NAME"

echo
echo "=== Готово ==="
systemctl --no-pager --full status "$SERVICE_NAME" || true
echo
echo "Логи (последние 100 строк):"
journalctl -u "$SERVICE_NAME" -n 100 --no-pager || true
