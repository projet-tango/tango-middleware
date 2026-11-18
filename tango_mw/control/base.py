"""Controller API.

A controller turns a *request* coming from GAMA (desired speed + heading in the
physical frame) and the *observed state* of the robot (from the camera) into a
*body command* (linear / angular / lateral velocity, physical units).

The robot's calibration model (see ``robot_model.py``) then converts that body
command into raw actuator commands, so controllers stay robot-agnostic.

Writing your own controller::

    from tango_mw.control import Controller, Param, BodyCommand

    class MyController(Controller):
        name = "my_controller"
        description = "..."
        params = {"k": Param(1.0, min=0, max=10, description="gain")}

        def compute(self, state, request, dt):
            err = request.heading_error(state.theta)
            return BodyCommand(linear=request.speed, angular=self.p["k"] * err)

Drop the file in a plugin directory (default ``controllers/``): it is loaded
and hot-reloaded automatically.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional

from ..frames import wrap_angle


@dataclass
class Param:
    default: Any
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    description: str = ""
    choices: Optional[List[Any]] = None

    def coerce(self, value: Any) -> Any:
        if value is None:
            return self.default
        d = self.default
        if isinstance(d, bool):
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on", "oui")
            value = bool(value)
        elif isinstance(d, int) and not isinstance(d, bool):
            value = int(float(value))
        elif isinstance(d, float):
            value = float(value)
        if self.choices is not None and value not in self.choices:
            raise ValueError(f"value {value!r} not in {self.choices}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if self.min is not None:
                value = max(self.min, value)
            if self.max is not None:
                value = min(self.max, value)
        return value

    def describe(self) -> dict:
        t = type(self.default).__name__
        return {
            "default": self.default,
            "type": t,
            "min": self.min,
            "max": self.max,
            "step": self.step,
            "description": self.description,
            "choices": self.choices,
        }


@dataclass
class RobotState:
    """Observed state of a robot, physical frame (m, rad, ccw)."""

    name: str
    x: float
    y: float
    theta: float
    vx: float = 0.0
    vy: float = 0.0
    omega: float = 0.0
    visible: bool = True
    age: float = 0.0  # seconds since last detection
    stamp: float = 0.0

    @property
    def speed(self) -> float:
        return math.hypot(self.vx, self.vy)

    @property
    def forward_speed(self) -> float:
        return self.vx * math.cos(self.theta) + self.vy * math.sin(self.theta)


@dataclass
class Request:
    """Request from GAMA, already converted to the physical frame."""

    speed: float = 0.0  # m/s (negative = backwards)
    heading: float = 0.0  # rad, ccw, physical frame
    stamp: float = 0.0
    stop: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)  # any additional JSON fields

    def heading_error(self, theta: float) -> float:
        return wrap_angle(self.heading - theta)


@dataclass
class BodyCommand:
    linear: float = 0.0  # m/s along robot heading
    angular: float = 0.0  # rad/s ccw
    lateral: float = 0.0  # m/s to the left (holonomic robots)
    extra: Dict[str, Any] = field(default_factory=dict)  # forwarded in json outputs


class Controller:
    """Base class for all controllers."""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    params: ClassVar[Dict[str, Param]] = {}

    def __init__(self, robot: str, params: Optional[Dict[str, Any]] = None, calibration: Any = None):
        self.robot = robot
        self.calibration = calibration
        self.p: Dict[str, Any] = {}
        self.configure(params or {})
        self.reset()

    def configure(self, params: Dict[str, Any]) -> None:
        self.p = {k: spec.coerce(params.get(k, spec.default)) for k, spec in self.params.items()}

    def reset(self) -> None:
        """Called when the robot (re)starts being controlled."""

    def compute(self, state: RobotState, request: Request, dt: float) -> BodyCommand:  # pragma: no cover
        raise NotImplementedError

    def debug(self) -> Dict[str, Any]:
        """Optional internal values exposed in the UI/JSON state."""
        return {}

    @classmethod
    def describe(cls) -> dict:
        return {
            "name": cls.name,
            "description": cls.description or (cls.__doc__ or "").strip(),
            "params": {k: v.describe() for k, v in cls.params.items()},
        }
