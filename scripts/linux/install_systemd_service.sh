#!/usr/bin/env sh
set -eu

# Installs and enables blackbox.service for systemd.

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
SERVICE_SRC="$PROJECT_ROOT/deploy/systemd/blackbox.service"
SERVICE_DST="/etc/systemd/system/blackbox.service"
ENV_FILE="/etc/default/blackbox"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo sh scripts/linux/install_systemd_service.sh"
  exit 1
fi

if [ ! -f "$SERVICE_SRC" ]; then
  echo "Service template not found: $SERVICE_SRC"
  exit 1
fi

chmod +x "$PROJECT_ROOT/scripts/linux/create_env.sh" "$PROJECT_ROOT/scripts/linux/run_blackbox.sh"

if ! id -u blackbox >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /opt/blackbox --shell /usr/sbin/nologin blackbox
fi

mkdir -p /opt/blackbox
cp -a "$PROJECT_ROOT/." /opt/blackbox/
# Всё дерево — пользователь службы (иначе .venv от root / чужой UID → Permission denied у uv).
chown -R blackbox:blackbox /opt/blackbox
chmod +x /opt/blackbox/scripts/linux/create_env.sh /opt/blackbox/scripts/linux/run_blackbox.sh

if [ ! -f /opt/blackbox/.env ]; then
  echo "Creating default /opt/blackbox/.env (no TTY — defaults only)..."
  sudo -u blackbox sh /opt/blackbox/scripts/linux/create_env.sh
fi

if [ ! -x /opt/blackbox/.local/bin/uv ] && [ ! -x /usr/local/bin/uv ]; then
  echo "uv not found in /opt/blackbox/.local/bin or /usr/local/bin — installing for user blackbox (official installer)..."
  if ! command -v curl >/dev/null 2>&1; then
    echo "ERROR: нужен curl для авто-установки uv, либо установите uv вручную в /usr/local/bin."
    echo "См. https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
  fi
  install -d -o blackbox -g blackbox /opt/blackbox/.local/share /opt/blackbox/.local/bin
  curl -LsSf https://astral.sh/uv/install.sh | sudo -u blackbox env HOME=/opt/blackbox sh || {
    echo "ERROR: не удалось скачать/установить uv. Установите вручную и повторите скрипт."
    exit 1
  }
fi

if [ ! -x /opt/blackbox/.local/bin/uv ] && [ ! -x /usr/local/bin/uv ]; then
  echo "ERROR: после установки uv по-прежнему нет в ожидаемых путях."
  exit 1
fi

BB_UV=/opt/blackbox/.local/bin/uv
[ -x "$BB_UV" ] || BB_UV=/usr/local/bin/uv
echo "Creating/updating venv as user blackbox (uv sync)..."
chown -R blackbox:blackbox /opt/blackbox
sudo -u blackbox env HOME=/opt/blackbox PATH="/opt/blackbox/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  sh -c "cd /opt/blackbox && \"$BB_UV\" sync --frozen --no-dev"

cp "$SERVICE_SRC" "$SERVICE_DST"

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
# Optional overrides for BlackBox systemd service
# Если uv в нестандартном месте: UV_BINARY=/полный/путь/к/uv
HOST=0.0.0.0
PORT=5000
APP_TIMEZONE=Europe/Moscow
MODBUS_PORT=/dev/ttyAMA0
MODBUS_SLAVE=1
MODBUS_BAUDRATE=9600
MODBUS_TIMEOUT=0.35
MODBUS_INTERVAL=0.12
MODBUS_ADDRESS_OFFSET=1
RAM_BATCH_SIZE=60
SECRET_KEY=change-me
EOF
fi

systemctl daemon-reload
systemctl enable --now blackbox.service
systemctl status --no-pager blackbox.service || true

echo
echo "Installed. Useful commands:"
echo "  systemctl status blackbox.service"
echo "  journalctl -u blackbox.service -f"
echo "  systemctl restart blackbox.service"
