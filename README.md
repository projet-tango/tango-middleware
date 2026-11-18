# Tango: GAMA ↔ physical robots middleware

This middleware connects a **GAMA** simulation (the logical world) with **real robots** that act it out (the physical world). All communication goes through **ROS 2**. It is written in Python and runs on Linux, Windows and macOS.

It has two jobs:

1. **Match the two worlds.** A camera you configure (USB, IP stream, or a phone) films the scene. AprilTags placed in the corners delimit the world, and every robot carries its own tag. The middleware detects the tags, computes the image → world homography, then publishes the robots' poses on ROS 2 at a fixed rate, both in metres and in GAMA coordinates.
2. **Drive the robots.** For each robot, GAMA sends a *speed + heading* request. A controller turns it into a robot command, then the robot's own calibration (gains, dead zones, drift) adapts it to the hardware. Several controllers are provided, calibration is automatic, and you can write your own controllers: they are hot-reloaded.

A **web administration interface** lets you set everything: camera, tags, GAMA frame, robots, controllers (with a code editor), calibration, manual driving, emergency stop, ROS network state (nodes, topics, reachable robots) and logs. Its **Start** tab is a step-by-step checklist that turns green as the setup progresses, every section has a **?** help bubble, and rarely needed settings are folded under *Advanced settings*.

**Setup manual** (GitHub Pages, from [`docs/`](docs/)): a complete walk-through with screenshots — 3 differential robots, a square field delimited by 4 tags, a phone as the camera, a GAMA model ([`docs/gama/tango_demo.gaml`](docs/gama/tango_demo.gaml)): hardware, software, configuration, calibration and running the demo. To publish it: repository *Settings → Pages → Deploy from a branch*, branch `main`, folder `/docs`.

```
 GAMA ──(ROS 2: /tango/requests)──►┌────────────── middleware ──────────────┐──► /robotX/cmd_vel ──► robots
      ◄─(ROS 2: /tango/world/state)─│ camera → AprilTag → homography → poses  │
                                    │ request → controller → calibration      │
                                    └──────── web interface :8080 ───────────┘
```

---

## Installation

```bash
pip install -e .            # numpy, opencv-python, fastapi, uvicorn, pydantic, pyyaml, websockets, cryptography
pip install -e .[dev]       # + pytest/httpx for the tests
pip install -e .[pupil]     # optional: reference AprilTag detector (pupil-apriltags)
```

**ROS 2**: `rclpy` comes with your ROS 2 distribution (Humble, Jazzy…). Source it before starting the middleware, in the same Python environment:

- Linux: `source /opt/ros/jazzy/setup.bash`
- Windows: `call C:\dev\ros2\local_setup.bat` (or your pixi/conda RoboStack install)
- macOS: RoboStack (conda) is the easiest option

The middleware only uses standard messages (`std_msgs`, `geometry_msgs`), so there is **nothing to compile**. Without ROS 2 it falls back to a *loopback* bus, so you can develop and test without ROS.

## Running

```bash
python -m tango_mw                          # = tango-mw run, reads/creates config/config.yaml
python -m tango_mw run --camera 0           # USB camera #0
python -m tango_mw run --camera mobile      # a phone (see "Phone as camera")
python -m tango_mw run --camera synthetic   # simulated camera (no hardware)
python -m tango_mw run --bus ros2           # fails explicitly if rclpy is missing
```

Then open **http://localhost:8080**. By default the interface only listens locally. To reach it from the network, set `web.host: 0.0.0.0`.

Other commands:

```bash
python -m tango_mw print-tags 0 1 2 3 10 11 --size 1000   # PNGs to print (tags/ folder)
python -m tango_mw new-controller my_controller           # creates controllers/my_controller.py
python -m tango_mw init-config config/room_b.yaml
python -m tango_mw service install                        # start automatically at boot (see below)
```

### Trying it without hardware

The `synthetic` camera source (the default) renders a virtual arena with its tags. The simulated robots react to the commands sent by the middleware, and their dynamics deliberately differ from the default settings: gains, dead zone, drift, tag mounted askew. So you can test everything without hardware: detection, homography, control, and check that automatic calibration finds the true parameters.

### Starting automatically at boot

```bash
python -m tango_mw service install [--ros-setup /opt/ros/jazzy/setup.bash] [--args "--bus ros2"]
python -m tango_mw service status
python -m tango_mw service uninstall
python -m tango_mw service install --dry-run   # only print the generated file
```

Run it from the working directory (the one containing `config/` and `controllers/`), with the Python interpreter that has the middleware installed. The middleware is restarted automatically if it stops.

