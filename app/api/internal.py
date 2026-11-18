"""Endpoints internos para que Django avise cambios de playlist."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services.paradero_push import notify_paradero

router = APIRouter(prefix="/api/internal", tags=["internal"])


class PlaylistRefreshBody(BaseModel):
    # Si viene, solo dispositivos vinculados a ese Paradero Django; si no, todos con vínculo
    paradero_id: int | None = Field(default=None, ge=1)


def _require_panel_token(x_panel_token: str | None) -> None:
    expected = (get_settings().django_panel_token or "").strip()
    if not expected or not x_panel_token or not hmac.compare_digest(x_panel_token, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/playlist-refresh")
async def playlist_refresh(
    request: Request,
    body: PlaylistRefreshBody | None = None,
    x_panel_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Django llama esto tras aprobar un anuncio o activar una campaña."""
    _require_panel_token(x_panel_token)
    db = request.app.state.db
    paradero_id = body.paradero_id if body else None

    if paradero_id is not None:
        devices = await db.list_devices_by_django_paradero(paradero_id)
        for d in devices:
            await notify_paradero(request, d["id"])
        return {"ok": True, "paradero_id": paradero_id, "devices": len(devices)}

    # Todos los que tienen django_paradero_id
    all_devs = await db.list_devices()
    n = 0
    for d in all_devs:
        if d.get("django_paradero_id") is not None:
            await notify_paradero(request, d["id"])
            n += 1
    return {"ok": True, "paradero_id": None, "devices": n}
