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

    px_per_m: float = Field(400.0, description="Résolution du rendu (pixels par mètre)")
    perspective: float = Field(0.06, description="Distorsion de perspective simulée (0 = vue de dessus parfaite)")
    noise_px: float = Field(0.0, description="Bruit gaussien sur l'image (niveaux de gris)")
    world_tag_size: float = Field(0.10, description="Côté des tags de coin (m)")
    robot_tag_size: float = Field(0.08, description="Côté des tags robots (m)")
    randomize_dynamics: bool = Field(True, description="Gains/offsets 'vrais' aléatoires pour tester la calibration")
    extra_robots: int = Field(0, ge=0, description="Robots simulés absents de la config (pour tester le scan / l'identification)")
    seed: int = 1


class CameraConfig(_Model):
    source: str = Field(
        "synthetic",
        description="Index caméra (ex: 0), chemin de fichier vidéo, URL (rtsp/http) ou 'synthetic'",
    )
    backend: Literal["auto", "dshow", "msmf", "v4l2", "avfoundation", "gstreamer", "ffmpeg"] = "auto"
    width: Optional[int] = 1280
    height: Optional[int] = 720
    fps: Optional[float] = 30.0
    fourcc: Optional[str] = Field(None, description="ex: MJPG (souvent nécessaire pour le 1080p en USB)")
    exposure: Optional[float] = Field(None, description="Exposition manuelle (valeur dépend du driver)")
    autofocus: Optional[bool] = None
    rotate: Literal[0, 90, 180, 270] = 0
    flip: Literal["none", "horizontal", "vertical", "both"] = "none"
    camera_matrix: Optional[List[List[float]]] = Field(None, description="Intrinsèques 3x3 (optionnel, pour corriger la distorsion)")
    dist_coeffs: Optional[List[float]] = None
    synthetic: SyntheticConfig = SyntheticConfig()


# --------------------------------------------------------------------------- detection
class DetectorConfig(_Model):
    family: Literal["tag16h5", "tag25h9", "tag36h10", "tag36h11"] = "tag36h11"
    backend: Literal["auto", "opencv", "pupil"] = "opencv"
    decimate: float = Field(1.0, description="Sous-échantillonnage de l'image avant détection (>1 = plus rapide)")
    corner_refinement: Literal["none", "subpix", "contour", "apriltag"] = "subpix"
    min_marker_perimeter: float = Field(0.02, description="Périmètre minimal relatif à la taille de l'image")


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
        description="Tags délimitant le monde et leurs coordonnées physiques (centre du tag, en m)",
    )
    use_tag_corners: bool = Field(
        False,
        description="Utiliser aussi les 4 coins des tags de monde (suppose des tags alignés, haut vers +y)",
    )
    tag_size: float = Field(0.10, description="Côté des tags de monde (m), utilisé si use_tag_corners")
    min_tags: int = Field(4, ge=4, description="Nombre minimal de tags de monde visibles pour (re)calculer H")
    homography_mode: Literal["continuous", "locked"] = Field(
        "continuous",
        description="continuous: H recalculée à chaque image ; locked: calculée une fois puis figée (re-verrouillable)",
    )
    smoothing: float = Field(0.0, ge=0.0, lt=1.0, description="Lissage exponentiel de H (0 = aucun)")
    ransac_threshold: float = Field(0.02, description="Seuil RANSAC (unités monde) si > 4 points")
    max_reprojection_error: float = Field(0.05, description="H rejetée si l'erreur moyenne dépasse ce seuil (m)")
    pose_smoothing: float = Field(0.3, ge=0.0, lt=1.0, description="Lissage exponentiel des poses robots")
    velocity_smoothing: float = Field(0.6, ge=0.0, lt=1.0, description="Lissage exponentiel des vitesses estimées")
    parallax: ParallaxConfig = ParallaxConfig()


class LogicalFrameConfig(_Model):
    """Mapping between the physical frame (m) and the GAMA (logical) frame."""

    width: float = Field(100.0, description="Largeur du monde GAMA")
    height: float = Field(62.5, description="Hauteur du monde GAMA")
    origin: Literal["top_left", "bottom_left"] = Field(
        "top_left", description="GAMA: origine en haut à gauche, y vers le bas"
    )
    heading_unit: Literal["deg", "rad"] = "deg"
    physical_bounds: Optional[List[float]] = Field(
        None,
        description="[xmin, ymin, xmax, ymax] physiques correspondant au monde GAMA (défaut: boîte englobante des tags)",
    )
    requests_in_logical: bool = Field(True, description="Les requêtes GAMA (vitesse/heading) sont en unités logiques")


