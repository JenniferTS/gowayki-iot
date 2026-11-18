from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import get_settings

router = APIRouter(prefix="/api/kiosk", tags=["kiosk"])


class DeviceInfo(BaseModel):
    platform: str | None = None
    os_version: str | None = None
    app_version: str | None = None


class PairRequest(BaseModel):
    code: str = Field(min_length=4, max_length=8)
    device: DeviceInfo | None = None


class HeartbeatRequest(BaseModel):
    app_version: str | None = None
    display_url: str | None = None
    page_ok: bool | None = None
    # GPS, batería, red y modelo enviados por la app (ver Telemetry en Flutter).
    telemetry: dict[str, Any] | None = None


class JobResultBody(BaseModel):
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    status: str = Field(default="done", pattern="^(done|error|timeout)$")


class GpioSnapshotBody(BaseModel):
    devices: list[dict[str, Any]] = Field(default_factory=list)
    available: bool = True
    error: str | None = None
    source: str = "gpio-monitor"


def _client_ip(request: Request) -> str:
    # El primer valor de X-Forwarded-For lo controla el cliente (permitiría
    # saltarse el rate limit de /pair); el último lo agrega nuestro proxy.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


@router.post("/pair")
async def pair(request: Request, body: PairRequest) -> dict[str, Any]:
    db = request.app.state.db
    settings = get_settings()
    ip = _client_ip(request)
    await db.record_pair_attempt(ip)
    attempts = await db.count_pair_attempts(ip, 60)
    if attempts > settings.pair_rate_limit_per_minute:
        raise HTTPException(status_code=429, detail="Too many attempts")

    code = "".join(ch for ch in body.code if ch.isdigit())
    if len(code) != 6:
        raise HTTPException(status_code=400, detail="Invalid code")

    result = await db.pair_with_code(
        code,
        body.device.model_dump() if body.device else None,
    )
    if not result:
        raise HTTPException(status_code=410, detail="Invalid or expired code")
    return {
        "kiosk_id": result["kiosk_id"],
        "token": result["token"],
        "display_url": result["display_url"],
        "name": result["name"],
    }


@router.post("/heartbeat")
async def heartbeat(
    request: Request,
    body: HeartbeatRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any] | None:
    token = _extract_bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")

    db = request.app.state.db
    result = await db.heartbeat(
        token,
        app_version=body.app_version,
        display_url=body.display_url,
        page_ok=body.page_ok,
        telemetry=body.telemetry,
    )
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Empty body → 204-compatible empty object (Flutter accepts 200 {})
    return result or {}


@router.get("/jobs/next")
async def next_job(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    token = _extract_bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    db = request.app.state.db
    device = await db.get_device_by_token(token)
    if not device:
        raise HTTPException(status_code=401, detail="Invalid token")
    job = await db.claim_next_remote_job(device["id"])
    if not job:
        return {"job": None}
    return {
        "job": {
            "id": job["id"],
            "kind": job["kind"],
            "command": job["command"],
            "timeout_sec": job["timeout_sec"],
        }
    }


@router.post("/jobs/{job_id}/result")
async def job_result(
    job_id: str,
    body: JobResultBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    token = _extract_bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    db = request.app.state.db
    device = await db.get_device_by_token(token)
    if not device:
        raise HTTPException(status_code=401, detail="Invalid token")
    job = await db.get_remote_job(job_id)
    if not job or job["device_id"] != device["id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    updated = await db.finish_remote_job(
        job_id,
        exit_code=body.exit_code,
        stdout=body.stdout,
        stderr=body.stderr,
        status=body.status,
    )
    return updated or {"ok": True}


@router.post("/gpio")
async def report_gpio(
    body: GpioSnapshotBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    token = _extract_bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    db = request.app.state.db
    device = await db.get_device_by_token(token)
    if not device:
        raise HTTPException(status_code=401, detail="Invalid token")
    snapshot = {
        "available": body.available,
        "error": body.error,
        "source": body.source,
        "devices": body.devices,
        "ts": time.time(),
    }
    await db.update_gpio_snapshot(device["id"], snapshot)
    hub = getattr(request.app.state, "gpio_hub", None)
    if hub:
        payload = await db.get_gpio_payload(device["id"])
        if payload:
            await hub.publish(device["id"], payload)
    return {"ok": True}
