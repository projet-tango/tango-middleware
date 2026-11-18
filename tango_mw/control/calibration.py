"""Automatic per-robot calibration procedures driven by camera tracking.

Each procedure sends raw open-loop commands to one robot, records its tag
trajectory, and fits the parameters of the robot model:

* ``heading_offset`` : angle between the tag "up" direction and the direction of travel.
* ``linear``         : raw-command -> speed gain + deadband (forward/backward runs),
                       plus straight-line angular drift.
* ``angular``        : raw-command -> rotation-rate gain + deadband (spins both ways).
                       For differential 'wheels' outputs this yields the effective wheel base.
* ``full``           : all of the above, in that order.

Runs alternate directions so the robot ends roughly where it started.
"""
from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Dict, Generator, List, Optional, Tuple

import numpy as np

from ..config import CalibrationData, RobotConfig
from ..frames import wrap_angle
from ..vision.tracker import RobotObservation
from .robot_model import raw_primitive, raw_stop

PROCEDURES = {
    "heading_offset": "Décalage angulaire du tag (avance/recule)",
    "linear": "Gain + zone morte en vitesse linéaire, dérive en ligne droite",
    "angular": "Gain + zone morte en rotation (ou entraxe pour 'wheels')",
    "full": "Les trois, dans l'ordre",
}

DEFAULT_OPTIONS: Dict[str, Any] = {
    "forward_raw": 0.2,
    "linear_levels": [0.1, 0.2, 0.3],
    "angular_levels": [0.5, 1.0, 2.0],
    "duration": 1.5,
    "pause": 0.5,
    "settle": 0.3,
    "lost_timeout": 0.7,
}

Sample = Tuple[float, float, float, float]  # t, x, y, tag_theta


def _fit_line(x: List[float], y: List[float]) -> Tuple[float, float]:
    if len(x) == 1:
        return (y[0] / x[0] if abs(x[0]) > 1e-9 else 0.0), 0.0
    a, b = np.polyfit(np.asarray(x), np.asarray(y), 1)
    return float(a), float(b)


def _circular_mean(angles: List[float]) -> float:
    return math.atan2(sum(math.sin(a) for a in angles), sum(math.cos(a) for a in angles))


class CalibrationError(RuntimeError):
    pass


