"""Frame sources: OpenCV capture (USB index, file, URL) and the synthetic camera."""
from __future__ import annotations

import logging
import platform
import time
from typing import List, Optional, Tuple

import cv2
import numpy as np

from ..config import CameraConfig

log = logging.getLogger(__name__)

_BACKENDS = {
    "auto": cv2.CAP_ANY,
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "v4l2": cv2.CAP_V4L2,
    "avfoundation": cv2.CAP_AVFOUNDATION,
    "gstreamer": cv2.CAP_GSTREAMER,
    "ffmpeg": cv2.CAP_FFMPEG,
}


class FrameSource:
    """Interface: read() -> (ok, frame BGR, timestamp)."""

    #: close and reopen the source after repeated read failures
    reopen_on_failure = True

    def status_error(self) -> Optional[str]:
        """Why the source currently delivers no image (None when everything is fine)."""
        return None

    def read(self) -> Tuple[bool, Optional[np.ndarray], float]:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def info(self) -> dict:
        return {}


class OpenCVSource(FrameSource):
    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        src = cfg.source.strip()
        target = int(src) if src.lstrip("-").isdigit() else src
        api = _BACKENDS[cfg.backend]
        if api == cv2.CAP_ANY and isinstance(target, int) and platform.system() == "Windows":
            # MSMF is slow to open and often ignores resolution requests; DSHOW is more predictable.
            api = cv2.CAP_DSHOW
        self.cap = cv2.VideoCapture(target, api)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera '{cfg.source}' (backend {cfg.backend})")
        if cfg.fourcc:
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*cfg.fourcc[:4].ljust(4)))
        if cfg.width:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        if cfg.height:
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        if cfg.fps:
            self.cap.set(cv2.CAP_PROP_FPS, cfg.fps)
        if cfg.autofocus is not None:
            self.cap.set(cv2.CAP_PROP_AUTOFOCUS, 1 if cfg.autofocus else 0)
        if cfg.exposure is not None:
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25 if platform.system() == "Linux" else 0)
            self.cap.set(cv2.CAP_PROP_EXPOSURE, cfg.exposure)
        self._is_file = isinstance(target, str) and not target.lower().startswith(("rtsp:", "http:", "https:", "udp:", "tcp:"))
        log.info(
            "Camera opened: %s %dx%d @%.1f",
            cfg.source,
            self.cap.get(cv2.CAP_PROP_FRAME_WIDTH),
            self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
            self.cap.get(cv2.CAP_PROP_FPS),
        )

    def read(self):
        ok, frame = self.cap.read()
        if not ok and self._is_file:
            # loop video files
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        return ok, frame, time.time()

    def close(self):
        self.cap.release()

    def info(self):
        return {
            "width": int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": float(self.cap.get(cv2.CAP_PROP_FPS)),
            "backend": self.cap.getBackendName() if hasattr(self.cap, "getBackendName") else "",
        }


def postprocess(frame: np.ndarray, cfg: CameraConfig) -> np.ndarray:
    if cfg.rotate == 90:
        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    elif cfg.rotate == 180:
        frame = cv2.rotate(frame, cv2.ROTATE_180)
    elif cfg.rotate == 270:
        frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if cfg.flip == "horizontal":
        frame = cv2.flip(frame, 1)
    elif cfg.flip == "vertical":
        frame = cv2.flip(frame, 0)
    elif cfg.flip == "both":
        frame = cv2.flip(frame, -1)
    return frame


def probe_cameras(max_index: int = 6) -> List[dict]:
    """List local camera indices that can be opened."""
    found = []
    api = cv2.CAP_DSHOW if platform.system() == "Windows" else cv2.CAP_ANY
    for i in range(max_index):
        cap = cv2.VideoCapture(i, api)
        if cap.isOpened():
            found.append(
                {
                    "index": i,
                    "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                }
            )
        cap.release()
    return found
