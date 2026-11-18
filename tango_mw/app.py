"""Orchestrator: camera -> detection -> tracking -> publication, and the control loop."""
from __future__ import annotations

import collections
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any, Deque, Dict, Optional

import cv2
import numpy as np

from .bus import Bus, LoopbackBus, make_bus
from .config import AppConfig, RobotConfig, save_config
from .control.base import Request
from .control.calibration import apply_result
from .control.manager import ControlManager
from .control.registry import ControllerRegistry
from .frames import LogicalFrame, apply_h
from .vision.camera import FrameSource, OpenCVSource, postprocess
from .vision.detector import TagDetector
from .vision.synthetic import SyntheticSource
from .vision.tracker import Tracker, WorldSnapshot

log = logging.getLogger(__name__)


class LogBuffer(logging.Handler):
    def __init__(self, size: int = 400):
        super().__init__(logging.INFO)
        self.records: Deque[dict] = collections.deque(maxlen=size)
        self.counter = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.counter += 1
        self.records.append({"id": self.counter, "t": record.created, "level": record.levelname,
                             "name": record.name, "msg": record.getMessage()})


class _Rate:
    def __init__(self, hz: float):
        self.period = 1.0 / max(hz, 1e-3)
        self.next = time.time()

    def sleep(self) -> None:
        self.next += self.period
        d = self.next - time.time()
        if d > 0:
            time.sleep(d)
        else:
            self.next = time.time()


