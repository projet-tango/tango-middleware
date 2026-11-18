"""Phone camera source: pairing, frame ingestion, discovery, reconnection."""
import json
import socket
import time

import cv2
import pytest

websockets_sync = pytest.importorskip("websockets.sync.client")

from tango_mw.app import App
from tango_mw.config import AppConfig, save_config
from tango_mw.vision.synthetic import SyntheticSource


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def app(tmp_path):
    cfg = AppConfig()
    cfg.ros.bus = "loopback"
    cfg.plugins.dirs = [str(tmp_path / "controllers")]
    cfg.camera.source = "mobile"
    cfg.camera.mobile.host = "127.0.0.1"
    cfg.camera.mobile.port = _free_port()
    cfg.camera.mobile.https_port = None
    save_config(cfg, tmp_path / "config.yaml")  # like `tango-mw run`, which creates the file first
    a = App(cfg, tmp_path / "config.yaml", base_dir=tmp_path)
    a.start()
    deadline = time.time() + 5
    while time.time() < deadline and a.mobile is None:
        time.sleep(0.05)
    assert a.mobile is not None, a.camera_error
    yield a
    a.stop()


def _connect(app, code=None, device="phone-1"):
    ws = websockets_sync.connect(f"ws://127.0.0.1:{app.cfg.camera.mobile.port}/ws", open_timeout=5)
    ws.send(json.dumps({"type": "hello", "code": app.cfg.camera.mobile.pairing_code if code is None else code,
                        "device": device, "name": device, "platform": "test"}))
    return ws


def _frames(cfg):
    sim = SyntheticSource(cfg.model_copy(update={"camera": cfg.camera.model_copy(update={"source": "synthetic"})}))
    while True:
        ok, frame, _ = sim.read()
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        yield buf.tobytes()


def test_pairing_code_generated_and_saved(app):
    code = app.cfg.camera.mobile.pairing_code
    assert len(code) == 6 and code.isdigit()
    assert code in app.config_path.read_text(encoding="utf-8")


def test_bad_code_is_refused(app):
    ws = _connect(app, code="nope")
    msg = json.loads(ws.recv(timeout=5))
    assert msg == {"type": "error", "reason": "bad_code"}
    with pytest.raises(Exception):
        ws.recv(timeout=5)
    assert app.mobile.client is None


def test_frames_reach_the_tracker(app):
    ws = _connect(app)
    assert json.loads(ws.recv(timeout=5))["type"] == "welcome"
    settings = json.loads(ws.recv(timeout=5))
    assert settings["type"] == "settings" and settings["width"] == 1280
    assert "waiting" not in (app.camera_problem() or "")
    gen = _frames(app.cfg)
    deadline = time.time() + 10
    while time.time() < deadline and not app.snapshot.valid:
        ws.send(next(gen))
        time.sleep(0.05)
    assert app.snapshot.valid, "homography not computed from phone frames"
    assert app.health()["camera_ok"]
    assert app.status()["mobile"]["client"]["name"] == "phone-1"
    # configuration changes are pushed to the phone
    cfg = app.cfg.model_copy(deep=True)
    cfg.camera.mobile.torch = True
    app.apply_config(cfg)
    while True:
        msg = json.loads(ws.recv(timeout=5))
        if msg.get("type") == "settings":
            break
    assert msg["torch"] is True
    ws.close()


def test_same_phone_reconnect_replaces_and_other_phone_is_busy(app):
    gen = _frames(app.cfg)
    a = _connect(app, device="phone-a")
    json.loads(a.recv(timeout=5))
    a.send(next(gen))
    b = _connect(app, device="phone-b")
    assert json.loads(b.recv(timeout=5)) == {"type": "error", "reason": "busy"}
    a2 = _connect(app, device="phone-a")  # e.g. Wi-Fi dropped, old socket half-open
    assert json.loads(a2.recv(timeout=5))["type"] == "welcome"
    time.sleep(0.2)
    assert app.mobile.client.device == "phone-a"
    a2.close()
    deadline = time.time() + 3
    while time.time() < deadline and app.mobile.client is not None:
        time.sleep(0.05)
    assert app.mobile.client is None
    assert "waiting" in app.camera_problem()


def test_udp_discovery(app):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(3)
        s.sendto(json.dumps({"type": "tango_discover"}).encode(), ("127.0.0.1", app.cfg.camera.mobile.port))
        reply = json.loads(s.recvfrom(2048)[0])
    assert reply["type"] == "tango_here" and reply["port"] == app.cfg.camera.mobile.port
    assert reply["code_required"] is True


def test_new_code_kicks_phone_and_switching_source_stops_receiver(app):
    ws = _connect(app)
    json.loads(ws.recv(timeout=5))
    app.new_pairing_code()
    msgs = []
    try:
        while True:
            msgs.append(json.loads(ws.recv(timeout=5)))
    except Exception:
        pass
    assert {"type": "error", "reason": "bad_code"} in msgs
    cfg = app.cfg.model_copy(deep=True)
    cfg.camera.source = "synthetic"
    app.apply_config(cfg)
    assert app.mobile is None


def test_qr_endpoint(app):
    from fastapi.testclient import TestClient
    from tango_mw.web.server import create_web_app

    client = TestClient(create_web_app(app))
    r = client.get("/api/mobile")
    assert r.status_code == 200 and r.json()["source_is_mobile"]
    r = client.get("/api/mobile/qr.png", params={"data": "https://10.0.0.2:47801/?app=47800#c=123456"})
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    img = cv2.imdecode(__import__("numpy").frombuffer(r.content, "uint8"), cv2.IMREAD_GRAYSCALE)
    text, *_ = cv2.QRCodeDetector().detectAndDecode(img)
    assert text == "https://10.0.0.2:47801/?app=47800#c=123456"
