"""Communication bus abstraction (ROS 2 or in-process loopback)."""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import AppConfig, RobotConfig

RequestCallback = Callable[[str, Dict[str, Any], str], None]  # robot, fields, source
EstopCallback = Callable[[bool], None]


def ns_topic(namespace: str, suffix: str) -> str:
    ns = "/" + namespace.strip("/") if namespace.strip("/") else ""
    return f"{ns}/{suffix.lstrip('/')}"


def output_topic(robot: RobotConfig) -> str:
    return robot.output.topic.replace("{name}", robot.name)


def parse_request_payload(text: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Accepted JSON forms on the requests topic::

        {"robot": "r1", "speed": 10, "heading": 90}
        {"requests": [{"robot": "r1", ...}, {"robot": "r2", ...}]}
        [{"robot": "r1", ...}, ...]
        {"r1": {"speed": 10, "heading": 90}, "r2": {...}}
    """
    data = json.loads(text)
    items: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(data, dict) and "requests" in data:
        data = data["requests"]
    if isinstance(data, list):
        for d in data:
            if isinstance(d, dict) and "robot" in d:
                items.append((str(d["robot"]), d))
    elif isinstance(data, dict) and "robot" in data:
        items.append((str(data["robot"]), data))
    elif isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                items.append((str(k), v))
    return items


def topic_map(cfg: AppConfig) -> List[Dict[str, str]]:
    """Every topic the middleware uses, for documentation / admin UI."""
    ns = cfg.ros.namespace
    rows = [
        {"topic": ns_topic(ns, "world/state"), "type": "std_msgs/String", "dir": "pub", "doc": "Full JSON state (world + robots, physical + logical)"},
        {"topic": ns_topic(ns, "world/homography"), "type": "std_msgs/Float64MultiArray", "dir": "pub", "doc": "H image -> physical world (3x3, row-major)"},
        {"topic": ns_topic(ns, "world/homography_logical"), "type": "std_msgs/Float64MultiArray", "dir": "pub", "doc": "H image -> GAMA world"},
        {"topic": ns_topic(ns, "world/bounds"), "type": "geometry_msgs/PolygonStamped", "dir": "pub", "doc": "World outline (physical)"},
        {"topic": ns_topic(ns, "robots/state"), "type": "std_msgs/String", "dir": "pub", "doc": "Poses of all robots (JSON)"},
        {"topic": ns_topic(ns, "status"), "type": "std_msgs/String", "dir": "pub", "doc": "Middleware health (JSON, 1 Hz)"},
        {"topic": ns_topic(ns, "requests"), "type": "std_msgs/String", "dir": "sub", "doc": "GAMA requests, JSON {robot, speed, heading}"},
        {"topic": ns_topic(ns, "estop"), "type": "std_msgs/Bool", "dir": "sub", "doc": "Global emergency stop"},
    ]
    for r in cfg.robots:
        if cfg.ros.publish_per_robot_topics:
            rows.append({"topic": ns_topic(ns, f"robots/{r.name}/pose"), "type": "geometry_msgs/PoseStamped", "dir": "pub", "doc": f"Physical pose of {r.name}"})
            rows.append({"topic": ns_topic(ns, f"robots/{r.name}/state"), "type": "std_msgs/String", "dir": "pub", "doc": f"JSON state of {r.name}"})
            rows.append({"topic": ns_topic(ns, f"robots/{r.name}/request"), "type": "std_msgs/Float64MultiArray", "dir": "sub", "doc": "[speed, heading]"})
        t = {"twist": "geometry_msgs/Twist", "wheels": "std_msgs/Float64MultiArray", "json": "std_msgs/String"}[r.output.type]
        rows.append({"topic": output_topic(r), "type": t, "dir": "pub", "doc": f"Commands for {r.name} ({r.output.type})"})
    return rows


def env_info() -> Dict[str, Optional[str]]:
    keys = ["ROS_DISTRO", "ROS_DOMAIN_ID", "RMW_IMPLEMENTATION", "ROS_LOCALHOST_ONLY",
            "ROS_AUTOMATIC_DISCOVERY_RANGE", "ROS_STATIC_PEERS", "CYCLONEDDS_URI", "FASTRTPS_DEFAULT_PROFILES_FILE"]
    return {k: os.environ.get(k) for k in keys}


class Bus:
    kind = "base"

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.on_request: Optional[RequestCallback] = None
        self.on_estop: Optional[EstopCallback] = None
        self.errors: Dict[str, str] = {}

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def sync_robots(self, robots: List[RobotConfig]) -> None: ...

    def publish_world(self, state: Dict[str, Any], H, H_logical, bounds) -> None: ...

    def publish_robot(self, name: str, state: Dict[str, Any]) -> None: ...

    def publish_status(self, status: Dict[str, Any]) -> None: ...

    def publish_command(self, robot: RobotConfig, raw: Dict[str, Any]) -> None: ...

    def network_info(self) -> Dict[str, Any]:
        return {"available": False, "env": env_info()}

    def robot_presence(self, robot: RobotConfig) -> Dict[str, Any]:
        return {"known": False}

    def requests_publishers(self) -> Optional[int]:
        return None

    # helpers for subclasses
    def _handle_request_text(self, text: str, source: str) -> None:
        try:
            items = parse_request_payload(text)
        except Exception as exc:
            self.errors["requests"] = f"JSON invalide: {exc}"
            return
        for robot, fields in items:
            if self.on_request:
                self.on_request(robot, fields, source)
