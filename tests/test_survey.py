"""Field size from the printed tag size: no tape measure needed."""
import time

import pytest
from fastapi.testclient import TestClient

from tango_mw.app import App
from tango_mw.config import AppConfig
from tango_mw.web.server import create_web_app


@pytest.fixture(params=["top", "oblique"])
def app(request, tmp_path):
    cfg = AppConfig()   # simulated field: boundary tags at (0,0), (1.6,0), (1.6,1.0), (0,1.0), 100 mm
    cfg.ros.bus = "loopback"
    cfg.plugins.dirs = [str(tmp_path / "c")]
    cfg.camera.synthetic.view = request.param
    cfg.camera.synthetic.noise_px = 2.0
    a = App(cfg, tmp_path / "config.yaml", base_dir=tmp_path)
    a.start()
    deadline = time.time() + 5
    while time.time() < deadline and not a.snapshot.valid:
        time.sleep(0.05)
    yield a
    a.stop()


def test_propose_world_without_measuring(app):
    client = TestClient(create_web_app(app))
    r = client.post("/api/setup/propose_world", json={"duration": 0.6})
    assert r.status_code == 200, r.text
    est = r.json()["estimated"]
    assert abs(est["width"] - 1.6) < 0.05 and abs(est["height"] - 1.0) < 0.04, est
    tags = {t["id"]: (t["x"], t["y"]) for t in r.json()["tags"]}
    assert sorted(tags) == [0, 1, 2, 3]
    xs = sorted({round(x, 3) for x, _ in tags.values()})
    assert xs[0] == 0 and abs(xs[-1] - est["width"]) < 1e-6
    # a measured size still wins
    r = client.post("/api/setup/propose_world", json={"duration": 0.3, "width": 1.5, "height": 1.5})
    assert r.json()["estimated"] is None and max(x for x, _ in [(t["x"], t["y"]) for t in r.json()["tags"]]) == 1.5
