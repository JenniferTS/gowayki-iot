from __future__ import annotations

import time
from typing import Any

from fastapi import Request

from app.config import get_settings
from app.db import AppDatabase
from app.services.gtfs import GtfsService
from app.services.media import with_public_ad_urls


async def build_paradero_payload(
    db: AppDatabase,
    gtfs: GtfsService,
    device: dict[str, Any],
) -> dict[str, Any]:
    stop = None
    routes: list[dict[str, Any]] = []
    if device.get("stop_id") and gtfs.available:
        stop = gtfs.get_stop(device["stop_id"])
        routes = gtfs.routes_for_stop(device["stop_id"])
    root = get_settings().root_path_normalized
    ads = with_public_ad_urls(await db.list_ads(), root)
    settings = await db.get_settings_map()
    return {
        "device": {
            "id": device["id"],
            "name": device["name"],
            "stop_id": device.get("stop_id"),
            "device_type": device.get("device_type"),
        },
        "stop": stop,
        "routes": routes,
        "ads": ads,
        "idle_seconds": settings["idle_seconds"],
        "ts": time.time(),
    }


async def notify_paradero(request: Request, device_id: str | None = None) -> None:
    """Empuja snapshot a un dispositivo o a todos los paraderos conectados por SSE."""
    hub = getattr(request.app.state, "paradero_hub", None)
    if not hub:
        return
    db: AppDatabase = request.app.state.db
    gtfs: GtfsService = request.app.state.gtfs
    if device_id:
        device = await db.get_device(device_id)
        if not device:
            return
        payload = await build_paradero_payload(db, gtfs, device)
        await hub.publish(device_id, payload)
        return
    devices = await db.list_devices()
    for device in devices:
        payload = await build_paradero_payload(db, gtfs, device)
        await hub.publish(device["id"], payload)
