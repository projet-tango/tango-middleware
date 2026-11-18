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
    """Le plus simple : ω = k·erreur de cap, v = vitesse demandée × cos(erreur)."""

    name = "proportional"
    params = {
        "k": Param(2.0, min=0.0, max=20.0, step=0.1, description="Gain proportionnel sur le cap"),
        "max_angular": Param(2.0, min=0.0, max=20.0, description="Saturation ω (rad/s)"),
    }

    def compute(self, state: RobotState, request: Request, dt: float) -> BodyCommand:
        err = request.heading_error(state.theta)
        return BodyCommand(
            linear=request.speed * max(0.0, math.cos(err)),
            angular=_clamp(self.p["k"] * err, self.p["max_angular"]),
        )


class HeadingPID(Controller):
    """PID sur l'erreur de cap, réduction de vitesse quand l'erreur est grande,
    rotation sur place au-delà d'un seuil. Option marche arrière automatique."""

    name = "heading_pid"
    params = {
        "kp": Param(2.5, min=0.0, max=50.0, step=0.1, description="Gain proportionnel"),
        "ki": Param(0.0, min=0.0, max=50.0, step=0.05, description="Gain intégral"),
        "kd": Param(0.1, min=0.0, max=10.0, step=0.01, description="Gain dérivé (sur la mesure)"),
        "i_limit": Param(1.0, min=0.0, max=100.0, description="Anti-windup : |intégrale| max"),
        "max_angular": Param(3.0, min=0.0, max=30.0, description="Saturation ω (rad/s)"),
        "turn_in_place": Param(1.2, min=0.0, max=math.pi, description="Erreur (rad) au-delà de laquelle v = 0"),
        "speed_scaling": Param("cos", choices=["cos", "linear", "none"], description="Réduction de v selon l'erreur"),
        "allow_reverse": Param(False, description="Reculer plutôt que faire demi-tour si la cible est derrière"),
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
    """HeadingPID + boucle de vitesse PI sur la vitesse mesurée par la caméra
    (feed-forward de la vitesse demandée + correction)."""

    name = "speed_heading_pid"
    params = {
        **HeadingPID.params,
        "kp_v": Param(0.5, min=0.0, max=10.0, step=0.05, description="Gain P vitesse"),
        "ki_v": Param(0.5, min=0.0, max=10.0, step=0.05, description="Gain I vitesse"),
        "iv_limit": Param(0.3, min=0.0, max=5.0, description="Anti-windup vitesse (m/s)"),
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
    """Robots omnidirectionnels : la vitesse monde demandée est projetée dans le
    repère robot (vx, vy) ; le cap est tenu (ou aligné sur le mouvement)."""

    name = "holonomic"
    params = {
        "orientation": Param("hold", choices=["hold", "face_motion"], description="Garder le cap initial ou s'aligner sur le mouvement"),
        "k_theta": Param(2.0, min=0.0, max=20.0, description="Gain sur l'orientation"),
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
    """Aucune boucle fermée : envoie v = vitesse demandée et ω = champ `angular`
    de la requête (JSON) — utile si GAMA calcule lui-même les commandes."""

    name = "passthrough"
    params = {}

    def compute(self, state: RobotState, request: Request, dt: float) -> BodyCommand:
        return BodyCommand(linear=request.speed, angular=float(request.extra.get("angular", 0.0)))


BUILTIN_CONTROLLERS = [Proportional, HeadingPID, SpeedHeadingPID, Holonomic, Passthrough]
