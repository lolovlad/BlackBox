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

chmod +x "$PROJECT_ROOT/scripts/linux/start_blackbox.sh"

if ! id -u blackbox >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /opt/blackbox --shell /usr/sbin/nologin blackbox
fi

mkdir -p /opt/blackbox
cp -a "$PROJECT_ROOT/." /opt/blackbox/
chown -R blackbox:blackbox /opt/blackbox

cp "$SERVICE_SRC" "$SERVICE_DST"

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
# Optional overrides for BlackBox systemd service
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
