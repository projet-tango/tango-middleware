"""Geometry helpers and the physical <-> logical (GAMA) frame mapping."""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence, Tuple

import numpy as np

from .config import LogicalFrameConfig, WorldConfig


def wrap_angle(a: float) -> float:
    """Wrap an angle to [-pi, pi)."""
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def apply_h(H: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Apply a 3x3 homography to an (N, 2) array of points."""
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    hom = np.hstack([pts, np.ones((len(pts), 1))]) @ H.T
    return hom[:, :2] / hom[:, 2:3]


def world_bbox(world: WorldConfig) -> Tuple[float, float, float, float]:
    xs = [t.x for t in world.tags] or [0.0, 1.0]
    ys = [t.y for t in world.tags] or [0.0, 1.0]
    return min(xs), min(ys), max(xs), max(ys)


def point_in_polygon(x: float, y: float, poly: Sequence[Tuple[float, float]], margin: float = 0.0) -> bool:
    """True if (x, y) is inside the polygon grown by `margin` (shrunk if negative)."""
    import cv2

    contour = np.asarray(poly, dtype=np.float32).reshape(-1, 1, 2)
    d = cv2.pointPolygonTest(contour, (float(x), float(y)), True)  # >0 inside
    return d >= -margin


def convex_hull(points: Iterable[Tuple[float, float]]) -> list:
    import cv2

    pts = np.asarray(list(points), dtype=np.float32)
    if len(pts) < 3:
        return [tuple(map(float, p)) for p in pts]
    hull = cv2.convexHull(pts).reshape(-1, 2)
    return [(float(x), float(y)) for x, y in hull]


class LogicalFrame:
    """Affine mapping between the physical world frame (metres, y up, ccw angles)
    and the GAMA logical frame (arbitrary units, configurable origin).

    Headings in the logical frame are expressed *in the logical axes*: with the
    GAMA convention (origin top-left, y down) that is 0 = east and positive =
    clockwise on screen, which is exactly GAMA's `heading`.
    """

    def __init__(self, cfg: LogicalFrameConfig, world: WorldConfig):
        self.cfg = cfg
        if cfg.physical_bounds and len(cfg.physical_bounds) == 4:
            x0, y0, x1, y1 = cfg.physical_bounds
        else:
            x0, y0, x1, y1 = world_bbox(world)
        if x1 - x0 <= 1e-9 or y1 - y0 <= 1e-9:
            x0, y0, x1, y1 = 0.0, 0.0, 1.0, 1.0
        sx = cfg.width / (x1 - x0)
        sy = cfg.height / (y1 - y0)
        if cfg.origin == "top_left":
            # lx = (x - x0) * sx ; ly = (y1 - y) * sy
            M = np.array([[sx, 0.0, -x0 * sx], [0.0, -sy, y1 * sy], [0.0, 0.0, 1.0]])
        else:
            M = np.array([[sx, 0.0, -x0 * sx], [0.0, sy, -y0 * sy], [0.0, 0.0, 1.0]])
        self.M = M  # physical -> logical
        self.M_inv = np.linalg.inv(M)
        self.bounds = (x0, y0, x1, y1)

    # -- angles
    def _to_unit(self, rad: float) -> float:
        return math.degrees(rad) if self.cfg.heading_unit == "deg" else rad

    def _from_unit(self, v: float) -> float:
        return math.radians(v) if self.cfg.heading_unit == "deg" else v

    # -- points
    def point_to_logical(self, x: float, y: float) -> Tuple[float, float]:
        p = self.M @ np.array([x, y, 1.0])
        return float(p[0]), float(p[1])

    def point_to_physical(self, lx: float, ly: float) -> Tuple[float, float]:
        p = self.M_inv @ np.array([lx, ly, 1.0])
        return float(p[0]), float(p[1])

    # -- vectors (speed + heading)
    def vector_to_logical(self, vx: float, vy: float) -> Tuple[float, float]:
        v = self.M[:2, :2] @ np.array([vx, vy])
        return float(v[0]), float(v[1])

    def vector_to_physical(self, lvx: float, lvy: float) -> Tuple[float, float]:
        v = self.M_inv[:2, :2] @ np.array([lvx, lvy])
        return float(v[0]), float(v[1])

    def heading_to_logical(self, theta: float) -> float:
        dx, dy = self.vector_to_logical(math.cos(theta), math.sin(theta))
        return self._to_unit(math.atan2(dy, dx))

    def heading_to_physical(self, heading: float) -> float:
        h = self._from_unit(heading)
        dx, dy = self.vector_to_physical(math.cos(h), math.sin(h))
        return math.atan2(dy, dx)

    def speed_heading_to_physical(self, speed: float, heading: float) -> Tuple[float, float]:
        """Logical (speed, heading) -> physical (speed m/s, heading rad ccw)."""
        h = self._from_unit(heading)
        vx, vy = self.vector_to_physical(speed * math.cos(h), speed * math.sin(h))
        s = math.hypot(vx, vy)
        if s < 1e-12:
            # zero speed: still convert the direction
            return 0.0, self.heading_to_physical(heading)
        return s * (1.0 if speed >= 0 else -1.0), math.atan2(vy, vx) if speed >= 0 else math.atan2(-vy, -vx)

    def pose_to_logical(self, x: float, y: float, theta: float, vx: float = 0.0, vy: float = 0.0) -> dict:
        lx, ly = self.point_to_logical(x, y)
        lvx, lvy = self.vector_to_logical(vx, vy)
        return {
            "x": lx,
            "y": ly,
            "heading": self.heading_to_logical(theta),
            "speed": math.hypot(lvx, lvy),
            "vx": lvx,
            "vy": lvy,
        }

    def matrix(self) -> list:
        return self.M.tolist()

    def physical_polygon_to_logical(self, poly: Optional[Sequence[Tuple[float, float]]]) -> list:
        if not poly:
            return []
        return [list(self.point_to_logical(x, y)) for x, y in poly]
