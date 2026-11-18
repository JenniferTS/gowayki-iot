from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api import admin, kiosk
from app.config import get_settings
from app.db import AppDatabase
from app.services.events import EventHub, ParaderoHub
from app.services.gtfs import GtfsService
from app.services.mqtt_bridge import MqttBridge
from app.services.paradero_push import build_paradero_payload

BASE_DIR = Path(__file__).resolve().parent
PI_AGENT_DIR = BASE_DIR.parent / "pi-agent"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _asset_version() -> str:
    """Hash de los assets del paradero: cambia en cada despliegue que los toque (rompe caché de WebView/CDN)."""
    import hashlib

    h = hashlib.sha1()
    for f in sorted((BASE_DIR / "static" / "paradero").rglob("*")):
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:10]


templates.env.globals["asset_v"] = _asset_version()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db = AppDatabase(settings.app_db_path)
    await db.connect()
    await db.seed_demo_ads_if_empty(settings)
    if settings.mqtt_auth_dir:
        from app.services.mqtt_users import write_auth_files

        try:
            users = await db.list_mqtt_users(with_hash=True)
            write_auth_files(settings.mqtt_auth_dir, users, settings.mqtt_topic_prefix)
            print(f"[mqtt-users] {len(users)} usuarios publicados en MQTT_AUTH_DIR")
        except OSError as exc:
            print(f"[mqtt-users] no se pudo escribir MQTT_AUTH_DIR: {exc}")
    gtfs = GtfsService(settings)
    try:
        gtfs.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"[gtfs] connection failed: {exc}")

    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    def on_mqtt(device_id: str, kind: str, data: dict[str, Any]) -> None:
        prefix = str(data.pop("_mqtt_prefix", "") or "")
        gpio_prefix = (settings.mqtt_gpio_prefix or "").strip("/")

        async def _apply() -> None:
            # Eventos del gpio-monitor (paradero/<gpio_id>/estado|on|off)
            if gpio_prefix and prefix == gpio_prefix and kind in ("estado", "on", "off"):
                estado = str(data.get("estado") or kind).lower()
                valor = data.get("valor")
                on = estado in ("on", "1", "true") or (
                    isinstance(valor, (int, float)) and float(valor) > 0
                )
                if estado in ("off", "0", "false"):
                    on = False
                text = None
                if data.get("nombre") and data.get("tipo") == "entrada":
                    text = "Activo" if on else "Inactivo"
                touched = await db.patch_gpio_live(
                    device_id,
                    on=on,
                    value=valor if valor is not None else (1 if on else 0),
                    text=text,
                    changed=data.get("ts"),
                    tipo=data.get("tipo"),
                )
                hub = getattr(app.state, "gpio_hub", None)
                if hub:
                    for did in touched:
                        payload = await db.get_gpio_payload(did)
                        if payload:
                            await hub.publish(did, payload)
                return

            # Telemetría / LWT de agentes gowayki/{kiosk_id}/...
            if kind in ("estado", "telemetria"):
                await db.apply_mqtt_event(device_id, kind, data)

        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(_apply(), loop)
        else:
            print(f"[mqtt] drop event (no loop): {device_id} {kind}")

    mqtt_bridge = MqttBridge(settings, on_mqtt)
    mqtt_bridge.start()

    app.state.db = db
    app.state.gtfs = gtfs
    app.state.settings = settings
    app.state.mqtt = mqtt_bridge
    app.state.paradero_hub = ParaderoHub()
    app.state.gpio_hub = EventHub()
    yield
    mqtt_bridge.stop()
    await db.close()
    gtfs.close()


app = FastAPI(title="GoWayki IoT Panel", version="0.1.0", lifespan=lifespan)
app.include_router(kiosk.router)
app.include_router(admin.router)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
if PI_AGENT_DIR.is_dir():
    # Público: curl -fsSL https://paradero.172-214-154-175.sslip.io/pi/install.sh | sudo bash
    app.mount("/pi", StaticFiles(directory=str(PI_AGENT_DIR), html=False), name="pi")


def _extract_token(
    authorization: str | None,
    kiosk_token: str | None,
) -> str | None:
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    return kiosk_token


def _admin_token_ok(
    *,
    admin_cookie: str | None,
    x_admin_token: str | None,
) -> bool:
    """Admin preview: cookie o header X-Admin-Token. Nunca query string."""
    expected = get_settings().admin_token
    if not expected:
        return False
    if x_admin_token and x_admin_token == expected:
        return True
    if admin_cookie and admin_cookie == expected:
        return True
    return False


