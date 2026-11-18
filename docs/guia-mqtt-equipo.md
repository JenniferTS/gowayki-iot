# Guía del equipo: conectarse al broker MQTT de GoWayki

Cada integrante tiene su **propio usuario MQTT**. Solo puedes publicar y suscribirte en
`gowayki/<tu-usuario>/#`; los topics de los demás están bloqueados.

## 1. Pide tus credenciales

El administrador crea tu usuario en `/admin` → pestaña **Usuarios MQTT** y te pasa:

| Dato | Valor |
|---|---|
| Host | `172-214-154-175.sslip.io` |
| Puerto | `1883` |
| Usuario | tu usuario (ej. `ana`) |
| Contraseña | se muestra **una sola vez**; guárdala. Si la pierdes, pide "Regenerar contraseña". |

> No subas tu contraseña a GitHub ni la pegues en grupos.

## 2. MQTT Explorer (laptop)

1. Descarga MQTT Explorer: <https://mqtt-explorer.com/> (Windows, macOS, Linux).
2. **+ Connections** → nueva conexión:
   - **Name:** GoWayki
   - **Protocol:** `mqtt://`
   - **Host:** `172-214-154-175.sslip.io`
   - **Port:** `1883`
   - **Validate certificate / Encryption (tls):** desactivados
   - **Username / Password:** los tuyos
3. **ADVANCED** → borra los topics por defecto (`#` y `$SYS/#`; no tienes permiso) y agrega `gowayki/<tu-usuario>/#`.
4. **CONNECT**.

### Probar

- Panel **Publish**: topic `gowayki/<tu-usuario>/potenciometro`, payload `1234` → aparece en la tarjeta "Demo en vivo" del admin.
- En el admin, selecciona tu usuario y activa el switch **LED** → verás `gowayki/<tu-usuario>/led` = `ON` / `OFF`.

### Problemas comunes

| Síntoma | Causa |
|---|---|
| `Not authorized` / desconexión inmediata | Usuario o contraseña incorrectos, o el usuario fue eliminado/regenerado. |
| Conecta pero no ves nada | Te suscribiste a `#`: cámbialo por `gowayki/<tu-usuario>/#`. |
| Publicas en `gowayki/otro/...` y no llega | Correcto: la ACL lo bloquea. |

## 3. ESP32

Sketch: [`docs/esp32/gowayki_mqtt_demo/gowayki_mqtt_demo.ino`](esp32/gowayki_mqtt_demo/gowayki_mqtt_demo.ino)
(potenciómetro en GPIO 34 → `gowayki/<usuario>/potenciometro`, LED en GPIO 2 ← `gowayki/<usuario>/led`).
Instala la librería **PubSubClient** (Nick O'Leary) y completa WiFi, host, usuario y contraseña.

Topics:

| Topic | Dirección | Payload |
|---|---|---|
| `gowayki/<usuario>/potenciometro` | ESP32 → broker | `0`–`4095` |
| `gowayki/<usuario>/led` | broker → ESP32 | `ON` / `OFF` |
