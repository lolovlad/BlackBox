#!/usr/bin/env sh
set -eu

# Устанавливает blackbox.service: копия в /opt/blackbox, venv, systemd.
# Служба запускается от root (см. deploy/systemd/blackbox.service).

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

mkdir -p /opt/blackbox
cp -a "$PROJECT_ROOT/." /opt/blackbox/
chown -R root:root /opt/blackbox
chmod +x /opt/blackbox/scripts/linux/create_env.sh /opt/blackbox/scripts/linux/run_blackbox.sh

if [ ! -f /opt/blackbox/.env ]; then
  echo "Creating default /opt/blackbox/.env (no TTY — defaults only)..."
  (cd /opt/blackbox && sh scripts/linux/create_env.sh)
fi

if [ ! -x /usr/local/bin/uv ] && [ ! -x /root/.local/bin/uv ]; then
  echo "uv not found — installing for root (official installer → usually ~/.local/bin)..."
  if ! command -v curl >/dev/null 2>&1; then
    echo "ERROR: нужен curl или установите uv в /usr/local/bin вручную."
    echo "См. https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
  fi
  curl -LsSf https://astral.sh/uv/install.sh | sh || {
    echo "ERROR: не удалось установить uv."
    exit 1
  }
fi

BB_UV=/usr/local/bin/uv
[ -x "$BB_UV" ] || BB_UV=/root/.local/bin/uv
if [ ! -x "$BB_UV" ]; then
  echo "ERROR: uv не найден в /usr/local/bin и /root/.local/bin."
  exit 1
fi

echo "Removing old .venv if present (fixes Permission denied from mixed owners)..."
rm -rf /opt/blackbox/.venv

echo "Creating venv: uv sync in /opt/blackbox..."
export PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin"
(cd /opt/blackbox && "$BB_UV" sync --frozen --no-dev)

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
echo "Installed. Service runs as root. Useful commands:"
echo "  systemctl status blackbox.service"
echo "  journalctl -u blackbox.service -f"
echo "  systemctl restart blackbox.service"
