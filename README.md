# GoWayki IoT Panel

Backend del quiosco de paradero para tablets Android (app Flutter) y agentes Raspberry Pi.

Panel local de desarrollo: `http://127.0.0.1:8000`

## Qué incluye

| Ruta | Uso |
|------|-----|
| `POST /api/kiosk/pair` | Vincular tablet/Pi con código 6 dígitos |
| `POST /api/kiosk/heartbeat` | Latido + comandos `reload` / `unpair` / `reboot` |
| `/paradero/{id}` | Pantalla rutas GTFS + playlist de ads |
| `/admin` | Panel (crear dispositivo, código, comandos, anuncios, MQTT) |
| `/health` | Healthcheck |

## Variables de entorno

Ver [`.env.example`](.env.example). **No subas secrets al repo.**

| Variable | Descripción |
|----------|-------------|
| `DATABASE_URL` | Postgres GTFS (solo lectura); vacío si usás feed local |
| `APP_DB_PATH` | SQLite del panel, ej. `./data/iot.db` o `/data/iot.db` |
| `ADMIN_TOKEN` | Token del panel `/admin` |
| `SECRET_KEY` | Secreto interno |
| `BASE_URL` | URL pública del panel |
| `AD_PLAYLIST_URLS` | Semilla inicial (coma-separada) si la BD está vacía |
| `MQTT_HOST` | Broker MQTT; vacío = bridge desactivado |

## Playlist de anuncios

En `/admin` → **Anuncios**:

- Solo **URLs directas** HTTPS a archivo
- Imágenes: `.jpg` `.jpeg` `.png` `.webp` `.gif` `.svg`
- Videos: `.mp4` `.webm` `.mov` `.m4v`
- No se aceptan YouTube / Vimeo / TikTok

## Raspberry Pi

Con el panel corriendo:

```bash
curl -fsSL http://127.0.0.1:8000/pi/install.sh | sudo bash
echo CODIGO | sudo tee /etc/gowayki/pair.code
sudo systemctl restart gowayki-pi
```

Detalle: [`pi-agent/README.md`](pi-agent/README.md).

## Desarrollo local

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # completar tokens (y DATABASE_URL si hay GTFS)
export APP_DB_PATH=./data/iot.db
uvicorn app.main:app --reload --port 8000
```

Abrir `http://127.0.0.1:8000/admin`.

## Docker

```bash
docker build -t gowayki-iot .
docker run --rm -p 8000:8000 \
  -e DATABASE_URL=postgres://.../gtfs \
  -e ADMIN_TOKEN=... \
  -e SECRET_KEY=... \
  -e BASE_URL=http://127.0.0.1:8000 \
  -e APP_DB_PATH=/data/iot.db \
  -v iot_data:/data \
  gowayki-iot
```

O con Compose: `docker compose up --build`.

## Flujo operativo

1. En `/admin` crear dispositivo (kiosco o pi), asignar `stop_id` GTFS.
2. Generar código de 6 dígitos.
3. En la tablet Flutter ingresar el código → abre `/paradero/{id}`.
4. Desde admin: Reload / Unpair / Reboot (Pi).

## Docs

- Agente Pi: [`docs/pi-agent.md`](docs/pi-agent.md) y [`pi-agent/README.md`](pi-agent/README.md)
- MQTT del equipo: [`docs/guia-mqtt-equipo.md`](docs/guia-mqtt-equipo.md)
- Arquitectura usuarios MQTT: [`docs/mqtt-usuarios-arquitectura.md`](docs/mqtt-usuarios-arquitectura.md)
