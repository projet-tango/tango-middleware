# Tango : middleware GAMA ↔ robots physiques

Ce middleware fait le lien entre une simulation **GAMA** (le monde logique) et des **robots réels** qui la jouent (le monde physique). Toutes les communications passent par **ROS 2**. Il est écrit en Python et tourne sous Linux, Windows et macOS.

Il a deux rôles :

1. **Faire correspondre les deux mondes.** Une caméra, que vous configurez, filme la scène. Des AprilTags placés dans les coins délimitent le monde, et chaque robot porte son propre tag. Le middleware détecte les tags, calcule l'homographie image → monde, puis publie à intervalle régulier sur ROS 2 les poses des robots, à la fois en mètres et dans les coordonnées GAMA.
2. **Piloter les robots.** GAMA envoie pour chaque robot une requête *vitesse + heading*. Un contrôleur la transforme en commande pour le robot, puis la calibration propre à ce robot (gains, zones mortes, dérive) l'adapte au matériel. Plusieurs contrôleurs sont fournis, la calibration est automatique, et vous pouvez écrire vos propres contrôleurs : ils sont rechargés à chaud.

Une **interface web d'administration** permet de tout régler : caméra, tags, repère GAMA, robots, contrôleurs (avec un éditeur de code), calibration, pilotage manuel, arrêt d'urgence, état du réseau ROS (nœuds, topics, robots joignables) et journal.

```
 GAMA ──(ROS 2: /tango/requests)──►┌────────────── middleware ──────────────┐──► /robotX/cmd_vel ──► robots
      ◄─(ROS 2: /tango/world/state)─│ caméra → AprilTag → homographie → poses │
                                    │ requête → contrôleur → calibration      │
                                    └──────── interface web :8080 ────────────┘
```

---

## Installation

```bash
pip install -e .            # numpy, opencv-python, fastapi, uvicorn, pydantic, pyyaml
pip install -e .[dev]       # + pytest/httpx pour les tests
pip install -e .[pupil]     # optionnel : détecteur AprilTag de référence (pupil-apriltags)
```

**ROS 2** : `rclpy` vient avec votre distribution ROS 2 (Humble, Jazzy…). Sourcez-la avant de lancer le middleware, dans le même environnement Python :

- Linux : `source /opt/ros/jazzy/setup.bash`
- Windows : `call C:\dev\ros2\local_setup.bat` (ou votre install pixi/conda RoboStack)
- macOS : RoboStack (conda) est la solution la plus simple

Le middleware n'utilise que des messages standard (`std_msgs`, `geometry_msgs`), donc il n'y a **aucun paquet à compiler**. Sans ROS 2, il passe en bus *loopback*, ce qui permet de développer et de tester hors ROS.

## Lancement

```bash
python -m tango_mw                          # = tango-mw run, lit/crée config/config.yaml
python -m tango_mw run --camera 0           # caméra USB n°0
python -m tango_mw run --camera synthetic   # caméra simulée (sans matériel)
python -m tango_mw run --bus ros2           # échoue explicitement si rclpy est absent
```

Ouvrez ensuite **http://localhost:8080**. Par défaut, l'interface n'écoute qu'en local. Pour y accéder depuis le réseau, mettez `web.host: 0.0.0.0`.

Autres commandes :

```bash
python -m tango_mw print-tags 0 1 2 3 10 11 --size 1000   # PNG à imprimer (dossier tags/)
python -m tango_mw new-controller mon_controleur          # crée controllers/mon_controleur.py
python -m tango_mw init-config config/salle_b.yaml
```

### Essayer sans matériel

La source caméra `synthetic` (valeur par défaut) affiche une arène virtuelle avec ses tags. Les robots simulés réagissent aux commandes envoyées par le middleware, et leurs dynamiques sont volontairement différentes des réglages par défaut : gains, zone morte, dérive, tag monté de travers. Vous pouvez donc tout tester sans matériel : détection, homographie, contrôle, et vérifier que la calibration automatique retrouve les vrais paramètres.

