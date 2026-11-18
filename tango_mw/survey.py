"""Estimate the real layout of floor tags from their printed size — no tape measure.

The field does not need to be measured: the GAMA world is defined by the corner tags wherever they
were laid, and a tag in the middle is located relative to them. Only the *units* need a real size
(speeds in m/s, tolerances, tag heights), and a rough one is enough. It comes from the tags
themselves: every tag is a square of known side (the printed size, exact) lying on the floor, so the
four corners of all tags must be the image of such squares through one floor -> image homography.
We fit that homography and every tag's position and rotation (least squares, Levenberg-Marquardt):

    corner_ik  ~  G (x_i, y_i) + rotate(a_i) * c_k          c_k = (+-L/2, +-L/2)

In simulation (1.5 m field, corners averaged over a few frames) distances come out within 1-3 %
with 100 mm tags at 1280x960, and better with bigger tags or a higher resolution.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

import cv2
import numpy as np

# corners of a printed tag in its own frame (y towards the tag's top), detector order TL TR BR BL
_UNIT = np.array([[-0.5, 0.5], [0.5, 0.5], [0.5, -0.5], [-0.5, -0.5]])


@dataclass
class SurveyResult:
    positions: Dict[int, Tuple[float, float]]   # tag id -> centre (m); frame: first tag at the origin, second on +x
    rms_px: float                               # how well the model explains the corners (pixels)


def _project(G: np.ndarray, pts: np.ndarray) -> np.ndarray:
    p = np.c_[pts, np.ones(len(pts))] @ G.T
    return p[:, :2] / p[:, 2:3]


def _unpack(v: np.ndarray, n: int) -> Tuple[np.ndarray, np.ndarray]:
    G = np.append(v[:8], 1.0).reshape(3, 3)
    poses = np.zeros((n, 3))
    poses[0, 2] = v[8]                           # first tag: at the origin, rotation free
    poses[1, 0], poses[1, 2] = v[9], v[10]       # second tag: on the x axis
    if n > 2:
        poses[2:] = v[11:].reshape(-1, 3)
    return G, poses


def _residuals(v: np.ndarray, corners: np.ndarray, side: np.ndarray) -> np.ndarray:
    G, poses = _unpack(v, len(corners))
    out = []
    for (x, y, a), obs, L in zip(poses, corners, side):
        c, s = math.cos(a), math.sin(a)
        local = _UNIT * L
        pts = np.c_[x + c * local[:, 0] - s * local[:, 1], y + s * local[:, 0] + c * local[:, 1]]
        out.append(_project(G, pts) - obs)
    return np.concatenate(out).ravel()


def _lm(v: np.ndarray, corners: np.ndarray, side: np.ndarray, max_iter: int = 100) -> Tuple[np.ndarray, float]:
    """Levenberg-Marquardt with a numerical Jacobian (a few tens of unknowns)."""
    lam = 1e-3
    r = _residuals(v, corners, side)
    cost = float(r @ r)
    scale = np.maximum(np.abs(v), 1e-3)
    for _ in range(max_iter):
        J = np.empty((len(r), len(v)))
        for j in range(len(v)):
            d = 1e-6 * scale[j]
            vj = v.copy()
            vj[j] += d
            J[:, j] = (_residuals(vj, corners, side) - r) / d
        A, g = J.T @ J, J.T @ r
        gain = 0.0
        for _ in range(10):
            v2 = v + np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-12), -g)
            r2 = _residuals(v2, corners, side)
            c2 = float(r2 @ r2)
            if c2 < cost:
                gain = cost - c2
                v, r, cost, lam = v2, r2, c2, max(lam / 3, 1e-9)
                break
            lam *= 4
        if gain <= 1e-10 * max(cost, 1e-12):
            break
    return v, cost


def survey(observations: Dict[int, np.ndarray], tag_sizes: Dict[int, float],
           order: Optional[Sequence[int]] = None) -> SurveyResult:
    """observations: tag id -> (4, 2) image corners (TL TR BR BL), ideally averaged over frames.
    tag_sizes: tag id -> printed side of the black square (m). order[0], order[1] fix the frame."""
    ids = [i for i in (order or []) if i in observations]
    ids += [i for i in sorted(observations) if i not in ids]
    if len(ids) < 2:
        raise ValueError("at least 2 floor tags must be visible")
    corners = np.array([np.asarray(observations[i], dtype=np.float64) for i in ids])
    side = np.array([float(tag_sizes[i]) for i in ids])
    n = len(ids)
    # start: the homography of the first tag alone (a square of known size), then every tag's pose
    H0 = cv2.getPerspectiveTransform((_UNIT * side[0]).astype(np.float32), corners[0].astype(np.float32))
    Hinv = np.linalg.inv(H0)
    poses = []
    for obs in corners:
        ctr = _project(Hinv, obs.mean(axis=0, keepdims=True))[0]
        top = _project(Hinv, ((obs[0] + obs[1]) / 2)[None])[0]
        poses.append([ctr[0], ctr[1], math.atan2(top[1] - ctr[1], top[0] - ctr[0]) - math.pi / 2])
    poses = np.array(poses)
    # gauge: first tag at the origin, second on +x
    t0 = poses[0, :2].copy()
    ang = math.atan2(poses[1, 1] - t0[1], poses[1, 0] - t0[0])
    R = np.array([[math.cos(-ang), -math.sin(-ang)], [math.sin(-ang), math.cos(-ang)]])
    poses[:, :2] = (poses[:, :2] - t0) @ R.T
    poses[:, 2] -= ang
    T = np.eye(3)
    T[:2, :2], T[:2, 2] = R.T, t0
    G0 = H0 @ T
    v0 = np.concatenate([(G0 / G0[2, 2]).ravel()[:8], [poses[0, 2], poses[1, 0], poses[1, 2]], poses[2:].ravel()])
    v, cost = _lm(v0, corners, side)
    _, fitted = _unpack(v, n)
    rms = math.sqrt(cost / (4 * n))
    return SurveyResult({i: (float(fitted[k, 0]), float(fitted[k, 1])) for k, i in enumerate(ids)}, rms)


def field_size(observations: Dict[int, np.ndarray], tag_size: float,
               corners_world: Dict[int, Tuple[float, float]]) -> Tuple[float, float, float]:
    """Real width and height of a rectangular field from its corner tags.
    corners_world: tag id -> its corner in a unit layout ((0,0), (1,0), (1,1), (0,1)): tells which
    tags share an edge. Returns (width, height, rms_px)."""
    ids = [i for i in corners_world if i in observations]
    if len(ids) < 3:
        raise ValueError("at least 3 corner tags must be visible to estimate the size")
    res = survey({i: observations[i] for i in ids}, {i: tag_size for i in ids})
    widths, heights = [], []
    for a, b in itertools.combinations(ids, 2):
        (xa, ya), (xb, yb) = corners_world[a], corners_world[b]
        d = math.dist(res.positions[a], res.positions[b])
        if abs(ya - yb) < 1e-9 and abs(xa - xb) > 1e-9:
            widths.append(d)
        elif abs(xa - xb) < 1e-9 and abs(ya - yb) > 1e-9:
            heights.append(d)
    if not widths or not heights:
        raise ValueError("the visible corner tags do not span both directions")
    return float(np.mean(widths)), float(np.mean(heights)), res.rms_px
