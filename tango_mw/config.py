"""Configuration model (pydantic) and YAML persistence.

Every tunable of the middleware lives here. The web UI edits this model,
the YAML file persists it, and the runtime components are rebuilt from it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------- camera
class SyntheticConfig(_Model):
    """Virtual camera + kinematic simulator, used to test without hardware."""

    px_per_m: float = Field(400.0, description="Rendering resolution (pixels per metre)")
    perspective: float = Field(0.06, description="Simulated perspective distortion (0 = perfect top view)")
    noise_px: float = Field(0.0, description="Gaussian image noise (grey levels)")
    world_tag_size: float = Field(0.10, description="Side of the corner tags (m)")
    robot_tag_size: float = Field(0.08, description="Side of the robot tags (m)")
    randomize_dynamics: bool = Field(True, description="Random 'true' gains/offsets to test calibration")
    extra_robots: int = Field(0, ge=0, description="Simulated robots missing from the config (to practise scanning / identification)")
    seed: int = 1


class MobileCameraConfig(_Model):
    """Phone used as the camera (source 'mobile'): the phone app pushes JPEG frames over WebSocket."""

    host: str = Field("0.0.0.0", description="Listen address of the phone receiver (separate from the web interface)")
    port: int = Field(47800, description="HTTP/WebSocket port of the Android app + UDP port for automatic discovery")
    https_port: Optional[int] = Field(
        47801, description="HTTPS port of the web camera page (iPhone/browser, self-signed certificate). Empty = disabled")
    require_code: bool = Field(True, description="Require the pairing code (recommended: otherwise any device on the network can send frames)")
    pairing_code: str = Field("", description="Pairing code (generated automatically when empty)")
    lens: Literal["back", "front"] = Field("back", description="Phone lens to use")
    jpeg_quality: int = Field(80, ge=30, le=100, description="JPEG quality sent by the phone")
    focus: Literal["auto", "infinity"] = Field("auto", description="infinity: fixed focus (avoids autofocus 'hunting')")
    exposure_compensation: int = Field(0, description="Exposure compensation (in steps, phone dependent)")
    torch: bool = Field(False, description="Keep the flash on")
    max_exposure_ms: Optional[float] = Field(
        None, gt=0, le=1000,
        description="Cap the exposure time (ms) to limit motion blur; the phone raises the ISO instead. "
                    "Empty = phone auto-exposure. Under mains lighting prefer 10 or 20 (50 Hz) / 8.3 or 16.7 (60 Hz) to avoid flicker")
    stale_after: float = Field(3.0, gt=0, description="A phone that has sent no frame for (s) can be replaced by another one")


class CameraConfig(_Model):
    source: str = Field(
        "synthetic",
        description="Camera index (e.g. 0), video file path, URL (rtsp/http), 'mobile' (phone) or 'synthetic'",
    )
    backend: Literal["auto", "dshow", "msmf", "v4l2", "avfoundation", "gstreamer", "ffmpeg"] = "auto"
    width: Optional[int] = 1280
    height: Optional[int] = 720
    fps: Optional[float] = 30.0
    fourcc: Optional[str] = Field(None, description="e.g. MJPG (often required for 1080p over USB)")
    exposure: Optional[float] = Field(None, description="Manual exposure (value depends on the driver)")
    autofocus: Optional[bool] = None
    rotate: Literal[0, 90, 180, 270] = 0
    flip: Literal["none", "horizontal", "vertical", "both"] = "none"
    camera_matrix: Optional[List[List[float]]] = Field(None, description="3x3 intrinsics (optional, to correct lens distortion)")
    dist_coeffs: Optional[List[float]] = None
    synthetic: SyntheticConfig = SyntheticConfig()
    mobile: MobileCameraConfig = MobileCameraConfig()

    @property
    def is_mobile(self) -> bool:
        return self.source.strip().lower() == "mobile"


# --------------------------------------------------------------------------- detection
class DetectorConfig(_Model):
    family: Literal["tag16h5", "tag25h9", "tag36h10", "tag36h11"] = "tag36h11"
    backend: Literal["auto", "opencv", "pupil"] = "opencv"
    decimate: float = Field(1.0, description="Image downsampling before detection (>1 = faster)")
    corner_refinement: Literal["none", "subpix", "contour", "apriltag"] = "subpix"
    min_marker_perimeter: float = Field(0.02, description="Minimal perimeter relative to the image size")


# --------------------------------------------------------------------------- world
class WorldTag(_Model):
    id: int
    x: float
    y: float


class ParallaxConfig(_Model):
    """Correct for tags mounted above the ground plane (camera ground point + height)."""

    enabled: bool = False
    camera_x: float = 0.0
    camera_y: float = 0.0
    camera_height: float = 2.0


class WorldConfig(_Model):
    tags: List[WorldTag] = Field(
        default_factory=lambda: [
            WorldTag(id=0, x=0.0, y=0.0),
            WorldTag(id=1, x=1.6, y=0.0),
            WorldTag(id=2, x=1.6, y=1.0),
            WorldTag(id=3, x=0.0, y=1.0),
        ],
        description="Tags delimiting the world and their physical coordinates (tag centre, in m)",
    )
    use_tag_corners: bool = Field(
        False,
        description="Also use the 4 corners of the world tags (assumes aligned tags, top towards +y)",
    )
    tag_size: float = Field(0.10, description="Side of the world tags (m), used with use_tag_corners")
    min_tags: int = Field(4, ge=4, description="Minimal number of visible world tags to (re)compute H")
    homography_mode: Literal["continuous", "locked"] = Field(
        "continuous",
        description="continuous: H recomputed on every frame; locked: computed once then frozen (can be re-locked)",
    )
    relock_on_move: bool = Field(
        True, description="locked mode: recompute H when the boundary tags no longer match it (camera bumped)")
    smoothing: float = Field(0.0, ge=0.0, lt=1.0, description="Exponential smoothing of H (0 = none)")
    ransac_threshold: float = Field(0.02, description="RANSAC threshold (world units) when > 4 points")
    max_reprojection_error: float = Field(0.05, description="H rejected when the mean error exceeds this threshold (m)")
    pose_smoothing: float = Field(0.3, ge=0.0, lt=1.0, description="Exponential smoothing of robot poses")
    velocity_smoothing: float = Field(0.6, ge=0.0, lt=1.0, description="Exponential smoothing of estimated velocities")
    parallax: ParallaxConfig = ParallaxConfig()


class LogicalFrameConfig(_Model):
    """Mapping between the physical frame (m) and the GAMA (logical) frame."""

    width: float = Field(100.0, description="GAMA world width")
    height: float = Field(62.5, description="GAMA world height")
    origin: Literal["top_left", "bottom_left"] = Field(
        "top_left", description="GAMA: origin at the top left, y pointing down"
    )
    heading_unit: Literal["deg", "rad"] = "deg"
    physical_bounds: Optional[List[float]] = Field(
        None,
        description="Physical [xmin, ymin, xmax, ymax] matching the GAMA world (default: bounding box of the tags)",
    )
    requests_in_logical: bool = Field(True, description="GAMA requests (speed/heading) are in logical units")


# --------------------------------------------------------------------------- robots
class CalibrationData(_Model):
    """Per-robot model turning physical body commands into raw actuator commands."""

    heading_offset: float = Field(0.0, description="Angle (rad) added to the tag heading to get the robot heading")
    tag_offset_x: float = Field(0.0, description="Robot centre position in the tag frame (m, forward)")
    tag_offset_y: float = Field(0.0, description="Robot centre position in the tag frame (m, to the left)")
    tag_height: float = Field(0.0, description="Tag height above the floor (m), for parallax correction")
    # twist output
    linear_gain: float = Field(1.0, description="Raw command per m/s")
    linear_deadband: float = Field(0.0, description="Minimal raw command to move")
    angular_gain: float = Field(1.0, description="Raw command per rad/s")
    angular_deadband: float = 0.0
    # wheels output
    wheel_gain: float = Field(1.0, description="Raw wheel command per m/s")
    wheel_deadband: float = 0.0
    wheel_base: float = Field(0.1, description="Effective wheel base (m)")
    right_wheel_scale: float = Field(1.0, description="Factor applied to the right wheel (imbalance)")
    # common
    drift_per_meter: float = Field(0.0, description="Angular drift (rad/m) in a straight line, compensated")
    max_linear: float = Field(0.5, description="Max physical linear speed (m/s)")
    max_angular: float = Field(3.0, description="Max physical angular speed (rad/s)")
    max_raw: float = Field(1.0, description="Saturation of raw commands")
    invert_linear: bool = False
    invert_angular: bool = False
    calibrated_at: Optional[str] = None


class OutputConfig(_Model):
    type: Literal["twist", "wheels", "json"] = Field(
        "twist", description="twist: geometry_msgs/Twist; wheels: Float64MultiArray [left, right]; json: std_msgs/String"
    )
    topic: str = Field("/{name}/cmd_vel", description="Output topic ({name} = robot name)")


class RobotConfig(_Model):
    name: str
    tag_id: int = Field(..., description="Id of the tag carried by the robot (negative = not assigned yet)")
    enabled: bool = True
    controller: str = "heading_pid"
    controller_params: Dict[str, Any] = Field(default_factory=dict)
    output: OutputConfig = OutputConfig()
    calibration: CalibrationData = CalibrationData()

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", v):
            raise ValueError("the name must be a valid ROS identifier (letters, digits, _)")
        return v


# --------------------------------------------------------------------------- infra
class RosConfig(_Model):
    bus: Literal["auto", "ros2", "loopback"] = Field(
        "auto", description="auto: ROS 2 when rclpy is available, otherwise loopback (no ROS)"
    )
    domain_id: Optional[int] = Field(None, description="ROS_DOMAIN_ID (empty = environment variable)")
    node_name: str = "tango_middleware"
    namespace: str = "/tango"
    frame_id: str = "tango_world"
    publish_rate: float = Field(10.0, gt=0, description="World state publication rate (Hz)")
    control_rate: float = Field(20.0, gt=0, description="Control loop rate (Hz)")
    qos_depth: int = 10
    publish_per_robot_topics: bool = True


class SafetyConfig(_Model):
    request_timeout: float = Field(1.0, description="Stop when no GAMA request for (s), 0 = never")
    tag_lost_timeout: float = Field(0.5, description="Stop when the robot tag has been lost for (s)")
    stop_out_of_bounds: bool = True
    bounds_margin: float = Field(0.0, description="Margin (m) around the area (negative = inside)")


class WebConfig(_Model):
    enabled: bool = True
    host: str = Field("127.0.0.1", description="0.0.0.0 for access from the network")
    port: int = 8080
    preview_fps: float = 10.0
    jpeg_quality: int = 75
    allow_code_edit: bool = Field(True, description="Allow editing controllers from the interface")


class PluginsConfig(_Model):
    dirs: List[str] = Field(default_factory=lambda: ["controllers"], description="User controller directories")
    auto_reload: bool = Field(True, description="Reload modified files automatically")


class AppConfig(_Model):
    camera: CameraConfig = CameraConfig()
    detector: DetectorConfig = DetectorConfig()
    world: WorldConfig = WorldConfig()
    logical: LogicalFrameConfig = LogicalFrameConfig()
    robots: List[RobotConfig] = Field(
        default_factory=lambda: [
            RobotConfig(name="robot1", tag_id=10),
            RobotConfig(name="robot2", tag_id=11),
        ]
    )
    ros: RosConfig = RosConfig()
    safety: SafetyConfig = SafetyConfig()
    web: WebConfig = WebConfig()
    plugins: PluginsConfig = PluginsConfig()

    @field_validator("robots")
    @classmethod
    def _unique(cls, robots: List[RobotConfig]) -> List[RobotConfig]:
        names = [r.name for r in robots]
        tags = [r.tag_id for r in robots]
        if len(set(names)) != len(names):
            raise ValueError("duplicate robot names")
        if len(set(tags)) != len(tags):
            raise ValueError("duplicate robot tag_id")
        return robots

    def robot(self, name: str) -> Optional[RobotConfig]:
        return next((r for r in self.robots if r.name == name), None)


# --------------------------------------------------------------------------- IO
def load_config(path: Optional[Path]) -> AppConfig:
    if path is None or not Path(path).exists():
        return AppConfig()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig.model_validate(data)


def save_config(cfg: AppConfig, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg.model_dump(mode="json"), f, sort_keys=False, allow_unicode=True)
    tmp.replace(path)
