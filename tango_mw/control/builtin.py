"""Built-in controllers."""
from __future__ import annotations

import math

from ..frames import wrap_angle
from .base import BodyCommand, Controller, Param, Request, RobotState


def _clamp(v: float, lim: float) -> float:
    return max(-lim, min(lim, v))


def _speed_scale(mode: str, err: float, threshold: float) -> float:
    if abs(err) > threshold:
        return 0.0
    if mode == "cos":
        return max(0.0, math.cos(err))
    if mode == "linear":
        return max(0.0, 1.0 - abs(err) / max(threshold, 1e-6))
    return 1.0


class Proportional(Controller):
    """The simplest: ω = k·heading error, v = requested speed × cos(error)."""

    name = "proportional"
    params = {
        "k": Param(2.0, min=0.0, max=20.0, step=0.1, description="Proportional gain on the heading"),
        "max_angular": Param(2.0, min=0.0, max=20.0, description="Saturation ω (rad/s)"),
    }

    def compute(self, state: RobotState, request: Request, dt: float) -> BodyCommand:
        err = request.heading_error(state.theta)
        return BodyCommand(
            linear=request.speed * max(0.0, math.cos(err)),
            angular=_clamp(self.p["k"] * err, self.p["max_angular"]),
        )


class HeadingPID(Controller):
    """PID on the heading error, slows down when the error is large,
    turns in place above a threshold. Optional automatic reverse."""

    name = "heading_pid"
    params = {
        "kp": Param(2.5, min=0.0, max=50.0, step=0.1, description="Proportional gain"),
        "ki": Param(0.0, min=0.0, max=50.0, step=0.05, description="Integral gain"),
        "kd": Param(0.1, min=0.0, max=10.0, step=0.01, description="Derivative gain (on the measurement)"),
        "i_limit": Param(1.0, min=0.0, max=100.0, description="Anti-windup: max |integral|"),
        "max_angular": Param(3.0, min=0.0, max=30.0, description="Saturation ω (rad/s)"),
        "turn_in_place": Param(1.2, min=0.0, max=math.pi, description="Error (rad) above which v = 0"),
        "speed_scaling": Param("cos", choices=["cos", "linear", "none"], description="How v is reduced with the error"),
        "allow_reverse": Param(False, description="Back up instead of turning around when the target is behind"),
    }

    def reset(self) -> None:
        self.i = 0.0
        self.err = 0.0

    def _heading_terms(self, state: RobotState, request: Request):
        heading, sign = request.heading, 1.0
        err = wrap_angle(heading - state.theta)
        if self.p["allow_reverse"] and abs(err) > math.pi / 2:
            err = wrap_angle(err + math.pi)
            sign = -1.0
        return err, sign

    def compute(self, state: RobotState, request: Request, dt: float) -> BodyCommand:
        err, sign = self._heading_terms(state, request)
        self.err = err
        if self.p["ki"] > 0:
            self.i = _clamp(self.i + err * dt, self.p["i_limit"])
        else:
            self.i = 0.0
        w = self.p["kp"] * err + self.p["ki"] * self.i - self.p["kd"] * state.omega
        v = sign * request.speed * _speed_scale(self.p["speed_scaling"], err, self.p["turn_in_place"])
        return BodyCommand(linear=v, angular=_clamp(w, self.p["max_angular"]))

    def debug(self):
        return {"heading_error": self.err, "integral": self.i}


class SpeedHeadingPID(HeadingPID):
    """HeadingPID + PI speed loop on the speed measured by the camera
    (feed-forward of the requested speed + correction)."""

    name = "speed_heading_pid"
    params = {
        **HeadingPID.params,
        "kp_v": Param(0.5, min=0.0, max=10.0, step=0.05, description="Speed P gain"),
        "ki_v": Param(0.5, min=0.0, max=10.0, step=0.05, description="Speed I gain"),
        "iv_limit": Param(0.3, min=0.0, max=5.0, description="Speed anti-windup (m/s)"),
    }

    def reset(self) -> None:
        super().reset()
        self.iv = 0.0

    def compute(self, state: RobotState, request: Request, dt: float) -> BodyCommand:
        cmd = super().compute(state, request, dt)
        target = cmd.linear
        if abs(target) < 1e-6:
            self.iv = 0.0
            return cmd
        ev = target - state.forward_speed
        self.iv = _clamp(self.iv + ev * dt, self.p["iv_limit"])
        cmd.linear = target + self.p["kp_v"] * ev + self.p["ki_v"] * self.iv
        if cmd.linear * target < 0:  # never invert direction through the correction
            cmd.linear = 0.0
        return cmd

    def debug(self):
        return {**super().debug(), "speed_integral": self.iv}


class Holonomic(Controller):
    """Omnidirectional robots: the requested world velocity is projected into the
    robot frame (vx, vy); the heading is held (or aligned with the motion)."""

    name = "holonomic"
    params = {
        "orientation": Param("hold", choices=["hold", "face_motion"], description="Keep the initial heading or align with the motion"),
        "k_theta": Param(2.0, min=0.0, max=20.0, description="Gain on the orientation"),
        "max_angular": Param(2.0, min=0.0, max=20.0),
    }

    def reset(self) -> None:
        self.hold = None

    def compute(self, state: RobotState, request: Request, dt: float) -> BodyCommand:
        if self.hold is None:
            self.hold = state.theta
        vx_w = request.speed * math.cos(request.heading)
        vy_w = request.speed * math.sin(request.heading)
        c, s = math.cos(state.theta), math.sin(state.theta)
        target = request.heading if self.p["orientation"] == "face_motion" and abs(request.speed) > 1e-6 else self.hold
        w = _clamp(self.p["k_theta"] * wrap_angle(target - state.theta), self.p["max_angular"])
        return BodyCommand(linear=c * vx_w + s * vy_w, lateral=-s * vx_w + c * vy_w, angular=w)


class Passthrough(Controller):
    """No closed loop: sends v = requested speed and ω = the request's `angular`
    field (JSON) — useful when GAMA computes the commands itself."""

    name = "passthrough"
    params = {}

    def compute(self, state: RobotState, request: Request, dt: float) -> BodyCommand:
        return BodyCommand(linear=request.speed, angular=float(request.extra.get("angular", 0.0)))


BUILTIN_CONTROLLERS = [Proportional, HeadingPID, SpeedHeadingPID, Holonomic, Passthrough]
