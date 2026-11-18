# Agente Raspberry Pi (esqueleto)

El panel trata al Pi como un dispositivo más (`device_type=pi`):

1. Admin crea dispositivo tipo **Raspberry Pi** y genera código 6 dígitos.
2. El agente llama `POST /api/kiosk/pair` (mismo contrato que la tablet).
3. Cada ~60 s llama `POST /api/kiosk/heartbeat` con `Authorization: Bearer <token>`.
4. Si la respuesta trae `command`:
   - `reload` → reinicia Chromium / recarga URL
   - `unpair` → borra token local y vuelve a pedir código
   - `reboot` → `sudo reboot` (solo en Pi)

## Dependencias sugeridas

```bash
sudo apt install -y python3-venv chromium-browser unclutter
python3 -m venv ~/gowayki-agent/.venv
~/gowayki-agent/.venv/bin/pip install httpx
```

## Esqueleto (`agent.py`)

```python
#!/usr/bin/env python3
"""Agente mínimo GoWayki IoT para Raspberry Pi — fase 2."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx

API = os.environ.get("API_BASE_URL", "https://paradero.172-214-154-175.sslip.io").rstrip("/")
STATE = Path.home() / ".gowayki_iot_session.json"


def load_session() -> dict | None:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return None


def save_session(data: dict) -> None:
    STATE.write_text(json.dumps(data))


def clear_session() -> None:
    if STATE.exists():
        STATE.unlink()


def pair(code: str) -> dict:
    r = httpx.post(
        f"{API}/api/kiosk/pair",
        json={
            "code": code,
            "device": {"platform": "linux", "os_version": "raspberrypi", "app_version": "pi-agent/0.1"},
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    save_session(data)
    return data


def heartbeat(session: dict) -> dict:
    r = httpx.post(
        f"{API}/api/kiosk/heartbeat",
        headers={"Authorization": f"Bearer {session['token']}"},
        json={"app_version": "pi-agent/0.1", "display_url": session.get("display_url"), "page_ok": True},
        timeout=20,
    )
    if r.status_code in (401, 403):
        clear_session()
        raise SystemExit("unpaired")
    r.raise_for_status()
    return r.json() if r.content else {}


def open_kiosk(url: str) -> None:
    subprocess.Popen(
        [
            "chromium-browser",
            "--kiosk",
            "--noerrdialogs",
            "--disable-infobars",
            url,
        ]
    )


def main() -> None:
    session = load_session()
    if not session:
        code = input("Código 6 dígitos: ").strip()
        session = pair(code)
        open_kiosk(session["display_url"])

    while True:
        try:
            result = heartbeat(session)
        except Exception as exc:
            print("heartbeat error", exc)
            time.sleep(15)
            continue

        cmd = result.get("command")
        if cmd == "unpair":
            clear_session()
            raise SystemExit("unpair")
        if cmd == "reload" or result.get("display_url"):
            url = result.get("display_url") or session["display_url"]
            session["display_url"] = url
            save_session(session)
            open_kiosk(url)
        if cmd == "reboot":
            subprocess.run(["sudo", "reboot"], check=False)

        time.sleep(60)


if __name__ == "__main__":
    main()
```

## Notas de seguridad

- No exponer SSH a internet sin llave y fail2ban.
- `reboot` solo debe existir en dispositivos `pi` de confianza.
- GPIO / sensores / cámara: fase posterior; este agente solo cubre display + comandos remotos.
