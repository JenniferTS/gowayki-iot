#!/bin/sh
# Entrypoint de Mosquitto para GoWayki IoT (eclipse-mosquitto:2, busybox sh).
#
# - Usuarios base (gowayki-iot, raspberry-pi, healthcheck) vienen de la env
#   MOSQUITTO_PASSWD_B64 (broker), igual que antes.
# - Usuarios del equipo los escribe el panel en el directorio compartido
#   $AUTH_DIR (passwd.dynamic, acl.dynamic, .version).
# - Un watcher revisa .version cada $WATCH_INTERVAL s; si cambió, fusiona
#   base + dinámico y envía SIGHUP a mosquitto (mismo PID tras exec), que recarga
#   password_file y acl_file sin cortar conexiones existentes.
set -eu
AUTH_DIR="${AUTH_DIR:-/mosquitto/auth}"
DATA="${DATA_DIR:-/mosquitto/data}"
CONF="${CONF_FILE:-/mosquitto/config/mosquitto.conf}"
LISTEN_PORT="${LISTEN_PORT:-1883}"
MAIN_PID=$$
WATCH_INTERVAL="${WATCH_INTERVAL:-2}"
RESERVED='^(gowayki-iot|raspberry-pi|healthcheck):'

mkdir -p "$DATA" "$(dirname "$CONF")"
echo "$MOSQUITTO_PASSWD_B64" | base64 -d > "$DATA/passwd.base"
cat > "$DATA/acl.base" <<'ACL'
# --- usuarios base (gestionados en el entorno) ---
user gowayki-iot
topic readwrite #

user raspberry-pi
topic readwrite paradero/#
topic readwrite gowayki/#

user healthcheck
topic read $SYS/#

ACL

merge() {
  cp "$DATA/passwd.base" "$DATA/passwd.new"
  cp "$DATA/acl.base" "$DATA/acl.new"
  if [ -f "$AUTH_DIR/passwd.dynamic" ]; then
    # nunca dejar que el panel sobreescriba usuarios base
    grep -Ev "$RESERVED" "$AUTH_DIR/passwd.dynamic" >> "$DATA/passwd.new" || true
  fi
  if [ -f "$AUTH_DIR/acl.dynamic" ]; then
    cat "$AUTH_DIR/acl.dynamic" >> "$DATA/acl.new"
  fi
  chown mosquitto:mosquitto "$DATA/passwd.new" "$DATA/acl.new" 2>/dev/null || true
  chmod 0600 "$DATA/passwd.new" "$DATA/acl.new"
  mv "$DATA/passwd.new" "$DATA/passwd"
  mv "$DATA/acl.new" "$DATA/acl"
}

merge
chown mosquitto:mosquitto "$DATA" 2>/dev/null || true
printf '%s\n' \
  "listener $LISTEN_PORT 0.0.0.0" \
  'allow_anonymous false' \
  "password_file $DATA/passwd" \
  "acl_file $DATA/acl" \
  'persistence true' \
  "persistence_location $DATA/" \
  'log_dest stdout' > "$CONF"

(
  last="$(cat "$AUTH_DIR/.version" 2>/dev/null || echo none)"
  while sleep "$WATCH_INTERVAL"; do
    cur="$(cat "$AUTH_DIR/.version" 2>/dev/null || echo none)"
    if [ "$cur" != "$last" ]; then
      last="$cur"
      if merge; then
        kill -HUP "$MAIN_PID" && echo "[auth-watch] passwd/acl recargados ($cur)"
      fi
    fi
  done
) &

exec mosquitto -c "$CONF"
