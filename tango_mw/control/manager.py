"""Per-robot control loop: requests + observations -> controller -> calibration -> raw command."""
from __future__ import annotations

import logging
import math
import threading
import traceback
from typing import Any, Callable, Dict, List, Optional

from ..config import AppConfig, RobotConfig
from ..frames import point_in_polygon
from ..vision.tracker import RobotObservation, WorldSnapshot
from .base import Controller, Request, RobotState
from .calibration import CalibrationJob
from .registry import ControllerRegistry
from .robot_model import RobotModel, raw_stop

log = logging.getLogger(__name__)


class RobotRuntime:
    def __init__(self, cfg: RobotConfig):
        self.cfg = cfg
        self.controller: Optional[Controller] = None
        self.controller_error: Optional[str] = None
        self.generation = -1
        self.model = RobotModel(cfg.calibration, cfg.output.type)
        self.request: Optional[Request] = None
        self.request_source = ""
        self.job: Optional[CalibrationJob] = None
        self.last_job: Optional[dict] = None
        self.status = "idle"
        self.last_raw: Dict[str, Any] = raw_stop(cfg.output.type)
        self.last_compute = 0.0
        self.active = False  # was driving at the previous step


class ControlManager:
    def __init__(self, cfg: AppConfig, registry: ControllerRegistry,
                 emit: Callable[[RobotConfig, Dict[str, Any]], None],
                 on_job_done: Callable[[str, Any], None]):
        self.cfg = cfg
        self.registry = registry
        self.emit = emit
        self.on_job_done = on_job_done
        self._lock = threading.RLock()
        self.estop = False
        self.robots: Dict[str, RobotRuntime] = {}
        self.set_config(cfg)

    # ------------------------------------------------------------------ config
    def set_config(self, cfg: AppConfig) -> None:
        with self._lock:
            self.cfg = cfg
            new: Dict[str, RobotRuntime] = {}
            for r in cfg.robots:
                rt = self.robots.get(r.name)
                if rt is None:
                    rt = RobotRuntime(r)
                else:
                    if (rt.cfg.controller != r.controller or rt.cfg.output.type != r.output.type
                            or (rt.controller is None and rt.cfg.controller_params != r.controller_params)):
                        rt.generation = -1  # force a new controller instance
                    elif rt.controller is not None and rt.cfg.controller_params != r.controller_params:
                        try:
                            rt.controller.configure(r.controller_params)
                        except Exception as exc:
                            rt.controller_error = f"paramètres invalides: {exc}"
                    rt.cfg = r
                    rt.model = RobotModel(r.calibration, r.output.type)
                    if rt.controller is not None:
                        rt.controller.calibration = r.calibration
                new[r.name] = rt
            for name, rt in self.robots.items():
                if name not in new and rt.active:
                    self.emit(rt.cfg, raw_stop(rt.cfg.output.type))
            self.robots = new

    def _ensure_controller(self, rt: RobotRuntime) -> None:
        if rt.generation == self.registry.generation and (rt.controller is not None or rt.controller_error):
            return
        rt.generation = self.registry.generation
        try:
            cls = self.registry.get(rt.cfg.controller)
            rt.controller = cls(rt.cfg.name, rt.cfg.controller_params, rt.cfg.calibration)
            rt.controller_error = None
        except Exception as exc:
            rt.controller = None
            rt.controller_error = f"{exc}"
            log.error("Robot %s: contrôleur '%s' indisponible: %s", rt.cfg.name, rt.cfg.controller, exc)

    # ------------------------------------------------------------------ inputs
    def set_request(self, robot: str, req: Request, source: str = "") -> bool:
        with self._lock:
            rt = self.robots.get(robot)
            if rt is None:
                return False
            rt.request = req
            rt.request_source = source
            return True

    def set_estop(self, on: bool) -> None:
        with self._lock:
            if on and not self.estop:
                log.warning("ARRÊT D'URGENCE activé")
                for rt in self.robots.values():
                    if rt.job:
                        rt.job.cancel()
            self.estop = on

    def start_calibration(self, robot: str, procedure: str, options: Optional[dict] = None) -> CalibrationJob:
        with self._lock:
            rt = self.robots.get(robot)
            if rt is None:
                raise KeyError(robot)
            if self.estop:
                raise RuntimeError("arrêt d'urgence actif")
            if rt.job and rt.job.status == "running":
                raise RuntimeError("une calibration est déjà en cours")
            rt.job = CalibrationJob(rt.cfg, procedure, options)
            return rt.job

    def start_identify(self, robot: str, options: Optional[dict] = None):
        from ..deploy import IdentifyJob

        with self._lock:
            rt = self.robots.get(robot)
            if rt is None:
                raise KeyError(robot)
            if self.estop:
                raise RuntimeError("arrêt d'urgence actif")
            busy = [n for n, r in self.robots.items() if r.job and r.job.status == "running"]
            if busy:
                raise RuntimeError(f"une procédure est déjà en cours ({', '.join(busy)})")
            rt.job = IdentifyJob(rt.cfg, [t.id for t in self.cfg.world.tags], options)
            return rt.job

    def cancel_calibration(self, robot: str) -> None:
        with self._lock:
            rt = self.robots.get(robot)
            if rt and rt.job:
                rt.job.cancel()

    # ------------------------------------------------------------------ loop
    def _in_bounds(self, x: float, y: float, bounds) -> bool:
        if not bounds or len(bounds) < 3:
            return True
        return point_in_polygon(x, y, bounds, self.cfg.safety.bounds_margin)

    def step(self, now: float, snap: WorldSnapshot) -> None:
        with self._lock:
            for rt in self.robots.values():
                if not rt.cfg.enabled:
                    if rt.active:
                        self.emit(rt.cfg, raw_stop(rt.cfg.output.type))
                        rt.active = False
                    rt.status = "désactivé"
                    continue
                raw = self._step_robot(rt, now, snap)
                rt.last_raw = raw
                self.emit(rt.cfg, raw)

    def _stop(self, rt: RobotRuntime, why: str) -> Dict[str, Any]:
        rt.status = why
        if rt.active and rt.controller is not None:
            rt.controller.reset()
        rt.active = False
        return raw_stop(rt.cfg.output.type)

    def _step_robot(self, rt: RobotRuntime, now: float, snap: WorldSnapshot) -> Dict[str, Any]:
        safety = self.cfg.safety
        obs: RobotObservation = snap.robots.get(rt.cfg.name) or RobotObservation(rt.cfg.name, rt.cfg.tag_id)
        age = now - obs.last_seen if obs.ever_seen else math.inf
        inside = self._in_bounds(obs.x, obs.y, snap.bounds) if obs.ever_seen else True

        # --- calibration has priority over GAMA requests
        if rt.job is not None:
            job = rt.job
            if self.estop:
                job.cancel()
            elif job.status == "running" and job.kind != "identify" and safety.stop_out_of_bounds and not inside:
                job.fail("robot sorti de la zone")
            raw = job.step(now, obs, snap) if job.status == "running" else raw_stop(rt.cfg.output.type)
            if job.status != "running":
                rt.last_job = job.describe()
                rt.job = None
                if job.status == "done":
                    self.on_job_done(rt.cfg.name, job)
            rt.status = f"{'identification' if job.kind == 'identify' else 'calibration'} ({job.phase})"
            rt.active = False
            return raw

        if self.estop:
            return self._stop(rt, "arrêt d'urgence")
        req = rt.request
        if req is None:
            return self._stop(rt, "en attente de requête")
        if safety.request_timeout > 0 and now - req.stamp > safety.request_timeout:
            return self._stop(rt, "requête expirée")
        if req.stop:
            return self._stop(rt, "stop demandé")
        if not obs.visible and age > safety.tag_lost_timeout:
            return self._stop(rt, "tag perdu")
        if snap.H is None:
            return self._stop(rt, "homographie indisponible")
        if safety.stop_out_of_bounds and not inside:
            # only allow requests heading back towards the inside of the arena
            cx = sum(p[0] for p in snap.bounds) / len(snap.bounds)
            cy = sum(p[1] for p in snap.bounds) / len(snap.bounds)
            if math.cos(req.heading) * (cx - obs.x) + math.sin(req.heading) * (cy - obs.y) <= 0 or req.speed < 0:
                return self._stop(rt, "hors zone")

        self._ensure_controller(rt)
        if rt.controller is None:
            return self._stop(rt, "contrôleur en erreur")
        if not rt.active:
            rt.controller.reset()
            rt.last_compute = now
        dt = max(1e-3, min(0.5, now - rt.last_compute))
        rt.last_compute = now
        state = RobotState(name=rt.cfg.name, x=obs.x, y=obs.y, theta=obs.theta, vx=obs.vx, vy=obs.vy,
                           omega=obs.omega, visible=obs.visible, age=age, stamp=obs.last_seen)
        try:
            body = rt.controller.compute(state, req, dt)
        except Exception:
            rt.controller_error = traceback.format_exc(limit=4)
            log.error("Contrôleur %s (%s) a levé une exception:\n%s", rt.cfg.controller, rt.cfg.name, rt.controller_error)
            rt.controller = None
            return self._stop(rt, "contrôleur en erreur")
        rt.active = True
        rt.status = "actif"
        return rt.model.to_raw(body)

    # ------------------------------------------------------------------ status
    def describe(self) -> Dict[str, Any]:
        with self._lock:
            out = {}
            for name, rt in self.robots.items():
                req = rt.request
                out[name] = {
                    "status": rt.status,
                    "controller": rt.cfg.controller,
                    "controller_error": rt.controller_error,
                    "controller_debug": (rt.controller.debug() if rt.controller else {}),
                    "request": None if req is None else {"speed": req.speed, "heading": req.heading,
                                                         "stamp": req.stamp, "stop": req.stop, "source": rt.request_source},
                    "command": rt.last_raw,
                    "calibration_job": rt.job.describe() if rt.job else rt.last_job,
                }
            return {"estop": self.estop, "robots": out}

    def robot_names(self) -> List[str]:
        return list(self.robots)
