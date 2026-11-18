from __future__ import annotations

import logging

from ..config import AppConfig
from .base import Bus
from .loopback import LoopbackBus

log = logging.getLogger(__name__)


def make_bus(cfg: AppConfig) -> Bus:
    kind = cfg.ros.bus
    if kind in ("auto", "ros2"):
        from .ros2 import Ros2Bus, ros_available

        if ros_available():
            return Ros2Bus(cfg)
        if kind == "ros2":
            raise RuntimeError(
                "rclpy not found: source your ROS 2 installation (e.g. `source /opt/ros/jazzy/setup.bash` "
                "or `call C:\\dev\\ros2\\local_setup.bat`) before starting the middleware"
            )
        log.warning("rclpy unavailable: using the loopback bus (no ROS communication)")
    return LoopbackBus(cfg)


__all__ = ["Bus", "LoopbackBus", "make_bus"]