# --------------------------------------------------------------------------- robots
class CalibrationData(_Model):
    """Per-robot model turning physical body commands into raw actuator commands."""

    heading_offset: float = Field(0.0, description="Angle (rad) ajouté au cap du tag pour obtenir le cap du robot")
    tag_offset_x: float = Field(0.0, description="Position du centre robot dans le repère du tag (m, vers l'avant)")
    tag_offset_y: float = Field(0.0, description="Position du centre robot dans le repère du tag (m, vers la gauche)")
    tag_height: float = Field(0.0, description="Hauteur du tag au-dessus du sol (m), pour la correction de parallaxe")
    # twist output
    linear_gain: float = Field(1.0, description="Commande brute par m/s")
    linear_deadband: float = Field(0.0, description="Commande brute minimale pour bouger")
    angular_gain: float = Field(1.0, description="Commande brute par rad/s")
    angular_deadband: float = 0.0
    # wheels output
    wheel_gain: float = Field(1.0, description="Commande roue brute par m/s")
    wheel_deadband: float = 0.0
    wheel_base: float = Field(0.1, description="Entraxe effectif (m)")
    right_wheel_scale: float = Field(1.0, description="Facteur appliqué à la roue droite (déséquilibre)")
    # common
    drift_per_meter: float = Field(0.0, description="Dérive angulaire (rad/m) en ligne droite, compensée")
    max_linear: float = Field(0.5, description="Vitesse linéaire physique max (m/s)")
    max_angular: float = Field(3.0, description="Vitesse angulaire physique max (rad/s)")
    max_raw: float = Field(1.0, description="Saturation des commandes brutes")
    invert_linear: bool = False
    invert_angular: bool = False
    calibrated_at: Optional[str] = None


class OutputConfig(_Model):
    type: Literal["twist", "wheels", "json"] = Field(
        "twist", description="twist: geometry_msgs/Twist ; wheels: Float64MultiArray [gauche, droite] ; json: std_msgs/String"
    )
    topic: str = Field("/{name}/cmd_vel", description="Topic de sortie ({name} = nom du robot)")


class RobotConfig(_Model):
    name: str
    tag_id: int = Field(..., description="Id du tag porté par le robot (négatif = pas encore associé)")
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
            raise ValueError("le nom doit être un identifiant ROS valide (lettres, chiffres, _)")
        return v


# --------------------------------------------------------------------------- infra
class RosConfig(_Model):
    bus: Literal["auto", "ros2", "loopback"] = Field(
        "auto", description="auto: ROS 2 si rclpy est disponible, sinon loopback (sans ROS)"
    )
    domain_id: Optional[int] = Field(None, description="ROS_DOMAIN_ID (vide = variable d'environnement)")
    node_name: str = "tango_middleware"
    namespace: str = "/tango"
    frame_id: str = "tango_world"
    publish_rate: float = Field(10.0, gt=0, description="Fréquence de publication de l'état du monde (Hz)")
    control_rate: float = Field(20.0, gt=0, description="Fréquence de la boucle de contrôle (Hz)")
    qos_depth: int = 10
    publish_per_robot_topics: bool = True


class SafetyConfig(_Model):
    request_timeout: float = Field(1.0, description="Arrêt si aucune requête GAMA depuis (s), 0 = jamais")
    tag_lost_timeout: float = Field(0.5, description="Arrêt si le tag du robot est perdu depuis (s)")
    stop_out_of_bounds: bool = True
    bounds_margin: float = Field(0.0, description="Marge (m) autour de la zone (négatif = à l'intérieur)")


class WebConfig(_Model):
    enabled: bool = True
    host: str = Field("127.0.0.1", description="0.0.0.0 pour un accès depuis le réseau")
    port: int = 8080
    preview_fps: float = 10.0
    jpeg_quality: int = 75
    allow_code_edit: bool = Field(True, description="Autoriser l'édition des contrôleurs depuis l'interface")


class PluginsConfig(_Model):
    dirs: List[str] = Field(default_factory=lambda: ["controllers"], description="Dossiers de contrôleurs utilisateur")
    auto_reload: bool = Field(True, description="Recharger automatiquement les fichiers modifiés")


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
            raise ValueError("noms de robots dupliqués")
        if len(set(tags)) != len(tags):
            raise ValueError("tag_id de robots dupliqués")
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
