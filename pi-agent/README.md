# Agente Raspberry Pi — GoWayki

Cliente IoT para Raspberry Pi. **No instala Mosquitto local**: se conecta al broker en el entorno y se vincula al panel con código de 6 dígitos.

## Identidad

1. Admin (`https://paradero.172-214-154-175.sslip.io/admin`) → dispositivo tipo **Raspberry Pi** → código 6 dígitos.
2. La Pi hace `POST /api/kiosk/pair` y guarda `kiosk_id` + `token` en `/var/lib/gowayki/session.json`.
3. MQTT usa topics `gowayki/{kiosk_id}/estado|telemetria|comandos`.

## Instalación (en la Pi)

El repo es privado: el instalador y los archivos del agente se sirven desde el panel.

```bash
curl -fsSL https://paradero.172-214-154-175.sslip.io/pi/install.sh | sudo bash
```

Vincular con el código de `/admin`:

```bash
echo CODIGO6 | sudo tee /etc/gowayki/pair.code
sudo systemctl restart gowayki-pi
journalctl -u gowayki-pi -f
```

Archivos públicos del panel: `/pi/install.sh`, `/pi/main.py`, `/pi/requirements.txt`, `/pi/gowayki-pi.service`.

## Variables (`/etc/gowayki/agent.env`)

| Variable | Descripción |
|----------|-------------|
| `API_BASE_URL` | `https://paradero.172-214-154-175.sslip.io` |
| `MQTT_HOST` | IP/host del Mosquitto de la VM |
| `MQTT_PORT` | `1883` |
| `MQTT_USER` / `MQTT_PASSWORD` | Opcional (broker MVP permite anónimo) |
| `ENABLE_KIOSK_DISPLAY` | `1` abre Chromium en `display_url` |

## Comandos remotos (MQTT o HTTP heartbeat)

JSON en `gowayki/{id}/comandos`:

```json
{"accion": "reload"}
{"accion": "reboot"}
{"accion": "unpair"}
```
