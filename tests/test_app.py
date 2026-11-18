"""End-to-end: synthetic camera + loopback bus + web API."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from tango_mw.app import App
from tango_mw.config import AppConfig
from tango_mw.web.server import create_web_app


@pytest.fixture
def app(tmp_path):
    cfg = AppConfig()
    cfg.ros.bus = "loopback"
    cfg.plugins.dirs = [str(tmp_path / "controllers")]
    a = App(cfg, tmp_path / "config.yaml", base_dir=tmp_path)
    a.start()
    deadline = time.time() + 5
    while time.time() < deadline and not a.snapshot.valid:
        time.sleep(0.05)
    yield a
    a.stop()


def test_world_published_and_request_drives_robot(app):
    assert app.snapshot.valid
    time.sleep(0.3)  # let the 10 Hz publisher run
    w = app.bus.last["world"]
    assert w["valid"] and set(w["robots"]) == {"robot1", "robot2"}
    x0 = app.snapshot.robots["robot1"].x
    # GAMA: heading 0 = east, logical units/s
    app.bus.inject_request(json.dumps({"robot": "robot1", "speed": 15, "heading": 0}))
    for _ in range(15):
        app.bus.inject_request(json.dumps({"robot": "robot1", "speed": 15, "heading": 0}))
        time.sleep(0.1)
    assert app.manager.robots["robot1"].status == "actif"
    assert app.bus.commands["robot1"]["angular"] != 0 or app.bus.commands["robot1"]["linear"] > 0
    assert app.snapshot.robots["robot1"].x > x0 + 0.02


def test_estop_and_timeout(app):
    app.handle_request("robot2", {"speed": 10, "heading": 0})
    app.manager.set_estop(True)
    time.sleep(0.2)
    assert app.bus.commands["robot2"]["linear"] == 0.0
    app.manager.set_estop(False)
    time.sleep(app.cfg.safety.request_timeout + 0.3)
    assert app.manager.robots["robot2"].status == "requête expirée"


def test_web_api(app):
    c = TestClient(create_web_app(app))
    assert c.get("/").status_code == 200
    s = c.get("/api/status").json()
    assert s["world"]["valid"] and "presence" in s
    assert c.get("/api/snapshot.jpg").headers["content-type"] == "image/jpeg"
    assert c.get("/api/tags/3.png").status_code == 200
    cfg = c.get("/api/config").json()
    cfg["robots"][0]["controller"] = "proportional"
    assert c.put("/api/config?save=true", json=cfg).status_code == 200
    assert app.cfg.robots[0].controller == "proportional"
    assert app.config_path.exists()
    bad = dict(cfg, world={"min_tags": 2})
    assert c.put("/api/config", json=bad).status_code == 400
    r = c.post("/api/controllers/new", json={"name": "abc"})
    assert r.status_code == 200
    assert any(x["name"] == "abc" for x in c.get("/api/controllers").json()["controllers"])
    assert c.get("/api/network").json()["bus"] == "loopback"
    assert c.post("/api/robots/robot1/calibrate", json={"procedure": "heading_offset"}).json()["status"] == "running"
    assert c.delete("/api/robots/robot1/calibrate").status_code == 200


def test_scan_and_identify_unassigned_robot(tmp_path):
    from tango_mw import deploy

    cfg = AppConfig()
    cfg.ros.bus = "loopback"
    cfg.plugins.dirs = []
    cfg.camera.synthetic.extra_robots = 1          # robot3 exists physically (tag 12) but is not configured
    cfg.robots[1].tag_id = -1                       # robot2's tag is unknown to the config
    a = App(cfg, None, base_dir=tmp_path)
    a.start()
    try:
        time.sleep(0.8)
        scan = deploy.scan_tags(a, 0.4)
        roles = {t["id"]: t["role"] for t in scan["tags"]}
        assert roles[0] == "world" and roles[10] == "robot" and roles[11] == "unknown" and roles[12] == "unknown"
        proposal = deploy.propose_world_corners(scan["tags"], 1.6, 1.0)
        assert {t["id"]: (t["x"], t["y"]) for t in proposal} == {t.id: (t.x, t.y) for t in cfg.world.tags}
        job = a.manager.start_identify("robot2")
        deadline = time.time() + 10
        while job.status == "running" and time.time() < deadline:
            time.sleep(0.1)
        assert job.status == "done", job.describe()
        time.sleep(0.2)
        assert a.cfg.robot("robot2").tag_id == 11
        # swapping: give tag 10 to robot2 -> robot1 gets robot2's old tag
        a.assign_tag("robot2", 10)
        assert a.cfg.robot("robot2").tag_id == 10 and a.cfg.robot("robot1").tag_id == 11
    finally:
        a.stop()