class App:
    def __init__(self, cfg: AppConfig, config_path: Optional[Path] = None, base_dir: Optional[Path] = None):
        self.cfg = cfg
        self.config_path = Path(config_path) if config_path else None
        self.base_dir = Path(base_dir or (self.config_path.parent.parent if self.config_path else Path.cwd()))
        self.logs = LogBuffer()
        logging.getLogger().addHandler(self.logs)
        self.started_at = time.time()

        self.registry = ControllerRegistry(cfg.plugins.dirs, base_dir=self.base_dir)
        self.tracker = Tracker(cfg.world, cfg.camera, cfg.robots)
        self.detector = TagDetector(cfg.detector)
        self.logical = LogicalFrame(cfg.logical, cfg.world)
        self.bus: Bus = LoopbackBus(cfg)
        self.bus_error: Optional[str] = None
        self.manager = ControlManager(cfg, self.registry, self._emit, self._job_done)

        self._src_lock = threading.Lock()
        self.source: Optional[FrameSource] = None
        self.camera_error: Optional[str] = None
        self._next_open = 0.0
        self.snapshot = WorldSnapshot()
        self.frame: Optional[np.ndarray] = None
        self.frame_seq = 0
        self._fps = 0.0
        self.last_request_time = 0.0
        self._running = False
        self._threads = []
        self._dirty = False  # config changed but not saved

    # ================================================================== lifecycle
    def start(self) -> None:
        self._start_bus()
        self._running = True
        for name, fn in (("vision", self._vision_loop), ("control", self._control_loop), ("publish", self._publish_loop)):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        log.info("Middleware démarré (bus: %s)", self.bus.kind)

    def stop(self) -> None:
        # send a last stop to every robot
        self.manager.set_estop(True)
        snap = self.snapshot
        try:
            self.manager.step(time.time(), snap)
        except Exception:
            pass
        self._running = False
        for t in self._threads:
            t.join(timeout=2.0)
        with self._src_lock:
            if self.source:
                self.source.close()
                self.source = None
        self.bus.stop()

    def _start_bus(self) -> None:
        try:
            bus = make_bus(self.cfg)
            bus.on_request = self.handle_request
            bus.on_estop = self.manager.set_estop
            bus.start()
            self.bus, self.bus_error = bus, None
        except Exception as exc:
            log.error("Bus ROS indisponible: %s — repli loopback", exc)
            self.bus_error = str(exc)
            self.bus = LoopbackBus(self.cfg)
            self.bus.on_request = self.handle_request
            self.bus.on_estop = self.manager.set_estop

    # ================================================================== vision
    def _open_source(self) -> None:
        cam = self.cfg.camera
        try:
            if cam.source.strip().lower() == "synthetic":
                self.source = SyntheticSource(self.cfg)
            else:
                self.source = OpenCVSource(cam)
            self.camera_error = None
        except Exception as exc:
            self.source = None
            if self.camera_error != str(exc):
                log.error("Caméra: %s", exc)
            self.camera_error = str(exc)
            self._next_open = time.time() + 2.0

    def _vision_loop(self) -> None:
        last = time.time()
        failures = 0
        while self._running:
            with self._src_lock:
                if self.source is None and time.time() >= self._next_open:
                    self._open_source()
                src, det, cam = self.source, self.detector, self.cfg.camera
            if src is None:
                time.sleep(0.1)
                continue
            ok, frame, stamp = src.read()
            if not ok or frame is None:
                failures += 1
                if failures > 30:
                    log.warning("Caméra: plus d'images, réouverture")
                    with self._src_lock:
                        if self.source is src:
                            src.close()
                            self.source = None
                    failures = 0
                time.sleep(0.02)
                continue
            failures = 0
            frame = postprocess(frame, cam)
            dt = stamp - last
            last = stamp
            if dt > 0:
                self._fps = 0.9 * self._fps + 0.1 / dt if self._fps else 1.0 / dt
            try:
                dets = det.detect(frame)
                snap = self.tracker.process(dets, stamp, (frame.shape[1], frame.shape[0]), self._fps)
            except Exception as exc:  # pragma: no cover
                log.exception("Erreur de détection: %s", exc)
                continue
            self.snapshot = snap
            self.frame = frame
            self.frame_seq += 1

    # ================================================================== control
    def _emit(self, robot: RobotConfig, raw: Dict[str, Any]) -> None:
        try:
            self.bus.publish_command(robot, raw)
        except Exception as exc:
            self.bus.errors[f"cmd:{robot.name}"] = repr(exc)
        src = self.source
        if isinstance(src, SyntheticSource):
            src.on_command(robot.name, raw)

    def _control_loop(self) -> None:
        rate = _Rate(self.cfg.ros.control_rate)
        last_reload = 0.0
        while self._running:
            now = time.time()
            if self.cfg.plugins.auto_reload and now - last_reload > 1.0:
                last_reload = now
                try:
                    if self.registry.reload():
                        log.info("Contrôleurs rechargés")
                except Exception:  # pragma: no cover
                    log.exception("rechargement des contrôleurs")
            try:
                self.manager.step(now, self.snapshot)
            except Exception:  # pragma: no cover
                log.exception("boucle de contrôle")
            rate.period = 1.0 / self.cfg.ros.control_rate
            rate.sleep()

    def handle_request(self, robot: str, fields: Dict[str, Any], source: str = "") -> bool:
        """Entry point for GAMA (and UI) requests."""
        try:
            now = time.time()
            frame = fields.get("frame") or ("logical" if self.cfg.logical.requests_in_logical else "physical")
            stop = bool(fields.get("stop", False))
            speed = float(fields.get("speed", 0.0))
            heading = fields.get("heading")
            prev = self.manager.robots.get(robot)
            if heading is None:
                if prev and prev.request:
                    h = prev.request.heading
                else:
                    obs = self.snapshot.robots.get(robot)
                    h = obs.theta if obs else 0.0
                if frame == "logical":
                    s, _ = self.logical.speed_heading_to_physical(speed, 0.0)
                    speed = s
                heading_phys = h
            elif frame == "logical":
                speed, heading_phys = self.logical.speed_heading_to_physical(speed, float(heading))
            else:
                heading_phys = float(heading)
            extra = {k: v for k, v in fields.items() if k not in ("robot", "speed", "heading", "stop", "frame")}
            ok = self.manager.set_request(robot, Request(speed=speed, heading=heading_phys, stamp=now, stop=stop, extra=extra), source)
            if ok:
                self.last_request_time = now
            else:
                log.debug("requête pour un robot inconnu: %s", robot)
            return ok
        except Exception as exc:
            log.warning("requête invalide pour %s: %s", robot, exc)
            return False

    def _job_done(self, robot: str, job) -> None:
        if job.kind == "identify":
            self.assign_tag(robot, int(job.result["tag_id"]))
        else:
            self._calibration_done(robot, dict(job.result))

    def assign_tag(self, robot: str, tag_id: int) -> None:
        """Give `tag_id` to `robot`; a robot that already had it gets the old tag (swap)."""
        cfg = self.cfg.model_copy(deep=True)
        r = cfg.robot(robot)
        if r is None:
            return
        if tag_id in {t.id for t in cfg.world.tags}:
            raise ValueError(f"le tag #{tag_id} est un tag de monde")
        other = next((o for o in cfg.robots if o.tag_id == tag_id and o.name != robot), None)
        old = r.tag_id
        r.tag_id = -10_000  # temporary, keeps tag ids unique during the swap
        if other is not None:
            other.tag_id = old if old != tag_id else min([x.tag_id for x in cfg.robots] + [0]) - 1
            log.warning("Tag #%d retiré de %s (attribué à %s), %s reçoit #%d", tag_id, other.name, robot, other.name, other.tag_id)
        r.tag_id = tag_id
        cfg = AppConfig.model_validate(cfg.model_dump())
        log.info("Robot %s associé au tag #%d", robot, tag_id)
        self.apply_config(cfg)
        if self.config_path:
            self.save()

    def _calibration_done(self, robot: str, result: Dict[str, Any]) -> None:
        cfg = self.cfg.model_copy(deep=True)
        r = cfg.robot(robot)
        if r is None:
            return
        r.calibration = apply_result(r.calibration, result)
        log.info("Calibration de %s appliquée: %s", robot, result)
        self.apply_config(cfg)
        if self.config_path:
            self.save()

    # ================================================================== publication
    def robot_json(self, name: str, snap: WorldSnapshot, now: float) -> Dict[str, Any]:
        o = snap.robots[name]
        return {
            "name": name,
            "tag_id": o.tag_id,
            "visible": o.visible,
            "ever_seen": o.ever_seen,
            "age": (now - o.last_seen) if o.ever_seen else None,
            "x": o.x, "y": o.y, "theta": o.theta,
            "vx": o.vx, "vy": o.vy, "omega": o.omega, "speed": o.speed,
            "logical": self.logical.pose_to_logical(o.x, o.y, o.theta, o.vx, o.vy),
        }

    def world_json(self, snap: Optional[WorldSnapshot] = None) -> Dict[str, Any]:
        snap = snap or self.snapshot
        now = time.time()
        H = snap.H
        HL = (self.logical.M @ H) if H is not None else None
        return {
            "stamp": now,
            "frame_stamp": snap.stamp,
            "valid": H is not None,
            "frame_id": self.cfg.ros.frame_id,
            "fps": snap.fps,
            "image_size": list(snap.image_size),
            "homography": H.tolist() if H is not None else None,
            "homography_logical": HL.tolist() if HL is not None else None,
            "physical_to_logical": self.logical.matrix(),
            "reprojection_error": snap.reprojection_error,
            "homography_age": (now - snap.homography_stamp) if H is not None else None,
            "homography_locked": snap.homography_locked,
            "world_tags_expected": [t.id for t in self.cfg.world.tags],
            "world_tags_visible": snap.world_tags_visible,
            "unknown_tags": snap.unknown_tags,
            "bounds": [list(p) for p in snap.bounds],
            "bounds_logical": self.logical.physical_polygon_to_logical(snap.bounds),
            "logical": {"width": self.cfg.logical.width, "height": self.cfg.logical.height,
                        "origin": self.cfg.logical.origin, "heading_unit": self.cfg.logical.heading_unit},
            "robots": {n: self.robot_json(n, snap, now) for n in snap.robots},
        }

    def health(self) -> Dict[str, Any]:
        snap = self.snapshot
        ctrl = self.manager.describe()
        return {
            "stamp": time.time(),
            "uptime": time.time() - self.started_at,
            "bus": self.bus.kind,
            "camera_ok": self.source is not None and self.camera_error is None,
            "camera_error": self.camera_error,
            "fps": snap.fps,
            "homography_valid": snap.H is not None,
            "estop": ctrl["estop"],
            "robots": {n: {"status": r["status"], "visible": snap.robots[n].visible if n in snap.robots else False}
                       for n, r in ctrl["robots"].items()},
        }

    def _publish_loop(self) -> None:
        rate = _Rate(self.cfg.ros.publish_rate)
        last_status = 0.0
        while self._running:
            try:
                snap = self.snapshot
                state = self.world_json(snap)
                H = snap.H
                self.bus.publish_world(state, H, (self.logical.M @ H) if H is not None else None, snap.bounds)
                for name, rs in state["robots"].items():
                    if rs["ever_seen"]:
                        self.bus.publish_robot(name, rs)
                now = time.time()
                if now - last_status > 1.0:
                    last_status = now
                    self.bus.publish_status(self.health())
            except Exception:  # pragma: no cover
                log.exception("publication")
            rate.period = 1.0 / self.cfg.ros.publish_rate
            rate.sleep()

    # ================================================================== config
    def apply_config(self, new: AppConfig) -> None:
        old = self.cfg
        self.cfg = new
        cam_changed = old.camera != new.camera or (
            new.camera.source == "synthetic" and (old.world != new.world or old.detector.family != new.detector.family))
        with self._src_lock:
            if old.detector != new.detector:
                self.detector = TagDetector(new.detector)
            if cam_changed and self.source is not None:
                self.source.close()
                self.source = None
                self._next_open = 0.0
            elif isinstance(self.source, SyntheticSource):
                self.source.app_cfg = new
                self.source.sync_robots(new.robots)
        if old.world != new.world or old.camera.camera_matrix != new.camera.camera_matrix or old.camera.dist_coeffs != new.camera.dist_coeffs:
            self.tracker.set_world(new.world, new.camera)
        self.tracker.set_robots(new.robots)
        self.logical = LogicalFrame(new.logical, new.world)
        if old.plugins.dirs != new.plugins.dirs:
            self.registry.set_dirs(new.plugins.dirs)
        self.manager.set_config(new)
        ros_keys = ("bus", "node_name", "namespace", "domain_id", "qos_depth", "publish_per_robot_topics")
        if any(getattr(old.ros, k) != getattr(new.ros, k) for k in ros_keys):
            log.info("Configuration ROS modifiée: redémarrage du bus")
            self.bus.stop()
            self._start_bus()
        else:
            self.bus.cfg = new
            self.bus.sync_robots(new.robots)
        self._dirty = True

    def save(self, path: Optional[Path] = None) -> Path:
        path = Path(path or self.config_path or (self.base_dir / "config" / "config.yaml"))
        save_config(self.cfg, path)
        self.config_path = path
        self._dirty = False
        log.info("Configuration enregistrée dans %s", path)
        return path

    def relock_homography(self) -> None:
        self.tracker.relock()

    # ================================================================== UI helpers
    def status(self) -> Dict[str, Any]:
        presence = {}
        for r in self.cfg.robots:
            try:
                presence[r.name] = self.bus.robot_presence(r)
            except Exception as exc:
                presence[r.name] = {"known": False, "error": repr(exc)}
        info = {}
        with self._src_lock:
            if self.source is not None:
                info = self.source.info()
        return {
            "health": self.health(),
            "world": self.world_json(),
            "control": self.manager.describe(),
            "presence": presence,
            "gama": {
                "publishers_on_requests": self.bus.requests_publishers(),
                "last_request_age": (time.time() - self.last_request_time) if self.last_request_time else None,
            },
            "bus": {"kind": self.bus.kind, "errors": dict(self.bus.errors), "error": self.bus_error},
            "camera": {"error": self.camera_error, "info": info, "source": self.cfg.camera.source},
            "registry_errors": dict(self.registry.errors),
            "config_dirty": self._dirty,
            "config_path": str(self.config_path) if self.config_path else None,
        }

    def annotated_jpeg(self, quality: Optional[int] = None) -> Optional[bytes]:
        frame, snap = self.frame, self.snapshot
        if frame is None:
            return None
        img = frame.copy()
        world_ids = {t.id for t in self.cfg.world.tags}
        robot_ids = {r.tag_id: r.name for r in self.cfg.robots}
        thick = max(1, img.shape[1] // 600)
        for d in snap.detections:
            color = (0, 200, 0) if d.id in world_ids else (255, 140, 0) if d.id in robot_ids else (0, 0, 255)
            cv2.polylines(img, [d.corners.astype(np.int32)], True, color, thick, cv2.LINE_AA)
            c = d.center.astype(int)
            label = robot_ids.get(d.id, str(d.id))
            cv2.putText(img, label, (c[0] + 6, c[1] - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45 * thick, color, thick, cv2.LINE_AA)
        if snap.H is not None:
            Hi = np.linalg.inv(snap.H)
            if len(snap.bounds) >= 3:
                pts = apply_h(Hi, np.array(snap.bounds)).astype(np.int32)
                cv2.polylines(img, [pts], True, (0, 220, 220), thick, cv2.LINE_AA)
            # logical grid (10 divisions)
            L = self.logical
            for i in range(1, 10):
                for a, b in (((L.cfg.width * i / 10, 0), (L.cfg.width * i / 10, L.cfg.height)),
                             ((0, L.cfg.height * i / 10), (L.cfg.width, L.cfg.height * i / 10))):
                    pa, pb = L.point_to_physical(*a), L.point_to_physical(*b)
                    q = apply_h(Hi, np.array([pa, pb])).astype(int)
                    cv2.line(img, tuple(q[0]), tuple(q[1]), (200, 200, 120), 1, cv2.LINE_AA)
            for name, o in snap.robots.items():
                if not o.visible:
                    continue
                L_arrow = 0.08
                p = apply_h(Hi, np.array([[o.x, o.y], [o.x + L_arrow * math.cos(o.theta), o.y + L_arrow * math.sin(o.theta)]])).astype(int)
                cv2.arrowedLine(img, tuple(p[0]), tuple(p[1]), (255, 0, 255), thick + 1, cv2.LINE_AA, tipLength=0.3)
                rt = self.manager.robots.get(name)
                if rt and rt.request and rt.status == "actif":
                    h = rt.request.heading
                    q = apply_h(Hi, np.array([[o.x, o.y], [o.x + L_arrow * math.cos(h), o.y + L_arrow * math.sin(h)]])).astype(int)
                    cv2.arrowedLine(img, tuple(q[0]), tuple(q[1]), (0, 255, 255), thick, cv2.LINE_AA, tipLength=0.3)
        txt = f"{snap.fps:4.1f} fps  H:{'ok' if snap.H is not None else '--'}"
        if snap.reprojection_error is not None:
            txt += f" err {snap.reprojection_error * 1000:.1f}mm"
        if self.manager.estop:
            txt += "  ARRET D'URGENCE"
        cv2.putText(img, txt, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, txt, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(quality or self.cfg.web.jpeg_quality)])
        return buf.tobytes() if ok else None
