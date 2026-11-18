"""Lee la playlist de anuncios del Paradero Digital (Django) para la tablet."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings

log = logging.getLogger("gowayki.django_playlist")


def _media_type(tipo: str | None, url: str) -> str:
    t = (tipo or "").lower()
    if t in ("video", "image"):
        return t
    u = (url or "").lower().split("?", 1)[0]
    if u.endswith((".mp4", ".webm", ".mov", ".m4v")):
        return "video"
    return "image"


async def fetch_django_ads(settings: Settings, paradero_id: int) -> list[dict[str, Any]] | None:
    """GET /api/paraderos/{id}/playlist/ con X-Panel-Token.

    Devuelve lista en formato del kiosco FastAPI ({url, media_type, duration_sec, ...})
    o None si Django no está configurado / falla (el caller puede caer a SQLite local).
    """
    base = (settings.django_url or "").rstrip("/")
    token = (settings.django_panel_token or "").strip()
    if not base or not token:
        return None

    url = f"{base}/api/paraderos/{int(paradero_id)}/playlist/"
    try:
        async with httpx.AsyncClient(timeout=settings.django_timeout_sec) as client:
            res = await client.get(url, headers={"X-Panel-Token": token, "Accept": "application/json"})
        if res.status_code != 200:
            log.warning("django playlist %s → %s %s", url, res.status_code, res.text[:200])
            return None
        data = res.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("django playlist error: %s", exc)
        return None

    anuncios = data.get("anuncios") if isinstance(data, dict) else None
    if not isinstance(anuncios, list):
        return None

    out: list[dict[str, Any]] = []
    for i, a in enumerate(anuncios):
        if not isinstance(a, dict):
            continue
        media_url = str(a.get("url") or "").strip()
        if not media_url:
            continue
        dur = a.get("duracion_seg")
        try:
            duration = int(dur) if dur is not None else 10
        except (TypeError, ValueError):
            duration = 10
        duration = max(3, min(120, duration))
        out.append(
            {
                "id": a.get("id") if a.get("id") is not None else f"dj-{i}",
                "url": media_url,
                "media_type": _media_type(str(a.get("tipo") or ""), media_url),
                "duration_sec": duration,
                "sort_order": i,
                "active": 1,
                "titulo": a.get("titulo") or "",
                "qr_url": a.get("qr_url"),
                "enlace": a.get("enlace") or "",
                "patrocinado": bool(a.get("patrocinado")),
                "source": "django",
            }
        )
    return out
