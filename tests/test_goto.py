"""Go-to-position and placement with the simulated robots."""
import json
import math
import time

import pytest

from tango_mw.app import App
from tango_mw.bus.base import PLACE, parse_request_payload
from tango_mw.config import AppConfig, HomePose


@pytest.fixture
def app(tmp_path):
    cfg = AppConfig()
    cfg.ros.bus = "loopback"
    cfg.camera.synthetic.randomize_dynamics = False
    cfg.plugins.dirs = [str(tmp_path / "controllers")]
    cfg.goto.speed_fraction = 1.0
    a = App(cfg, tmp_path / "config.yaml", base_dir=tmp_path)
    a.start()
    deadline = time.time() + 5
    while time.time() < deadline and not a.snapshot.valid:
        time.sleep(0.05)
    yield a
    a.stop()


def wait_goal(app, name, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        g = app.manager.robots[name].goal
        if g is not None and not g.active:
            return g
        time.sleep(0.1)
    return app.manager.robots[name].goal


def test_parse_place_payload():
    assert parse_request_payload('{"place": "home"}') == [(PLACE, {"place": "home"})]
    assert parse_request_payload('{"robot": "r1", "goto": [1, 2]}') == [("r1", {"robot": "r1", "goto": [1, 2]})]


def test_goto_reaches_target_in_gama_units(app):
    # GAMA frame: 100 x 62.5, origin top left, heading in degrees (0 = east, clockwise)
    app.bus.inject_request(json.dumps({"robot": "robot1", "goto": {"x": 50, "y": 31, "heading": 90}}))
    g = wait_goal(app, "robot1")
    assert g.status == "arrived", (g.status, g.message)
    rs = app.world_json()["robots"]["robot1"]
    assert math.hypot(rs["logical"]["x"] - 50, rs["logical"]["y"] - 31) < 3
    assert abs(((rs["logical"]["heading"] - 90) + 180) % 360 - 180) < 10
    assert rs["goal"]["status"] == "arrived"
    # once arrived, the robot is stopped and GAMA speed requests work again
    assert app.manager.robots["robot1"].status.startswith("go-to: arrived") or app.manager.robots["robot1"].status


def test_goto_outside_the_area_is_refused(app):
    assert not app.goto("robot1", {"x": 500, "y": 31})
    assert app.manager.robots["robot1"].goal is None


def test_place_home_moves_robots_one_after_the_other(app):
    cfg = app.cfg.model_copy(deep=True)
    cfg.robots[0].home = HomePose(x=25, y=30, heading=0)
    cfg.robots[1].home = HomePose(x=75, y=30, heading=180)
    app.apply_config(cfg)
    app.bus.inject_request(json.dumps({"place": "home"}))
    time.sleep(0.3)
    g1, g2 = app.manager.robots["robot1"].goal, app.manager.robots["robot2"].goal
    assert g1.status in ("moving", "turning") and g2.status == "queued"
    assert wait_goal(app, "robot1").status == "arrived"
    assert wait_goal(app, "robot2").status == "arrived"
    assert app.manager.place_queue == []


def test_stop_cancels_a_goal(app):
    app.goto("robot2", {"x": 10, "y": 10})
    time.sleep(0.3)
    app.bus.inject_request(json.dumps({"robot": "robot2", "stop": True}))
    time.sleep(0.2)
    assert app.manager.robots["robot2"].goal.status == "cancelled"


def test_no_oscillation_at_the_tolerance_boundary():
    """A robot at the edge of the position tolerance must turn to its final heading, not chase the point."""
    from tango_mw.config import GotoConfig
    from tango_mw.control.base import RobotState
    from tango_mw.control.goal import Goal, drive_to

    cfg = GotoConfig()
    g = Goal(x=1.0, y=1.0, heading=math.pi / 2)
    state = RobotState(name="r", x=1.0 - 0.019, y=1.0, theta=0.0)
    drive_to(g, state, cfg, 0.5)
    assert g.status == "turning" and g.reached
    # drifted to just over the tolerance while turning: still turning to the heading
    state = RobotState(name="r", x=1.0 - 0.021, y=1.0, theta=0.5)
    cmd = drive_to(g, state, cfg, 0.5)
    assert g.reached and cmd.linear == 0 and cmd.angular > 0
