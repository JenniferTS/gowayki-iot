"""El hash $7$ de Python debe coincidir byte a byte con mosquitto_passwd."""
import base64
import shutil
import subprocess

import pytest

from app.services.mqtt_users import hash_password, render_acl, validate_username, verify_password

BIN = shutil.which("mosquitto_passwd")


@pytest.mark.skipif(not BIN, reason="mosquitto_passwd no instalado")
@pytest.mark.parametrize("pw", ["abc", "Ünïcødé pässwörd", "x" * 60, "p$7$:#"])
def test_matches_mosquitto_passwd(tmp_path, pw):
    f = tmp_path / "passwd"
    subprocess.run([BIN, "-c", "-b", str(f), "u", pw], check=True, capture_output=True)
    ref = f.read_text().strip().split(":", 1)[1]
    _, kind, iters, salt, _h = ref.split("$")
    assert kind == "7"
    assert hash_password(pw, salt=base64.b64decode(salt), iterations=int(iters)) == ref
    assert verify_password(pw, ref) and not verify_password(pw + "!", ref)


def test_python_hash_format():
    h = hash_password("secret")
    parts = h.split("$")
    assert parts[1] == "7" and parts[2] == "101"
    assert len(base64.b64decode(parts[3])) == 12 and len(base64.b64decode(parts[4])) == 64


@pytest.mark.parametrize("bad", ["ab", "Ana/x", "a+b", "#", "gowayki-iot", "healthcheck", "x" * 40])
def test_invalid_usernames(bad):
    with pytest.raises(ValueError):
        validate_username(bad)


def test_acl():
    acl = render_acl([{"username": "ana"}], "gowayki")
    assert "user ana\ntopic readwrite gowayki/ana/#" in acl
