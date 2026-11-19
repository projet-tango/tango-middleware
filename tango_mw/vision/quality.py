"""Setup checks: is the image good enough for reliable AprilTag tracking?

Measured on the tags themselves (what matters for detection) rather than on the whole
image: contrast inside each tag, glare, size of one tag "module" (bit) in pixels, edge
sharpness, and how steadily each tag is detected. With a phone camera, the exposure time
and ISO reported by the phone tell whether the light is too low and how much a moving
robot blurs during one exposure.

Each check is ``{"id", "label", "status": ok|warn|bad|unknown, "value", "advice"}``.
"""
from __future__ import annotations

import collections
import logging
import math
import time
from typing import Any, Deque, Dict, List, Optional, Set

import cv2
import numpy as np

from ..config import AppConfig
from ..frames import apply_h
from .camera_pose import CameraPose, parallax_shift
from .detector import TagDetection, render_tag

log = logging.getLogger(__name__)

# modules (bits) across a tag, black border included
MODULES = {"tag16h5": 6, "tag25h9": 7, "tag36h10": 8, "tag36h11": 8}
RANK = {"ok": 0, "unknown": 0, "warn": 1, "bad": 2}


def _check(cid: str, label: str, status: str, value: str, advice: str = "") -> Dict[str, Any]:
    return {"id": cid, "label": label, "status": status, "value": value, "advice": advice if status in ("warn", "bad") else ""}


_PATTERNS: Dict[tuple, np.ndarray] = {}


def tag_pattern(family: str, tag_id: int, modules: int) -> np.ndarray:
    """Expected bits of a tag (True = white), black border included."""
    key = (family, tag_id)
    if key not in _PATTERNS:
        img = render_tag(family, tag_id, modules * 10, quiet_zone=0.0)
        _PATTERNS[key] = img.reshape(modules, 10, modules, 10)[:, 5, :, 5] > 127
    return _PATTERNS[key]


