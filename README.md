# GoWayki IoT Panel

Backend del quiosco de paradero para tablets Android (app Flutter) y, en fase 2, agentes Raspberry Pi.

- Dominio: `https://paradero.172-214-154-175.sslip.io`
- Repo: `https://github.com/JenniferTS/gowayki-iot`
- Contrato app: ver app Flutter `operative/gowayki-iot/docs/api-kiosco.md`

## Qué incluye

| Ruta | Uso |
|------|-----|
| `POST /api/kiosk/pair` | Vincular tablet/Pi con código 6 dígitos |
| `POST /api/kiosk/heartbeat` | Latido + comandos `reload` / `unpair` / `reboot` |
| `/paradero/{id}` | Pantalla rutas GTFS + playlist de ads |
| `/admin` | Panel mínimo (crear dispositivo, código, comandos) |
| `/health` | Healthcheck |

## Variables de entorno

Ver [`.env.example`](.env.example). **No subi secrets al repo.**

| Variable | Descripción |
|----------|-------------|
| `DATABASE_URL` | Postgres **gtfs-arequipa** (solo lectura) |
| `APP_DB_PATH` | SQLite del panel, ej. `/data/iot.db` (volumen persistente) |
| `ADMIN_TOKEN` | Token del panel `/admin` |
| `SECRET_KEY` | Secreto interno |
| `BASE_URL` | `https://paradero.172-214-154-175.sslip.io` |
| `AD_PLAYLIST_URLS` | Semilla inicial (coma-separada) si la BD está vacía; luego se gestiona en `/admin` |

## Playlist de anuncios

En `/admin` → **Anuncios**:

- Solo **URLs directas** HTTPS a archivo
- Imágenes: `.jpg` `.jpeg` `.png` `.webp` `.gif` `.svg`
- Videos: `.mp4` `.webm` `.mov` `.m4v`
- No se aceptan YouTube / Vimeo / TikTok

## Raspberry Pi

Instalación (sin clonar el repo privado):

```bash
curl -fsSL https://paradero.172-214-154-175.sslip.io/pi/install.sh | sudo bash
echo CODIGO | sudo tee /etc/gowayki/pair.code
sudo systemctl restart gowayki-pi
```

Detalle: [`pi-agent/README.md`](pi-agent/README.md). MQTT: Mosquitto en la VM del grupo en `MQTT_HOST:1883`.

## Desarrollo local

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
copy .env.example .env   # completar DATABASE_URL y tokens
set APP_DB_PATH=./data/iot.db
uvicorn app.main:app --reload --port 8000
```

Abrir `http://127.0.0.1:8000/admin`.

## Docker

```bash
docker build -t gowayki-iot .
docker run --rm -p 8000:8000 \
  -e DATABASE_URL=postgres://.../gtfs-arequipa \
  -e ADMIN_TOKEN=... \
  -e SECRET_KEY=... \
  -e BASE_URL=https://paradero.172-214-154-175.sslip.io \
  -e APP_DB_PATH=/data/iot.db \
  -v iot_data:/data \
  gowayki-iot
```

## Flujo operativo

1. En `/admin` crear dispositivo (kiosco o pi), asignar `stop_id` GTFS.
2. Generar código de 6 dígitos.
3. En la tablet Flutter ingresar el código → abre `/paradero/{id}`.
4. Desde admin: Reload / Unpair / Reboot (Pi).

## Raspberry Pi (fase 2 — esqueleto)

Ver [`docs/pi-agent.md`](docs/pi-agent.md). El backend ya acepta `device_type=pi` y el comando `reboot` en heartbeat.
