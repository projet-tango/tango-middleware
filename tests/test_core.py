import json
import math
import time
from pathlib import Path

import pytest

from tango_mw.bus.base import parse_request_payload, topic_map
from tango_mw.config import AppConfig, CalibrationData, LogicalFrameConfig, RobotConfig, load_config, save_config
from tango_mw.control.base import BodyCommand, Request, RobotState
from tango_mw.control.builtin import HeadingPID, Holonomic
from tango_mw.control.calibration import CalibrationJob, apply_result
from tango_mw.control.registry import ControllerRegistry
from tango_mw.control.robot_model import RobotModel
from tango_mw.frames import LogicalFrame, wrap_angle
from tango_mw.vision.detector import TagDetector
from tango_mw.vision.synthetic import SimRobot, SyntheticSource, _dead
from tango_mw.vision.tracker import RobotObservation, Tracker


# ---------------------------------------------------------------- frames
def test_logical_frame_gama_convention():
    cfg = AppConfig()
    L = LogicalFrame(LogicalFrameConfig(width=100, height=50, origin="top_left"), cfg.world)  # world 1.6 x 1.0
    assert L.point_to_logical(0.0, 1.0) == pytest.approx((0.0, 0.0))  # top-left
    assert L.point_to_logical(1.6, 0.0) == pytest.approx((100.0, 50.0))
    # physical north (+y) is "up" on screen = GAMA heading -90 deg
    assert L.heading_to_logical(math.pi / 2) == pytest.approx(-90.0)
    assert L.heading_to_logical(0.0) == pytest.approx(0.0)
    # GAMA heading 90 (south on screen) at 10 units/s
    s, h = L.speed_heading_to_physical(10.0, 90.0)
    assert h == pytest.approx(-math.pi / 2)
    assert s == pytest.approx(10.0 / 50.0)  # 50 units per metre vertically
    x, y = L.point_to_physical(*L.point_to_logical(0.3, 0.7))
    assert (x, y) == pytest.approx((0.3, 0.7))


def test_wrap_angle():
    assert wrap_angle(3 * math.pi) == pytest.approx(-math.pi)
    assert wrap_angle(-0.1) == pytest.approx(-0.1)


# ---------------------------------------------------------------- vision
def test_synthetic_tracking_accuracy():
    cfg = AppConfig()
    cfg.camera.synthetic.perspective = 0.1
    src = SyntheticSource(cfg)
    det = TagDetector(cfg.detector)
    tr = Tracker(cfg.world, cfg.camera, cfg.robots)
    ok, frame, t = src.read()
    snap = tr.process(det.detect(frame), t)
    assert snap.valid and snap.reprojection_error < 1e-3
    assert sorted(snap.world_tags_visible) == [0, 1, 2, 3]
    for name, sim in src.robots.items():
        o = snap.robots[name]
        assert o.visible
        assert math.hypot(o.x - sim.x, o.y - sim.y) < 0.005
        assert abs(wrap_angle(o.tag_theta - (sim.theta + sim.tag_mount))) < 0.06


def test_homography_needs_min_tags():
    cfg = AppConfig()
    src = SyntheticSource(cfg)
    det = TagDetector(cfg.detector)
    tr = Tracker(cfg.world, cfg.camera, cfg.robots)
    _, frame, t = src.read()
    dets = [d for d in det.detect(frame) if d.id != 2]  # hide one corner
    snap = tr.process(dets, t)
    assert not snap.valid
    assert not snap.robots["robot1"].visible


# ---------------------------------------------------------------- control
def test_heading_pid_turns_towards_target():
    c = HeadingPID("r", {"kp": 2.0, "kd": 0.0})
    cmd = c.compute(RobotState("r", 0, 0, 0.0), Request(speed=0.2, heading=0.5), 0.05)
    assert cmd.angular == pytest.approx(1.0)
    assert 0 < cmd.linear < 0.2
    cmd = c.compute(RobotState("r", 0, 0, 0.0), Request(speed=0.2, heading=3.0), 0.05)
    assert cmd.linear == 0.0  # turn in place
    c = HeadingPID("r", {"allow_reverse": True, "kd": 0.0})
    cmd = c.compute(RobotState("r", 0, 0, 0.0), Request(speed=0.2, heading=math.pi), 0.05)
    assert cmd.linear < 0 and abs(cmd.angular) < 1e-9


def test_holonomic_projection():
    c = Holonomic("r", {})
    cmd = c.compute(RobotState("r", 0, 0, math.pi / 2), Request(speed=1.0, heading=0.0), 0.05)
    assert cmd.linear == pytest.approx(0.0, abs=1e-9)
    assert cmd.lateral == pytest.approx(-1.0)  # world +x is the robot's right


