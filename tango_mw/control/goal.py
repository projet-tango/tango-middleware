"""Go-to-pose: drive a robot to a position (and optionally a heading), then stop.

Used to place the robots before a GAMA run, so nobody has to put them at exact spots by
hand: GAMA (or the UI) sends ``{"robot": "robot1", "goto": {"x": 20, "y": 20, "heading": 0}}``,
or ``{"place": "home"}`` to bring every robot to its configured home, one after the other.

The motion is built in (it does not use the robot's controller) and goes through the
robot's calibration like any other command: turn towards the target, drive in a straight
line while correcting the heading, then turn to the final heading. A robot waits while
another robot stands in its way, and gives up after ``goto.timeout`` seconds.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional

from ..config import GotoConfig
from ..frames import wrap_angle
from .base import BodyCommand, RobotState

ACTIVE = ("queued", "moving", "turning", "waiting")


@dataclass
class Goal:
    x: float  # physical frame (m)
    y: float
    heading: Optional[float] = None  # rad, ccw; None = any final heading
    source: str = ""
    status: str = "moving"  # queued | moving | turning | waiting | arrived | failed | cancelled
    message: str = ""
    started: float = field(default_factory=time.time)
    distance: float = math.inf
    reached: bool = False  # position reached: only the final heading is left (hysteresis)
    ended: float = 0.0

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    def describe(self) -> Dict[str, Any]:
        return {"x": self.x, "y": self.y, "heading": self.heading, "status": self.status,
                "message": self.message, "distance": None if math.isinf(self.distance) else self.distance,
                "source": self.source, "elapsed": (self.ended or time.time()) - self.started}


def drive_to(goal: Goal, state: RobotState, cfg: GotoConfig, max_linear: float,
             others: Iterable[RobotState] = ()) -> Optional[BodyCommand]:
    """One control step. Returns None once the goal is reached (status set to 'arrived')."""
    dx, dy = goal.x - state.x, goal.y - state.y
    goal.distance = math.hypot(dx, dy)
    speed = max(0.02, cfg.speed_fraction * max_linear)
    # Hysteresis: once the position is reached, do not chase it again for a centimetre of drift
    # while turning in place (that would alternate between two turns forever).
    if goal.distance <= cfg.position_tolerance:
        goal.reached = True
    elif goal.distance > 3 * cfg.position_tolerance:
        goal.reached = False
    if not goal.reached:
        bearing = math.atan2(dy, dx)
        err = wrap_angle(bearing - state.theta)
        # yield to a robot standing in the way (within clearance, in front of us)
        for o in others:
            ox, oy = o.x - state.x, o.y - state.y
            d = math.hypot(ox, oy)
            if d < cfg.clearance and abs(wrap_angle(math.atan2(oy, ox) - state.theta)) < math.radians(60):
                goal.status, goal.message = "waiting", f"{o.name} in the way"
                return BodyCommand()
        goal.message = ""
        w = max(-cfg.max_turn_rate, min(cfg.max_turn_rate, cfg.k_heading * err))
        if abs(err) > math.radians(35):  # turn in place first
            goal.status = "turning"
            return BodyCommand(linear=0.0, angular=w)
        goal.status = "moving"
        # slow down when close, and when not facing the target
        v = min(speed, cfg.k_distance * goal.distance) * max(0.0, math.cos(err))
        return BodyCommand(linear=max(v, 0.02), angular=w)
    if goal.heading is not None:
        err = wrap_angle(goal.heading - state.theta)
        if abs(err) > math.radians(cfg.heading_tolerance):
            goal.status = "turning"
            w = max(-cfg.max_turn_rate, min(cfg.max_turn_rate, cfg.k_heading * err))
            # never below the turn rate that actually moves the robot
            return BodyCommand(angular=math.copysign(max(abs(w), 0.3), w))
    goal.status, goal.message = "arrived", ""
    return None
