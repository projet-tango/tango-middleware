"""Fast deployment helpers: scan visible tags, propose the world frame, discover
robots on ROS and identify each robot's tag by making it move.

Nothing here modifies the configuration by itself: helpers return *proposals*
that the UI writes into the editable draft, so the manual path always remains.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .config import AppConfig, RobotConfig
from .frames import apply_h, wrap_angle
from .vision.tracker import RobotObservation, WorldSnapshot

# --------------------------------------------------------------------------- scan


def scan_tags(app, duration: float = 1.0) -> Dict[str, Any]:
    """Accumulate detections over `duration` seconds for stable results."""
    stats: Dict[int, Dict[str, Any]] = {}
    frames = 0
    last_seq = -1
    t_end = time.time() + max(0.1, duration)
    while time.time() < t_end:
        if app.frame_seq != last_seq:
            last_seq = app.frame_seq
            snap: WorldSnapshot = app.snapshot
            frames += 1
            for d in snap.detections:
                s = stats.setdefault(d.id, {"n": 0, "centers": [], "sides": [], "angles": []})
                s["n"] += 1
                s["centers"].append(d.center)
                s["sides"].append(float(np.mean(np.linalg.norm(np.roll(d.corners, -1, 0) - d.corners, axis=1))))
                v = d.top_mid - d.center
                s["angles"].append(math.atan2(v[1], v[0]))
        time.sleep(0.02)
    cfg: AppConfig = app.cfg
    world_ids = {t.id for t in cfg.world.tags}
    robots = {r.tag_id: r.name for r in cfg.robots}
    snap = app.snapshot
    H = snap.H
    tags = []
    for tid, s in sorted(stats.items()):
        c = np.mean(s["centers"], axis=0)
        row = {
            "id": tid,
            "seen": s["n"] / max(frames, 1),
            "u": float(c[0]),
            "v": float(c[1]),
            "side_px": float(np.mean(s["sides"])),
            "image_angle": math.degrees(math.atan2(np.mean(np.sin(s["angles"])), np.mean(np.cos(s["angles"])))),
            "role": "world" if tid in world_ids else ("robot" if tid in robots else "unknown"),
            "robot": robots.get(tid),
        }
        if H is not None:
            x, y = apply_h(H, app.tracker._undistort(np.array([c])))[0]
            row["world"] = [float(x), float(y)]
        tags.append(row)
    return {"frames": frames, "tags": tags, "homography_valid": H is not None, "image_size": list(snap.image_size)}


# --------------------------------------------------------------------------- world


def propose_world_corners(tags: List[Dict[str, Any]], width: float, height: float,
                          rotation: int = 0, mirror: bool = False,
                          ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """Pick the 4 tags closest to the image corners and give them world coordinates.

    Assumes the camera looks down with image "up" ~ world +y. `rotation`
    (0/90/180/270) and `mirror` fix other camera mountings.
    """
    cand = [t for t in tags if (ids is None or t["id"] in ids)]
    if ids is None:
        cand = [t for t in cand if t["role"] != "robot"]
    if len(cand) < 4:
        raise ValueError(f"au moins 4 tags (hors robots) doivent être visibles, {len(cand)} trouvé(s)")
    pts = {t["id"]: (t["u"], t["v"]) for t in cand}
    pick = [
        min(pts, key=lambda i: pts[i][0] + pts[i][1]),   # image top-left
        max(pts, key=lambda i: pts[i][0] - pts[i][1]),   # image top-right
        max(pts, key=lambda i: pts[i][0] + pts[i][1]),   # image bottom-right
        min(pts, key=lambda i: pts[i][0] - pts[i][1]),   # image bottom-left
    ]
    if len(set(pick)) != 4:
        raise ValueError("impossible d'identifier 4 coins distincts (tags trop alignés ?)")
    world = [(0.0, height), (width, height), (width, 0.0), (0.0, 0.0)]
    k = (int(rotation) // 90) % 4
    world = world[k:] + world[:k]
    if mirror:
        world = [(width - x, y) for x, y in world]
    return [{"id": i, "x": round(w[0], 6), "y": round(w[1], 6)} for i, w in zip(pick, world)]


def measure_tags(scan: Dict[str, Any], ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """World positions of scanned tags (needs a valid homography)."""
    if not scan.get("homography_valid"):
        raise ValueError("homographie invalide : configurez d'abord au moins 4 tags de monde")
    out = []
    for t in scan["tags"]:
        if "world" not in t:
            continue
        if ids is not None and t["id"] not in ids:
            continue
        if ids is None and t["role"] != "unknown":
            continue
        out.append({"id": t["id"], "x": round(t["world"][0], 4), "y": round(t["world"][1], 4)})
    return out


# --------------------------------------------------------------------------- ROS discovery


def discover_robots(network: Dict[str, Any], template: str, cfg: AppConfig) -> Dict[str, Any]:
    """Find robot names from ROS topics matching the output template (e.g. /{name}/cmd_vel)."""
    if not network.get("available"):
        return {"available": False, "reason": network.get("reason") or "ROS 2 indisponible", "robots": []}
    if "{name}" not in template:
        raise ValueError("le modèle de topic doit contenir {name}")
    pre, post = template.split("{name}", 1)
    rx = re.compile("^" + re.escape(pre) + r"([A-Za-z][A-Za-z0-9_]*)" + re.escape(post) + "$")
    known = {r.name for r in cfg.robots}
    found = []
    for t in network.get("topics", []):
        m = rx.match(t["name"])
        if not m:
            continue
        found.append({
            "name": m.group(1),
            "topic": t["name"],
            "types": t["types"],
            "subscribers": t["subscribers"],
            "publishers": t["publishers"],
            "configured": m.group(1) in known,
        })
    return {"available": True, "robots": found}


def next_unassigned_tag(cfg_robots: List[Dict[str, Any]]) -> int:
    used = [r["tag_id"] for r in cfg_robots if r["tag_id"] < 0]
    return min(used + [0]) - 1


# --------------------------------------------------------------------------- identification


class IdentifyJob:
    """Moves one robot (spin in place by default) and finds which tag moved.

    Works in image space, so it needs neither a homography nor a correct tag_id.
    """

    kind = "identify"
    procedure = "identify"

    def __init__(self, robot: RobotConfig, exclude_ids: List[int], options: Optional[Dict[str, Any]] = None):
        from .control.robot_model import raw_primitive, raw_stop

        self._prim, self._stop = raw_primitive, raw_stop
        self.robot = robot.name
        self.output_type = robot.output.type
        o = {"motion": "turn", "raw": None, "duration": 1.2, "pause": 0.4,
             "min_rotation_deg": 20.0, "min_displacement_px": 12.0}
        o.update(options or {})
        if o["raw"] is None:
            o["raw"] = (1.0 if self.output_type != "wheels" else 0.15) if o["motion"] == "turn" else 0.15
        self.opt = o
        self.exclude = set(exclude_ids)
        self.status, self.phase, self.error = "running", "start", None
        self.result: Dict[str, Any] = {}
        self.log: List[str] = []
        self._t0: Optional[float] = None
        self._before: Dict[int, List[Tuple[float, float, float]]] = {}
        self._after: Dict[int, List[Tuple[float, float, float]]] = {}

    def _record(self, store, snap: Optional[WorldSnapshot]) -> None:
        if snap is None:
            return
        for d in snap.detections:
            if d.id in self.exclude:
                continue
            v = d.top_mid - d.center
            store.setdefault(d.id, []).append((float(d.center[0]), float(d.center[1]), math.atan2(v[1], v[0])))

    def step(self, now: float, obs: RobotObservation, snap: Optional[WorldSnapshot] = None) -> Dict[str, Any]:
        if self.status != "running":
            return self._stop(self.output_type)
        if self._t0 is None:
            self._t0 = now
        t = now - self._t0
        p, d = self.opt["pause"], self.opt["duration"]
        u = float(self.opt["raw"])
        kind = "turn" if self.opt["motion"] == "turn" else "forward"
        if t < p:
            self.phase = "référence"
            self._record(self._before, snap)
            return self._stop(self.output_type)
        if t < p + d:
            self.phase = "mouvement"
            return self._prim(self.output_type, kind, u)
        if t < 2 * p + d:
            self.phase = "mesure"
            if t > p + d + p / 2:
                self._record(self._after, snap)
            return self._stop(self.output_type)
        if t < 2 * p + 2 * d:
            if self.phase != "retour":
                self._decide()
            self.phase = "retour"
            return self._prim(self.output_type, kind, -u)
        if self.status == "running" and self.phase == "retour":
            self.status = "done" if "tag_id" in self.result else "failed"
            self.phase = "done"
        return self._stop(self.output_type)

    def _decide(self) -> None:
        scores = []
        for tid, before in self._before.items():
            after = self._after.get(tid)
            if not after or len(before) < 2:
                continue
            b, a = np.array(before), np.array(after)
            if self.opt["motion"] == "turn":
                rb = math.atan2(np.mean(np.sin(b[:, 2])), np.mean(np.cos(b[:, 2])))
                ra = math.atan2(np.mean(np.sin(a[:, 2])), np.mean(np.cos(a[:, 2])))
                score = abs(math.degrees(wrap_angle(ra - rb)))
                thr = self.opt["min_rotation_deg"]
            else:
                score = float(np.linalg.norm(a[:, :2].mean(0) - b[:, :2].mean(0)))
                thr = self.opt["min_displacement_px"]
            scores.append((score, tid))
        scores.sort(reverse=True)
        self.result["scores"] = {str(t): round(s, 2) for s, t in scores[:6]}
        unit = "°" if self.opt["motion"] == "turn" else " px"
        self.log.append("mouvements mesurés: " + (", ".join(f"#{t}: {s:.1f}{unit}" for s, t in scores[:6]) or "aucun tag"))
        if not scores or scores[0][0] < thr:
            self.error = "aucun tag n'a bougé : robot éteint, mauvais topic, ou commande trop faible (option raw)"
            self.log.append("échec: " + self.error)
            return
        if len(scores) > 1 and scores[1][0] > max(thr, scores[0][0] / 3):
            self.error = f"ambigu : #{scores[0][1]} et #{scores[1][1]} ont tous deux bougé"
            self.log.append("échec: " + self.error)
            return
        self.result["tag_id"] = scores[0][1]
        self.log.append(f"tag identifié: #{scores[0][1]}")

    def cancel(self) -> None:
        if self.status == "running":
            self.status = "cancelled"
            self.log.append("annulé")

    def fail(self, msg: str) -> None:
        self.status, self.error = "failed", msg
        self.log.append("échec: " + msg)

    def describe(self) -> Dict[str, Any]:
        return {"robot": self.robot, "procedure": "identify", "status": self.status, "phase": self.phase,
                "result": self.result, "error": self.error, "log": self.log[-20:]}
