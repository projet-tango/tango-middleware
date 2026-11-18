"""ROS 2 bus (rclpy). Uses only standard message packages (std_msgs, geometry_msgs),
so nothing has to be compiled: any ROS 2 distribution (Humble, Jazzy, ...) on
Linux, Windows or macOS works, as does GAMA through rosbridge."""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional

from ..config import AppConfig, RobotConfig
from .base import Bus, env_info, ns_topic, output_topic

log = logging.getLogger(__name__)


def ros_available() -> bool:
    try:
        import rclpy  # noqa: F401
        from geometry_msgs.msg import Twist  # noqa: F401
        from std_msgs.msg import String  # noqa: F401

        return True
    except Exception:
        return False


class Ros2Bus(Bus):
    kind = "ros2"

    def __init__(self, cfg: AppConfig):
        super().__init__(cfg)
        import rclpy
        from geometry_msgs.msg import Point32, PolygonStamped, PoseStamped, Twist
        from std_msgs.msg import Bool, Float64MultiArray, String

        self.rclpy = rclpy
        self.M = dict(Point32=Point32, PolygonStamped=PolygonStamped, PoseStamped=PoseStamped, Twist=Twist,
                      Bool=Bool, Float64MultiArray=Float64MultiArray, String=String)
        self.ctx = None
        self.node = None
        self.executor = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.RLock()
        self._robot_pubs: Dict[str, Dict[str, Any]] = {}
        self._out_pubs: Dict[str, Any] = {}  # name -> (topic, type, publisher)
        self._graph_cache: Dict[str, Any] = {}
        self._graph_stamp = 0.0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        rclpy = self.rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor

        self.ctx = Context()
        kwargs = {"context": self.ctx}
        if self.cfg.ros.domain_id is not None:
            kwargs["domain_id"] = int(self.cfg.ros.domain_id)
        rclpy.init(**kwargs)
        self.node = rclpy.create_node(self.cfg.ros.node_name, context=self.ctx)
        M, q, ns = self.M, self.cfg.ros.qos_depth, self.cfg.ros.namespace
        n = self.node
        self.pub_world = n.create_publisher(M["String"], ns_topic(ns, "world/state"), q)
        self.pub_h = n.create_publisher(M["Float64MultiArray"], ns_topic(ns, "world/homography"), q)
        self.pub_hl = n.create_publisher(M["Float64MultiArray"], ns_topic(ns, "world/homography_logical"), q)
        self.pub_bounds = n.create_publisher(M["PolygonStamped"], ns_topic(ns, "world/bounds"), q)
        self.pub_robots = n.create_publisher(M["String"], ns_topic(ns, "robots/state"), q)
        self.pub_status = n.create_publisher(M["String"], ns_topic(ns, "status"), q)
        self.requests_topic = ns_topic(ns, "requests")
        n.create_subscription(M["String"], self.requests_topic, lambda m: self._handle_request_text(m.data, "ros"), q)
        n.create_subscription(M["Bool"], ns_topic(ns, "estop"), self._on_estop, q)
        self.executor = SingleThreadedExecutor(context=self.ctx)
        self.executor.add_node(n)
        self._running = True
        self._thread = threading.Thread(target=self._spin, name="ros2-spin", daemon=True)
        self._thread.start()
        self.sync_robots(self.cfg.robots)
        log.info("ROS 2 démarré: nœud %s, namespace %s, domain %s", self.cfg.ros.node_name, ns,
                 self.cfg.ros.domain_id if self.cfg.ros.domain_id is not None else env_info().get("ROS_DOMAIN_ID") or 0)

    def _spin(self) -> None:
        while self._running:
            try:
                self.executor.spin_once(timeout_sec=0.1)
            except Exception as exc:  # pragma: no cover
                if self._running:
                    self.errors["spin"] = repr(exc)
                    time.sleep(0.1)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        try:
            if self.executor:
                self.executor.shutdown()
            if self.node:
                self.node.destroy_node()
            if self.ctx and self.ctx.ok():
                self.rclpy.shutdown(context=self.ctx)
        except Exception as exc:  # pragma: no cover
            log.warning("arrêt ROS 2: %s", exc)

    def _on_estop(self, msg) -> None:
        if self.on_estop:
            self.on_estop(bool(msg.data))

    # ------------------------------------------------------------------ robots
    def sync_robots(self, robots: List[RobotConfig]) -> None:
        if self.node is None:
            return
        M, q, ns = self.M, self.cfg.ros.qos_depth, self.cfg.ros.namespace
        with self._lock:
            names = {r.name for r in robots}
            for name in list(self._robot_pubs):
                if name not in names or not self.cfg.ros.publish_per_robot_topics:
                    for ent in self._robot_pubs.pop(name).values():
                        self.node.destroy_publisher(ent) if hasattr(ent, "publish") else self.node.destroy_subscription(ent)
            for name in list(self._out_pubs):
                if name not in names:
                    self.node.destroy_publisher(self._out_pubs.pop(name)[2])
            for r in robots:
                try:
                    if self.cfg.ros.publish_per_robot_topics and r.name not in self._robot_pubs:
                        self._robot_pubs[r.name] = {
                            "pose": self.node.create_publisher(M["PoseStamped"], ns_topic(ns, f"robots/{r.name}/pose"), q),
                            "state": self.node.create_publisher(M["String"], ns_topic(ns, f"robots/{r.name}/state"), q),
                            "req": self.node.create_subscription(
                                M["Float64MultiArray"], ns_topic(ns, f"robots/{r.name}/request"),
                                lambda m, name=r.name: self._on_robot_request(name, m), q),
                        }
                    topic = output_topic(r)
                    cur = self._out_pubs.get(r.name)
                    if cur is None or cur[0] != topic or cur[1] != r.output.type:
                        if cur is not None:
                            self.node.destroy_publisher(cur[2])
                        mtype = {"twist": M["Twist"], "wheels": M["Float64MultiArray"], "json": M["String"]}[r.output.type]
                        self._out_pubs[r.name] = (topic, r.output.type, self.node.create_publisher(mtype, topic, q))
                    self.errors.pop(f"robot:{r.name}", None)
                except Exception as exc:
                    self.errors[f"robot:{r.name}"] = f"topics invalides: {exc}"
                    log.error("Robot %s: %s", r.name, exc)

    def _on_robot_request(self, name: str, msg) -> None:
        d = list(msg.data)
        if len(d) >= 2 and self.on_request:
            self.on_request(name, {"speed": d[0], "heading": d[1]}, "ros")

    # ------------------------------------------------------------------ publish
    def _stamp(self):
        return self.node.get_clock().now().to_msg()

    def _matrix(self, H) -> Any:
        m = self.M["Float64MultiArray"]()
        m.data = [float(v) for v in H.flatten()] if H is not None else []
        return m

    def publish_world(self, state, H, H_logical, bounds) -> None:
        if self.node is None:
            return
        String = self.M["String"]
        self.pub_world.publish(String(data=json.dumps(state)))
        self.pub_robots.publish(String(data=json.dumps({"stamp": state.get("stamp"), "robots": state.get("robots", {})})))
        if H is not None:
            self.pub_h.publish(self._matrix(H))
            self.pub_hl.publish(self._matrix(H_logical))
        poly = self.M["PolygonStamped"]()
        poly.header.stamp = self._stamp()
        poly.header.frame_id = self.cfg.ros.frame_id
        poly.polygon.points = [self.M["Point32"](x=float(x), y=float(y), z=0.0) for x, y in bounds]
        self.pub_bounds.publish(poly)

    def publish_robot(self, name: str, state: Dict[str, Any]) -> None:
        ent = self._robot_pubs.get(name)
        if not ent:
            return
        p = self.M["PoseStamped"]()
        p.header.stamp = self._stamp()
        p.header.frame_id = self.cfg.ros.frame_id
        p.pose.position.x = float(state["x"])
        p.pose.position.y = float(state["y"])
        th = float(state["theta"])
        p.pose.orientation.z = math.sin(th / 2)
        p.pose.orientation.w = math.cos(th / 2)
        ent["pose"].publish(p)
        ent["state"].publish(self.M["String"](data=json.dumps(state)))

    def publish_status(self, status: Dict[str, Any]) -> None:
        if self.node is not None:
            self.pub_status.publish(self.M["String"](data=json.dumps(status)))

    def publish_command(self, robot: RobotConfig, raw: Dict[str, Any]) -> None:
        cur = self._out_pubs.get(robot.name)
        if cur is None:
            return
        _, kind, pub = cur
        if kind == "twist":
            m = self.M["Twist"]()
            m.linear.x = float(raw.get("linear", 0.0))
            m.linear.y = float(raw.get("lateral", 0.0))
            m.angular.z = float(raw.get("angular", 0.0))
        elif kind == "wheels":
            m = self.M["Float64MultiArray"]()
            m.data = [float(v) for v in raw.get("wheels", [0.0, 0.0])]
        else:
            m = self.M["String"](data=json.dumps(raw))
        pub.publish(m)

    # ------------------------------------------------------------------ discovery
    def network_info(self) -> Dict[str, Any]:
        """Snapshot of the ROS graph (cached 1 s)."""
        now = time.time()
        if now - self._graph_stamp < 1.0 and self._graph_cache:
            return self._graph_cache
        n = self.node
        info: Dict[str, Any] = {"available": True, "env": env_info(), "node": n.get_fully_qualified_name()}
        try:
            from rclpy.utilities import get_rmw_implementation_identifier

            info["rmw"] = get_rmw_implementation_identifier()
        except Exception:
            info["rmw"] = None
        info["domain_id"] = self.ctx.get_domain_id() if hasattr(self.ctx, "get_domain_id") else None
        nodes = []
        for name, ns in n.get_node_names_and_namespaces():
            fq = (ns.rstrip("/") + "/" + name) if ns else "/" + name
            nodes.append(fq)
        info["nodes"] = sorted(set(nodes))
        topics = []
        for name, types in n.get_topic_names_and_types():
            row = {"name": name, "types": types, "publishers": n.count_publishers(name), "subscribers": n.count_subscribers(name)}
            topics.append(row)
        info["topics"] = sorted(topics, key=lambda r: r["name"])
        try:
            info["services"] = sorted(s for s, _ in n.get_service_names_and_types())
        except Exception:
            info["services"] = []
        self._graph_cache, self._graph_stamp = info, now
        return info

    def robot_presence(self, robot: RobotConfig) -> Dict[str, Any]:
        """A robot is considered reachable if some node subscribes to its command topic."""
        if self.node is None:
            return {"known": False}
        topic = self.node.resolve_topic_name(output_topic(robot)) if hasattr(self.node, "resolve_topic_name") else output_topic(robot)
        try:
            infos = self.node.get_subscriptions_info_by_topic(topic)
            subs = sorted({(i.node_namespace.rstrip("/") + "/" + i.node_name) for i in infos})
        except Exception:
            subs = []
        return {"known": True, "topic": topic, "subscribers": subs, "online": len(subs) > 0}

    def requests_publishers(self) -> Optional[int]:
        if self.node is None:
            return None
        try:
            infos = self.node.get_publishers_info_by_topic(self.requests_topic)
            return len(infos)
        except Exception:
            return None
