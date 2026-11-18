"""Usuarios MQTT: hash Mosquitto `$7$` (PBKDF2-SHA512) y generación de passwd/ACL.

Mecanismo de recarga (ver docs/mqtt-usuarios-arquitectura.md):
el panel escribe ``passwd.dynamic`` y ``acl.dynamic`` de forma atómica en un
directorio compartido (MQTT_AUTH_DIR) montado también en el contenedor de
Mosquitto. El entrypoint de Mosquitto vigila el archivo ``.version`` y, cuando
cambia, fusiona los usuarios base (kiosco, pi, healthcheck) con los dinámicos y
envía SIGHUP al broker, que recarga password_file y acl_file sin reiniciar.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import string
import tempfile
import time
from pathlib import Path
from typing import Iterable

# Mismos parámetros por defecto que mosquitto_passwd 2.x
PBKDF2_ITERATIONS = 101
SALT_LEN = 12

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}$")
# Usuarios base del broker (passwd base) — no se pueden crear aquí.
RESERVED_USERNAMES = {"gowayki-iot", "raspberry-pi", "healthcheck", "admin", "root", "mosquitto"}


def hash_password(password: str, *, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Devuelve ``$7$<iter>$<salt_b64>$<hash_b64>`` compatible con mosquitto_passwd."""
    if salt is None:
        salt = secrets.token_bytes(SALT_LEN)
    dk = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, iterations)
    return f"$7${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, kind, iters, salt_b64, hash_b64 = encoded.split("$")
        if kind != "7":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, int(iters))
    return hmac.compare_digest(dk, expected)


def generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def validate_username(username: str) -> str:
    u = (username or "").strip().lower()
    if not USERNAME_RE.match(u):
        raise ValueError("Usuario inválido: 3-32 caracteres a-z, 0-9, '-' o '_' (empieza con letra/número)")
    if u in RESERVED_USERNAMES:
        raise ValueError("Ese nombre de usuario está reservado")
    return u


def user_topic_root(prefix: str, username: str) -> str:
    return f"{prefix.strip('/')}/{username}"


def render_passwd(users: Iterable[dict]) -> str:
    return "".join(f"{u['username']}:{u['password_hash']}\n" for u in users)


def render_acl(users: Iterable[dict], prefix: str) -> str:
    lines = ["# Generado por GoWayki IoT panel — no editar a mano", ""]
    for u in users:
        lines += [f"user {u['username']}", f"topic readwrite {user_topic_root(prefix, u['username'])}/#", ""]
    return "\n".join(lines)


def _atomic_write(path: Path, content: str, mode: int = 0o640) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_auth_files(auth_dir: str, users: list[dict], prefix: str) -> bool:
    """Escribe passwd.dynamic / acl.dynamic y actualiza .version (señal de recarga)."""
    if not auth_dir:
        return False
    d = Path(auth_dir)
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write(d / "passwd.dynamic", render_passwd(users))
    _atomic_write(d / "acl.dynamic", render_acl(users, prefix))
    _atomic_write(d / ".version", f"{time.time_ns()}\n", mode=0o644)
    return True
