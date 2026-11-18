# Usuarios MQTT — arquitectura

## Qué hace

Pestaña **Usuarios MQTT** en `/admin`:

- listar / crear / eliminar usuarios y regenerar contraseña (24 caracteres aleatorios, se muestra **una sola vez**);
- en SQLite (`mqtt_users`) solo se guarda el hash Mosquitto `$7$` (PBKDF2-SHA512, 101 iteraciones, sal de 12 bytes) —
  implementado en `app/services/mqtt_users.py` y verificado byte a byte contra `mosquitto_passwd` (`tests/test_mqtt_hash.py`);
- ACL: cada usuario solo puede usar `gowayki/<usuario>/#`;
- demo en vivo: tarjeta con `gowayki/<usuario>/potenciometro` y switch ON/OFF que publica (retain) en `gowayki/<usuario>/led`.

API (header `X-Admin-Token`): `GET/POST /api/admin/mqtt-users`, `POST /api/admin/mqtt-users/{u}/regenerate`,
`DELETE /api/admin/mqtt-users/{u}`, `GET /api/admin/mqtt-users/live`, `POST /api/admin/mqtt-users/{u}/led` (`{"on": true}`).

## Mecanismo: directorio compartido + SIGHUP

Se eligió frente al plugin dynamic-security porque:

- no requiere un usuario admin con permisos sobre `$CONTROL/#` ni mantener un segundo almacén sincronizado con SQLite;
- SQLite sigue siendo la única fuente de verdad: el panel regenera los archivos completos en cada cambio y al arrancar (idempotente);
- si el panel está caído, el broker sigue funcionando con los últimos archivos.

Flujo:

1. El panel escribe de forma atómica (`tmp + rename`) en `MQTT_AUTH_DIR`:
   `passwd.dynamic`, `acl.dynamic` y `.version` (timestamp).
2. Ese directorio se monta **solo lectura** en Mosquitto (ej. `/mosquitto/auth`).
3. `deploy/mosquitto/entrypoint.sh` arranca un watcher (cada 2 s) que, si `.version` cambia, fusiona
   **usuarios base** (`MOSQUITTO_PASSWD_B64`: gowayki-iot, raspberry-pi, healthcheck) + dinámicos en
   `/mosquitto/data/passwd` y `/mosquitto/data/acl`, y envía `SIGHUP` a mosquitto (recarga sin cortar clientes).
4. Las líneas de usuarios base en `passwd.dynamic` se descartan (nombres reservados en la API).

ACL base (en el entrypoint):

| Usuario | Permisos |
|---|---|
| `gowayki-iot` (panel/kiosco) | `readwrite #` |
| `raspberry-pi` | `readwrite paradero/#`, `readwrite gowayki/#` |
| `healthcheck` | `read $SYS/#` |
| usuarios del panel | `readwrite gowayki/<usuario>/#` |

Nota: tras eliminar/regenerar, las conexiones **nuevas** se rechazan en ~2 s; una sesión ya abierta puede seguir hasta que se reconecte.

## Variables del panel

- `MQTT_HOST` / `MQTT_PORT` / `MQTT_USER` / `MQTT_PASSWORD` — bridge del panel hacia el broker
- `MQTT_AUTH_DIR` — directorio donde escribe passwd/acl dinámicos
- `MQTT_PUBLIC_HOST` / `MQTT_PUBLIC_PORT` — host que se muestra a los usuarios del equipo

## Pruebas

- `pytest tests/test_mqtt_hash.py` — hash == `mosquitto_passwd`, validación de usuarios, ACL.
- `tests/e2e_mqtt_users.py` — panel + mosquitto local con `deploy/mosquitto/entrypoint.sh`
  (`DATA_DIR`, `CONF_FILE`, `AUTH_DIR`, `LISTEN_PORT` permiten correrlo sin Docker).
