"""Example user controller: heading following OR going to a point.

If GAMA adds {"goal_x": .., "goal_y": ..} (in metres, physical frame) to its
JSON request, the robot goes to that point; otherwise it follows speed/heading.
Edit this file: it is hot-reloaded as soon as it is saved.
"""
import math

from tango_mw.control import BodyCommand, Controller, Param


class GoToGoal(Controller):
    name = "go_to_goal"
    description = "Follows the requested heading, or goes to (goal_x, goal_y) when present in the request"
    params = {
        "k_heading": Param(3.0, min=0.0, max=20.0, step=0.1, description="Gain on the heading error"),
        "k_dist": Param(1.0, min=0.0, max=10.0, step=0.1, description="Distance -> speed gain (goal mode)"),
        "tolerance": Param(0.02, min=0.0, max=0.5, description="Arrival distance (m)"),
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