async def _authorize_paradero(
    device_id: str,
    request: Request,
    *,
    authorization: str | None,
    kiosk_token: str | None,
    admin_cookie: str | None,
    x_admin_token: str | None,
) -> dict[str, Any]:
    """Acceso de lectura a /paradero: token del dispositivo o admin (cookie/header)."""
    db: AppDatabase = request.app.state.db
    device = await db.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Paradero not found")

    if _admin_token_ok(admin_cookie=admin_cookie, x_admin_token=x_admin_token):
        return device

    token = _extract_token(authorization, kiosk_token)
    if not token:
        raise HTTPException(status_code=401, detail="Auth required")
    by_token = await db.get_device_by_token(token)
    if not by_token or by_token["id"] != device_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return device


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    mqtt = getattr(request.app.state, "mqtt", None)
    return {
        "ok": True,
        "gtfs": bool(request.app.state.gtfs.available),
        "mqtt": {
            "enabled": bool(mqtt and mqtt.enabled),
            "connected": bool(mqtt and mqtt.connected),
            "error": getattr(mqtt, "last_error", None) if mqtt else None,
        },
        "ts": time.time(),
    }


@app.get("/", response_class=HTMLResponse)
async def root(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"base_url": get_settings().base_url},
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"base_url": get_settings().base_url},
    )


@app.get("/remote/{device_id}", response_class=HTMLResponse)
async def remote_console(device_id: str, request: Request) -> HTMLResponse:
    db: AppDatabase = request.app.state.db
    device = await db.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return templates.TemplateResponse(
        request,
        "remote.html",
        {
            "device_id": device_id,
            "device_name": device.get("name") or device_id,
            "base_url": get_settings().base_url,
        },
    )


@app.get("/gpio/{device_id}", response_class=HTMLResponse)
async def gpio_console(device_id: str, request: Request) -> HTMLResponse:
    db: AppDatabase = request.app.state.db
    device = await db.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return templates.TemplateResponse(
        request,
        "gpio.html",
        {
            "device_id": device_id,
            "device_name": device.get("name") or device_id,
            "base_url": get_settings().base_url,
        },
    )


@app.get("/paradero/{device_id}", response_class=HTMLResponse)
async def paradero_page(
    device_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    kiosk_token: str | None = Cookie(default=None),
    admin_token: str | None = Cookie(default=None),
    x_admin_token: str | None = Header(default=None),
) -> HTMLResponse:
    device = await _authorize_paradero(
        device_id,
        request,
        authorization=authorization,
        kiosk_token=kiosk_token,
        admin_cookie=admin_token,
        x_admin_token=x_admin_token,
    )
    db: AppDatabase = request.app.state.db

    stop = None
    routes: list[dict[str, Any]] = []
    gtfs: GtfsService = request.app.state.gtfs
    if device.get("stop_id") and gtfs.available:
        stop = gtfs.get_stop(device["stop_id"])
        routes = gtfs.routes_for_stop(device["stop_id"])

    ads = await db.list_ads()
    settings_map = await db.get_settings_map()
    resp = templates.TemplateResponse(
        request,
        "paradero.html",
        {
            "device": device,
            "stop": stop,
            "routes": routes,
            "ads": ads,
            "idle_seconds": settings_map["idle_seconds"],
            "base_url": get_settings().base_url,
        },
    )
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.get("/api/paradero/{device_id}/data")
async def paradero_data(
    device_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    kiosk_token: str | None = Cookie(default=None),
    admin_token: str | None = Cookie(default=None),
    x_admin_token: str | None = Header(default=None),
) -> JSONResponse:
    device = await _authorize_paradero(
        device_id,
        request,
        authorization=authorization,
        kiosk_token=kiosk_token,
        admin_cookie=admin_token,
        x_admin_token=x_admin_token,
    )
    db: AppDatabase = request.app.state.db
    payload = await build_paradero_payload(db, request.app.state.gtfs, device)
    return JSONResponse(payload)


@app.get("/api/paradero/{device_id}/stream")
async def paradero_stream(
    device_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    kiosk_token: str | None = Cookie(default=None),
    admin_token: str | None = Cookie(default=None),
    x_admin_token: str | None = Header(default=None),
) -> StreamingResponse:
    """SSE: push inmediato de anuncios/rutas cuando el admin cambia algo.

    Solo lectura: suscribirse no actualiza latido ni estado del dispositivo.
    """
    device = await _authorize_paradero(
        device_id,
        request,
        authorization=authorization,
        kiosk_token=kiosk_token,
        admin_cookie=admin_token,
        x_admin_token=x_admin_token,
    )
    db: AppDatabase = request.app.state.db

    hub: ParaderoHub = request.app.state.paradero_hub
    queue = await hub.subscribe(device_id)

    async def event_gen():
        try:
            # Snapshot inicial
            payload = await build_paradero_payload(db, request.app.state.gtfs, device)
            yield f"event: update\ndata: {json.dumps(payload, default=str)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"event: update\ndata: {json.dumps(msg, default=str)}\n\n"
                except TimeoutError:
                    # keep-alive para proxies (no es polling de datos)
                    yield ": ping\n\n"
        finally:
            await hub.unsubscribe(device_id, queue)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
