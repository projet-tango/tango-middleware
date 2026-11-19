"""Merge what several cameras see into one world snapshot.

Every camera has its own homography to the *same* floor frame (the one defined by the boundary tags),
so their robot positions are directly comparable. For each robot:

* seen by one camera: that camera's estimate;
* seen by several: a weighted average, the weight being the square of the tag size in pixels (a
  tag seen bigger is located more precisely), after bringing every observation to the same instant
  with its velocity (the cameras are not synchronised);
* seen by two cameras or more whose position is known: **triangulation**. A tag at height h above the
  ground point q projects on the floor, for a camera standing above c at height H, at
  p = c + (q - c) H / (H - h); i.e.  H q + h (p - c) = H p , linear in (q, h). With two views or more
  this gives the ground position without knowing the tag height, and measures that height.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .tracker import RobotObservation, WorldSnapshot

FRESH = 0.3  # s: an observation older than this does not count as "seen now"


@dataclass
class View:
    name: str
    snap: WorldSnapshot


@dataclass
class FusionInfo:
    cameras: List[str] = field(default_factory=list)     # cameras seeing the robot now
    method: str = ""                                     # single | average | triangulated
    height: Optional[float] = None                       # tag height measured by triangulation (m)


def triangulate(obs: Sequence[Tuple[np.ndarray, Tuple[float, float, float], float]]) -> Optional[Tuple[np.ndarray, float, float]]:
    """obs: (floor point p of the tag seen as if on the floor, camera (cx, cy, H), weight).
    Returns (ground point q, height h, rms residual in m) or None if ill-conditioned."""
    A, b = [], []
    for p, (cx, cy, H), w in obs:
        s = math.sqrt(w)
        d = np.asarray(p) - (cx, cy)
        A.append([s * H, 0, s * d[0]])
        b.append(s * H * p[0])
        A.append([0, s * H, s * d[1]])
        b.append(s * H * p[1])
    A, b = np.array(A), np.array(b)
    if np.linalg.cond(A) > 1e5:
        return None  # cameras looking from the same direction: no depth information
    x, *_ = np.linalg.lstsq(A, b, rcond=None)
    q, h = x[:2], float(x[2])
    # residual in metres on the floor
    res = []
    for p, (cx, cy, H), _ in obs:
        if H <= h:
            return None
        pred = np.array([cx, cy]) + (q - (cx, cy)) * H / (H - h)
        res.append(np.linalg.norm(pred - p))
    return q, h, float(np.sqrt(np.mean(np.square(res))))


def fuse(views: List[View], main: str, now: float) -> Tuple[WorldSnapshot, Dict[str, FusionInfo]]:
    """One snapshot for control and publication. World-level fields come from the main camera (or the
    first camera with a valid homography); robots are fused across cameras."""
    by_name = {v.name: v.snap for v in views}
    base = by_name.get(main)
    if base is None or base.H is None:
        base = next((v.snap for v in views if v.snap.H is not None), base or WorldSnapshot())
    out = WorldSnapshot(stamp=max((v.snap.stamp for v in views), default=0.0), H=base.H,
                        reprojection_error=base.reprojection_error, homography_stamp=base.homography_stamp,
                        homography_locked=base.homography_locked, camera_moved_at=base.camera_moved_at,
                        camera=base.camera, world_tags_visible=sorted({t for v in views for t in v.snap.world_tags_visible}),
                        bounds=base.bounds, detections=base.detections,
                        unknown_tags=sorted({t for v in views for t in v.snap.unknown_tags}),
                        fps=base.fps, image_size=base.image_size)
    infos: Dict[str, FusionInfo] = {}
    names = list(dict.fromkeys(n for v in views for n in v.snap.robots))
    for name in names:
        cands = [(v.name, v.snap, v.snap.robots[name]) for v in views
                 if name in v.snap.robots and v.snap.robots[name].visible and v.snap.H is not None
                 and now - v.snap.robots[name].last_seen < FRESH]
        info = FusionInfo(cameras=[c[0] for c in cands])
        if not cands:
            # nobody sees it now: keep the most recent estimate
            seen = [v.snap.robots[name] for v in views if name in v.snap.robots and v.snap.robots[name].ever_seen]
            o = max(seen, key=lambda r: r.last_seen).copy() if seen else next(
                v.snap.robots[name] for v in views if name in v.snap.robots).copy()
            o.visible = False
            out.robots[name] = o
            infos[name] = info
            continue
        if len(cands) == 1:
            out.robots[name] = cands[0][2].copy()
            info.method = "single"
            infos[name] = info
            continue
        t_ref = max(c[2].last_seen for c in cands)
        w = np.array([max(c[2].px, 1.0) ** 2 for c in cands])
        w = w / w.sum()

        def at_ref(o: RobotObservation, x: float, y: float) -> np.ndarray:
            dt = t_ref - o.last_seen
            return np.array([x + o.vx * dt, y + o.vy * dt])

        pos = sum(wi * at_ref(c[2], c[2].x, c[2].y) for wi, c in zip(w, cands))
        info.method = "average"
        located = [(at_ref(c[2], c[2].floor_x, c[2].floor_y), (c[1].camera.x, c[1].camera.y, c[1].camera.height), wi)
                   for wi, c in zip(w, cands) if c[1].camera is not None]
        if len(located) >= 2:
            tri = triangulate(located)
            if tri is not None and -0.05 < tri[1] < 1.5 and tri[2] < 0.03:
                # the tag's ground point, plus the calibrated tag -> robot centre offset
                best = cands[int(np.argmax(w))][2]
                pos = tri[0] + np.array([best.x - best.tag_x, best.y - best.tag_y])
                info.height, info.method = tri[1], "triangulated"
        th = math.atan2(sum(wi * math.sin(c[2].theta) for wi, c in zip(w, cands)),
                        sum(wi * math.cos(c[2].theta) for wi, c in zip(w, cands)))
        o = cands[int(np.argmax(w))][2].copy()
        o.x, o.y, o.theta = float(pos[0]), float(pos[1]), th
        o.vx = float(sum(wi * c[2].vx for wi, c in zip(w, cands)))
        o.vy = float(sum(wi * c[2].vy for wi, c in zip(w, cands)))
        o.omega = float(sum(wi * c[2].omega for wi, c in zip(w, cands)))
        o.last_seen = t_ref
        o.visible = True
        out.robots[name] = o
        infos[name] = info
    return out, infos
