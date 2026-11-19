"""Several cameras: fusion, triangulated tag heights, per-camera video, several phones."""
import json
import math
import socket
import time

import pytest
from fastapi.testclient import TestClient

from tango_mw.app import App
from tango_mw.config import AppConfig, CameraConfig
from tango_mw.web.server import create_web_app

websockets_sync = pytest.importorskip("websockets.sync.client")


def wait(pred, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def two_cams(tmp_path):
    cfg = AppConfig()
    cfg.ros.bus = "loopback"
    cfg.plugins.dirs = [str(tmp_path / "c")]
    syn = cfg.camera.synthetic
    syn.randomize_dynamics = False
    syn.robot_tag_height = 0.12                       # tag heights are NOT configured on the robots
    syn.view, syn.camera_height, syn.camera_y = "oblique", 2.0, 0.5
    side = CameraConfig(name="side", source="synthetic")
    side.synthetic.view, side.synthetic.camera_y = "oblique", -0.8
    cfg.cameras = [side]
    a = App(cfg, tmp_path / "config.yaml", base_dir=tmp_path)
    a.start()
    assert wait(lambda: all(u.snapshot.H is not None for u in a.units.values()) and len(a.tag_heights_measured()) == 2)
    yield a
    a.stop()


def test_robots_are_triangulated_and_tag_heights_measured(two_cams):
    a = two_cams
    truth = a.sim.truth()
    for name, o in a.snapshot.robots.items():
        assert a.fusion[name].method == "triangulated" and set(a.fusion[name].cameras) == {"main", "side"}
        assert math.hypot(o.x - truth[name]["x"], o.y - truth[name]["y"]) < 0.01
    for h in a.tag_heights_measured().values():
        assert abs(h - 0.12) < 0.01
    # the measured heights make the per-camera estimates right too, and satisfy the setup check
    def per_camera_err(u):
        o = u.snapshot.robots["robot1"]
        return math.hypot(o.x - truth["robot1"]["x"], o.y - truth["robot1"]["y"])
    # (the measured height reaches the trackers with the next frames)
    for u in a.units.values():
        assert wait(lambda: per_camera_err(u) < 0.005), per_camera_err(u)
    # setup checks run on a ~2 s window of frames
    assert wait(lambda: any(c["id"] == "side.parallax" and c["status"] == "ok" for c in a.checks_result()["checks"]))
    checks = a.checks_result()["checks"]
    side_parallax = next(c for c in checks if c["id"] == "side.parallax")
    assert "measured by triangulation" in side_parallax["value"]
    assert all(c["label"].startswith(("main: ", "side: ")) for c in checks)


def test_cameras_api_and_per_camera_video(two_cams):
    client = TestClient(create_web_app(two_cams))
    cams = client.get("/api/cameras/status").json()
    assert [c["name"] for c in cams] == ["main", "side"]
    assert all(c["homography_valid"] and c["camera"] for c in cams)
    assert abs(cams[0]["camera"]["height"] - 2.0) < 0.05 and abs(cams[1]["camera"]["height"] - 1.6) < 0.05
    for cam in ("main", "side"):
        r = client.get("/api/snapshot.jpg", params={"camera": cam})
        assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    assert client.get("/api/snapshot.jpg", params={"camera": "nope"}).status_code == 404
    st = client.get("/api/status").json()
    assert st["fusion"]["robot1"]["method"] == "triangulated" and len(st["cameras"]) == 2


def test_cameras_can_be_added_and_removed_live(two_cams):
    a = two_cams
    cfg = a.cfg.model_copy(deep=True)
    cfg.cameras = []
    a.apply_config(cfg)
    assert list(a.units) == ["main"]
    cfg = a.cfg.model_copy(deep=True)
    top = CameraConfig(name="top", source="synthetic")
    cfg.cameras = [top]
    a.apply_config(cfg)
    assert list(a.units) == ["main", "top"]
    assert wait(lambda: a.units["top"].snapshot.H is not None)


# ------------------------------------------------------------------ several phones
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def phones(tmp_path):
    cfg = AppConfig()
    cfg.ros.bus = "loopback"
    cfg.plugins.dirs = [str(tmp_path / "c")]
    cfg.camera.source = "mobile"
    m = cfg.camera.mobile
    m.host, m.port, m.https_port = "127.0.0.1", _free_port(), None
    cfg.cameras = [CameraConfig(name="side", source="mobile")]
    a = App(cfg, tmp_path / "config.yaml", base_dir=tmp_path)
    a.start()
    assert wait(lambda: a.mobile is not None)
    yield a
    a.stop()


def _hello(a, device):
    ws = websockets_sync.connect(f"ws://127.0.0.1:{a.cfg.camera.mobile.port}/ws", open_timeout=5)
    ws.send(json.dumps({"type": "hello", "code": a.cfg.camera.mobile.pairing_code, "device": device, "name": device}))
    return ws, json.loads(ws.recv(timeout=5))


def test_one_phone_per_camera(phones, tmp_path):
    a = phones
    wa, ra = _hello(a, "phone-a")
    wb, rb = _hello(a, "phone-b")
    assert (ra["type"], ra["camera"]) == ("welcome", "main") and (rb["type"], rb["camera"]) == ("welcome", "side")
    wc, rc = _hello(a, "phone-c")
    assert rc == {"type": "error", "reason": "busy"}
    # phone-a drops and comes back: it gets its camera again
    wa.close()
    assert wait(lambda: a.mobile.client_of("main") is None)
    wa2, ra2 = _hello(a, "phone-a")
    assert ra2["camera"] == "main"
    assert json.loads((tmp_path / "phones.json").read_text()) == {"main": "phone-a", "side": "phone-b"}
    # forgetting the side camera's phone frees it for another phone
    a.mobile.forget("side")
    assert wait(lambda: a.mobile.client_of("side") is None)
    _, rc2 = _hello(a, "phone-c")
    assert rc2["camera"] == "side"
    desc = a.mobile.describe()
    assert [c["camera"] for c in desc["cameras"]] == ["main", "side"]
