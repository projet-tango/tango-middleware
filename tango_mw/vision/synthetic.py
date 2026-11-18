"""Synthetic camera: renders AprilTags of a simulated arena seen from above.

Simulated robots react to the raw commands emitted by the middleware with
"true" dynamics (gains, deadband, drift, tag mounting offset) that differ from
the default calibration, so the whole loop - detection, homography, control,
and calibration procedures - can be exercised without any hardware.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import cv2
import numpy as np

from ..config import AppConfig, RobotConfig
from ..frames import world_bbox, wrap_angle
from .camera import FrameSource
from .detector import render_tag


@dataclass
class SimRobot:
    name: str
    tag_id: int
    x: float
    y: float
    theta: float
    # "true" dynamics
    lin_gain: float = 1.0  # m/s per raw unit
    lin_dead: float = 0.0
    ang_gain: float = 1.0  # rad/s per raw unit
    ang_dead: float = 0.0
    drift: float = 0.0  # rad per metre
    tag_mount: float = 0.0  # tag heading = robot heading + tag_mount
    wheel_base: float = 0.12
    right_scale: float = 1.0
    output: str = "twist"
    cmd: Optional[dict] = None
    cmd_stamp: float = 0.0


def _dead(u: float, dead: float) -> float:
    return math.copysign(max(0.0, abs(u) - dead), u)


class SyntheticSource(FrameSource):
    def __init__(self, cfg: AppConfig):
        self.app_cfg = cfg
        self.cfg = cfg.camera.synthetic
        self.family = cfg.detector.family
        self.rng = np.random.default_rng(self.cfg.seed)
        self.lock = threading.Lock()
        self.robots: Dict[str, SimRobot] = {}
        self._tag_cache: Dict[int, np.ndarray] = {}
        self._last = time.time()
        self._period = 1.0 / float(cfg.camera.fps or 30.0)
        x0, y0, x1, y1 = world_bbox(cfg.world)
        m = max(self.cfg.world_tag_size, 0.05) * 1.2
        self.bounds = (x0, y0, x1, y1)
        self.view = (x0 - m, y0 - m, x1 + m, y1 + m)
        s = self.cfg.px_per_m
        self.size = (int((self.view[2] - self.view[0]) * s), int((self.view[3] - self.view[1]) * s))
        # world -> pixel (top-down, y flipped) followed by a mild perspective warp
        W, Hh = self.size
        A = np.array([[s, 0, -self.view[0] * s], [0, -s, self.view[3] * s], [0, 0, 1.0]])
        p = self.cfg.perspective
        src = np.float32([[0, 0], [W, 0], [W, Hh], [0, Hh]])
        dst = np.float32([[W * p, Hh * p * 0.5], [W * (1 - p * 0.6), 0], [W, Hh], [0, Hh * (1 - p * 0.3)]])
        self.G = cv2.getPerspectiveTransform(src, dst) @ A
        self.sync_robots(cfg.robots)

    # ------------------------------------------------------------------ robots
    def sync_robots(self, robots: List[RobotConfig]) -> None:
        """Simulated robots are keyed by name and keep their *physical* tag even if
        the configuration changes (like real robots). Robots whose configured tag is
        unassigned (< 0) get a free physical tag. `extra_robots` adds robots that are
        not in the configuration yet (to test scanning / identification)."""
        with self.lock:
            x0, y0, x1, y1 = self.bounds
            world_ids = {t.id for t in self.app_cfg.world.tags}
            names = [r.name for r in robots]
            outputs = {r.name: r.output.type for r in robots}
            wanted = {r.name: r.tag_id for r in robots}
            if not hasattr(self, "_extra_names"):  # fixed at creation
                self._extra_names, k = [], len(robots) + 1
                for _ in range(int(self.cfg.extra_robots)):
                    while f"robot{k}" in wanted:
                        k += 1
                    self._extra_names.append(f"robot{k}")
                    k += 1
            for e in self._extra_names:
                if e not in wanted:
                    wanted[e] = -1
                    names.append(e)
            new: Dict[str, SimRobot] = {}
            used = {r.tag_id for r in self.robots.values() if r.name in wanted}
            n = max(1, len(names))
            for i, name in enumerate(names):
                old = self.robots.get(name)
                if old is not None:
                    old.output = outputs.get(name, old.output)
                    new[name] = old
                    continue
                tag = wanted[name]
                if tag < 0 or tag in used or tag in world_ids:
                    tag = 10
                    while tag in used or tag in world_ids or tag in wanted.values():
                        tag += 1
                used.add(tag)
                rnd = self.rng if self.cfg.randomize_dynamics else None
                u = (lambda a, b: float(rnd.uniform(a, b))) if rnd is not None else (lambda a, b: (a + b) / 2)
                new[name] = SimRobot(
                    name=name,
                    tag_id=tag,
                    x=x0 + (x1 - x0) * (i + 1) / (n + 1),
                    y=(y0 + y1) / 2,
                    theta=math.pi / 2,
                    lin_gain=u(0.6, 1.4) if rnd is not None else 1.0,
                    lin_dead=u(0.0, 0.08) if rnd is not None else 0.0,
                    ang_gain=u(0.6, 1.4) if rnd is not None else 1.0,
                    ang_dead=u(0.0, 0.1) if rnd is not None else 0.0,
                    drift=u(-0.4, 0.4) if rnd is not None else 0.0,
                    tag_mount=u(-0.3, 0.3) if rnd is not None else 0.0,
                    right_scale=u(0.9, 1.1) if rnd is not None else 1.0,
                    output=outputs.get(name, "twist"),
                )
            self.robots = new

    def on_command(self, name: str, raw: dict) -> None:
        with self.lock:
            r = self.robots.get(name)
            if r is not None:
                r.cmd = raw
                r.cmd_stamp = time.time()

    def truth(self) -> dict:
        with self.lock:
            return {n: dict(r.__dict__, cmd=None) for n, r in self.robots.items()}

    def _step(self, dt: float) -> None:
        now = time.time()
        x0, y0, x1, y1 = self.bounds
        for r in self.robots.values():
            c = r.cmd if (r.cmd and now - r.cmd_stamp < 0.5) else None
            v = w = 0.0
            if c:
                if r.output == "wheels":
                    l, rr = (c.get("wheels") or [0.0, 0.0])[:2]
                    vl = _dead(l, r.lin_dead) * r.lin_gain
                    vr = _dead(rr, r.lin_dead) * r.lin_gain * r.right_scale
                    v, w = (vl + vr) / 2, (vr - vl) / r.wheel_base
                else:
                    v = _dead(float(c.get("linear", 0.0)), r.lin_dead) * r.lin_gain
                    w = _dead(float(c.get("angular", 0.0)), r.ang_dead) * r.ang_gain
                w += r.drift * v
            r.theta = wrap_angle(r.theta + w * dt)
            r.x = min(max(r.x + v * math.cos(r.theta) * dt, x0 + 0.03), x1 - 0.03)
            r.y = min(max(r.y + v * math.sin(r.theta) * dt, y0 + 0.03), y1 - 0.03)

    # ------------------------------------------------------------------ render
    def _tag(self, tag_id: int) -> np.ndarray:
        img = self._tag_cache.get(tag_id)
        if img is None:
            img = render_tag(self.family, tag_id, 120, quiet_zone=0.0)
            self._tag_cache[tag_id] = img
        return img

    def _draw_tag(self, canvas: np.ndarray, tag_id: int, x: float, y: float, theta: float, size: float, white_pad=0.3):
        f = np.array([math.cos(theta), math.sin(theta)])
        l = np.array([-math.sin(theta), math.cos(theta)])
        c = np.array([x, y])

        def corners(h):
            return np.array([c + (f + l) * h, c + (f - l) * h, c + (-f - l) * h, c + (-f + l) * h])

        def to_px(pts):
            hom = np.hstack([pts, np.ones((4, 1))]) @ self.G.T
            return (hom[:, :2] / hom[:, 2:3]).astype(np.float32)

        # white quiet zone
        cv2.fillConvexPoly(canvas, to_px(corners(size / 2 * (1 + white_pad))).astype(np.int32), 255, cv2.LINE_AA)
        tag = self._tag(tag_id)
        n = tag.shape[0]
        src = np.float32([[0, 0], [n, 0], [n, n], [0, n]])
        M = cv2.getPerspectiveTransform(src, to_px(corners(size / 2)))
        warped = cv2.warpPerspective(tag, M, (canvas.shape[1], canvas.shape[0]), flags=cv2.INTER_LINEAR, borderValue=0)
        mask = cv2.warpPerspective(np.full_like(tag, 255), M, (canvas.shape[1], canvas.shape[0]), flags=cv2.INTER_NEAREST)
        np.copyto(canvas, warped, where=mask > 0)

    def read(self):
        # pace like a real camera
        now = time.time()
        wait = self._period - (now - self._last)
        if wait > 0:
            time.sleep(wait)
        now = time.time()
        dt = min(0.2, now - self._last)
        self._last = now
        with self.lock:
            self._step(dt)
            canvas = np.full((self.size[1], self.size[0]), 170, np.uint8)
            # floor
            fl = np.array([[self.bounds[0], self.bounds[1]], [self.bounds[2], self.bounds[1]],
                           [self.bounds[2], self.bounds[3]], [self.bounds[0], self.bounds[3]]])
            hom = np.hstack([fl, np.ones((4, 1))]) @ self.G.T
            cv2.fillConvexPoly(canvas, (hom[:, :2] / hom[:, 2:3]).astype(np.int32), 215)
            for t in self.app_cfg.world.tags:
                self._draw_tag(canvas, t.id, t.x, t.y, math.pi / 2, self.cfg.world_tag_size)
            for r in self.robots.values():
                self._draw_tag(canvas, r.tag_id, r.x, r.y, r.theta + r.tag_mount, self.cfg.robot_tag_size)
        if self.cfg.noise_px > 0:
            noise = self.rng.normal(0, self.cfg.noise_px, canvas.shape)
            canvas = np.clip(canvas.astype(np.float32) + noise, 0, 255).astype(np.uint8)
        return True, cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR), now

    def info(self):
        return {"width": self.size[0], "height": self.size[1], "fps": 1.0 / self._period, "backend": "synthetic"}