| OS | Mechanism | Starts |
|---|---|---|
| Linux | systemd user unit `~/.config/systemd/user/tango-mw.service` + `loginctl enable-linger` | at boot, even with nobody logged in. Logs: `journalctl --user -u tango-mw -f` |
| Windows | scheduled task `TangoMiddleware` running `config/tango-mw-service.cmd` (restart loop) | at user logon (enable automatic logon with `netplwiz` for an unattended PC) |
| macOS | launchd agent `~/Library/LaunchAgents/fr.projettango.middleware.plist` (KeepAlive) | at logon. Logs: `logs/tango-mw.log` |

ROS 2 must be sourced in the service: pass `--ros-setup` (on Linux it is detected from `ROS_DISTRO` when the command is run from a sourced shell). Configuration, pairing code and calibration are stored in the YAML file, so everything comes back as it was, and paired phones reconnect on their own.

---

## Physical setup

1. Print the tags: *Field & tags* tab → *Print boundary tags* / *Print robot tags* opens an A4 sheet at real size (print at 100 %, check the 100 mm ruler). Robot tags carry a FRONT arrow. *Download all* gives vector SVGs at real size and PNGs (`/print/tags`, `/api/tags/<id>.svg?size_mm=`, `/api/tags/archive.zip`).
2. Place **at least 4 boundary tags** on the floor, usually in the corners. In *World & GAMA*, enter each tag's `id` and the position of its **centre** in metres. These positions define the physical frame: x to the right, y "up", angles counter-clockwise.
3. Stick a tag on every robot, with the top of the tag facing the robot's front. The small remaining offset is measured by the `heading_offset` calibration. In *Robots*, declare each robot's name, `tag_id` and output.
4. In *Camera & detection*, choose the source: a USB index (the *Detect local cameras* button lists them), a video file, an RTSP/HTTP URL, or `mobile` for a phone. Set the resolution, fps, FOURCC, exposure and rotation. Add the intrinsics if the lens distorts the image a lot.
5. In *World & GAMA*, set the GAMA world size (`width` × `height`). By default the tags' bounding box maps to the whole GAMA world.
6. Check that the *Setup checks* card (dashboard and camera tab) is green, with a robot driving across the area.
7. Run the **full** calibration for each robot (Robots tab), then test with manual driving.

### Setup checks (lighting and image quality)

The middleware continuously measures whether the image is good enough for reliable tracking, mostly **on the tags themselves**: each detected tag is rectified and every bit is read and compared with the bit pattern it should have. Results (ok / warn / bad, with advice) are shown in the *Setup checks* card, as a `setup` badge in the header (hover for details), in the log when they get worse, at `GET /api/checks`, and as `setup_checks` in `/tango/status`.

| Check | Measures | Warn / bad when |
|---|---|---|
| Frame rate | processed fps vs `camera.fps` | below 80 % / 50 % |
| Image brightness | whole image | very dark (brightest 2 % below 70/255), or > 8 % saturated |
| Boundary tags detected | detection rate of each boundary tag over the last ~2 s | below 95 % / 50 % |
| Robot tags detection | robots seen intermittently (5–90 % of frames) | flickering detection |
| Tag contrast | median white bits − median black bits, worst tag | below 90 / 50 (out of 255) |
| Lighting uniformity | contrast of the dimmest vs brightest tag | ratio below 0.5 |
| Glare on tags | black bits that read white (reflections) | any / > 10 % of a tag's black bits |
| Tag size in the image | pixels per bit, smallest tag | below 3 / 2 px |
| Sharpness | edge width vs bit size (focus, blur) | above 0.7 / 1 bit |
| Light flicker | frame-to-frame brightness change (mains-powered lights vs short exposures) | above 3 % / 6 % |
| Light level (phone) | exposure time and ISO reported by the phone | longest exposure (or the `max_exposure_ms` cap) **and** ISO ≥ 1600 / 3200 |
| Motion blur (phone) | distance a robot at its `max_linear` travels during one exposure | above half a bit / one bit |

Phones lengthen the exposure up to the frame period before raising the gain, even in good light (a Pixel 9a stays at ~30 ms at 30 fps). Robots moving fast may then blur: the *motion blur* check shows by how much. To fix it, set `camera.mobile.max_exposure_ms` (e.g. 10): the phone switches to manual exposure with that cap and raises the ISO to keep the brightness auto-exposure had chosen (it follows later light changes). With too little light the ISO saturates and the *light level* check turns red: add light. Under mains-powered lights, use a multiple of 10 ms (50 Hz) or 8.3 ms (60 Hz) to avoid flicker.

