from __future__ import annotations

from urllib.parse import urlparse

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}


def _path_ext(url: str) -> str:
    path = urlparse(url).path.lower()
    # strip query already handled by urlparse.path
    if "." not in path.rsplit("/", 1)[-1]:
        return ""
    return "." + path.rsplit(".", 1)[-1]


def detect_media_type(url: str) -> str | None:
    ext = _path_ext(url)
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def validate_ad_url(url: str) -> tuple[str, str]:
    """Return (normalized_url, media_type) or raise ValueError."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("URL vacía")

    parsed = urlparse(raw)
    if parsed.scheme not in ("https", "http"):
        raise ValueError("La URL debe empezar con https:// (o http:// en desarrollo)")
    if not parsed.netloc:
        raise ValueError("URL inválida: falta el host")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ValueError("En producción usa https:// (http solo permitido en localhost)")

    # Reject common page hosts that are not direct media files
    host = (parsed.hostname or "").lower()
    blocked = (
        "youtube.com",
        "www.youtube.com",
        "youtu.be",
        "vimeo.com",
        "www.vimeo.com",
        "tiktok.com",
        "www.tiktok.com",
    )
    if host in blocked:
        raise ValueError("No se admiten enlaces de YouTube/Vimeo/TikTok: usa URL directa al archivo (.mp4, .jpg…)")

    media = detect_media_type(raw)
    if not media:
        raise ValueError(
            "Extensión no soportada. Imágenes: "
            + ", ".join(sorted(IMAGE_EXTS))
            + " · Videos: "
            + ", ".join(sorted(VIDEO_EXTS))
        )

    return raw, media
