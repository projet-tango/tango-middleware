"""AprilTag detection with a common output format.

Backends:
* ``opencv``  : cv2.aruco (AprilTag dictionaries), available on every platform
                with ``opencv-python`` >= 4.7, no extra dependency.
* ``pupil``   : the reference AprilTag C library through ``pupil-apriltags``.

Corners are always returned in the order top-left, top-right, bottom-right,
bottom-left *of the printed tag* (cv2.aruco convention), in image pixels.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

import cv2
import numpy as np

from ..config import DetectorConfig

log = logging.getLogger(__name__)


@dataclass
class TagDetection:
    id: int
    corners: np.ndarray  # (4, 2) float64, TL TR BR BL

    @property
    def center(self) -> np.ndarray:
        return self.corners.mean(axis=0)

    @property
    def top_mid(self) -> np.ndarray:
        return (self.corners[0] + self.corners[1]) / 2.0


_ARUCO_FAMILIES = {
    "tag16h5": "DICT_APRILTAG_16h5",
    "tag25h9": "DICT_APRILTAG_25h9",
    "tag36h10": "DICT_APRILTAG_36h10",
    "tag36h11": "DICT_APRILTAG_36h11",
}


def aruco_dictionary(family: str):
    return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, _ARUCO_FAMILIES[family]))


def render_tag(family: str, tag_id: int, side_px: int, quiet_zone: float = 0.25) -> np.ndarray:
    """Render a printable tag (grayscale) with a white quiet zone around it."""
    img = cv2.aruco.generateImageMarker(aruco_dictionary(family), int(tag_id), int(side_px), borderBits=1)
    pad = int(round(side_px * quiet_zone))
    return cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)


class TagDetector:
    def __init__(self, cfg: DetectorConfig):
        self.cfg = cfg
        self.backend = cfg.backend
        self._pupil = None
        self._aruco = None
        if self.backend in ("auto", "pupil"):
            try:
                from pupil_apriltags import Detector  # type: ignore

                self._pupil = Detector(
                    families=cfg.family,
                    quad_decimate=max(1.0, float(cfg.decimate)),
                    refine_edges=1,
                )
                self.backend = "pupil"
            except Exception as exc:  # pragma: no cover - optional dependency
                if self.backend == "pupil":
                    log.warning("pupil-apriltags unavailable (%s), falling back to OpenCV", exc)
                self.backend = "opencv"
        if self.backend == "opencv":
            params = cv2.aruco.DetectorParameters()
            params.cornerRefinementMethod = {
                "none": cv2.aruco.CORNER_REFINE_NONE,
                "subpix": cv2.aruco.CORNER_REFINE_SUBPIX,
                "contour": cv2.aruco.CORNER_REFINE_CONTOUR,
                "apriltag": cv2.aruco.CORNER_REFINE_APRILTAG,
            }[cfg.corner_refinement]
            params.minMarkerPerimeterRate = float(cfg.min_marker_perimeter)
            self._aruco = cv2.aruco.ArucoDetector(aruco_dictionary(cfg.family), params)

    def detect(self, image: np.ndarray) -> List[TagDetection]:
        gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if self._pupil is not None:
            out = []
            for d in self._pupil.detect(gray):
                # pupil-apriltags order: (-1,1),(1,1),(1,-1),(-1,-1) in tag frame,
                # i.e. BL, BR, TR, TL in image space -> reorder to TL TR BR BL.
                c = np.asarray(d.corners, dtype=np.float64)[[3, 2, 1, 0]]
                out.append(TagDetection(int(d.tag_id), c))
            return out

        scale = float(self.cfg.decimate)
        work = gray
        if scale > 1.0:
            work = cv2.resize(gray, None, fx=1.0 / scale, fy=1.0 / scale, interpolation=cv2.INTER_AREA)
        corners, ids, _ = self._aruco.detectMarkers(work)
        if ids is None:
            return []
        out = []
        for c, i in zip(corners, ids.flatten()):
            pts = np.asarray(c, dtype=np.float64).reshape(4, 2)
            if scale > 1.0:
                pts = (pts + 0.5) * scale - 0.5
            out.append(TagDetection(int(i), pts))
        return out