def tag_metrics(gray: np.ndarray, det: TagDetection, family: str, modules: int) -> Dict[str, float]:
    """Contrast between black and white bits, glare and edge sharpness of one detected tag.

    The tag is rectified with its corners and every bit is read at its centre, then compared
    with the expected pattern: this measures the contrast the detector really sees, and
    "glare" = black bits that read brighter than the black/white midpoint.
    """
    c = det.corners.astype(np.float32)
    side = float(min(np.linalg.norm(c[i] - c[(i + 1) % 4]) for i in range(4)))
    cell = 8
    n = modules * cell
    M = cv2.getPerspectiveTransform(c, np.float32([[0, 0], [n, 0], [n, n], [0, n]]))
    rect = cv2.warpPerspective(gray, M, (n, n), flags=cv2.INTER_LINEAR)
    bits = rect.reshape(modules, cell, modules, cell)[:, 2:6, :, 2:6].mean(axis=(1, 3))
    pattern = tag_pattern(family, det.id, modules)
    white, black = bits[pattern], bits[~pattern]
    if not white.size or not black.size:
        return {"module_px": side / modules, "contrast": 0.0, "glare": 0.0, "edge_px": 99.0}
    w_med, b_med = float(np.median(white)), float(np.median(black))
    mid = (w_med + b_med) / 2.0
    # edge width ~ contrast / steepest slope (a 3x3 Sobel answers 8x the slope of a ramp)
    h, w = gray.shape
    x0, y0 = np.maximum(np.floor(c.min(axis=0)).astype(int), 0)
    x1, y1 = np.minimum(np.ceil(c.max(axis=0)).astype(int), [w - 1, h - 1])
    patch = gray[y0:y1 + 1, x0:x1 + 1]
    edge = 99.0
    if patch.size >= 16:
        gx = cv2.Sobel(patch, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(patch, cv2.CV_32F, 0, 1, ksize=3)
        slope = float(np.percentile(np.hypot(gx, gy), 99)) / 8.0
        edge = (w_med - b_med) / slope if slope > 1e-3 else 99.0
    return {
        "module_px": side / modules,
        "contrast": w_med - b_med,
        "glare": float(np.mean(black > mid)),
        "edge_px": max(edge, 0.0),
    }


class SetupChecks:
    """Fed by the vision loop with every frame; heavy measurements twice per second."""

    def __init__(self, window: int = 60):
        self.history: Deque[Set[int]] = collections.deque(maxlen=window)
        self.levels: Deque[float] = collections.deque(maxlen=window)  # mean brightness of every frame
        self.result: Dict[str, Any] = {"overall": "unknown", "checks": [], "stamp": 0.0}
        self._next = 0.0
        self._last_overall = "unknown"
        self._last_log = 0.0
        self._prev = "unknown"

    def reset(self) -> None:
        """No more frames (camera closed, phone gone): forget the window, results become 'unknown'."""
        self.history.clear()
        self.levels.clear()
        self.result = {"overall": "unknown", "checks": [], "stamp": time.time()}

    def update(self, frame: np.ndarray, dets: List[TagDetection], snap, cfg: AppConfig, fps: float,
               phone: Optional[Dict[str, Any]] = None, camera=None, single_camera: bool = True,
               measured_heights: Optional[Dict[str, float]] = None) -> None:
        """camera: this camera's configuration (default: the main one). With several cameras a robot
        legitimately leaves one camera's view, so the 'flickering robot' check is skipped, and tag
        heights measured by triangulation count as set."""
        self._camera = camera if camera is not None else cfg.camera
        self._single = single_camera
        self._measured = measured_heights or {}
        self.history.append({d.id for d in dets})
        g = frame if frame.ndim == 2 else frame[:, :, 1]  # green ~ luminance, no conversion needed
        self.levels.append(float(g[::16, ::16].mean()))
        now = time.time()
        if now < self._next or len(self.history) < self.history.maxlen:
            return  # wait for a full window (~2 s): the frame rate is still ramping up after a (re)connection
        self._next = now + 0.5
        try:
            checks = self._measure(frame, dets, snap, cfg, fps, phone or {})
        except Exception:  # pragma: no cover - never break the vision loop for diagnostics
            log.exception("setup checks")
            return
        overall = max((c["status"] for c in checks), key=lambda s: RANK[s], default="unknown")
        if not any(c["status"] != "unknown" for c in checks):
            overall = "unknown"
        self.result = {"overall": overall, "checks": checks, "stamp": now}
        stable, self._prev = overall == self._prev, overall  # log only what lasts two evaluations
        if not stable:
            return
        if RANK[overall] > RANK[self._last_overall] or (overall == "bad" and now - self._last_log > 60):
            failing = ", ".join(f"{c['label']} ({c['value']})" for c in checks if c["status"] == overall)
            log.warning("Setup checks %s: %s", overall.upper(), failing)
            self._last_log = now
        elif overall == "ok" and self._last_overall in ("warn", "bad"):
            log.info("Setup checks back to OK")
        self._last_overall = overall

    # ------------------------------------------------------------------ measurements
    def _measure(self, frame, dets, snap, cfg: AppConfig, fps: float, phone: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        modules = MODULES.get(cfg.detector.family, 8)
        world_ids = [t.id for t in cfg.world.tags]
        robot_tag = {r.tag_id: r.name for r in cfg.robots if r.tag_id >= 0}
        by_id = {d.id: d for d in dets}
        tm = {i: tag_metrics(gray, d, cfg.detector.family, modules) for i, d in by_id.items() if i in world_ids or i in robot_tag}

        # frame rate
        cam = getattr(self, "_camera", None) or cfg.camera
        want = cam.fps or 0
        if want and fps:
            r = fps / want
            out.append(_check("fps", "Frame rate", "ok" if r >= 0.8 else "warn" if r >= 0.5 else "bad",
                              f"{fps:.0f} / {want:.0f} fps",
                              "The camera cannot keep up: lower the resolution or fps, add light (auto-exposure slows "
                              "the camera down in the dark), or check the Wi-Fi with a phone camera."))

        # whole image exposure
        small = cv2.resize(gray, (min(640, gray.shape[1]), int(gray.shape[0] * min(640, gray.shape[1]) / gray.shape[1])))
        p98 = float(np.percentile(small, 98))
        sat = float(np.mean(small >= 250))
        if p98 < 70:
            out.append(_check("exposure", "Image brightness", "bad", f"brightest 2% below {p98:.0f}/255",
                              "The image is very dark: add light or raise the exposure (camera.exposure or camera.mobile.exposure_compensation)."))
        elif sat > 0.08:
            out.append(_check("exposure", "Image brightness", "warn", f"{sat:.0%} of pixels saturated",
                              "Large over-exposed areas: lower the exposure or avoid direct light / reflections on the floor."))
        else:
            out.append(_check("exposure", "Image brightness", "ok", f"{sat:.0%} saturated"))

        # flicker: mains-powered lights pulse at 100/120 Hz; a short exposure that is not a multiple of
        # 10 ms (8.3 ms at 60 Hz) catches a different part of the pulse on every frame
        lv = np.array(self.levels)
        if len(lv) >= 8 and lv.mean() > 5:
            step = float(np.median(np.abs(np.diff(lv))))
            jitter = step / float(lv.mean())
            if step < 1.0:  # below one grey level: sensor noise in a dark image, not flicker
                jitter = 0.0
            out.append(_check("flicker", "Light flicker", "ok" if jitter < 0.03 else "warn" if jitter < 0.06 else "bad",
                              f"{jitter:.1%} frame-to-frame brightness change",
                              "The brightness pulses between frames (mains-powered lights). Use an exposure that is a multiple of "
                              "10 ms (50 Hz grid) or 8.3 ms (60 Hz): set camera.mobile.max_exposure_ms to 10 or 20, or remove it."))

        # boundary tag detection stability
        n = len(self.history)
        rates = {i: sum(1 for h in self.history if i in h) / n for i in world_ids}
        weak = {i: r for i, r in rates.items() if r < 0.95}
        worst = min(rates.values()) if rates else 0.0
        out.append(_check("world_tags", "Boundary tags detected", "ok" if not weak else "warn" if worst >= 0.5 else "bad",
                          ("none in view" if rates and max(rates.values()) == 0 else
                           ", ".join(f"#{i} {r:.0%}" for i, r in sorted(weak.items())) or f"all {len(world_ids)} in every frame"),
                          "Some boundary tags are missed: check they are in view, flat, well lit and not too small."))

        # robot tags flickering (a robot can legitimately be out of view: only partial detection is suspicious)
        flicker, seen = [], 0
        for tid, name in robot_tag.items():
            r = sum(1 for h in self.history if tid in h) / n
            seen += r > 0.05
            if 0.05 < r < 0.9:
                flicker.append(f"{name} {r:.0%}")
        if robot_tag and getattr(self, "_single", True):
            out.append(_check("robot_tags", "Robot tags detection", "warn" if flicker else "ok" if seen else "unknown",
                              ", ".join(flicker) or (f"steady ({seen}/{len(robot_tag)} in view)" if seen else "no robot in view"),
                              "A robot tag is detected intermittently: motion blur, glare, tilt or tag too small."))

        # phone sensor: exposure time / ISO tell whether the room is dark for this frame rate
        exp_ms, iso = phone.get("exposure_ms"), phone.get("iso")
        if exp_ms:
            limit = 1000.0 / max(phone.get("camera_fps") or want or 30, 1)
            if cam.mobile.max_exposure_ms and phone.get("exposure_mode") == "manual":
                limit = min(limit, cam.mobile.max_exposure_ms)
            # Phones lengthen the exposure first (up to the frame period) and only then raise the gain:
            # the room is dark when the exposure is at its limit AND the ISO climbs (more noise).
            maxed = exp_ms >= 0.85 * limit
            if not iso:
                st = "unknown"
            else:
                st = "bad" if maxed and iso >= 3200 else "warn" if maxed and iso >= 1600 else "ok"
            out.append(_check("light", "Light level (phone sensor)", st,
                              f"exposure {exp_ms:.1f} ms, ISO {iso:.0f}" if iso else f"exposure {exp_ms:.1f} ms",
                              "The phone is at its longest exposure and raises the gain: the room is too dark for this "
                              "frame rate / exposure cap (noisy image). Add light, lower camera.fps, or raise "
                              "camera.mobile.max_exposure_ms."))

        out.append(parallax_check(snap, cfg, getattr(self, "_measured", {})))

        if not tm:
            out.append(_check("tags", "Tag image quality", "unknown", "no configured tag in view"))
            return out

        # contrast (light level as the detector sees it)
        cmin_id = min(tm, key=lambda i: tm[i]["contrast"])
        cmin = tm[cmin_id]["contrast"]
        out.append(_check("contrast", "Tag contrast", "ok" if cmin >= 90 else "warn" if cmin >= 50 else "bad",
                          f"lowest {cmin:.0f}/255 between black and white bits (#{cmin_id})",
                          "Black and white are too close on some tags: add light, lower glare, or print with a darker black / matte paper."))
        cs = [m["contrast"] for m in tm.values()]
        if len(cs) >= 3 and max(cs) > 0 and min(cs) / max(cs) < 0.5:
            out.append(_check("uniformity", "Lighting uniformity", "warn", f"tag contrast {min(cs):.0f}…{max(cs):.0f}",
                              "Lighting is uneven across the area: some tags are much darker than others."))
        else:
            out.append(_check("uniformity", "Lighting uniformity", "ok", f"tag contrast {min(cs):.0f}…{max(cs):.0f}"))

        # glare on tags
        glare = {i: m["glare"] for i, m in tm.items() if m["glare"] > 0}
        out.append(_check("glare", "Glare on tags", "bad" if any(g > 0.1 for g in glare.values()) else "warn" if glare else "ok",
                          ", ".join(f"#{i}: {g:.0%} of black bits read white" for i, g in glare.items()) or "none",
                          "Reflections burn part of a tag: use matte paper, move or diffuse the light, or tilt the tag."))

        # size of one module in pixels
        mod_id = min(tm, key=lambda i: tm[i]["module_px"])
        mod = tm[mod_id]["module_px"]
        out.append(_check("tag_size", "Tag size in the image", "ok" if mod >= 3 else "warn" if mod >= 2 else "bad",
                          f"{mod:.1f} px per bit (#{mod_id})",
                          "Tags are small in the image: raise the resolution, bring the camera closer or print bigger tags "
                          "(aim for 3+ px per bit, 5+ for robots that move fast)."))

        # sharpness: edge width compared with a module
        sharp_id = max(tm, key=lambda i: tm[i]["edge_px"] / max(tm[i]["module_px"], 1e-6))
        edge = tm[sharp_id]["edge_px"]
        ratio = edge / max(tm[sharp_id]["module_px"], 1e-6)
        out.append(_check("sharpness", "Sharpness", "ok" if ratio < 0.7 else "warn" if ratio < 1.0 else "bad",
                          f"edges {edge:.1f} px wide (#{sharp_id})",
                          "Blurry tags: fix the focus (camera.mobile.focus: infinity, or the lens), or reduce motion blur with more light."))

        # motion blur: how far a robot at full speed moves during one exposure
        if exp_ms:
            v = max((r.calibration.max_linear for r in cfg.robots), default=0.5)
            ppm = _px_per_metre(snap)
            if ppm:
                blur = exp_ms / 1000.0 * v * ppm
                rmod = min((tm[t]["module_px"] for t in tm if t in robot_tag), default=mod)
                out.append(_check("motion_blur", "Motion blur at full speed", "ok" if blur < 0.5 * rmod else "warn" if blur < rmod else "bad",
                                  f"{blur:.1f} px at {v:.2f} m/s (bit = {rmod:.1f} px)",
                                  "A robot at full speed smears its tag during one exposure: add light (shorter exposure), "
                                  "or lower the robots' max_linear."))
        return out


def parallax_check(snap, cfg: AppConfig, measured: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Robot tags above the floor appear shifted, the more so the more oblique the camera."""
    px = cfg.world.parallax
    cam = getattr(snap, "camera", None) if px.auto_camera else CameraPose(px.camera_x, px.camera_y, px.camera_height, 0.0, 0.0, "manual")
    label = "Tag height correction"
    if cam is None:
        return _check("parallax", label, "unknown", "camera position not known yet (needs the boundary tags)")
    robots = [r for r in cfg.robots if r.enabled]
    measured = measured or {}
    missing = [r.name for r in robots if r.calibration.tag_height <= 0 and r.name not in measured]
    corners = [(t.x, t.y) for t in cfg.world.tags] or [(cam.x, cam.y)]
    shift10 = max(parallax_shift(cam, x, y, 0.10) for x, y in corners)  # worst case: farthest corner
    where = f"camera at ({cam.x:.2f}, {cam.y:.2f}) m, {cam.height:.2f} m high, tilted {math.degrees(cam.tilt):.0f}°"
    if not px.enabled:
        if any(r.calibration.tag_height > 0 for r in robots):
            return _check("parallax", label, "warn", "correction disabled (world.parallax.enabled)",
                          "Robot tag heights are set but the correction is off: enable world.parallax.")
        return _check("parallax", label, "warn" if shift10 > 0.02 else "ok", f"correction disabled; {where}",
                      "Enable world.parallax and set each robot's tag height.")
    if missing and shift10 > 0.02:
        return _check("parallax", label, "bad" if shift10 > 0.08 else "warn",
                      f"tag height not set for {', '.join(missing)}; a tag 10 cm high is seen up to {shift10 * 100:.0f} cm off",
                      "Robots tab: measure the height of each robot's tag above the floor and type it in 'Tag height'. "
                      "With this camera position, positions are wrong by that much otherwise.")
    done = len(robots) - len(missing)
    auto = [n for n in measured if any(r.name == n and r.calibration.tag_height <= 0 for r in robots)]
    extra = f" ({len(auto)} measured by triangulation)" if auto else ""
    return _check("parallax", label, "ok", f"{done}/{len(robots)} robots corrected{extra}; {where}")


def _px_per_metre(snap) -> Optional[float]:
    """Image scale at the centre of the world, from the homography."""
    H = getattr(snap, "H", None)
    if H is None or not snap.bounds:
        return None
    b = np.array(snap.bounds, dtype=np.float64)
    c = b.mean(axis=0)
    Hi = np.linalg.inv(H)
    p = apply_h(Hi, np.array([c, c + [0.01, 0.0], c + [0.0, 0.01]]))
    d = (np.linalg.norm(p[1] - p[0]) + np.linalg.norm(p[2] - p[0])) / 2.0
    return float(d * 100.0) if math.isfinite(d) else None
