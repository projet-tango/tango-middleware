"""Camera position from the floor homography, and parallax correction with an oblique camera."""
import math

import numpy as np
import pytest

from tango_mw.config import AppConfig
from tango_mw.vision.camera_pose import estimate_camera_pose, parallax_shift
from tango_mw.vision.detector import TagDetector
from tango_mw.vision.synthetic import SyntheticSource
from tango_mw.vision.tracker import Tracker

W, H, FOV = 1280, 960, 70.0


def floor_to_image(C, target=(0.75, 0.75, 0.0), fov=FOV):
    f = (W / 2) / math.tan(math.radians(fov) / 2)
    K = np.array([[f, 0, W / 2], [0, f, H / 2], [0, 0, 1.0]])
    C, T = np.array(C, float), np.array(target, float)
    fwd = (T - C) / np.linalg.norm(T - C)
    up = np.array([0, 0, 1.0]) if abs(fwd[2]) < 0.99 else np.array([0, 1.0, 0])
    r = np.cross(fwd, up)
    r /= np.linalg.norm(r)
    R = np.vstack([r, np.cross(fwd, r), fwd])
    t = -R @ C
    return K @ np.column_stack([R[:, 0], R[:, 1], t])


@pytest.mark.parametrize("C", [(0.75, -0.8, 1.6), (0.75, -0.3, 1.2), (-0.5, -0.5, 1.7), (2.4, 0.75, 1.5), (0.2, 2.5, 2.0)])
def test_oblique_camera_position_is_recovered(C):
    cam = estimate_camera_pose(np.linalg.inv(floor_to_image(C, fov=62.0)), (W, H), fov_deg=75.0)
    assert cam.source == "estimated"          # the focal length comes from the perspective, not the guess
    assert math.dist((cam.x, cam.y, cam.height), C) < 0.01
    assert abs(cam.focal_px - (W / 2) / math.tan(math.radians(31))) < 5


def test_overhead_camera_uses_the_field_of_view():
    cam = estimate_camera_pose(np.linalg.inv(floor_to_image((0.75, 0.75, 1.8))), (W, H), fov_deg=70.0)
    assert cam.source == "fov" and math.degrees(cam.tilt) < 1
    assert abs(cam.x - 0.75) < 0.01 and abs(cam.y - 0.75) < 0.01 and abs(cam.height - 1.8) < 0.01


def test_parallax_shift():
    from tango_mw.vision.camera_pose import CameraPose

    cam = CameraPose(0.0, 0.0, 1.6, 0.9, 900, "estimated")
    assert abs(parallax_shift(cam, 2.0, 0.0, 0.1) - 2.0 * 0.1 / 1.5) < 1e-9


def run_oblique(tag_height_in_config: float):
    cfg = AppConfig()
    cfg.camera.synthetic.view = "oblique"
    cfg.camera.synthetic.randomize_dynamics = False
    cfg.camera.synthetic.robot_tag_height = 0.12
    for r in cfg.robots:
        r.calibration.tag_height = tag_height_in_config
    src = SyntheticSource(cfg)
    det, tracker = TagDetector(cfg.detector), Tracker(cfg.world, cfg.camera, cfg.robots)
    snap = None
    for i in range(3):
        ok, frame, _ = src.read()
        snap = tracker.process(det.detect(frame), i / 30, (frame.shape[1], frame.shape[0]))
    truth = src.truth()
    errs = [math.hypot(snap.robots[n].x - truth[n]["x"], snap.robots[n].y - truth[n]["y"]) for n in truth]
    return snap, src, errs


def test_oblique_view_with_parallax_correction():
    snap, src, errs = run_oblique(0.12)
    assert snap.H is not None and all(o.visible for o in snap.robots.values())
    cam = snap.camera
    assert cam is not None and math.degrees(cam.tilt) > 30
    assert math.dist((cam.x, cam.y, cam.height), src.camera_truth) < 0.05
    assert max(errs) < 0.015, errs


def test_oblique_view_without_the_tag_height_is_off_by_centimetres():
    _, _, errs = run_oblique(0.0)
    assert max(errs) > 0.08, errs


def test_old_configurations_get_the_automatic_correction(tmp_path):
    import yaml

    from tango_mw.config import load_config

    p = tmp_path / "old.yaml"
    p.write_text(yaml.safe_dump({"world": {"parallax": {"enabled": False, "camera_x": 0.0, "camera_y": 0.0, "camera_height": 2.0}}}))
    px = load_config(p).world.parallax
    assert px.enabled and px.auto_camera
    p.write_text(yaml.safe_dump({"world": {"parallax": {"enabled": True, "camera_x": 0.7, "camera_y": -1.0, "camera_height": 1.6}}}))
    px = load_config(p).world.parallax
    assert px.enabled and not px.auto_camera and px.camera_height == 1.6   # a measured position is kept
