"""Where is the camera? Recovered from the floor homography alone.

The homography maps image pixels to the floor (metres). Seen through a pinhole camera K [R | t],
the floor plane z = 0 gives  image ~ K [r1 r2 t] (x, y, 1). With the principal point at the image
centre and square pixels, K only has one unknown, the focal length f, and r1, r2 being orthonormal
gives two equations for it:

    r1 . r2 = 0          and          |r1| = |r2|

Solved together by least squares (each one degenerates for some viewpoints, never both at once
when the view is oblique). The camera centre is then C = -R^T t: its ground point (x, y) and height.

Seen from straight above, perspective is too weak to tell the focal length; a typical field of view
is used instead (or the lens intrinsics when configured). The height is then a few % off, which
barely matters there: parallax is smallest under the camera.

This lets the middleware correct the parallax of tags mounted above the floor without anyone
measuring where the camera is, which matters most for a camera at head height looking at the field
from the side.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

# below this tilt (camera axis vs vertical) the focal length cannot be estimated reliably
MIN_TILT_FOR_FOCAL = math.radians(15)


@dataclass
class CameraPose:
    x: float              # ground point below the camera, physical frame (m)
    y: float
    height: float         # above the floor (m)
    tilt: float           # angle between the optical axis and the vertical (rad): 0 = looking straight down
    focal_px: float
    source: str           # "estimated" (from perspective), "fov" (typical field of view), "intrinsics"

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "height": self.height, "tilt_deg": math.degrees(self.tilt),
                "focal_px": self.focal_px, "source": self.source}


def _pose(G: np.ndarray, K: np.ndarray) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """G: floor (x, y, 1) -> pixels. Returns (R, C) or None."""
    M = np.linalg.solve(K, G)
    n1, n2 = np.linalg.norm(M[:, 0]), np.linalg.norm(M[:, 1])
    if n1 < 1e-12 or n2 < 1e-12:
        return None
    lam = 2.0 / (n1 + n2)
    if M[2, 2] * lam < 0:  # the floor must be in front of the camera
        lam = -lam
    r1, r2, t = lam * M[:, 0], lam * M[:, 1], lam * M[:, 2]
    r1 /= np.linalg.norm(r1)
    r2 = r2 - r1 * float(r1 @ r2)
    r2 /= np.linalg.norm(r2)
    r3 = np.cross(r1, r2)
    R = np.column_stack([r1, r2, r3])
    C = -R.T @ t
    if C[2] < 0:  # mirror solution: flip the floor normal
        R = np.column_stack([r1, r2, -r3])
        C = -R.T @ t
    return R, C


def _focal_from_h(G: np.ndarray) -> Optional[float]:
    """Least-squares focal length (pixels) from a centred homography, None if undetermined."""
    g = G / np.linalg.norm(G[:, 0])
    (a, b), (c, d), (e, f) = g[0, :2], g[1, :2], g[2, :2]
    den1, num1 = e * f, -(a * b + c * d)
    den2, num2 = e * e - f * f, b * b + d * d - a * a - c * c
    den = den1 * den1 + den2 * den2
    if den <= 0:
        return None
    v = (num1 * den1 + num2 * den2) / den
    return math.sqrt(v) if v > 0 else None


def estimate_camera_pose(H_img_to_world: np.ndarray, image_size: Sequence[int],
                         camera_matrix: Optional[List[List[float]]] = None,
                         fov_deg: float = 70.0) -> Optional[CameraPose]:
    """Camera ground point, height and tilt from the image -> floor homography."""
    w, h = image_size
    if H_img_to_world is None or not w or not h:
        return None
    try:
        G = np.linalg.inv(np.asarray(H_img_to_world, dtype=np.float64))
    except np.linalg.LinAlgError:
        return None
    if camera_matrix:
        K = np.asarray(camera_matrix, dtype=np.float64)
        res = _pose(G, K)
        source, focal = "intrinsics", float(K[0, 0])
    else:
        f_fov = (w / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
        T = np.array([[1, 0, -w / 2.0], [0, 1, -h / 2.0], [0, 0, 1]])
        Gc = T @ G
        K0 = np.diag([f_fov, f_fov, 1.0])
        res = _pose(Gc, K0)
        if res is None:
            return None
        tilt = math.acos(max(-1.0, min(1.0, abs(float(res[0][2, 2])))))
        focal, source = f_fov, "fov"
        if tilt > MIN_TILT_FOR_FOCAL:
            f_est = _focal_from_h(Gc)
            if f_est is not None and 0.4 * f_fov < f_est < 2.5 * f_fov:
                focal, source = f_est, "estimated"
                res = _pose(Gc, np.diag([focal, focal, 1.0]))
    if res is None:
        return None
    R, C = res
    tilt = math.acos(max(-1.0, min(1.0, abs(float(R[2, 2])))))
    if not np.all(np.isfinite(C)) or C[2] <= 0:
        return None
    return CameraPose(float(C[0]), float(C[1]), float(C[2]), tilt, float(focal), source)


def parallax_shift(cam: CameraPose, x: float, y: float, tag_height: float) -> float:
    """How far (m) a tag 'tag_height' above (x, y) appears from it, seen from 'cam'."""
    if cam.height <= tag_height:
        return math.inf
    return math.hypot(x - cam.x, y - cam.y) * tag_height / (cam.height - tag_height)
