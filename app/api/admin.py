from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.config import Settings, get_settings
from app.services import mqtt_users as mu
from app.services.media import validate_ad_url
from app.services.paradero_push import notify_paradero

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> Settings:
    settings = get_settings()
    provided = x_admin_token or token or request.cookies.get("admin_token")
    if not provided or provided != settings.admin_token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return settings


class CreateDeviceBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    stop_id: str | None = None
    device_type: str = Field(default="kiosk", pattern="^(kiosk|pi)$")
    # ID numérico del Paradero en Django (playlist de campañas pagadas)
    django_paradero_id: int | None = None


class UpdateDeviceBody(BaseModel):
    name: str | None = None
    stop_id: str | None = None
    device_type: str | None = Field(default=None, pattern="^(kiosk|pi)$")
    django_paradero_id: int | None = None


class CommandBody(BaseModel):
    command: str = Field(pattern="^(reload|unpair|reboot)$")
    display_url: str | None = None


class RemoteExecBody(BaseModel):
    command: str = Field(min_length=1, max_length=4000)
    timeout_sec: int = Field(default=30, ge=5, le=120)


class GpioSetBody(BaseModel):
    state: str | None = Field(default=None, pattern="^(on|off|toggle)$")
    value: int | None = Field(default=None, ge=0, le=100)


class CreateAdBody(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    duration_sec: int = Field(default=10, ge=3, le=300)
    active: bool = True

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        url, _media = validate_ad_url(value)
        return url


class UpdateAdBody(BaseModel):
    url: str | None = Field(default=None, min_length=8, max_length=2000)
    duration_sec: int | None = Field(default=None, ge=3, le=300)
    active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10_000)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        url, _media = validate_ad_url(value)
        return url


class ReorderAdsBody(BaseModel):
    ids: list[int] = Field(min_length=1)


class SettingsBody(BaseModel):
    idle_seconds: int = Field(ge=10, le=600)


def _enrich(device: dict[str, Any], settings: Settings) -> dict[str, Any]:
    last = device.get("last_heartbeat_at")
    mqtt_seen = device.get("mqtt_last_seen_at")
    online = bool(last and (time.time() - float(last)) <= settings.online_threshold_seconds)
    mqtt_online = bool(
        device.get("mqtt_online")
        and mqtt_seen
        and (time.time() - float(mqtt_seen)) <= settings.online_threshold_seconds
    )
    telemetry = None
    if device.get("last_telemetry"):
        try:
            telemetry = json.loads(device["last_telemetry"])
        except ValueError:
            telemetry = None
    return {
        **device,
        "last_telemetry": None,
        "telemetry": telemetry,
        "online": online or mqtt_online,
        "mqtt_online": mqtt_online,
        "paired": bool(device.get("token_hash")),
        "token_hash": None,  # never expose
    }


@router.get("/devices")
async def list_devices(
    request: Request,
    settings: Settings = Depends(require_admin),
) -> list[dict[str, Any]]:
    devices = await request.app.state.db.list_devices()
    return [_enrich(d, settings) for d in devices]


@router.post("/devices")
async def create_device(
    request: Request,
    body: CreateDeviceBody,
    settings: Settings = Depends(require_admin),
) -> dict[str, Any]:
    db = request.app.state.db
    if body.stop_id and request.app.state.gtfs.available:
        stop = request.app.state.gtfs.get_stop(body.stop_id)
        if not stop:
            raise HTTPException(status_code=400, detail="stop_id not found in GTFS")
    device = await db.create_device(
        name=body.name,
        stop_id=body.stop_id,
        device_type=body.device_type,
        base_url=settings.base_url,
        django_paradero_id=body.django_paradero_id,
    )
    return _enrich(device, settings)


@router.patch("/devices/{device_id}")
async def update_device(
    device_id: str,
    body: UpdateDeviceBody,
    request: Request,
    settings: Settings = Depends(require_admin),
) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    if data.get("stop_id") and request.app.state.gtfs.available:
        stop = request.app.state.gtfs.get_stop(data["stop_id"])
        if not stop:
            raise HTTPException(status_code=400, detail="stop_id not found in GTFS")
    device = await request.app.state.db.update_device(
        device_id,
        name=data.get("name"),
        stop_id=data.get("stop_id"),
        device_type=data.get("device_type"),
        django_paradero_id=data.get("django_paradero_id"),
        update_stop="stop_id" in data,
        update_django_paradero="django_paradero_id" in data,
    )
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    await notify_paradero(request, device_id)
    return _enrich(device, settings)