def test_param_coercion_and_clamp():
    c = HeadingPID("r", {"kp": "100", "speed_scaling": "none", "allow_reverse": "true"})
    assert c.p["kp"] == 50.0 and c.p["allow_reverse"] is True
    with pytest.raises(ValueError):
        HeadingPID("r", {"speed_scaling": "bogus"})


def test_robot_model_twist_and_wheels():
    cal = CalibrationData(linear_gain=2.0, linear_deadband=0.1, angular_gain=0.5, drift_per_meter=0.0, max_raw=10)
    raw = RobotModel(cal, "twist").to_raw(BodyCommand(linear=0.2, angular=-1.0))
    assert raw["linear"] == pytest.approx(0.5)
    assert raw["angular"] == pytest.approx(-0.5)
    assert RobotModel(cal, "twist").to_raw(BodyCommand())["linear"] == 0.0
    cal = CalibrationData(wheel_gain=1.0, wheel_base=0.2, max_raw=10)
    raw = RobotModel(cal, "wheels").to_raw(BodyCommand(linear=0.1, angular=1.0))
    assert raw["wheels"] == pytest.approx([0.0, 0.2])


def test_registry_plugins_hot_reload(tmp_path: Path):
    reg = ControllerRegistry(["plugins"], base_dir=tmp_path)
    p = reg.create("my_ctrl")
    assert "my_ctrl" in reg.classes and p.exists()
    gen = reg.generation
    code = p.read_text().replace('"k": Param(2.0', '"k": Param(7.0')
    reg.write_source("my_ctrl.py", code)
    assert reg.get("my_ctrl").params["k"].default == 7.0 and reg.generation > gen
    # a broken file keeps the previous version and reports the error
    p.write_text("raise RuntimeError('boom')")
    time.sleep(0.01)
    reg.reload(force=True)
    assert "my_ctrl" in reg.classes and str(p) in reg.errors
    with pytest.raises(SyntaxError):
        reg.write_source("my_ctrl.py", "def (")
    p.unlink()
    reg.reload()
    assert "my_ctrl" not in reg.classes
    with pytest.raises(ValueError):
        reg.write_source("../evil.py", "x=1")


# ---------------------------------------------------------------- calibration
def _run_calibration(sim: SimRobot, output: str, procedure: str = "full") -> dict:
    """Drive a CalibrationJob against a kinematic robot with simulated time."""
    robot = RobotConfig(name="r", tag_id=1)
    robot.output.type = output
    job = CalibrationJob(robot, procedure)
    t, dt = 0.0, 0.02
    while job.status == "running" and t < 300:
        obs = RobotObservation("r", 1, visible=True, tag_x=sim.x, tag_y=sim.y,
                               tag_theta=wrap_angle(sim.theta + sim.tag_mount), last_seen=t, ever_seen=True)
        raw = job.step(t, obs)
        if output == "wheels":
            l, r = raw.get("wheels", [0, 0])
            vl, vr = _dead(l, sim.lin_dead) * sim.lin_gain, _dead(r, sim.lin_dead) * sim.lin_gain * sim.right_scale
            v, w = (vl + vr) / 2, (vr - vl) / sim.wheel_base
        else:
            v = _dead(raw.get("linear", 0.0), sim.lin_dead) * sim.lin_gain
            w = _dead(raw.get("angular", 0.0), sim.ang_dead) * sim.ang_gain
        w += sim.drift * v
        sim.theta = wrap_angle(sim.theta + w * dt)
        sim.x += v * math.cos(sim.theta) * dt
        sim.y += v * math.sin(sim.theta) * dt
        t += dt
    assert job.status == "done", job.describe()
    return job.result


def test_calibration_twist_recovers_truth():
    sim = SimRobot("r", 1, 0.0, 0.0, 0.3, lin_gain=1.3, lin_dead=0.05, ang_gain=0.7, ang_dead=0.1, drift=0.2, tag_mount=0.25)
    res = _run_calibration(sim, "twist")
    assert res["heading_offset"] == pytest.approx(-0.25, abs=0.02)
    assert res["linear_gain"] == pytest.approx(1 / 1.3, rel=0.05)
    assert res["linear_deadband"] == pytest.approx(0.05, abs=0.01)
    assert res["angular_gain"] == pytest.approx(1 / 0.7, rel=0.05)
    assert res["angular_deadband"] == pytest.approx(0.1, abs=0.02)
    assert res["drift_per_meter"] == pytest.approx(0.2, abs=0.03)
    cal = apply_result(CalibrationData(), res)
    assert cal.linear_gain == pytest.approx(res["linear_gain"])


