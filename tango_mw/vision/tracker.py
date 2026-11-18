"""World/robot tracking: homography from boundary tags and robot poses.

The homography ``H`` maps (undistorted) image pixels to the physical world
plane (metres).  It is estimated from the boundary tags whose world positions
are configured; robots are located by mapping their tag's center and top-edge
midpoint through ``H``.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from ..config import CameraConfig, RobotConfig, WorldConfig
from ..frames import apply_h, convex_hull, wrap_angle
from .detector import TagDetection


@dataclass
class RobotObservation:
    name: str
    tag_id: int
    visible: bool = False
    x: float = 0.0
    y: float = 0.0
    theta: float = 0.0  # robot heading (tag heading + calibration offset), rad, ccw
    tag_x: float = 0.0  # raw tag position (before calibration offsets)
    tag_y: float = 0.0
    tag_theta: float = 0.0  # raw tag heading (before calibration offset)
    vx: float = 0.0
    vy: float = 0.0
    omega: float = 0.0
    last_seen: float = 0.0
    image_center: Tuple[float, float] = (0.0, 0.0)
    ever_seen: bool = False

    @property
    def speed(self) -> float:
        return math.hypot(self.vx, self.vy)

    def forward_speed(self) -> float:
        """Signed speed along the robot heading."""
        return self.vx * math.cos(self.theta) + self.vy * math.sin(self.theta)

    def copy(self) -> "RobotObservation":
        return RobotObservation(**self.__dict__)


@dataclass
class WorldSnapshot:
    stamp: float = 0.0
    frame_index: int = 0
    H: Optional[np.ndarray] = None  # image -> world
    reprojection_error: Optional[float] = None
    homography_stamp: float = 0.0
    homography_locked: bool = False
    world_tags_visible: List[int] = field(default_factory=list)
    bounds: List[Tuple[float, float]] = field(default_factory=list)
    robots: Dict[str, RobotObservation] = field(default_factory=dict)
    detections: List[TagDetection] = field(default_factory=list)
    unknown_tags: List[int] = field(default_factory=list)
    fps: float = 0.0
    image_size: Tuple[int, int] = (0, 0)

    @property
    def valid(self) -> bool:
        return self.H is not None


class Tracker:
    def __init__(self, world: WorldConfig, camera: CameraConfig, robots: List[RobotConfig]):
        self._lock = threading.Lock()
        self.world = world
        self.camera = camera
        self._H: Optional[np.ndarray] = None
        self._H_err: Optional[float] = None
        self._H_stamp = 0.0
        self._locked = False
        self._robots: Dict[str, RobotObservation] = {}
        self._robot_cfg: Dict[int, RobotConfig] = {}
        self.set_robots(robots)
        self.bounds = convex_hull((t.x, t.y) for t in world.tags)

    # ------------------------------------------------------------------ config
    def set_robots(self, robots: List[RobotConfig]) -> None:
        with self._lock:
            self._robot_cfg = {r.tag_id: r for r in robots}
            new = {}
            for r in robots:
                old = self._robots.get(r.name)
                if old is not None and old.tag_id == r.tag_id:
                    new[r.name] = old
                else:
                    new[r.name] = RobotObservation(name=r.name, tag_id=r.tag_id)
            self._robots = new

    def set_world(self, world: WorldConfig, camera: CameraConfig) -> None:
        with self._lock:
            self.world = world
            self.camera = camera
            self.bounds = convex_hull((t.x, t.y) for t in world.tags)
            self._H = None
            self._locked = False

    def relock(self) -> None:
        with self._lock:
            self._locked = False

    # ------------------------------------------------------------------ helpers
    def _undistort(self, pts: np.ndarray) -> np.ndarray:
        cam = self.camera
        if not cam.camera_matrix:
            return pts
        K = np.asarray(cam.camera_matrix, dtype=np.float64)
        D = np.asarray(cam.dist_coeffs or [0, 0, 0, 0, 0], dtype=np.float64)
        out = cv2.undistortPoints(pts.reshape(-1, 1, 2).astype(np.float64), K, D, P=K)
        return out.reshape(-1, 2)

    def _world_points_for(self, det: TagDetection, x: float, y: float):
        if not self.world.use_tag_corners:
            return [det.center], [(x, y)]
        s = self.world.tag_size / 2.0
        img = [det.center] + list(det.corners)
        wld = [(x, y), (x - s, y + s), (x + s, y + s), (x + s, y - s), (x - s, y - s)]
        return img, wld

    def _estimate_h(self, dets: Dict[int, TagDetection]) -> Tuple[Optional[np.ndarray], Optional[float], List[int]]:
        src, dst, seen = [], [], []
        for t in self.world.tags:
            d = dets.get(t.id)
            if d is None:
                continue
            seen.append(t.id)
            i, w = self._world_points_for(d, t.x, t.y)
            src.extend(i)
            dst.extend(w)
        if len(seen) < self.world.min_tags or len(src) < 4:
            return None, None, seen
        src_a = self._undistort(np.asarray(src, dtype=np.float64))
        dst_a = np.asarray(dst, dtype=np.float64)
        method = cv2.RANSAC if len(src_a) > 4 else 0
        H, _ = cv2.findHomography(src_a, dst_a, method, self.world.ransac_threshold)
        if H is None or not np.all(np.isfinite(H)) or abs(H[2, 2]) < 1e-12:
            return None, None, seen
        H = H / H[2, 2]
        err = float(np.mean(np.linalg.norm(apply_h(H, src_a) - dst_a, axis=1)))
        return H, err, seen

    def _parallax(self, p: np.ndarray, tag_height: float) -> np.ndarray:
        px = self.world.parallax
        if not px.enabled or tag_height <= 0 or px.camera_height <= tag_height:
            return p
        c = np.array([px.camera_x, px.camera_y])
        return c + (p - c) * (1.0 - tag_height / px.camera_height)

    # ------------------------------------------------------------------ main
    def process(self, detections: List[TagDetection], stamp: float, image_size=(0, 0), fps: float = 0.0) -> WorldSnapshot:
        with self._lock:
            by_id: Dict[int, TagDetection] = {}
            for d in detections:
                by_id.setdefault(d.id, d)  # ignore duplicates

            # --- homography
            if not (self.world.homography_mode == "locked" and self._locked and self._H is not None):
                H, err, seen = self._estimate_h(by_id)
                if H is not None and err is not None and err <= self.world.max_reprojection_error:
                    a = self.world.smoothing
                    if self._H is not None and a > 0:
                        H = a * self._H + (1 - a) * H
                        H = H / H[2, 2]
                    self._H, self._H_err, self._H_stamp = H, err, stamp
                    if self.world.homography_mode == "locked":
                        self._locked = True
            else:
                seen = [t.id for t in self.world.tags if t.id in by_id]

            # --- robots
            H = self._H
            world_ids = {t.id for t in self.world.tags}
            for name, obs in self._robots.items():
                d = by_id.get(obs.tag_id)
                if d is None or H is None:
                    obs.visible = False
                    continue
                cfg = self._robot_cfg.get(obs.tag_id)
                cal = cfg.calibration if cfg else None
                img = self._undistort(np.array([d.center, d.top_mid]))
                wc, wt = apply_h(H, img)
                th = cal.tag_height if cal else 0.0
                wc, wt = self._parallax(wc, th), self._parallax(wt, th)
                tag_theta = math.atan2(wt[1] - wc[1], wt[0] - wc[0])
                theta = wrap_angle(tag_theta + (cal.heading_offset if cal else 0.0))
                ox, oy = (cal.tag_offset_x, cal.tag_offset_y) if cal else (0.0, 0.0)
                x = wc[0] + ox * math.cos(tag_theta) - oy * math.sin(tag_theta)
                y = wc[1] + ox * math.sin(tag_theta) + oy * math.cos(tag_theta)
                self._update_obs(obs, x, y, theta, float(wc[0]), float(wc[1]), tag_theta, stamp)
                obs.image_center = (float(d.center[0]), float(d.center[1]))

            known = world_ids | {o.tag_id for o in self._robots.values()}
            return WorldSnapshot(
                stamp=stamp,
                H=None if self._H is None else self._H.copy(),
                reprojection_error=self._H_err,
                homography_stamp=self._H_stamp,
                homography_locked=self._locked,
                world_tags_visible=seen,
                bounds=list(self.bounds),
                robots={n: o.copy() for n, o in self._robots.items()},
                detections=detections,
                unknown_tags=sorted(i for i in by_id if i not in known),
                fps=fps,
                image_size=image_size,
            )

    def _update_obs(self, obs: RobotObservation, x, y, theta, tx, ty, tag_theta, stamp) -> None:
        dt = stamp - obs.last_seen
        fresh = obs.ever_seen and 0 < dt < 0.5
        a = self.world.pose_smoothing if fresh else 0.0
        nx = a * obs.x + (1 - a) * x
        ny = a * obs.y + (1 - a) * y
        nth = wrap_angle(obs.theta + (1 - a) * wrap_angle(theta - obs.theta)) if fresh else theta
        if fresh:
            b = self.world.velocity_smoothing
            obs.vx = b * obs.vx + (1 - b) * (nx - obs.x) / dt
            obs.vy = b * obs.vy + (1 - b) * (ny - obs.y) / dt
            obs.omega = b * obs.omega + (1 - b) * wrap_angle(nth - obs.theta) / dt
        else:
            obs.vx = obs.vy = obs.omega = 0.0
        obs.x, obs.y, obs.theta = nx, ny, nth
        obs.tag_x, obs.tag_y, obs.tag_theta = tx, ty, tag_theta
        obs.visible = True
        obs.ever_seen = True
        obs.last_seen = stamp
