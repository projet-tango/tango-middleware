"""Exemple de contrôleur utilisateur : suivi de cap OU ralliement d'un point.

Si GAMA ajoute {"goal_x": .., "goal_y": ..} (en mètres, repère physique) dans
sa requête JSON, le robot rejoint ce point ; sinon il suit vitesse/heading.
Modifiez ce fichier : il est rechargé à chaud dès l'enregistrement.
"""
import math

from tango_mw.control import BodyCommand, Controller, Param


class GoToGoal(Controller):
    name = "go_to_goal"
    description = "Suit le heading demandé, ou rallie (goal_x, goal_y) si présents dans la requête"
    params = {
        "k_heading": Param(3.0, min=0.0, max=20.0, step=0.1, description="Gain sur l'erreur de cap"),
        "k_dist": Param(1.0, min=0.0, max=10.0, step=0.1, description="Gain distance -> vitesse (mode goal)"),
        "tolerance": Param(0.02, min=0.0, max=0.5, description="Distance d'arrivée (m)"),
    }

    def compute(self, state, request, dt):
        speed, heading = request.speed, request.heading
        gx, gy = request.extra.get("goal_x"), request.extra.get("goal_y")
        if gx is not None and gy is not None:
            dx, dy = float(gx) - state.x, float(gy) - state.y
            dist = math.hypot(dx, dy)
            if dist < self.p["tolerance"]:
                return BodyCommand()
            heading = math.atan2(dy, dx)
            speed = min(abs(request.speed) or 0.2, self.p["k_dist"] * dist)
        err = math.atan2(math.sin(heading - state.theta), math.cos(heading - state.theta))
        return BodyCommand(
            linear=speed * max(0.0, math.cos(err)),
            angular=self.p["k_heading"] * err,
        )