---

## Mise en place physique

1. Imprimez des tags de la famille configurée (`tag36h11` par défaut), avec un bord blanc.
2. Placez **au moins 4 tags de délimitation** sur le sol, en général aux coins. Dans *Monde & GAMA*, saisissez pour chacun son `id` et la position de son **centre** en mètres. Ces positions définissent le repère physique : x vers la droite, y vers le « haut », angles dans le sens antihoraire.
3. Collez un tag sur chaque robot, le haut du tag tourné vers l'avant du robot. Le léger décalage restant est mesuré par la calibration `heading_offset`. Dans *Robots*, déclarez pour chaque robot son nom, son `tag_id` et sa sortie.
4. Dans *Caméra*, choisissez la source : un index USB (le bouton « Détecter » liste les caméras), un fichier vidéo ou une URL RTSP/HTTP. Réglez la résolution, les fps, le FOURCC, l'exposition et la rotation. Ajoutez les intrinsèques si l'objectif déforme beaucoup l'image.
5. Dans *Monde & GAMA*, réglez la taille du monde GAMA (`width` × `height`). Par défaut, la boîte englobante des tags correspond à tout le monde GAMA.
6. Lancez la calibration **full** pour chaque robot (onglet Robots), puis testez avec le pilotage manuel.

### Déploiement rapide par scan (onglet *Déploiement*)

Les étapes 2 et 3 peuvent aussi se faire en scannant la scène. L'assistant n'écrit que dans la **configuration en cours d'édition** : vous relisez ou corrigez ce qu'il propose, puis vous cliquez « Appliquer ». La saisie manuelle reste toujours possible.

- **Scanner** : liste les tags visibles avec leur rôle actuel (repère, robot ou inconnu), leur taux de détection et leur position. Chaque tag inconnu peut être affecté à un robot, devenir un nouveau robot ou devenir un repère.
- **Coins du monde** : vous saisissez la largeur et la hauteur mesurées entre les centres des tags. Les 4 tags les plus proches des coins de l'image deviennent alors les coins du monde. Le haut de l'image correspond à +y ; si la caméra est montée autrement, réglez la rotation ou le miroir.
- **Repères supplémentaires** : une fois l'homographie valide, d'autres tags posés au sol sont mesurés et peuvent être ajoutés comme repères (à cocher un par un). Le monde reste alors calculable même si un coin est masqué.
- **Robots** : « un robot par tag inconnu », ou **découverte sur ROS**. Cette découverte cherche les topics qui suivent le modèle `/{name}/cmd_vel`, et le type de message détermine la sortie : `Twist`, roues ou JSON. Les robots découverts n'ont pas encore de tag (`tag_id` négatif).
- **Identifier** : le middleware fait pivoter le robot sur place pendant ~1 s via son topic, puis retient le tag qui a tourné dans l'image. Cela ne nécessite ni homographie ni tag correct au départ. « Identifier tous » enchaîne tous les robots non associés ou invisibles. Si le tag appartenait déjà à un autre robot, les deux sont échangés. Si le robot ne peut pas pivoter, l'option « avance » le fait avancer à la place.

Le scan est aussi disponible en API REST : `/api/setup/scan`, `/api/setup/propose_world`, `/api/setup/measure_tags`, `/api/setup/discover_robots`, `/api/robots/<nom>/identify`, `/api/robots/<nom>/assign_tag`. Dans la caméra simulée, `camera.synthetic.extra_robots` ajoute des robots absents de la configuration pour s'entraîner.

> **Parallaxe** : un tag posé haut sur un robot est vu un peu décalé par rapport au sol. Si c'est gênant, activez `world.parallax`, avec la position au sol et la hauteur de la caméra, et renseignez `calibration.tag_height` pour chaque robot.

---

## Interface ROS 2

Le namespace par défaut est `/tango`. La liste complète, avec ce qui est connecté en face, s'affiche dans l'onglet *Réseau ROS*.

