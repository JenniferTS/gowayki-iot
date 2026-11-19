#!/usr/bin/env python3
"""GoWayki Pi agent: HTTP pair + remote shell jobs + MQTT realtime."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import paho.mqtt.client as mqtt

API = os.environ.get("API_BASE_URL", "https://paradero.172-214-154-175.sslip.io").rstrip("/")
MQTT_HOST = os.environ.get("MQTT_HOST", "172-214-154-175.sslip.io")
# 1883 suele estar bloqueado hacia la VM; 8883 (MQTTS) es el puerto público.
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_TLS = os.environ.get("MQTT_TLS", "1" if MQTT_PORT == 8883 else "0") not in ("0", "false", "False")
MQTT_TLS_INSECURE = os.environ.get("MQTT_TLS_INSECURE", "0") in ("1", "true", "True")
TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "gowayki").strip("/")
ENABLE_KIOSK = os.environ.get("ENABLE_KIOSK_DISPLAY", "1") not in ("0", "false", "False")

STATE_DIR = Path(os.environ.get("GOWAYKI_STATE_DIR", "/var/lib/gowayki"))
SESSION_PATH = STATE_DIR / "session.json"
PAIR_CODE_PATH = Path(os.environ.get("GOWAYKI_PAIR_CODE_FILE", "/etc/gowayki/pair.code"))
APP_VERSION = "pi-agent/0.3.2"
GPIO_MONITOR_URL = os.environ.get("GPIO_MONITOR_URL", "http://127.0.0.1:8080").rstrip("/")


def load_session() -> dict | None:
    if SESSION_PATH.exists():
        return json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    return None


def save_session(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def clear_session() -> None:
    if SESSION_PATH.exists():
        SESSION_PATH.unlink()


def read_pair_code() -> str | None:
    env_code = (os.environ.get("PAIR_CODE") or "").strip()
    if env_code:
        return "".join(ch for ch in env_code if ch.isdigit())
    if PAIR_CODE_PATH.exists():
        raw = PAIR_CODE_PATH.read_text(encoding="utf-8").strip()
        code = "".join(ch for ch in raw if ch.isdigit())
        return code or None
    if os.isatty(0):
        raw = input("Código 6 dígitos del admin: ").strip()
        return "".join(ch for ch in raw if ch.isdigit()) or None
    return None


def pair(code: str) -> dict:
    r = httpx.post(
        f"{API}/api/kiosk/pair",
        json={
            "code": code,
            "device": {
                "platform": "linux",
                "os_version": "raspberrypi",
                "app_version": APP_VERSION,
            },
        },
        timeout=20,
    )
    if r.status_code in (400, 404, 410):
        raise SystemExit(f"Código inválido o expirado ({r.status_code})")
    if r.status_code == 429:
        raise SystemExit("Demasiados intentos de pair")
    r.raise_for_status()
    data = r.json()
    save_session(data)
    if PAIR_CODE_PATH.exists():
        try:
            PAIR_CODE_PATH.unlink()
        except OSError:
            pass
    return data


def auth_headers(session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['token']}"}


def http_heartbeat(session: dict, page_ok: bool = True) -> dict:
    r = httpx.post(
        f"{API}/api/kiosk/heartbeat",
        headers=auth_headers(session),
        json={
            "app_version": APP_VERSION,
            "display_url": session.get("display_url"),
            "page_ok": page_ok,
        },
        timeout=20,
    )
    if r.status_code in (401, 403):
        clear_session()
        raise SystemExit("unpaired by server")
    r.raise_for_status()
    return r.json() if r.content else {}


def poll_job(session: dict) -> dict | None:
    r = httpx.get(
        f"{API}/api/kiosk/jobs/next",
        headers=auth_headers(session),
        timeout=20,
    )
    if r.status_code in (401, 403):
        clear_session()
        raise SystemExit("unpaired by server")
    r.raise_for_status()
    data = r.json() if r.content else {}
    return data.get("job")


def report_job(session: dict, job_id: str, *, exit_code: int | None, stdout: str, stderr: str, status: str) -> None:
    r = httpx.post(
        f"{API}/api/kiosk/jobs/{job_id}/result",
        headers=auth_headers(session),
        json={
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "status": status,
        },
        timeout=30,
    )
    r.raise_for_status()


def fetch_gpio_snapshot() -> dict:
    try:
        r = httpx.get(f"{GPIO_MONITOR_URL}/api/devices", timeout=3)
        if r.status_code != 200:
            return {"available": False, "error": f"http {r.status_code}", "devices": [], "source": "gpio-monitor"}
        data = r.json()
        devices = data.get("devices") if isinstance(data, dict) else data
        return {
            "available": True,
            "error": None,
            "devices": devices or [],
            "source": "gpio-monitor",
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc), "devices": [], "source": "gpio-monitor"}


def report_gpio(session: dict, snapshot: dict) -> None:
    r = httpx.post(
        f"{API}/api/kiosk/gpio",
        headers=auth_headers(session),
        json={
            "available": bool(snapshot.get("available")),
            "error": snapshot.get("error"),
            "source": snapshot.get("source") or "gpio-monitor",
            "devices": snapshot.get("devices") or [],
        },
        timeout=15,
    )
    if r.status_code in (401, 403):
        clear_session()
        raise SystemExit("unpaired by server")
    r.raise_for_status()


def gpio_set(gpio_id: str, *, state: str | None = None, value: int | None = None) -> tuple[int, str, str, str]:
    body: dict = {}
    if state is not None:
        body["state"] = state
    if value is not None:
        body["value"] = value
    try:
        r = httpx.post(
            f"{GPIO_MONITOR_URL}/api/devices/{gpio_id}/set",
            json=body,
            timeout=8,
        )
        text = r.text
        if r.status_code >= 400:
            return r.status_code, "", text, "error"
        return 0, text, "", "done"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc), "error"


def run_shell(command: str, timeout_sec: int = 30) -> tuple[int | None, str, str, str]:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            executable="/bin/bash",
        )
        return proc.returncode, proc.stdout or "", proc.stderr or "", "done"
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return None, out, (err + "\n[timeout]").strip(), "timeout"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc), "error"


def open_kiosk(url: str) -> None:
    if not ENABLE_KIOSK or not url:
        return
    for bin_name in ("chromium-browser", "chromium", "google-chrome"):
        try:
            subprocess.Popen(
                [
                    bin_name,
                    "--kiosk",
                    "--noerrdialogs",
                    "--disable-infobars",
                    "--check-for-update-interval=31536000",
                    url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"kiosk opened with {bin_name}: {url}")
            return
        except FileNotFoundError:
            continue
    print("Chromium not found; skipping display")


def topic(device_id: str, name: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/{name}"


class Agent:
    def __init__(self, session: dict) -> None:
        self.session = session
        self.device_id = session["kiosk_id"]
        self.mqtt_ok = False
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{self.device_id}_pi",
            clean_session=True,
        )
        if MQTT_USER:
            self.client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
        if MQTT_TLS:
            self.client.tls_set()
            if MQTT_TLS_INSECURE:
                self.client.tls_insecure_set(True)
        offline = json.dumps({"online": False, "ts": time.time(), "app": APP_VERSION})
        self.client.will_set(
            topic(self.device_id, "estado"),
            payload=offline,
            qos=1,
            retain=True,
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        rc = reason_code if isinstance(reason_code, int) else getattr(reason_code, "value", reason_code)
        print(f"mqtt connected rc={rc}")
        self.mqtt_ok = rc == 0
        if rc != 0:
            return
        client.subscribe(topic(self.device_id, "comandos"), qos=1)
        online = json.dumps({"online": True, "ts": time.time(), "app": APP_VERSION})
        client.publish(topic(self.device_id, "estado"), online, qos=1, retain=True)

    def _mqtt_publish(self, name: str, payload: str, *, qos: int = 0, retain: bool = False) -> None:
        if not self.mqtt_ok:
            return
        try:
            self.client.publish(topic(self.device_id, name), payload, qos=qos, retain=retain)
        except Exception as exc:  # noqa: BLE001
            print(f"mqtt publish error: {exc}")
            self.mqtt_ok = False

    def _on_message(self, client, userdata, msg):
        raw = msg.payload.decode("utf-8", errors="replace")
        print(f"mqtt cmd {msg.topic}: {raw}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"accion": raw.strip()}
        accion = (payload.get("accion") or payload.get("command") or "").lower()
        self._handle_command(accion)

    def _handle_command(self, accion: str) -> None:
        if accion == "reload":
            url = self.session.get("display_url")
            if url:
                open_kiosk(url)
        elif accion == "reboot":
            subprocess.run(["sudo", "reboot"], check=False)
        elif accion == "unpair":
            clear_session()
            offline = json.dumps({"online": False, "ts": time.time(), "reason": "unpair"})
            self._mqtt_publish("estado", offline, qos=1, retain=True)
            raise SystemExit("unpair")
        else:
            print(f"unknown command: {accion}")

    def _process_jobs(self) -> None:
        for _ in range(3):
            job = poll_job(self.session)
            if not job:
                return
            kind = (job.get("kind") or "shell").lower()
            print(f"job {job['id']} kind={kind}: {job.get('command')}")
            timeout = int(job.get("timeout_sec") or 30)
            if kind == "gpio_set":
                try:
                    payload = json.loads(job["command"])
                except json.JSONDecodeError:
                    report_job(self.session, job["id"], exit_code=1, stdout="", stderr="invalid gpio payload", status="error")
                    continue
                code, out, err, status = gpio_set(
                    str(payload.get("gpio_id") or ""),
                    state=payload.get("state"),
                    value=payload.get("value"),
                )
                report_job(self.session, job["id"], exit_code=code, stdout=out, stderr=err, status=status)
                # refresh snapshot after set
                try:
                    report_gpio(self.session, fetch_gpio_snapshot())
                except Exception as exc:  # noqa: BLE001
                    print(f"gpio refresh error: {exc}")
            else:
                code, out, err, status = run_shell(job["command"], timeout)
                report_job(
                    self.session,
                    job["id"],
                    exit_code=code,
                    stdout=out,
                    stderr=err,
                    status=status,
                )

    def run(self) -> None:
        open_kiosk(self.session.get("display_url") or "")
        # MQTT es opcional: si 1883/credenciales fallan, el agente sigue por HTTP (pair/heartbeat/jobs).
        try:
            print(f"mqtt connecting {MQTT_HOST}:{MQTT_PORT} tls={MQTT_TLS} user={MQTT_USER or '-'}")
            self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            self.client.loop_start()
        except Exception as exc:  # noqa: BLE001
            print(f"mqtt unavailable ({exc}); continuing with HTTP only")
            self.mqtt_ok = False
        last_http = 0.0
        last_gpio = 0.0
        print(f"agent {APP_VERSION} running device={self.device_id} api={API}")
        try:
            while True:
                now = time.time()
                try:
                    self._process_jobs()
                except SystemExit:
                    raise
                except Exception as exc:  # noqa: BLE001
                    print(f"job poll error: {exc}")

                if now - last_gpio >= 1.0:
                    try:
                        snap = fetch_gpio_snapshot()
                        report_gpio(self.session, snap)
                        tele_gpio = {
                            "ts": now,
                            "app": APP_VERSION,
                            "gpio_available": snap.get("available"),
                            "gpio_count": len(snap.get("devices") or []),
                        }
                        self._mqtt_publish("telemetria", json.dumps(tele_gpio))
                    except SystemExit:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        print(f"gpio sync error: {exc}")
                    last_gpio = now

                tele = {
                    "ts": now,
                    "app": APP_VERSION,
                    "display_url": self.session.get("display_url"),
                }
                self._mqtt_publish("telemetria", json.dumps(tele))
                if now - last_http >= 60:
                    try:
                        result = http_heartbeat(self.session)
                        cmd = (result.get("command") or "").lower()
                        if cmd:
                            self._handle_command(cmd)
                        new_url = result.get("display_url")
                        if new_url and new_url != self.session.get("display_url"):
                            self.session["display_url"] = new_url
                            save_session(self.session)
                            open_kiosk(new_url)
                    except SystemExit:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        print(f"http heartbeat error: {exc}")
                    last_http = now
                time.sleep(3)
        finally:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass


def main() -> None:
    session = load_session()
    if not session:
        code = read_pair_code()
        if not code or len(code) != 6:
            raise SystemExit(
                f"Sin sesión. Escribe el código en {PAIR_CODE_PATH} o PAIR_CODE=......"
            )
        print(f"pairing with code {code}…")
        session = pair(code)
        print(f"paired as {session.get('kiosk_id')} → {session.get('display_url')}")

    Agent(session).run()


if __name__ == "__main__":
    main()
