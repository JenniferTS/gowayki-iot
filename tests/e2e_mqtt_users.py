"""E2E: panel + mosquitto local (deploy/mosquitto/entrypoint.sh).

Uso: BASE=http://127.0.0.1:8765 ADMIN_TOKEN=... MQTT_PORT=18830 python tests/e2e_mqtt_users.py
Nunca imprime contraseñas.
"""
import os
import sys
import threading
import time

import httpx
import paho.mqtt.client as mqtt

BASE = os.environ.get("BASE", "http://127.0.0.1:8765")
TOKEN = os.environ["ADMIN_TOKEN"]
HOST, PORT = "127.0.0.1", int(os.environ.get("MQTT_PORT", "18830"))
H = {"X-Admin-Token": TOKEN}
results = []


def check(name, ok):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name)


def connect(user, pw):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"e2e-{user}-{time.time_ns()}")
    c.username_pw_set(user, pw)
    ev, rc = threading.Event(), {}
    c.on_connect = lambda cl, u, f, r, p=None: (rc.setdefault("rc", r), ev.set())
    c.connect(HOST, PORT)
    c.loop_start()
    ev.wait(5)
    ok = rc.get("rc") is not None and not rc["rc"].is_failure
    if not ok:
        c.loop_stop()
        c.disconnect()
        return None
    return c


def wait_login(user, pw, expect, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        c = connect(user, pw)
        if c:
            c.loop_stop(); c.disconnect()
        if bool(c) == expect:
            return True
        time.sleep(0.5)
    return False


def roundtrip(pub, sub, topic, payload):
    got = threading.Event()
    sub.on_message = lambda c, u, m: got.set() if m.topic == topic and m.payload.decode() == payload else None
    sub.subscribe(topic, qos=1)
    time.sleep(0.5)
    pub.publish(topic, payload, qos=1)
    return got.wait(2)


with httpx.Client(base_url=BASE, headers=H, timeout=10) as http:
    for u in ("ana", "beto"):
        http.delete(f"/api/admin/mqtt-users/{u}")
    check("401 sin token", httpx.get(f"{BASE}/api/admin/mqtt-users").status_code == 401)
    a = http.post("/api/admin/mqtt-users", json={"username": "ana"}).json()
    b = http.post("/api/admin/mqtt-users", json={"username": "beto"}).json()
    check("crear devuelve password una vez + synced", len(a["password"]) >= 20 and a["synced"])
    lst = http.get("/api/admin/mqtt-users").json()
    check("listado sin password/hash", "password" not in str(lst) and "$7$" not in str(lst))
    check("duplicado -> 409", http.post("/api/admin/mqtt-users", json={"username": "ana"}).status_code == 409)
    check("reservado -> 400", http.post("/api/admin/mqtt-users", json={"username": "gowayki-iot"}).status_code == 400)

    check("login ana OK tras crear (sin redeploy)", wait_login("ana", a["password"], True))
    check("password incorrecta rechazada", wait_login("ana", "wrong", False, 2))

    ca, cb = connect("ana", a["password"]), connect("beto", b["password"])
    check("ana pub/sub en gowayki/ana/#", roundtrip(ca, ca, "gowayki/ana/test", "hola"))
    check("ACL: ana NO lee gowayki/beto/#", not roundtrip(cb, ca, "gowayki/beto/x", "secreto"))
    check("ACL: ana NO publica en gowayki/beto/#", not roundtrip(ca, cb, "gowayki/beto/y", "intruso"))
    check("ACL: ana NO lee paradero/#", not roundtrip(cb, ca, "paradero/1/estado", "z") )

    ca.publish("gowayki/ana/potenciometro", "2048", qos=1)
    time.sleep(1.5)
    live = http.get("/api/admin/mqtt-users/live").json()
    check("card en vivo recibe potenciometro", live["values"].get("ana", {}).get("potenciometro") == "2048")
    got = threading.Event()
    ca.on_message = lambda c, u, m: got.set() if m.topic == "gowayki/ana/led" and m.payload == b"ON" else None
    ca.subscribe("gowayki/ana/led", qos=1); time.sleep(0.5)
    r = http.post("/api/admin/mqtt-users/ana/led", json={"on": True})
    check("switch LED publica ON a gowayki/ana/led", r.status_code == 200 and got.wait(3))
    ca.loop_stop(); ca.disconnect(); cb.loop_stop(); cb.disconnect()

    n = http.post("/api/admin/mqtt-users/ana/regenerate").json()
    check("regenerar: password nueva funciona", wait_login("ana", n["password"], True))
    check("regenerar: password vieja rechazada", wait_login("ana", a["password"], False))

    check("eliminar ana", http.delete("/api/admin/mqtt-users/ana").status_code == 200)
    check("login ana rechazado tras eliminar", wait_login("ana", n["password"], False))
    check("beto sigue funcionando", wait_login("beto", b["password"], True))
    http.delete("/api/admin/mqtt-users/beto")

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} OK")
sys.exit(1 if failed else 0)