class CalibrationJob:
    kind = "calibration"

    def __init__(self, robot: RobotConfig, procedure: str, options: Optional[Dict[str, Any]] = None):
        if procedure not in PROCEDURES:
            raise ValueError(f"procédure inconnue: {procedure}")
        self.robot = robot.name
        self.output_type = robot.output.type
        self.cal = robot.calibration.model_copy()
        self.procedure = procedure
        self.opt = {**DEFAULT_OPTIONS, **(options or {})}
        self.status = "running"
        self.phase = "start"
        self.log: List[str] = []
        self.result: Dict[str, Any] = {}
        self.error: Optional[str] = None
        self.now = 0.0
        self.obs: Optional[RobotObservation] = None
        self._gen = self._run()
        self._last_seen = None

    # ------------------------------------------------------------------ driving
    def step(self, now: float, obs: RobotObservation, snap: Any = None) -> Dict[str, Any]:
        """Advance the procedure; returns the raw command to send."""
        if self.status != "running":
            return raw_stop(self.output_type)
        self.now, self.obs = now, obs
        if obs.visible:
            self._last_seen = now
        elif self._last_seen is None:
            self._last_seen = now
        if now - self._last_seen > self.opt["lost_timeout"]:
            self.fail("tag du robot perdu pendant la calibration")
            return raw_stop(self.output_type)
        try:
            return next(self._gen)
        except StopIteration:
            self.status = "done"
            self.phase = "done"
            self.result["calibrated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
            self._say("terminé: " + ", ".join(f"{k}={v:.4g}" for k, v in self.result.items() if isinstance(v, float)))
        except CalibrationError as exc:
            self.fail(str(exc))
        except Exception as exc:  # pragma: no cover
            self.fail(f"erreur interne: {exc!r}")
        return raw_stop(self.output_type)

    def cancel(self) -> None:
        if self.status == "running":
            self.status = "cancelled"
            self._say("annulé")

    def fail(self, msg: str) -> None:
        self.status = "failed"
        self.error = msg
        self._say("échec: " + msg)

    def describe(self) -> dict:
        return {
            "robot": self.robot,
            "procedure": self.procedure,
            "status": self.status,
            "phase": self.phase,
            "result": self.result,
            "error": self.error,
            "log": self.log[-20:],
        }

    def _say(self, msg: str) -> None:
        self.log.append(msg)

    # ------------------------------------------------------------------ primitives
    def _hold(self, raw: Dict[str, Any], duration: float, record: Optional[List[Sample]] = None) -> Generator:
        t0 = None
        while True:
            if t0 is None:
                t0 = self.now
            if record is not None and self.obs is not None and self.obs.visible:
                record.append((self.now - t0, self.obs.tag_x, self.obs.tag_y, self.obs.tag_theta))
            if self.now - t0 >= duration:
                return
            yield raw

    def _pause(self) -> Generator:
        yield from self._hold(raw_stop(self.output_type), self.opt["pause"])

    def _trim(self, rec: List[Sample]) -> List[Sample]:
        out = [s for s in rec if s[0] >= self.opt["settle"]]
        if len(out) < 4:
            raise CalibrationError("pas assez de mesures (robot invisible ou durée trop courte)")
        return out

    @staticmethod
    def _velocity(rec: List[Sample]) -> Tuple[float, float, float]:
        """(vx, vy, omega) by least squares over a recording."""
        t = np.array([s[0] for s in rec])
        vx = np.polyfit(t, [s[1] for s in rec], 1)[0]
        vy = np.polyfit(t, [s[2] for s in rec], 1)[0]
        th = np.unwrap([s[3] for s in rec])
        w = np.polyfit(t, th, 1)[0]
        return float(vx), float(vy), float(w)

    def _robot_heading(self, tag_theta: float) -> float:
        return wrap_angle(tag_theta + self.cal.heading_offset)

    # ------------------------------------------------------------------ procedures
    def _run(self) -> Generator:
        steps = ["heading_offset", "linear", "angular"] if self.procedure == "full" else [self.procedure]
        yield from self._pause()
        for s in steps:
            yield from getattr(self, "_proc_" + s)()
        yield raw_stop(self.output_type)

    def _proc_heading_offset(self) -> Generator:
        self.phase = "heading_offset"
        u = float(self.opt["forward_raw"])
        estimates = []
        for sign in (1.0, -1.0):
            rec: List[Sample] = []
            yield from self._hold(raw_primitive(self.output_type, "forward", sign * u), self.opt["duration"], rec)
            yield from self._pause()
            rec = self._trim(rec)
            dx, dy = rec[-1][1] - rec[0][1], rec[-1][2] - rec[0][2]
            if math.hypot(dx, dy) < 0.03:
                raise CalibrationError(f"le robot n'a pas bougé (commande {sign * u:+.3g}), augmentez forward_raw")
            motion = math.atan2(dy, dx) if sign > 0 else math.atan2(-dy, -dx)
            estimates.append(wrap_angle(motion - _circular_mean([s[3] for s in rec])))
        off = _circular_mean(estimates)
        self.cal.heading_offset = off
        self.result["heading_offset"] = off
        self._say(f"heading_offset = {math.degrees(off):.1f}°")

    def _proc_linear(self) -> Generator:
        self.phase = "linear"
        us, speeds, drifts = [], [], []
        for u in self.opt["linear_levels"]:
            for sign in (1.0, -1.0):
                rec: List[Sample] = []
                yield from self._hold(raw_primitive(self.output_type, "forward", sign * u), self.opt["duration"], rec)
                yield from self._pause()
                rec = self._trim(rec)
                vx, vy, w = self._velocity(rec)
                head = self._robot_heading(_circular_mean([s[3] for s in rec]))
                v = vx * math.cos(head) + vy * math.sin(head)  # signed forward speed
                self._say(f"u={sign * u:+.3g} -> v={v:+.3f} m/s, ω={w:+.3f} rad/s")
                if abs(v) > 0.01:
                    us.append(abs(u))
                    speeds.append(abs(v))
                    drifts.append(w / v)
                    if v * sign < 0:
                        self._say("attention: le robot recule pour une commande positive (invert_linear ?)")
        if not us:
            raise CalibrationError("aucun mouvement détecté, augmentez linear_levels")
        k, b = _fit_line(us, speeds)
        if k <= 1e-6:
            raise CalibrationError("ajustement invalide (gain nul ou négatif)")
        dead = max(0.0, -b / k)
        drift = float(np.median(drifts))
        if self.output_type == "wheels":
            self.result.update(wheel_gain=1.0 / k, wheel_deadband=dead)
            self.cal.wheel_gain, self.cal.wheel_deadband = 1.0 / k, dead
        else:
            self.result.update(linear_gain=1.0 / k, linear_deadband=dead)
            self.cal.linear_gain, self.cal.linear_deadband = 1.0 / k, dead
        self.result["drift_per_meter"] = drift
        self.cal.drift_per_meter = drift
        self._k_lin = k

    def _proc_angular(self) -> Generator:
        self.phase = "angular"
        us, rates = [], []
        for u in self.opt["angular_levels"]:
            for sign in (1.0, -1.0):
                rec: List[Sample] = []
                yield from self._hold(raw_primitive(self.output_type, "turn", sign * u), self.opt["duration"], rec)
                yield from self._pause()
                rec = self._trim(rec)
                _, _, w = self._velocity(rec)
                self._say(f"u={sign * u:+.3g} -> ω={w:+.3f} rad/s")
                if abs(w) > 0.05:
                    us.append(abs(u))
                    rates.append(abs(w))
                    if w * sign < 0:
                        self._say("attention: rotation inversée (invert_angular ?)")
        if not us:
            raise CalibrationError("aucune rotation détectée, augmentez angular_levels")
        k, b = _fit_line(us, rates)
        if k <= 1e-6:
            raise CalibrationError("ajustement invalide (gain nul ou négatif)")
        dead = max(0.0, -b / k)
        if self.output_type == "wheels":
            k_lin = getattr(self, "_k_lin", 1.0 / max(self.cal.wheel_gain, 1e-9))
            base = 2.0 * k_lin / k
            self.result["wheel_base"] = base
            self.cal.wheel_base = base
        else:
            self.result.update(angular_gain=1.0 / k, angular_deadband=dead)
            self.cal.angular_gain, self.cal.angular_deadband = 1.0 / k, dead


def apply_result(cal: CalibrationData, result: Dict[str, Any]) -> CalibrationData:
    data = cal.model_dump()
    data.update({k: v for k, v in result.items() if k in data})
    return CalibrationData.model_validate(data)