| Topic | Type | Sens | Contenu |
|---|---|---|---|
| `/tango/world/state` | `std_msgs/String` | pub | JSON complet : homographies, contour, robots (physique + logique) |
| `/tango/robots/state` | `std_msgs/String` | pub | JSON des robots uniquement |
| `/tango/world/homography` | `std_msgs/Float64MultiArray` | pub | H image → monde physique (3×3, ligne par ligne) |
| `/tango/world/homography_logical` | `std_msgs/Float64MultiArray` | pub | H image → monde GAMA |
| `/tango/world/bounds` | `geometry_msgs/PolygonStamped` | pub | contour du monde (m) |
| `/tango/robots/<nom>/pose` | `geometry_msgs/PoseStamped` | pub | pose physique (frame `tango_world`) |
| `/tango/robots/<nom>/state` | `std_msgs/String` | pub | JSON d'un robot |
| `/tango/status` | `std_msgs/String` | pub | santé du middleware (1 Hz) |
| `/tango/requests` | `std_msgs/String` | sub | requêtes GAMA (JSON) |
| `/tango/robots/<nom>/request` | `std_msgs/Float64MultiArray` | sub | `[vitesse, heading]` |
| `/tango/estop` | `std_msgs/Bool` | sub | arrêt d'urgence global |
| `/<nom>/cmd_vel` (configurable) | `Twist`, `Float64MultiArray [g, d]` ou `String` | pub | commandes robot |

### Requêtes GAMA