@router.post("/devices/{device_id}/pairing-code")
async def create_pairing_code(
    device_id: str,
    request: Request,
    settings: Settings = Depends(require_admin),
    regenerate: bool = Query(default=False),
) -> dict[str, Any]:
    db = request.app.state.db
    device = await db.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    try:
        if regenerate:
            code = await db.regenerate_pair_code(device_id)
        else:
            code = await db.create_pairing_code(device_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "code": code,
        "device_id": device_id,
        "permanent": True,
        "name": device["name"],
    }


@router.delete("/devices/{device_id}")
async def delete_device(
    device_id: str,
    request: Request,
    _: Settings = Depends(require_admin),
) -> dict[str, bool]:
    ok = await request.app.state.db.delete_device(device_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Device not found")
    return {"ok": True}


@router.post("/devices/{device_id}/command")
async def enqueue_command(
    device_id: str,
    body: CommandBody,
    request: Request,
    settings: Settings = Depends(require_admin),
) -> dict[str, Any]:
    try:
        device = await request.app.state.db.enqueue_command(
            device_id,
            body.command,
            body.display_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    # Dual path: also publish MQTT for live Pis
    bridge = getattr(request.app.state, "mqtt", None)
    mqtt_ok = False
    if bridge:
        mqtt_ok = bridge.publish_command(device_id, body.command)
    enriched = _enrich(device, settings)
    enriched["mqtt_published"] = mqtt_ok
    return enriched


@router.get("/settings")
async def admin_get_settings(
    request: Request,
    _: Settings = Depends(require_admin),
) -> dict[str, Any]:
    return await request.app.state.db.get_settings_map()


@router.put("/settings")
async def admin_update_settings(
    request: Request,
    body: SettingsBody,
    _: Settings = Depends(require_admin),
) -> dict[str, Any]:
    data = await request.app.state.db.set_settings(idle_seconds=body.idle_seconds)
    # Empujar idle_seconds a todos los kioscos conectados por SSE.
    await notify_paradero(request)
    return data


@router.get("/stops")
async def search_stops(
    request: Request,
    q: str = Query(min_length=2),
    _: Settings = Depends(require_admin),
) -> list[dict[str, Any]]:
    gtfs = request.app.state.gtfs
    if not gtfs.available:
        return []
    return gtfs.search_stops(q)


@router.get("/ads")
async def list_ads(
    request: Request,
    _: Settings = Depends(require_admin),
) -> list[dict[str, Any]]:
    return await request.app.state.db.list_ads(include_inactive=True)


@router.post("/ads")
async def create_ad(
    request: Request,
    body: CreateAdBody,
    _: Settings = Depends(require_admin),
) -> dict[str, Any]:
    try:
        url, media_type = validate_ad_url(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Videos: duración por defecto más larga si el usuario dejó 10
    duration = body.duration_sec
    if media_type == "video" and duration == 10:
        duration = 30
    ad = await request.app.state.db.create_ad(
        url=url,
        media_type=media_type,
        duration_sec=duration,
        active=body.active,
    )
    await notify_paradero(request)
    return ad


@router.put("/ads/reorder")
async def reorder_ads(
    body: ReorderAdsBody,
    request: Request,
    _: Settings = Depends(require_admin),
) -> list[dict[str, Any]]:
    ads = await request.app.state.db.reorder_ads(body.ids)
    await notify_paradero(request)
    return ads


@router.patch("/ads/{ad_id}")
async def update_ad(
    ad_id: int,
    body: UpdateAdBody,
    request: Request,
    _: Settings = Depends(require_admin),
) -> dict[str, Any]:
    media_type = None
    url = body.url
    if url is not None:
        try:
            url, media_type = validate_ad_url(url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    ad = await request.app.state.db.update_ad(
        ad_id,
        url=url,
        media_type=media_type,
        duration_sec=body.duration_sec,
        sort_order=body.sort_order,
        active=body.active,
    )
    if not ad:
        raise HTTPException(status_code=404, detail="Ad not found")
    await notify_paradero(request)
    return ad


@router.delete("/ads/{ad_id}")
async def delete_ad(
    ad_id: int,
    request: Request,
    _: Settings = Depends(require_admin),
) -> dict[str, bool]:
    ok = await request.app.state.db.delete_ad(ad_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Ad not found")
    await notify_paradero(request)
    return {"ok": True}


@router.post("/devices/{device_id}/exec")
async def enqueue_remote_exec(
    device_id: str,
    body: RemoteExecBody,
    request: Request,
    _: Settings = Depends(require_admin),
) -> dict[str, Any]:
    device = await request.app.state.db.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.get("token_hash"):
        raise HTTPException(status_code=400, detail="Device not paired")
    job = await request.app.state.db.create_remote_job(
        device_id,
        body.command.strip(),
        timeout_sec=body.timeout_sec,
    )
    return job


@router.get("/devices/{device_id}/jobs")
async def list_remote_jobs(
    device_id: str,
    request: Request,
    _: Settings = Depends(require_admin),
) -> list[dict[str, Any]]:
    device = await request.app.state.db.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return await request.app.state.db.list_remote_jobs(device_id)


@router.get("/jobs/{job_id}")
async def get_remote_job(
    job_id: str,
    request: Request,
    _: Settings = Depends(require_admin),
) -> dict[str, Any]:
    job = await request.app.state.db.get_remote_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/devices/{device_id}/gpio")
async def get_device_gpio(
    device_id: str,
    request: Request,
    settings: Settings = Depends(require_admin),
) -> dict[str, Any]:
    device = await request.app.state.db.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    snap = {}
    raw = device.get("gpio_snapshot")
    if raw:
        try:
            snap = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            snap = {}
    return {
        "device": _enrich(device, settings),
        "gpio_updated_at": device.get("gpio_updated_at"),
        "snapshot": snap,
    }


@router.get("/devices/{device_id}/gpio/stream")
async def stream_device_gpio(
    device_id: str,
    request: Request,
    settings: Settings = Depends(require_admin),
) -> StreamingResponse:
    """SSE en vivo: LEDs/PIR sin polling (MQTT gpio-monitor + sync del agente)."""
    db = request.app.state.db
    device = await db.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    hub = getattr(request.app.state, "gpio_hub", None)
    if not hub:
        raise HTTPException(status_code=503, detail="GPIO hub unavailable")

    queue = await hub.subscribe(device_id)

    async def event_gen():
        try:
            snap = {}
            raw = device.get("gpio_snapshot")
            if raw:
                try:
                    snap = json.loads(raw) if isinstance(raw, str) else raw
                except json.JSONDecodeError:
                    snap = {}
            initial = {
                "device": _enrich(device, settings),
                "gpio_updated_at": device.get("gpio_updated_at"),
                "snapshot": snap,
            }
            yield f"event: update\ndata: {json.dumps(initial, default=str)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25.0)
                    # Asegura device enrich si viene solo snapshot
                    if "device" not in msg:
                        d = await db.get_device(device_id)
                        if d:
                            msg = {
                                **msg,
                                "device": _enrich(d, settings),
                            }
                    yield f"event: update\ndata: {json.dumps(msg, default=str)}\n\n"
                except TimeoutError:
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


@router.post("/devices/{device_id}/gpio/{gpio_id}/set")
async def set_device_gpio(
    device_id: str,
    gpio_id: str,
    body: GpioSetBody,
    request: Request,
    _: Settings = Depends(require_admin),
) -> dict[str, Any]:
    device = await request.app.state.db.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.get("token_hash"):
        raise HTTPException(status_code=400, detail="Device not paired")
    if body.state is None and body.value is None:
        raise HTTPException(status_code=400, detail="state or value required")
    payload = {"gpio_id": gpio_id}
    if body.state is not None:
        payload["state"] = body.state
    if body.value is not None:
        payload["value"] = body.value
    # Camino rápido: MQTT directo a gpio-monitor (paradero/<id>/set)
    bridge = getattr(request.app.state, "mqtt", None)
    mqtt_ok = False
    if bridge and body.state is not None:
        mqtt_ok = bridge.publish_gpio_set(gpio_id, state=body.state)
    elif bridge and body.value is not None:
        mqtt_ok = bridge.publish_gpio_set(gpio_id, value=body.value)
    # Fallback / confirmación vía agente HTTP
    job = await request.app.state.db.create_remote_job(
        device_id,
        json.dumps(payload),
        kind="gpio_set",
        timeout_sec=20,
    )
    return {**job, "mqtt_published": mqtt_ok}


# ---------------------------------------------------------------------------
# Usuarios MQTT (pestaña "Usuarios MQTT" del admin)
# ---------------------------------------------------------------------------


class CreateMqttUserBody(BaseModel):
    username: str = Field(min_length=3, max_length=32)


class MqttLedBody(BaseModel):
    on: bool


async def _sync_mqtt_auth(request: Request, settings: Settings) -> bool:
    users = await request.app.state.db.list_mqtt_users(with_hash=True)
    try:
        return mu.write_auth_files(settings.mqtt_auth_dir, users, settings.mqtt_topic_prefix)
    except OSError as exc:
        print(f"[mqtt-users] no se pudo escribir {settings.mqtt_auth_dir}: {exc}")
        raise HTTPException(status_code=500, detail="No se pudo publicar passwd/acl al broker") from exc


def _connection_info(settings: Settings, username: str) -> dict[str, Any]:
    return {
        "host": settings.mqtt_public_host,
        "port": settings.mqtt_public_port,
        "username": username,
        "topic_root": f"{mu.user_topic_root(settings.mqtt_topic_prefix, username)}/#",
    }


@router.get("/mqtt-users")
async def list_mqtt_users(request: Request, settings: Settings = Depends(require_admin)) -> dict[str, Any]:
    users = await request.app.state.db.list_mqtt_users()
    return {
        "users": [{**u, **_connection_info(settings, u["username"])} for u in users],
        "sync_enabled": bool(settings.mqtt_auth_dir),
        "broker": {"host": settings.mqtt_public_host, "port": settings.mqtt_public_port},
    }


@router.post("/mqtt-users")
async def create_mqtt_user(
    request: Request, body: CreateMqttUserBody, settings: Settings = Depends(require_admin)
) -> dict[str, Any]:
    try:
        username = mu.validate_username(body.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    password = mu.generate_password()
    if not await request.app.state.db.create_mqtt_user(username, mu.hash_password(password)):
        raise HTTPException(status_code=409, detail="El usuario ya existe")
    synced = await _sync_mqtt_auth(request, settings)
    # La contraseña solo se devuelve en esta respuesta; en BD queda solo el hash $7$.
    return {**_connection_info(settings, username), "password": password, "synced": synced}


@router.post("/mqtt-users/{username}/regenerate")
async def regenerate_mqtt_password(
    request: Request, username: str, settings: Settings = Depends(require_admin)
) -> dict[str, Any]:
    password = mu.generate_password()
    if not await request.app.state.db.set_mqtt_user_password(username, mu.hash_password(password)):
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    synced = await _sync_mqtt_auth(request, settings)
    return {**_connection_info(settings, username), "password": password, "synced": synced}


@router.delete("/mqtt-users/{username}")
async def delete_mqtt_user(request: Request, username: str, settings: Settings = Depends(require_admin)) -> dict[str, Any]:
    if not await request.app.state.db.delete_mqtt_user(username):
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    synced = await _sync_mqtt_auth(request, settings)
    return {"ok": True, "synced": synced}


@router.get("/mqtt-users/live")
async def mqtt_users_live(request: Request, settings: Settings = Depends(require_admin)) -> dict[str, Any]:
    mqtt_bridge = getattr(request.app.state, "mqtt", None)
    return {
        "connected": bool(mqtt_bridge and mqtt_bridge.connected),
        "values": dict(getattr(mqtt_bridge, "demo_values", {}) or {}),
    }


@router.post("/mqtt-users/{username}/led")
async def mqtt_user_led(
    request: Request, username: str, body: MqttLedBody, settings: Settings = Depends(require_admin)
) -> dict[str, Any]:
    if not await request.app.state.db.get_mqtt_user(username):
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    mqtt_bridge = getattr(request.app.state, "mqtt", None)
    if not mqtt_bridge or not mqtt_bridge.publish_user_led(username, body.on):
        raise HTTPException(status_code=503, detail="Bridge MQTT desconectado")
    return {"ok": True, "topic": f"{settings.mqtt_topic_prefix}/{username}/led", "payload": "ON" if body.on else "OFF"}
