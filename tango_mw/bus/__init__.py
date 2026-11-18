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
                "rclpy introuvable : sourcez votre installation ROS 2 (ex: `source /opt/ros/jazzy/setup.bash` "
                "ou `call C:\\dev\\ros2\\local_setup.bat`) avant de lancer le middleware"
            )
        log.warning("rclpy indisponible : utilisation du bus loopback (aucune communication ROS)")
    return LoopbackBus(cfg)


__all__ = ["Bus", "LoopbackBus", "make_bus"]
