from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # URL pública del panel (incluye prefijo si va detrás de nginx, ej. .../iot)
    base_url: str = "https://paradero.172-214-154-175.sslip.io/iot"
    # Prefijo público cuando nginx hace strip (ej. /iot). Vacío = raíz (dev local).
    root_path: str = ""
    database_url: str = ""
    app_db_path: str = "./data/iot.db"
    admin_token: str = "change-me"
    secret_key: str = "change-me"
    port: int = 8000
    ad_playlist_urls: str = ""
    pairing_ttl_minutes: int = 10
    pair_rate_limit_per_minute: int = 5
    online_threshold_seconds: int = 120

    @property
    def root_path_normalized(self) -> str:
        p = (self.root_path or "").strip().rstrip("/")
        if p and not p.startswith("/"):
            p = f"/{p}"
        return p

    # MQTT (Mosquitto de la VM) — leave mqtt_host empty to disable bridge
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    mqtt_topic_prefix: str = "gowayki"
    # Prefijo del panel gpio-monitor en la Pi (topics paradero/<id>/estado)
    mqtt_gpio_prefix: str = "paradero"
    # Directorio compartido con el contenedor Mosquitto (passwd/acl dinámicos).
    # Vacío = la pestaña "Usuarios MQTT" guarda en SQLite pero no publica archivos.
    mqtt_auth_dir: str = ""
    # Host que se muestra a los usuarios del equipo (placeholder; no poner IPs en el repo)
    mqtt_public_host: str = "172-214-154-175.sslip.io"
    mqtt_public_port: int = 1883

    # Feed GTFS local (pruebas sin Postgres). Ej: /workspace/gtfs
    gtfs_local_dir: str = ""
    # Segundos de inactividad antes del salvapantallas de anuncios
    default_idle_seconds: int = 45

    # Paradero Digital (Django): playlist de anuncios pagados/aprobados
    # Vacío = solo playlist local SQLite del FastAPI
    django_url: str = ""
    # Mismo valor que PANEL_IOT_TOKEN en Django / .env del panel Node
    django_panel_token: str = ""
    # Timeout HTTP al pedir /api/paraderos/<id>/playlist/
    django_timeout_sec: float = 4.0

    @property
    def ad_urls(self) -> list[str]:
        if not self.ad_playlist_urls.strip():
            return []
        return [u.strip() for u in self.ad_playlist_urls.split(",") if u.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
