"""Per-robot calibration model: physical body command -> raw actuator command."""
from __future__ import annotations

import math
from typing import Dict

from ..config import CalibrationData
from .base import BodyCommand


def _map(v: float, gain: float, dead: float, max_raw: float) -> float:
    if abs(v) < 1e-9:
        return 0.0
    raw = math.copysign(dead + gain * abs(v), v)
    return max(-max_raw, min(max_raw, raw))


class RobotModel:
    def __init__(self, cal: CalibrationData, output_type: str):
        self.cal = cal
        self.output_type = output_type

    def to_raw(self, cmd: BodyCommand) -> Dict[str, object]:
        c = self.cal
        v = max(-c.max_linear, min(c.max_linear, cmd.linear))
        vy = max(-c.max_linear, min(c.max_linear, cmd.lateral))
        w = cmd.angular - c.drift_per_meter * v  # cancel straight-line drift
        w = max(-c.max_angular, min(c.max_angular, w))
        if c.invert_linear:
            v, vy = -v, -vy
        if c.invert_angular:
            w = -w
        out: Dict[str, object] = {"body": {"linear": v, "angular": w, "lateral": vy}}
        if self.output_type == "wheels":
            vl = v - w * c.wheel_base / 2
            vr = v + w * c.wheel_base / 2
            out["wheels"] = [
                _map(vl, c.wheel_gain, c.wheel_deadband, c.max_raw),
                _map(vr, c.wheel_gain * c.right_wheel_scale, c.wheel_deadband, c.max_raw),
            ]
        else:
            out["linear"] = _map(v, c.linear_gain, c.linear_deadband, c.max_raw)
            out["lateral"] = _map(vy, c.linear_gain, c.linear_deadband, c.max_raw)
            out["angular"] = _map(w, c.angular_gain, c.angular_deadband, c.max_raw)
        if cmd.extra:
            out["extra"] = cmd.extra
        return out


def raw_stop(output_type: str) -> Dict[str, object]:
    if output_type == "wheels":
        return {"wheels": [0.0, 0.0], "body": {"linear": 0.0, "angular": 0.0, "lateral": 0.0}}
    return {"linear": 0.0, "angular": 0.0, "lateral": 0.0, "body": {"linear": 0.0, "angular": 0.0, "lateral": 0.0}}


def raw_primitive(output_type: str, kind: str, u: float) -> Dict[str, object]:
    """Raw commands used by calibration: 'forward' or 'turn' with raw amplitude u."""
    if output_type == "wheels":
        w = [u, u] if kind == "forward" else [-u, u]
        return {"wheels": w}
    if kind == "forward":
        return {"linear": u, "angular": 0.0, "lateral": 0.0}
    return {"linear": 0.0, "angular": u, "lateral": 0.0}
