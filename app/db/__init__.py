from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any

import aiosqlite

from app.config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    device_type TEXT NOT NULL DEFAULT 'kiosk',
    stop_id TEXT,
    token_hash TEXT,
    display_url TEXT,
    pending_command TEXT,
    pending_display_url TEXT,
    last_heartbeat_at REAL,
    last_app_version TEXT,
    last_page_ok INTEGER,
    last_device_info TEXT,
    reported_display_url TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pairing_codes (
    code TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    created_at REAL NOT NULL,
    FOREIGN KEY (device_id) REFERENCES devices(id)
);

CREATE TABLE IF NOT EXISTS pair_attempts (
    ip TEXT NOT NULL,
    attempted_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ad_playlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'image',
    duration_sec INTEGER NOT NULL DEFAULT 10,
    sort_order INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS remote_jobs (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'shell',
    command TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    exit_code INTEGER,
    stdout TEXT,
    stderr TEXT,
    timeout_sec INTEGER NOT NULL DEFAULT 30,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    FOREIGN KEY (device_id) REFERENCES devices(id)
);

CREATE INDEX IF NOT EXISTS idx_pair_attempts_ip_time ON pair_attempts(ip, attempted_at);
CREATE INDEX IF NOT EXISTS idx_devices_token_hash ON devices(token_hash);
CREATE INDEX IF NOT EXISTS idx_remote_jobs_device_status ON remote_jobs(device_id, status, created_at);

CREATE TABLE IF NOT EXISTS mqtt_users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MIGRATE_SQL = [
    "ALTER TABLE devices ADD COLUMN mqtt_online INTEGER",
    "ALTER TABLE devices ADD COLUMN mqtt_last_seen_at REAL",
    "ALTER TABLE devices ADD COLUMN mqtt_last_payload TEXT",
    "ALTER TABLE devices ADD COLUMN last_telemetry TEXT",
    "ALTER TABLE devices ADD COLUMN last_lat REAL",
    "ALTER TABLE devices ADD COLUMN last_lon REAL",
    "ALTER TABLE devices ADD COLUMN last_location_accuracy_m REAL",
    "ALTER TABLE devices ADD COLUMN last_location_at REAL",
    "ALTER TABLE devices ADD COLUMN last_battery_level INTEGER",
    "ALTER TABLE devices ADD COLUMN last_battery_charging INTEGER",
    "ALTER TABLE devices ADD COLUMN gpio_snapshot TEXT",
    "ALTER TABLE devices ADD COLUMN gpio_updated_at REAL",
    "ALTER TABLE devices ADD COLUMN pair_code TEXT",
    "ALTER TABLE devices ADD COLUMN reported_display_url TEXT",
]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_device_id() -> str:
    return f"k_{secrets.token_urlsafe(12)}"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_pair_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


class AppDatabase:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        for stmt in MIGRATE_SQL:
            try:
                await self._conn.execute(stmt)
            except aiosqlite.OperationalError:
                pass
        await self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_pair_code ON devices(pair_code)"
        )
        await self._conn.commit()
        await self._backfill_pair_codes()
        await self._ensure_default_settings()

    # ---- Usuarios MQTT ----
    async def list_mqtt_users(self, *, with_hash: bool = False) -> list[dict[str, Any]]:
        cols = "username, password_hash, created_at, updated_at" if with_hash else "username, created_at, updated_at"
        cur = await self.conn.execute(f"SELECT {cols} FROM mqtt_users ORDER BY username")
        return [dict(r) for r in await cur.fetchall()]

    async def get_mqtt_user(self, username: str) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT username, created_at, updated_at FROM mqtt_users WHERE username = ?", (username,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def create_mqtt_user(self, username: str, password_hash: str) -> bool:
        now = time.time()
        try:
            await self.conn.execute(
                "INSERT INTO mqtt_users (username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (username, password_hash, now, now),
            )
        except aiosqlite.IntegrityError:
            return False
        await self.conn.commit()
        return True

    async def set_mqtt_user_password(self, username: str, password_hash: str) -> bool:
        cur = await self.conn.execute(
            "UPDATE mqtt_users SET password_hash = ?, updated_at = ? WHERE username = ?",
            (password_hash, time.time(), username),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def delete_mqtt_user(self, username: str) -> bool:
        cur = await self.conn.execute("DELETE FROM mqtt_users WHERE username = ?", (username,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database not connected")
        return self._conn

    async def _alloc_pair_code(self) -> str:
        for _ in range(40):
            code = new_pair_code()
            cur = await self.conn.execute(
                "SELECT 1 AS ok FROM devices WHERE pair_code = ?",
                (code,),
            )
            if not await cur.fetchone():
                return code
        raise RuntimeError("Could not allocate unique pair code")

    async def _backfill_pair_codes(self) -> None:
        cur = await self.conn.execute(
            "SELECT id FROM devices WHERE pair_code IS NULL OR pair_code = ''"
        )
        rows = await cur.fetchall()
        for row in rows:
            code = await self._alloc_pair_code()
            await self.conn.execute(
                "UPDATE devices SET pair_code = ? WHERE id = ?",
                (code, row["id"]),
            )
        if rows:
            await self.conn.commit()

    async def create_device(
        self,
        *,
        name: str,
        stop_id: str | None,
        device_type: str,
        base_url: str,
    ) -> dict[str, Any]:
        now = time.time()
        device_id = new_device_id()
        pair_code = await self._alloc_pair_code()
        display_url = f"{base_url.rstrip('/')}/paradero/{device_id}"
        await self.conn.execute(
            """
            INSERT INTO devices
              (id, name, device_type, stop_id, display_url, pair_code, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (device_id, name, device_type, stop_id, display_url, pair_code, now, now),
        )
        await self.conn.commit()
        return await self.get_device(device_id)  # type: ignore[return-value]

    async def update_device(
        self,
        device_id: str,
        *,
        name: str | None = None,
        stop_id: str | None = None,
        device_type: str | None = None,
        update_stop: bool = False,
    ) -> dict[str, Any] | None:
        device = await self.get_device(device_id)
        if not device:
            return None
        now = time.time()
        new_stop = (stop_id or None) if update_stop else device.get("stop_id")
        await self.conn.execute(
            """
            UPDATE devices
            SET name = COALESCE(?, name),
                stop_id = ?,
                device_type = COALESCE(?, device_type),
                updated_at = ?
            WHERE id = ?
            """,
            (name, new_stop if update_stop else device.get("stop_id"), device_type, now, device_id),
        )
        await self.conn.commit()
        return await self.get_device(device_id)

    async def list_devices(self) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM devices ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_device(self, device_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_device_by_token(self, token: str) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT * FROM devices WHERE token_hash = ?",
            (hash_token(token),),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def create_pairing_code(self, device_id: str, ttl_minutes: int = 0) -> str:
        """Devuelve el código fijo del dispositivo (no expira ni se regenera)."""
        device = await self.get_device(device_id)
        if not device:
            raise ValueError("Device not found")
        code = device.get("pair_code")
        if code and len(str(code)) == 6:
            return str(code)
        code = await self._alloc_pair_code()
        await self.conn.execute(
            "UPDATE devices SET pair_code = ?, updated_at = ? WHERE id = ?",
            (code, time.time(), device_id),
        )
        await self.conn.commit()
        return code

    async def regenerate_pair_code(self, device_id: str) -> str:
        device = await self.get_device(device_id)
        if not device:
            raise ValueError("Device not found")
        code = await self._alloc_pair_code()
        await self.conn.execute(
            "UPDATE devices SET pair_code = ?, updated_at = ? WHERE id = ?",
            (code, time.time(), device_id),
        )
        await self.conn.commit()
        return code

    async def delete_device(self, device_id: str) -> bool:
        device = await self.get_device(device_id)
        if not device:
            return False
        await self.conn.execute("DELETE FROM pairing_codes WHERE device_id = ?", (device_id,))
        await self.conn.execute("DELETE FROM remote_jobs WHERE device_id = ?", (device_id,))
        cur = await self.conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def count_pair_attempts(self, ip: str, window_seconds: int = 60) -> int:
        since = time.time() - window_seconds
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM pair_attempts WHERE ip = ? AND attempted_at >= ?",
            (ip, since),
        )
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def record_pair_attempt(self, ip: str) -> None:
        await self.conn.execute(
            "INSERT INTO pair_attempts (ip, attempted_at) VALUES (?, ?)",
            (ip, time.time()),
        )
        # prune old
        await self.conn.execute(
            "DELETE FROM pair_attempts WHERE attempted_at < ?",
            (time.time() - 3600,),
        )
        await self.conn.commit()

    async def pair_with_code(
        self,
        code: str,
        device_info: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        now = time.time()
        # Código fijo y único por dispositivo (reutilizable tras unpair).
        cur = await self.conn.execute(
            "SELECT * FROM devices WHERE pair_code = ?",
            (code,),
        )
        row = await cur.fetchone()
        if not row:
            return None

        token = new_token()
        token_h = hash_token(token)
        await self.conn.execute(
            """
            UPDATE devices
            SET token_hash = ?,
                last_device_info = ?,
                last_heartbeat_at = ?,
                pending_command = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (
                token_h,
                json.dumps(device_info or {}),
                now,
                now,
                row["id"],
            ),
        )
        await self.conn.commit()
        return {
            "kiosk_id": row["id"],
            "token": token,
            "display_url": row["display_url"],
            "name": row["name"],
            "device_type": row["device_type"],
        }

    async def heartbeat(
        self,
        token: str,
        *,
        app_version: str | None,
        display_url: str | None,
        page_ok: bool | None,
        telemetry: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        device = await self.get_device_by_token(token)
        if not device:
            return None
        now = time.time()
        if telemetry:
            await self._store_telemetry(device["id"], telemetry, now)
        await self.conn.execute(
            """
            UPDATE devices
            SET last_heartbeat_at = ?,
                last_app_version = COALESCE(?, last_app_version),
                last_page_ok = ?,
                reported_display_url = COALESCE(?, reported_display_url),
                updated_at = ?
            WHERE id = ?
            """,
            (
                now,
                app_version,
                1 if page_ok else 0 if page_ok is not None else device.get("last_page_ok"),
                display_url,
                now,
                device["id"],
            ),
        )

        command = device.get("pending_command")
        pending_url = device.get("pending_display_url")
        response: dict[str, Any] = {}

        if command:
            response["command"] = command
            await self.conn.execute(
                "UPDATE devices SET pending_command = NULL WHERE id = ?",
                (device["id"],),
            )
        if pending_url:
            response["display_url"] = pending_url
            await self.conn.execute(
                """
                UPDATE devices
                SET display_url = ?, pending_display_url = NULL
                WHERE id = ?
                """,
                (pending_url, device["id"]),
            )
        await self.conn.commit()
        return response

    async def _store_telemetry(
        self, device_id: str, telemetry: dict[str, Any], now: float
    ) -> None:
        location = telemetry.get("location") if isinstance(telemetry.get("location"), dict) else None
        battery = telemetry.get("battery") if isinstance(telemetry.get("battery"), dict) else None
        lat = lon = acc = None
        if location:
            try:
                lat, lon = float(location["lat"]), float(location["lon"])
                acc = float(location["accuracy_m"]) if location.get("accuracy_m") is not None else None
            except (KeyError, TypeError, ValueError):
                lat = lon = acc = None
        level = battery.get("level") if battery else None
        charging = battery.get("charging") if battery else None
        await self.conn.execute(
            """
            UPDATE devices
            SET last_telemetry = ?,
                last_lat = COALESCE(?, last_lat),
                last_lon = COALESCE(?, last_lon),
                last_location_accuracy_m = CASE WHEN ? IS NULL THEN last_location_accuracy_m ELSE ? END,
                last_location_at = CASE WHEN ? IS NULL THEN last_location_at ELSE ? END,
                last_battery_level = COALESCE(?, last_battery_level),
                last_battery_charging = COALESCE(?, last_battery_charging)
            WHERE id = ?
            """,
            (
                json.dumps(telemetry)[:20_000],
                lat,
                lon,
                lat, acc,
                lat, now,
                int(level) if isinstance(level, (int, float)) else None,
                None if charging is None else (1 if charging else 0),
                device_id,
            ),
        )

    async def enqueue_command(
        self,
        device_id: str,
        command: str,
        display_url: str | None = None,
    ) -> dict[str, Any] | None:
        device = await self.get_device(device_id)
        if not device:
            return None
        if command == "unpair":
            await self.conn.execute(
                """
                UPDATE devices
                SET pending_command = 'unpair',
                    token_hash = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (time.time(), device_id),
            )
        elif command == "reload":
            await self.conn.execute(
                """
                UPDATE devices SET pending_command = 'reload', updated_at = ?
                WHERE id = ?
                """,
                (time.time(), device_id),
            )
        elif command == "reboot":
            await self.conn.execute(
                """
                UPDATE devices SET pending_command = 'reboot', updated_at = ?
                WHERE id = ?
                """,
                (time.time(), device_id),
            )
        else:
            raise ValueError(f"Unknown command: {command}")

        if display_url is not None:
            await self.conn.execute(
                """
                UPDATE devices SET pending_display_url = ?, updated_at = ?
                WHERE id = ?
                """,
                (display_url, time.time(), device_id),
            )
        await self.conn.commit()
        return await self.get_device(device_id)

    async def revoke_token(self, device_id: str) -> None:
        await self.conn.execute(
            """
            UPDATE devices
            SET token_hash = NULL, pending_command = 'unpair', updated_at = ?
            WHERE id = ?
            """,
            (time.time(), device_id),
        )
        await self.conn.commit()

    async def apply_mqtt_event(
        self,
        device_id: str,
        kind: str,
        data: dict[str, Any],
    ) -> None:
        device = await self.get_device(device_id)
        if not device:
            return
        now = time.time()
        payload = json.dumps(data)
        if kind == "estado":
            online = 1 if data.get("online") else 0
            await self.conn.execute(
                """
                UPDATE devices
                SET mqtt_online = ?,
                    mqtt_last_seen_at = ?,
                    mqtt_last_payload = ?,
                    last_heartbeat_at = CASE WHEN ? = 1 THEN ? ELSE last_heartbeat_at END,
                    updated_at = ?
                WHERE id = ?
                """,
                (online, now, payload, online, now, now, device_id),
            )
        elif kind == "telemetria":
            await self.conn.execute(
                """
                UPDATE devices
                SET mqtt_online = 1,
                    mqtt_last_seen_at = ?,
                    mqtt_last_payload = ?,
                    last_heartbeat_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, payload, now, now, device_id),
            )
        await self.conn.commit()

    async def list_ads(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        if include_inactive:
            cur = await self.conn.execute(
                "SELECT * FROM ad_playlist ORDER BY sort_order ASC, id ASC"
            )
        else:
            cur = await self.conn.execute(
                """
                SELECT * FROM ad_playlist
                WHERE active = 1
                ORDER BY sort_order ASC, id ASC
                """
            )
        return [dict(r) for r in await cur.fetchall()]

    async def get_ad(self, ad_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM ad_playlist WHERE id = ?", (ad_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def create_ad(
        self,
        *,
        url: str,
        media_type: str,
        duration_sec: int,
        sort_order: int | None = None,
        active: bool = True,
    ) -> dict[str, Any]:
        if sort_order is None:
            cur = await self.conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM ad_playlist"
            )
            row = await cur.fetchone()
            sort_order = int(row["n"]) if row else 0
        cur = await self.conn.execute(
            """
            INSERT INTO ad_playlist (url, media_type, duration_sec, sort_order, active)
            VALUES (?, ?, ?, ?, ?)
            """,
            (url, media_type, duration_sec, sort_order, 1 if active else 0),
        )
        await self.conn.commit()
        return await self.get_ad(cur.lastrowid)  # type: ignore[arg-type]

    async def update_ad(
        self,
        ad_id: int,
        *,
        url: str | None = None,
        media_type: str | None = None,
        duration_sec: int | None = None,
        sort_order: int | None = None,
        active: bool | None = None,
    ) -> dict[str, Any] | None:
        ad = await self.get_ad(ad_id)
        if not ad:
            return None
        await self.conn.execute(
            """
            UPDATE ad_playlist
            SET url = COALESCE(?, url),
                media_type = COALESCE(?, media_type),
                duration_sec = COALESCE(?, duration_sec),
                sort_order = COALESCE(?, sort_order),
                active = COALESCE(?, active)
            WHERE id = ?
            """,
            (
                url,
                media_type,
                duration_sec,
                sort_order,
                None if active is None else (1 if active else 0),
                ad_id,
            ),
        )
        await self.conn.commit()
        return await self.get_ad(ad_id)

    async def delete_ad(self, ad_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM ad_playlist WHERE id = ?", (ad_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def reorder_ads(self, ordered_ids: list[int]) -> list[dict[str, Any]]:
        for i, ad_id in enumerate(ordered_ids):
            await self.conn.execute(
                "UPDATE ad_playlist SET sort_order = ? WHERE id = ?",
                (i, ad_id),
            )
        await self.conn.commit()
        return await self.list_ads(include_inactive=True)


    async def _ensure_default_settings(self) -> None:
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO app_settings (key, value)
            VALUES ('idle_seconds', '45')
            """
        )
        await self.conn.commit()

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        cur = await self.conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (key,),
        )
        row = await cur.fetchone()
        if not row:
            return default
        return str(row["value"])

    async def get_settings_map(self) -> dict[str, Any]:
        cur = await self.conn.execute("SELECT key, value FROM app_settings")
        rows = await cur.fetchall()
        data = {str(r["key"]): str(r["value"]) for r in rows}
        idle = int(data.get("idle_seconds") or 45)
        idle = max(10, min(600, idle))
        return {"idle_seconds": idle}

    async def set_settings(self, *, idle_seconds: int | None = None) -> dict[str, Any]:
        if idle_seconds is not None:
            idle = max(10, min(600, int(idle_seconds)))
            await self.conn.execute(
                """
                INSERT INTO app_settings (key, value) VALUES ('idle_seconds', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(idle),),
            )
            await self.conn.commit()
        return await self.get_settings_map()

    async def seed_demo_ads_if_empty(self, settings: Settings) -> None:
        cur = await self.conn.execute("SELECT COUNT(*) AS c FROM ad_playlist")
        row = await cur.fetchone()
        if row and int(row["c"]) > 0:
            return
        urls = settings.ad_urls
        if urls:
            for i, url in enumerate(urls):
                media = "video" if any(url.lower().endswith(ext) for ext in (".mp4", ".webm", ".mov")) else "image"
                await self.conn.execute(
                    """
                    INSERT INTO ad_playlist (url, media_type, duration_sec, sort_order, active)
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (url, media, 12 if media == "image" else 30, i),
                )
        else:
            # Local CSS gradient placeholders via data URLs are handled in the page;
            # store sentinel entries so the API returns a playlist.
            demos = [
                ("/static/paradero/ads/demo1.svg", "image", 10),
                ("/static/paradero/ads/demo2.svg", "image", 10),
                ("/static/paradero/ads/demo3.svg", "image", 10),
            ]
            for i, (url, media, dur) in enumerate(demos):
                await self.conn.execute(
                    """
                    INSERT INTO ad_playlist (url, media_type, duration_sec, sort_order, active)
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (url, media, dur, i),
                )
        await self.conn.commit()

    async def create_remote_job(
        self,
        device_id: str,
        command: str,
        *,
        kind: str = "shell",
        timeout_sec: int = 30,
    ) -> dict[str, Any]:
        job_id = f"j_{secrets.token_urlsafe(10)}"
        now = time.time()
        await self.conn.execute(
            """
            INSERT INTO remote_jobs
              (id, device_id, kind, command, status, timeout_sec, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (job_id, device_id, kind, command, timeout_sec, now),
        )
        await self.conn.commit()
        job = await self.get_remote_job(job_id)
        assert job is not None
        return job

    async def get_remote_job(self, job_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM remote_jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_remote_jobs(self, device_id: str, *, limit: int = 30) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            """
            SELECT * FROM remote_jobs
            WHERE device_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (device_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def claim_next_remote_job(self, device_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            """
            SELECT * FROM remote_jobs
            WHERE device_id = ? AND status = 'pending'
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (device_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        job = dict(row)
        now = time.time()
        await self.conn.execute(
            """
            UPDATE remote_jobs
            SET status = 'running', started_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (now, job["id"]),
        )
        await self.conn.commit()
        return await self.get_remote_job(job["id"])

    async def finish_remote_job(
        self,
        job_id: str,
        *,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        status: str = "done",
    ) -> dict[str, Any] | None:
        stdout = (stdout or "")[:100_000]
        stderr = (stderr or "")[:100_000]
        await self.conn.execute(
            """
            UPDATE remote_jobs
            SET status = ?, exit_code = ?, stdout = ?, stderr = ?, finished_at = ?
            WHERE id = ?
            """,
            (status, exit_code, stdout, stderr, time.time(), job_id),
        )
        await self.conn.commit()
        return await self.get_remote_job(job_id)

    async def update_gpio_snapshot(self, device_id: str, snapshot: dict[str, Any]) -> None:
        await self.conn.execute(
            """
            UPDATE devices
            SET gpio_snapshot = ?, gpio_updated_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(snapshot), time.time(), time.time(), device_id),
        )
        await self.conn.commit()

    async def patch_gpio_live(
        self,
        gpio_id: str,
        *,
        on: bool,
        value: Any,
        text: str | None = None,
        changed: float | None = None,
        tipo: str | None = None,
    ) -> list[str]:
        """Actualiza el live de un gpio dentro de los snapshots de Pis. Devuelve device_ids tocados."""
        cur = await self.conn.execute(
            "SELECT id, gpio_snapshot FROM devices WHERE device_type = 'pi' AND gpio_snapshot IS NOT NULL"
        )
        rows = await cur.fetchall()
        touched: list[str] = []
        now = time.time()
        for row in rows:
            try:
                snap = json.loads(row["gpio_snapshot"] or "{}")
            except json.JSONDecodeError:
                continue
            devices = snap.get("devices")
            if not isinstance(devices, list):
                continue
            changed_any = False
            for g in devices:
                if not isinstance(g, dict) or g.get("id") != gpio_id:
                    continue
                live = dict(g.get("live") or {})
                prev_on = bool(live.get("on"))
                live["on"] = bool(on)
                live["value"] = value
                live["sync"] = True
                live["changed"] = float(changed) if changed is not None else now
                if text:
                    live["text"] = text
                elif g.get("type") == "entrada" or tipo == "entrada":
                    live["text"] = "Activo" if on else "Inactivo"
                else:
                    live["text"] = "Encendido" if on else "Apagado"
                if (g.get("type") == "entrada" or tipo == "entrada") and on and not prev_on:
                    live["count"] = int(live.get("count") or 0) + 1
                g["live"] = live
                changed_any = True
            if changed_any:
                snap["ts"] = now
                await self.conn.execute(
                    """
                    UPDATE devices
                    SET gpio_snapshot = ?, gpio_updated_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (json.dumps(snap), now, now, row["id"]),
                )
                touched.append(row["id"])
        if touched:
            await self.conn.commit()
        return touched

    async def get_gpio_payload(self, device_id: str) -> dict[str, Any] | None:
        device = await self.get_device(device_id)
        if not device:
            return None
        snap: dict[str, Any] = {}
        raw = device.get("gpio_snapshot")
        if raw:
            try:
                snap = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                snap = {}
        return {
            "device_id": device_id,
            "gpio_updated_at": device.get("gpio_updated_at"),
            "snapshot": snap,
        }
