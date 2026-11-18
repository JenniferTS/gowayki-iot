from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

import paho.mqtt.client as mqtt

from app.config import Settings


PublishFn = Callable[[str, str], None]


class MqttBridge:
    """Background MQTT client: subscribe estado/telemetria, publish comandos."""

    def __init__(self, settings: Settings, on_message: Callable[[str, str, dict[str, Any]], None]) -> None:
        self.settings = settings
        self.on_message = on_message
        self._client: mqtt.Client | None = None
        self._thread: threading.Thread | None = None
        self.connected = False
        self.last_error: str | None = None
        # Demo usuarios MQTT: último valor por usuario {user: {"potenciometro": .., "led": .., "ts": ..}}
        self.demo_values: dict[str, dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.mqtt_host)

    def start(self) -> None:
        if not self.enabled:
            print("[mqtt] disabled (MQTT_HOST empty)")
            return
        self._thread = threading.Thread(target=self._run, name="mqtt-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        self.connected = False

    def publish_command(self, device_id: str, accion: str) -> bool:
        if not self._client or not self.connected:
            return False
        topic = f"{self.settings.mqtt_topic_prefix}/{device_id}/comandos"
        payload = json.dumps({"accion": accion, "ts": time.time()})
        info = self._client.publish(topic, payload, qos=1)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def publish_gpio_set(self, gpio_id: str, *, state: str | None = None, value: int | None = None) -> bool:
        """Comando directo al gpio-monitor: paradero/<id>/set."""
        if not self._client or not self.connected:
            return False
        base = (self.settings.mqtt_gpio_prefix or "paradero").strip("/")
        topic = f"{base}/{gpio_id}/set"
        if state is not None:
            payload = state
        elif value is not None:
            payload = str(value)
        else:
            return False
        info = self._client.publish(topic, payload, qos=0)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def publish_user_led(self, username: str, on: bool) -> bool:
        """Demo: publica ON/OFF en gowayki/<user>/led (retain para que el ESP32 lo reciba al conectar)."""
        if not self._client or not self.connected:
            return False
        topic = f"{self.settings.mqtt_topic_prefix}/{username}/led"
        info = self._client.publish(topic, "ON" if on else "OFF", qos=1, retain=True)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def _run(self) -> None:
        prefix = self.settings.mqtt_topic_prefix
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"iot-panel-bridge-{int(time.time())}",
        )
        if self.settings.mqtt_user:
            client.username_pw_set(self.settings.mqtt_user, self.settings.mqtt_password)
        client.on_connect = self._on_connect
        client.on_message = self._on_mqtt_message
        client.on_disconnect = self._on_disconnect
        self._client = client
        while True:
            try:
                client.connect(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=60)
                client.loop_forever(retry_first_connection=True)
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.last_error = str(exc)
                print(f"[mqtt] reconnecting after error: {exc}")
                time.sleep(5)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        rc = reason_code if isinstance(reason_code, int) else getattr(reason_code, "value", reason_code)
        if rc == 0:
            self.connected = True
            self.last_error = None
            prefix = self.settings.mqtt_topic_prefix
            client.subscribe(f"{prefix}/+/estado", qos=1)
            client.subscribe(f"{prefix}/+/telemetria", qos=0)
            client.subscribe(f"{prefix}/+/potenciometro", qos=0)
            client.subscribe(f"{prefix}/+/led", qos=0)
            gpio_prefix = (self.settings.mqtt_gpio_prefix or "").strip("/")
            if gpio_prefix and gpio_prefix != prefix:
                client.subscribe(f"{gpio_prefix}/+/estado", qos=1)
                client.subscribe(f"{gpio_prefix}/+/on", qos=0)
                client.subscribe(f"{gpio_prefix}/+/off", qos=0)
                print(f"[mqtt] connected, subscribed {prefix}/+ and {gpio_prefix}/+")
            else:
                print(f"[mqtt] connected, subscribed {prefix}/+/estado|telemetria")
        else:
            self.connected = False
            self.last_error = f"connect rc={rc}"
            print(f"[mqtt] connect failed: {self.last_error}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        print(f"[mqtt] disconnected: {reason_code}")

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            parts = msg.topic.split("/")
            # {prefix}/{id}/{kind}
            if len(parts) < 3:
                return
            prefix, entity_id, kind = parts[0], parts[1], parts[2]
            raw = msg.payload.decode("utf-8", errors="replace")
            if prefix == self.settings.mqtt_topic_prefix and kind in ("potenciometro", "led") and len(parts) == 3:
                entry = self.demo_values.setdefault(entity_id, {})
                entry[kind] = raw[:64]
                entry[f"{kind}_ts"] = time.time()
                return
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"raw": raw}
            if not isinstance(data, dict):
                data = {"value": data}
            # Marca origen del topic para el handler
            data["_mqtt_prefix"] = prefix
            data["_mqtt_topic"] = msg.topic
            if kind in ("on", "off") and "estado" not in data:
                data["estado"] = kind
            self.on_message(entity_id, kind, data)
        except Exception as exc:  # noqa: BLE001
            print(f"[mqtt] message handler error: {exc}")
