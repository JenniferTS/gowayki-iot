#!/usr/bin/env bash
# install.sh — GoWayki Pi agent (MQTT client → broker remoto; NO Mosquitto local)
# Uso: curl -fsSL http://127.0.0.1:8000/pi/install.sh | sudo bash
set -euo pipefail

PANEL_URL="${PANEL_URL:-${API_BASE_URL:-http://127.0.0.1:8000}}"
PANEL_URL="${PANEL_URL%/}"
INSTALL_DIR="${INSTALL_DIR:-/opt/gowayki-pi}"
SERVICE_USER="${SERVICE_USER:-gowayki_user}"
API_BASE_URL="${API_BASE_URL:-$PANEL_URL}"
MQTT_HOST="${MQTT_HOST:-localhost}"
MQTT_PORT="${MQTT_PORT:-1883}"

echo ">>> GoWayki Pi agent installer"
echo "    panel:       $PANEL_URL"
echo "    install dir: $INSTALL_DIR"
echo "    user:        $SERVICE_USER"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Ejecuta con sudo: curl -fsSL ${PANEL_URL}/pi/install.sh | sudo bash"
  exit 1
fi

echo ">>> apt packages…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip curl ca-certificates \
  chromium-browser unclutter || \
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip curl ca-certificates \
  chromium unclutter || true

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  echo ">>> creating user $SERVICE_USER"
  useradd -m -s /bin/bash "$SERVICE_USER"
fi

echo ">>> installing agent files to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" /etc/gowayki /var/lib/gowayki
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"

fetch_from_panel() {
  local name="$1"
  echo "    download ${PANEL_URL}/pi/${name}"
  curl -fsSL "${PANEL_URL}/pi/${name}" -o "${INSTALL_DIR}/${name}"
}

if [[ -n "${SCRIPT_DIR}" && -f "${SCRIPT_DIR}/main.py" ]]; then
  cp -f "$SCRIPT_DIR/main.py" "$INSTALL_DIR/"
  cp -f "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
  cp -f "$SCRIPT_DIR/gowayki-pi.service" "$INSTALL_DIR/"
  cp -f "$SCRIPT_DIR/config.example.env" "$INSTALL_DIR/"
else
  fetch_from_panel main.py
  fetch_from_panel requirements.txt
  fetch_from_panel gowayki-pi.service
  fetch_from_panel config.example.env
fi

echo ">>> python venv…"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

if [[ ! -f /etc/gowayki/agent.env ]]; then
  echo ">>> writing /etc/gowayki/agent.env"
  cat > /etc/gowayki/agent.env <<EOF
API_BASE_URL=${API_BASE_URL}
MQTT_HOST=${MQTT_HOST}
MQTT_PORT=${MQTT_PORT}
MQTT_USER=${MQTT_USER:-}
MQTT_PASSWORD=${MQTT_PASSWORD:-}
MQTT_TOPIC_PREFIX=gowayki
ENABLE_KIOSK_DISPLAY=1
EOF
  chmod 640 /etc/gowayki/agent.env
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" /var/lib/gowayki
chown root:"$SERVICE_USER" /etc/gowayki /etc/gowayki/agent.env
chmod 750 /etc/gowayki

if [[ ! -f /etc/sudoers.d/gowayki-pi ]]; then
  echo "$SERVICE_USER ALL=(root) NOPASSWD: /sbin/reboot, /usr/sbin/reboot" > /etc/sudoers.d/gowayki-pi
  chmod 440 /etc/sudoers.d/gowayki-pi
fi

echo ">>> systemd unit…"
sed "s/User=gowayki_user/User=${SERVICE_USER}/; s/Group=gowayki_user/Group=${SERVICE_USER}/" \
  "$INSTALL_DIR/gowayki-pi.service" > /etc/systemd/system/gowayki-pi.service
systemctl daemon-reload
systemctl enable gowayki-pi.service
systemctl restart gowayki-pi.service || true

echo
echo ">>> Instalación lista."
echo "1) En ${PANEL_URL}/admin crea un dispositivo tipo Raspberry Pi y genera código."
echo "2) En esta Pi:"
echo "     echo 123456 | sudo tee /etc/gowayki/pair.code"
echo "     sudo systemctl restart gowayki-pi"
echo "3) Logs: journalctl -u gowayki-pi -f"
