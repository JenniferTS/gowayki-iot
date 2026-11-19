#!/usr/bin/env bash
# install.sh — GoWayki Pi agent (MQTT client → VM broker; NO local Mosquitto)
# Uso: curl -fsSL https://paradero.172-214-154-175.sslip.io/pi/install.sh | sudo bash
set -euo pipefail

PANEL_URL="${PANEL_URL:-${API_BASE_URL:-https://paradero.172-214-154-175.sslip.io}}"
PANEL_URL="${PANEL_URL%/}"
INSTALL_DIR="${INSTALL_DIR:-/opt/gowayki-pi}"
SERVICE_USER="${SERVICE_USER:-gowayki_user}"
API_BASE_URL="${API_BASE_URL:-$PANEL_URL}"
MQTT_HOST="${MQTT_HOST:-172-214-154-175.sslip.io}"
MQTT_PORT="${MQTT_PORT:-8883}"
MQTT_TLS="${MQTT_TLS:-1}"

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

# Credenciales MQTT (archivo solo en el panel/VM, no en git público)
if curl -fsSL "${PANEL_URL}/pi/mqtt.env" -o /tmp/gowayki-mqtt.env 2>/dev/null; then
  # shellcheck disable=SC1091
  set -a
  # shellcheck source=/dev/null
  source /tmp/gowayki-mqtt.env
  set +a
  rm -f /tmp/gowayki-mqtt.env
  echo ">>> mqtt.env del panel cargado (user=${MQTT_USER:-})"
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
MQTT_TLS=${MQTT_TLS}
MQTT_USER=${MQTT_USER:-}
MQTT_PASSWORD=${MQTT_PASSWORD:-}
MQTT_TOPIC_PREFIX=gowayki
ENABLE_KIOSK_DISPLAY=1
EOF
  chmod 640 /etc/gowayki/agent.env
else
  # Asegurar puerto TLS si el env viejo aún tiene 1883
  if grep -q '^MQTT_PORT=1883$' /etc/gowayki/agent.env; then
    echo ">>> migrando MQTT_PORT 1883 → 8883 en agent.env"
    sed -i 's/^MQTT_PORT=1883$/MQTT_PORT=8883/' /etc/gowayki/agent.env
    grep -q '^MQTT_TLS=' /etc/gowayki/agent.env || echo 'MQTT_TLS=1' >> /etc/gowayki/agent.env
  fi
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
echo "1) En ${PANEL_URL}/iot/admin crea un dispositivo tipo Raspberry Pi y genera código."
echo "2) En esta Pi:"
echo "     echo 123456 | sudo tee /etc/gowayki/pair.code"
echo "     sudo systemctl restart gowayki-pi"
echo "3) Logs: journalctl -u gowayki-pi -f"
echo "Nota: MQTT usa el puerto 8883 (TLS). El pairing/heartbeat va por HTTPS /api/kiosk/."
