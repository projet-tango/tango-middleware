"""Setup checks on degraded images, and camera-move detection with a locked homography."""
import cv2
import numpy as np
import pytest

from tango_mw.config import AppConfig
from tango_mw.vision.detector import TagDetector
from tango_mw.vision.quality import SetupChecks
from tango_mw.vision.synthetic import SyntheticSource
from tango_mw.vision.tracker import Tracker


@pytest.fixture(scope="module")
def scene():
    cfg = AppConfig()
    frame = SyntheticSource(cfg).read()[1]
    return cfg, frame


def run_checks(cfg, frame, n=8):
    det = TagDetector(cfg.detector)
    tracker = Tracker(cfg.world, cfg.camera, cfg.robots)
    sc = SetupChecks(window=n)
    for i in range(n):
        dets = det.detect(frame)
        snap = tracker.process(dets, float(i) / 30, (frame.shape[1], frame.shape[0]), 30.0)
        sc._next = 0.0
        sc.update(frame, dets, snap, cfg, 30.0, {"exposure_ms": 33.0, "iso": 2000, "camera_fps": 30})
    return {c["id"]: c for c in sc.result["checks"]}, sc.result["overall"]


def test_clean_image_is_ok_except_phone_light(scene):
    cfg, frame = scene
    checks, _ = run_checks(cfg, frame)
    for cid in ("world_tags", "contrast", "glare", "tag_size", "sharpness", "exposure"):
        assert checks[cid]["status"] == "ok", checks[cid]
    # 33 ms at 30 fps and ISO 2000: the phone is running out of light
    assert checks["light"]["status"] == "warn" and checks["light"]["advice"]
    assert checks["motion_blur"]["status"] in ("ok", "warn", "bad")


def test_dim_image_lowers_contrast(scene):
    cfg, frame = scene
    dim = (frame.astype(np.float32) * 0.15 + 10).astype(np.uint8)
    checks, overall = run_checks(cfg, dim)
    assert checks["contrast"]["status"] == "bad"
    assert overall == "bad"


def test_glare_on_a_tag_is_reported(scene):
    cfg, frame = scene
    det = TagDetector(cfg.detector).detect(frame)
    t = next(d for d in det if d.id == cfg.world.tags[0].id)
    img = frame.copy()
    c = t.corners.mean(axis=0).astype(int)
    r = int(np.linalg.norm(t.corners[0] - t.corners[2]) * 0.2)
    cv2.circle(img, tuple(int(v) for v in c), r, (255, 255, 255), -1)
    checks, _ = run_checks(cfg, img)
    if checks["world_tags"]["status"] == "ok":  # still detected: the burned bits must show up
        assert checks["glare"]["status"] in ("warn", "bad"), checks["glare"]
    else:  # glare killed the detection altogether
        assert checks["world_tags"]["status"] in ("warn", "bad")


def test_blur_is_reported(scene):
    cfg, frame = scene
    blurry = cv2.GaussianBlur(frame, (0, 0), 2.0)
    checks, _ = run_checks(cfg, blurry)
    assert checks.get("sharpness", {}).get("status") in ("warn", "bad") or checks["world_tags"]["status"] != "ok"


def test_locked_homography_follows_a_camera_move(scene):
    cfg, frame = scene
    cfg = cfg.model_copy(deep=True)
    cfg.world.homography_mode = "locked"
    det = TagDetector(cfg.detector)
    tracker = Tracker(cfg.world, cfg.camera, cfg.robots)
    snap = tracker.process(det.detect(frame), 0.0, (frame.shape[1], frame.shape[0]))
    assert snap.homography_locked
    H0 = snap.H.copy()
    # the camera is bumped: the whole image shifts by ~23 px (6 cm on the floor)
    moved = cv2.warpAffine(frame, np.float32([[1, 0, 20], [0, 1, 12]]), (frame.shape[1], frame.shape[0]),
                           borderValue=(255, 255, 255))
    for i in range(1, 8):
        snap = tracker.process(det.detect(moved), i / 30, (frame.shape[1], frame.shape[0]))
    assert snap.camera_moved_at > 0
    assert not np.allclose(snap.H, H0)
    assert snap.homography_locked  # recomputed then locked again
    assert snap.reprojection_error < cfg.world.max_reprojection_error


def test_flicker_is_reported(scene):
    cfg, frame = scene
    det = TagDetector(cfg.detector)
    tracker = Tracker(cfg.world, cfg.camera, cfg.robots)
    sc = SetupChecks(window=20)
    for i in range(20):
        f = (frame.astype(np.float32) * (0.85 if i % 2 else 1.0)).astype(np.uint8)  # 15 % pulse
        dets = det.detect(f)
        snap = tracker.process(dets, i / 30, (f.shape[1], f.shape[0]), 30.0)
        sc._next = 0.0
        sc.update(f, dets, snap, cfg, 30.0)
    checks = {c["id"]: c for c in sc.result["checks"]}
    assert checks["flicker"]["status"] == "bad"


def test_light_check_uses_the_exposure_cap(scene):
    cfg, frame = scene
    cfg = cfg.model_copy(deep=True)
    cfg.camera.mobile.max_exposure_ms = 8.0
    det = TagDetector(cfg.detector)
    tracker = Tracker(cfg.world, cfg.camera, cfg.robots)
    sc = SetupChecks(window=8)
    for i in range(8):
        dets = det.detect(frame)
        snap = tracker.process(dets, i / 30, (frame.shape[1], frame.shape[0]), 30.0)
        sc._next = 0.0
        sc.update(frame, dets, snap, cfg, 30.0,
                  {"exposure_ms": 8.0, "iso": 2500, "camera_fps": 30, "exposure_mode": "manual"})
    checks = {c["id"]: c for c in sc.result["checks"]}
    assert checks["light"]["status"] == "warn"  # at the 8 ms cap with a high gain
    assert checks["flicker"]["status"] == "ok"