Par défaut, `logical.requests_in_logical: true` : la vitesse est en unités GAMA par seconde et le heading en degrés GAMA (0 = est, sens horaire à l'écran).

```json
{"robot": "robot1", "speed": 5.0, "heading": 90}
{"requests": [{"robot": "robot1", "speed": 5, "heading": 0}, {"robot": "robot2", "stop": true}]}
{"robot1": {"speed": 5, "heading": 0}, "robot2": {"speed": 3, "heading": 180}}
```

- `"frame": "physical"` passe une requête en m/s et en radians (sens antihoraire).
- `"stop": true` arrête le robot.
- Tout champ en plus (par exemple `goal_x`) est transmis tel quel au contrôleur dans `request.extra`.
- Une requête reste valable `safety.request_timeout` secondes. GAMA doit donc la renvoyer à chaque cycle, sinon le robot s'arrête.

### État publié (extrait)

```json
{"valid": true, "reprojection_error": 0.0004, "homography": [[...]], "physical_to_logical": [[...]],
 "robots": {"robot1": {"visible": true, "x": 0.53, "y": 0.50, "theta": 1.57, "speed": 0.0,
                       "logical": {"x": 33.3, "y": 31.2, "heading": -90.0, "speed": 0.0}}}}
```

---

## Contrôle et calibration

La chaîne de commande est : **requête (physique) → contrôleur → `BodyCommand` (v, ω, v_lat en unités physiques) → modèle de calibration → commande brute → topic**.

Contrôleurs fournis, chacun avec des paramètres modifiables dans l'UI :

| Nom | Principe |
|---|---|
| `proportional` | ω = k·erreur de cap, v = vitesse × cos(erreur) |
| `heading_pid` | PID de cap avec anti-windup, rotation sur place au-delà d'un seuil, marche arrière en option |
| `speed_heading_pid` | `heading_pid` + boucle PI sur la vitesse mesurée par la caméra |
| `holonomic` | robots omnidirectionnels : vitesse monde projetée en (vx, vy), cap tenu ou aligné |
| `passthrough` | pas de boucle fermée (v = vitesse, ω = champ `angular` de la requête) |
| `go_to_goal` | exemple de plugin dans `controllers/` : suit le heading ou rallie `goal_x/goal_y` |

Procédures de **calibration par robot**, qui se lancent depuis l'UI ou via `POST /api/robots/<nom>/calibrate`. Le robot fait des allers-retours en boucle ouverte, filmé par la caméra :

- `heading_offset` : angle entre le tag et le vrai sens de marche.
- `linear` : gain et zone morte de la commande linéaire, plus la dérive en ligne droite (compensée ensuite).
- `angular` : gain et zone morte en rotation. Pour une sortie `wheels`, donne l'entraxe effectif.
- `full` : les trois à la suite. Les résultats sont appliqués et enregistrés dans le YAML.

La calibration s'arrête si le tag est perdu, si le robot sort de la zone ou si l'arrêt d'urgence est activé. Les amplitudes (`linear_levels`, `angular_levels`), la durée et les pauses se règlent dans les options de la procédure.

### Écrire son propre contrôleur

Créez un fichier `.py` dans `controllers/` : via l'UI (onglet *Contrôleurs* → « Créer depuis le modèle »), avec `python -m tango_mw new-controller nom`, ou à la main.

```python
import math
from tango_mw.control import BodyCommand, Controller, Param

class MonControleur(Controller):
    name = "mon_controleur"
    params = {"k": Param(2.0, min=0, max=20, description="gain de cap")}

    def reset(self):            # appelé quand le robot (re)démarre
        self.integrale = 0.0

    def compute(self, state, request, dt):
        err = request.heading_error(state.theta)          # rad, dans [-π, π)
        return BodyCommand(linear=request.speed * max(0, math.cos(err)),
                           angular=self.p["k"] * err)
```

- Le fichier est **rechargé à chaud** dès qu'il est enregistré. Les robots qui utilisent ce contrôleur reçoivent une nouvelle instance.
- Si le fichier a une erreur, l'ancienne version reste active et l'erreur s'affiche dans l'UI.
- Si `compute` lève une exception, le robot est arrêté et la trace s'affiche.
- Les paramètres déclarés dans `params` apparaissent automatiquement dans l'UI.
- Un paquet pip peut aussi exposer des contrôleurs via l'entry-point `tango_mw.controllers`.

---

## Administration

- **Réseau ROS** : distribution, RMW, domain ID, variables de découverte, liste des nœuds et topics (avec le nombre de publieurs et d'abonnés). Pour chaque topic du middleware, l'onglet montre qui est connecté en face.
- **Disponibilité des robots** : un robot est « joignable » si au moins un nœud est abonné à son topic de commande. L'onglet indique aussi si son tag est vu.
- **GAMA** : nombre de publieurs sur `/tango/requests` et âge de la dernière requête.
- **Arrêt d'urgence** : bouton dans l'UI ou topic `/tango/estop`. Les robots s'arrêtent aussi automatiquement si leur tag est perdu, si la requête a expiré, si l'homographie est absente ou s'ils sortent de la zone (ils ne peuvent alors que rentrer).
- **Configuration** : tout s'applique à chaud, puis « Enregistrer » écrit le YAML. Changer le namespace, le nœud ou le domain redémarre le bus ROS. L'hôte et le port web ne changent qu'au redémarrage.
- **API REST** documentée sur `/api/docs`. Elle permet d'automatiser la mise en place : config, calibration, requêtes, snapshot JPEG, flux MJPEG, PNG des tags.

> L'éditeur de contrôleurs exécute du code Python. Si l'interface est exposée sur le réseau, désactivez-le avec `web.allow_code_edit: false`.

## Structure

```
tango_mw/
  config.py            modèle de configuration (pydantic) + YAML
  frames.py            géométrie, correspondance physique ↔ GAMA
  vision/              caméra, détection AprilTag, homographie & tracking, caméra synthétique
  control/             API contrôleur, contrôleurs intégrés, registre/plugins, calibration, boucle
  bus/                 ROS 2 (rclpy) et loopback
  app.py               orchestration (threads vision / contrôle / publication)
  web/                 serveur FastAPI + interface
controllers/           vos contrôleurs (rechargés à chaud)
tests/                 python -m pytest
```