These checks only inform: robots are already stopped automatically when their tag is lost.

With `world.homography_mode: locked`, the locked homography is still compared with the boundary tags on every frame. If the camera is bumped (tags off by more than `world.max_reprojection_error` for 5 frames, with at least `min_tags` visible), it is recomputed automatically and a warning is logged (`world.relock_on_move`, on by default). In `continuous` mode it simply follows the tags on every frame.

### Phone as camera

Any phone can be the camera: an **Android app** ([tango-camera-mobile](https://github.com/projet-tango/tango-camera-mobile)) or, for an **iPhone** (or a phone without the app), a **web page** served by the middleware. The phone streams JPEG frames over Wi-Fi; nothing else changes (detection, homography, `rotate`/`flip`, intrinsics).

1. Set the camera source to `mobile`: *Use a phone as camera* button in the *Camera & detection* tab, or `--camera mobile`.
2. The *Phone as camera* card shows a 6-digit **pairing code**, the addresses and a **QR code**.
3. **Android**: open the app. It finds the middleware by itself (UDP broadcast); enter the code or scan the QR code once.
   **iPhone**: scan the QR code with the Camera app, accept the self-signed certificate, tap *Start camera*.
4. The card then shows the phone, its frame rate, battery, temperature, sensor frame rate, JPEG encoding time, bandwidth and whether its screen is on.

**Frame rate** (measured on a Pixel 9a, Wi-Fi, middleware on a laptop): 30 fps at 640×480, 1280×720 and 1920×1080 (`camera.fps: 30`); about 50–60 fps at 640×480 and 1280×720 with `camera.fps: 60` (49 fps at 1920×1080, 20 Mbit/s). The app pins the camera's exposure frame-rate range, otherwise auto-exposure drops to ~15 fps in dim light; the trade-off is a darker image in a dim room (raise `exposure_compensation` or add light). Shorter exposures also reduce motion blur on moving robots. Older phones are usually limited by JPEG encoding (shown in the card).

**Locked screen**: the Android app keeps streaming with the screen off or locked (foreground service), which saves most of the battery. A browser page stops the camera when the screen locks: on an iPhone keep the page in front, plugged in.

Pairing happens once: the code is saved in the YAML file and on the phone. Afterwards the phone reconnects on its own (Wi-Fi drop, middleware restart, phone reboot with the Android app) within a few seconds. Camera settings (`camera.width/height/fps` and `camera.mobile.*`: lens, JPEG quality, focus, exposure compensation, torch) are pushed live to the phone.

The phone receiver is separate from the web interface, which can therefore stay on `127.0.0.1`:

| Port | Use |
|---|---|
| `camera.mobile.port` (47800, TCP) | WebSocket for the Android app, `/info` |
| `camera.mobile.port` (47800, UDP) | automatic discovery |
| `camera.mobile.https_port` (47801, TCP) | HTTPS camera page (browsers need HTTPS for the camera); self-signed certificate created in `config/` |

On **Windows**, allow Python through the firewall on *private* networks for these ports (prompt on first run), and make sure the Wi-Fi network is set as *private*. Networks that isolate clients from each other (some guest/enterprise Wi-Fi) block discovery and streaming: use a dedicated router or a hotspot.

> Security: with `camera.mobile.require_code: true` (default), only paired phones are accepted. *New code* in the card generates a new code and disconnects paired phones. One phone streams at a time: another phone is refused while the current one is sending frames.

The protocol (WebSocket + UDP discovery) is documented at the top of `tango_mw/vision/mobile.py`. REST endpoints: `GET /api/mobile`, `POST /api/mobile/new_code`, `POST /api/mobile/disconnect`, `GET /api/mobile/qr.png?data=…`.

### Quick deployment by scanning (*Deployment* tab)

Steps 2 and 3 can also be done by scanning the scene. The assistant only writes to the **configuration being edited**: you review or correct its proposals, then click *Apply*. Manual entry is always possible.

- **Scan**: lists the visible tags with their current role (landmark, robot or unknown), their detection rate and their position. Each unknown tag can be assigned to a robot, become a new robot or become a landmark.
- **World corners**: you enter the width and height measured between the tags' centres. The 4 tags closest to the image corners then become the world corners. The top of the image is +y; if the camera is mounted differently, set the rotation or mirroring.
- **Extra landmarks**: once the homography is valid, other tags lying on the floor are measured and can be added as landmarks (ticked one by one). The world can then still be computed if a corner is hidden.
- **Robots**: "one robot per unknown tag", or **discovery on ROS**. This discovery looks for topics following the `/{name}/cmd_vel` pattern, and the message type sets the output: `Twist`, wheels or JSON. Discovered robots have no tag yet (negative `tag_id`).
- **Identify**: the middleware spins the robot in place for ~1 s through its topic, then keeps the tag that rotated in the image. This needs neither a homography nor a correct tag to begin with. "Identify all" goes through every unassigned or invisible robot. If the tag already belonged to another robot, the two are swapped. If the robot cannot spin, the "forward" option makes it move forward instead.

The scan is also available through the REST API: `/api/setup/scan`, `/api/setup/propose_world`, `/api/setup/measure_tags`, `/api/setup/discover_robots`, `/api/robots/<name>/identify`, `/api/robots/<name>/assign_tag`. In the simulated camera, `camera.synthetic.extra_robots` adds robots missing from the configuration, for practice.

> **Parallax**: a tag mounted high on a robot appears slightly shifted relative to the floor. If this matters, enable `world.parallax`, with the camera's ground position and height, and fill in `calibration.tag_height` for each robot.

---

## ROS 2 interface

The default namespace is `/tango`. The full list, with what is connected on the other side, is shown in the *ROS network* tab.

| Topic | Type | Dir. | Content |
|---|---|---|---|
| `/tango/world/state` | `std_msgs/String` | pub | full JSON: homographies, outline, robots (physical + logical) |
| `/tango/robots/state` | `std_msgs/String` | pub | robots-only JSON |
| `/tango/world/homography` | `std_msgs/Float64MultiArray` | pub | H image → physical world (3×3, row-major) |
| `/tango/world/homography_logical` | `std_msgs/Float64MultiArray` | pub | H image → GAMA world |
| `/tango/world/bounds` | `geometry_msgs/PolygonStamped` | pub | world outline (m) |
| `/tango/robots/<name>/pose` | `geometry_msgs/PoseStamped` | pub | physical pose (frame `tango_world`) |
| `/tango/robots/<name>/state` | `std_msgs/String` | pub | JSON for one robot |
| `/tango/status` | `std_msgs/String` | pub | middleware health (1 Hz) |
| `/tango/requests` | `std_msgs/String` | sub | GAMA requests (JSON) |
| `/tango/robots/<name>/request` | `std_msgs/Float64MultiArray` | sub | `[speed, heading]` |
| `/tango/estop` | `std_msgs/Bool` | sub | global emergency stop |
| `/<name>/cmd_vel` (configurable) | `Twist`, `Float64MultiArray [l, r]` or `String` | pub | robot commands |

### GAMA requests

By default `logical.requests_in_logical: true`: speed is in GAMA units per second and heading in GAMA degrees (0 = east, clockwise on screen).

```json
{"robot": "robot1", "speed": 5.0, "heading": 90}
{"requests": [{"robot": "robot1", "speed": 5, "heading": 0}, {"robot": "robot2", "stop": true}]}
{"robot1": {"speed": 5, "heading": 0}, "robot2": {"speed": 3, "heading": 180}}
```

- `"frame": "physical"` makes a request use m/s and radians (counter-clockwise).
- `"stop": true` stops the robot (and cancels a go-to).
- `{"robot": "robot1", "goto": {"x": 50, "y": 50, "heading": 90}}` drives the robot to a point (heading optional) with its calibration, then stops; `"goto": null` cancels. No need to resend it. A go-to has priority over speed requests for that robot.
- `{"place": "home"}` or `{"place": {"robot1": {"x": .., "y": ..}, ...}}` places several robots **one after the other**, so nobody has to put them at exact spots by hand before a run (`home` = each robot's configured home position, settable with *Use current position* in its card, or the *Place robots at home* button). Progress is in the robot state: `goal.status` = `queued`, `moving`, `turning`, `waiting`, `arrived`, `failed` or `cancelled`. Settings: `goto.*` (speed, tolerances, clearance to other robots, timeout).
- Any extra field (for example `goal_x`) is passed as is to the controller in `request.extra`.
- A request stays valid for `safety.request_timeout` seconds. GAMA must therefore resend it every cycle, otherwise the robot stops.

### Published state (excerpt)

```json
{"valid": true, "reprojection_error": 0.0004, "homography": [[...]], "physical_to_logical": [[...]],
 "robots": {"robot1": {"visible": true, "x": 0.53, "y": 0.50, "theta": 1.57, "speed": 0.0,
                       "logical": {"x": 33.3, "y": 31.2, "heading": -90.0, "speed": 0.0},
                       "goal": {"status": "arrived", "logical": {"x": 25, "y": 80, "heading": 270}}}}}
```

---

## Control and calibration

The command chain is: **request (physical) → controller → `BodyCommand` (v, ω, v_lat in physical units) → calibration model → raw command → topic**.

Provided controllers, each with parameters editable in the UI:

| Name | Principle |
|---|---|
| `proportional` | ω = k·heading error, v = speed × cos(error) |
| `heading_pid` | heading PID with anti-windup, turn in place above a threshold, optional reverse |
| `speed_heading_pid` | `heading_pid` + PI loop on the speed measured by the camera |
| `holonomic` | omnidirectional robots: world velocity projected onto (vx, vy), heading held or aligned |
| `passthrough` | no closed loop (v = speed, ω = the request's `angular` field) |
| `go_to_goal` | example plugin in `controllers/`: follows the heading or heads for `goal_x/goal_y` |

Per-robot **calibration procedures**, started from the UI or through `POST /api/robots/<name>/calibrate`. The robot goes back and forth in open loop, filmed by the camera:

- `heading_offset`: angle between the tag and the actual direction of travel.
- `linear`: gain and dead zone of the linear command, plus straight-line drift (compensated afterwards).
- `angular`: gain and dead zone in rotation. For a `wheels` output, gives the effective wheel base.
- `full`: the three in sequence. Results are applied and saved to the YAML.

Calibration stops if the tag is lost, if the robot leaves the area or if the emergency stop is triggered. Amplitudes (`linear_levels`, `angular_levels`), duration and pauses are set in the procedure options.

### Writing your own controller

Create a `.py` file in `controllers/`: from the UI (*Controllers* tab → *Create from template*), with `python -m tango_mw new-controller name`, or by hand.

```python
import math
from tango_mw.control import BodyCommand, Controller, Param

class MyController(Controller):
    name = "my_controller"
    params = {"k": Param(2.0, min=0, max=20, description="heading gain")}

    def reset(self):            # called when the robot (re)starts
        self.integral = 0.0

    def compute(self, state, request, dt):
        err = request.heading_error(state.theta)          # rad, in [-π, π)
        return BodyCommand(linear=request.speed * max(0, math.cos(err)),
                           angular=self.p["k"] * err)
```

- The file is **hot-reloaded** as soon as it is saved. Robots using this controller get a new instance.
- If the file has an error, the previous version stays active and the error is shown in the UI.
- If `compute` raises an exception, the robot is stopped and the traceback is shown.
- Parameters declared in `params` appear automatically in the UI.
- A pip package can also expose controllers through the `tango_mw.controllers` entry point.

---

## Administration

- **ROS network**: distribution, RMW, domain ID, discovery variables, list of nodes and topics (with the number of publishers and subscribers). For each middleware topic, the tab shows who is connected on the other side.
- **Robot availability**: a robot is "reachable" if at least one node subscribes to its command topic. The tab also shows whether its tag is seen.
- **GAMA**: number of publishers on `/tango/requests` and age of the last request.
- **Camera**: the header badge shows the frame rate; hover it to see why there is no image (camera closed, no frame for 3 s, waiting for a phone…).
- **Emergency stop**: button in the UI or topic `/tango/estop`. Robots also stop automatically if their tag is lost, if the request has expired, if the homography is missing or if they leave the area (they can then only come back in).
- **Configuration**: everything applies live, then *Save* writes the YAML. Changing the namespace, node or domain restarts the ROS bus. The web host and port only change on restart.
- **REST API** documented at `/api/docs`. It lets you automate the setup: config, calibration, requests, JPEG snapshot, MJPEG stream, tag PNGs.

> The controller editor executes Python code. If the interface is exposed on the network, disable it with `web.allow_code_edit: false`.

## Layout

```
tango_mw/
  config.py            configuration model (pydantic) + YAML
  frames.py            geometry, physical ↔ GAMA mapping
  vision/              camera, phone receiver (mobile.py), setup checks (quality.py), AprilTag detection, homography & tracking, synthetic camera
  control/             controller API, built-in controllers, registry/plugins, calibration, go-to/placement, loop
  bus/                 ROS 2 (rclpy) and loopback
  app.py               orchestration (vision / control / publication threads)
  service.py           start at boot (systemd / scheduled task / launchd)
  tagsheet.py          printable tags (SVG at real size, A4 sheets, ZIP)
  web/                 FastAPI server + interface (static/mobile.html: phone camera page)
controllers/           your controllers (hot-reloaded)
tests/                 python -m pytest
```