def test_calibration_wheels_estimates_wheel_base():
    sim = SimRobot("r", 1, 0.0, 0.0, 0.0, lin_gain=0.8, lin_dead=0.02, wheel_base=0.15, output="wheels")
    res = _run_calibration(sim, "wheels")
    assert res["wheel_gain"] == pytest.approx(1 / 0.8, rel=0.05)
    assert res["wheel_base"] == pytest.approx(0.15, rel=0.1)


# ---------------------------------------------------------------- bus / config
def test_parse_request_payload_forms():
    assert parse_request_payload('{"robot": "a", "speed": 1, "heading": 2}') == [("a", {"robot": "a", "speed": 1, "heading": 2})]
    assert [r for r, _ in parse_request_payload('{"requests": [{"robot": "a"}, {"robot": "b"}]}')] == ["a", "b"]
    assert [r for r, _ in parse_request_payload('{"a": {"speed": 1}, "b": {"speed": 2}}')] == ["a", "b"]


def test_config_roundtrip_and_validation(tmp_path: Path):
    cfg = AppConfig()
    p = tmp_path / "c.yaml"
    save_config(cfg, p)
    assert load_config(p) == cfg
    data = cfg.model_dump()
    data["robots"].append(dict(data["robots"][0]))
    with pytest.raises(Exception):
        AppConfig.model_validate(data)
    assert any(r["topic"] == "/robot1/cmd_vel" for r in topic_map(cfg))


# ---------------------------------------------------------------- deployment
from tango_mw.deploy import discover_robots, propose_world_corners


def _tags(points):
    return [{"id": i, "u": u, "v": v, "role": "unknown"} for i, (u, v) in points.items()]


def test_propose_world_corners_orientation():
    # image: tag 5 top-left, 6 top-right, 7 bottom-right, 8 bottom-left, 9 in the middle
    tags = _tags({5: (10, 10), 6: (600, 20), 7: (610, 400), 8: (5, 390), 9: (300, 200)})
    res = {t["id"]: (t["x"], t["y"]) for t in propose_world_corners(tags, 2.0, 1.0)}
    assert res == {5: (0.0, 1.0), 6: (2.0, 1.0), 7: (2.0, 0.0), 8: (0.0, 0.0)}
    res = {t["id"]: (t["x"], t["y"]) for t in propose_world_corners(tags, 2.0, 1.0, rotation=180)}
    assert res[5] == (2.0, 0.0) and res[7] == (0.0, 1.0)
    res = {t["id"]: (t["x"], t["y"]) for t in propose_world_corners(tags, 2.0, 1.0, mirror=True)}
    assert res[5] == (2.0, 1.0)
    with pytest.raises(ValueError):
        propose_world_corners(tags[:3], 1, 1)


def test_discover_robots_from_topics():
    net = {"available": True, "topics": [
        {"name": "/alpha/cmd_vel", "types": ["geometry_msgs/msg/Twist"], "publishers": 0, "subscribers": 1},
        {"name": "/beta/cmd_vel", "types": ["geometry_msgs/msg/Twist"], "publishers": 0, "subscribers": 1},
        {"name": "/beta/odom", "types": ["nav_msgs/msg/Odometry"], "publishers": 1, "subscribers": 0},
    ]}
    cfg = AppConfig(robots=[RobotConfig(name="alpha", tag_id=3)])
    r = discover_robots(net, "/{name}/cmd_vel", cfg)
    assert [(x["name"], x["configured"]) for x in r["robots"]] == [("alpha", True), ("beta", False)]
    assert not discover_robots({"available": False}, "/{name}/cmd_vel", cfg)["available"]


def test_service_files(tmp_path):
    from tango_mw import service

    s = service.Spec(workdir=tmp_path, config=tmp_path / "config" / "config.yaml", python="/usr/bin/python3",
                     ros_setup="/opt/ros/jazzy/setup.bash", extra_args="--bus ros2")
    unit = service.systemd_unit(s)
    assert "Restart=always" in unit and "source /opt/ros/jazzy/setup.bash && exec /usr/bin/python3 -m tango_mw run" in unit
    assert "--bus ros2" in unit
    win = service.windows_script(s)
    assert ":loop" in win and 'call "/opt/ros/jazzy/setup.bash"' in win and "\r\n" in win
    plist = service.launchd_plist(s, tmp_path)
    assert "<key>KeepAlive</key><true/>" in plist and "&amp;&amp; exec" in plist
