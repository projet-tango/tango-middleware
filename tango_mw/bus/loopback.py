"""In-process bus used without ROS (tests, offline development)."""
from __future__ import annotations

import threading
from typing import Any, Dict, List

from ..config import RobotConfig
from .base import Bus, env_info, output_topic


class LoopbackBus(Bus):
    kind = "loopback"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._lock = threading.Lock()
        self.last: Dict[str, Any] = {}
        self.commands: Dict[str, Dict[str, Any]] = {}

    def publish_world(self, state, H, H_logical, bounds):
        with self._lock:
            self.last["world"] = state

    def publish_robot(self, name, state):
        with self._lock:
            self.last[f"robot:{name}"] = state

    def publish_status(self, status):
        with self._lock:
            self.last["status"] = status

    def publish_command(self, robot: RobotConfig, raw):
        with self._lock:
            self.commands[robot.name] = raw
            self.last[output_topic(robot)] = raw

    def inject_request(self, text: str) -> None:
        self._handle_request_text(text, "loopback")

    def network_info(self):
        return {
            "available": False,
            "reason": "bus loopback (ROS 2 non utilisé)",
            "env": env_info(),
            "nodes": [],
            "topics": [],
        }
