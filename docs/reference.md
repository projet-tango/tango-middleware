---
title: 6. Talking to the middleware
nav_order: 7
---

# 6. Talking to the middleware
{: .no_toc }

1. TOC
{:toc}

Everything goes through ROS 2, as JSON text in `std_msgs/String` messages (namespace `/tango` by default).
With the ROS plugin for GAMA:

```gaml
ros_node node <- ros_node("my_model");
ros_publisher requests <- ros_publisher(node, ros_topic("/tango/requests", "std_msgs/msg/String"));
ros_subscription states <- ros_subscription(node, ros_topic("/tango/robots/state", "std_msgs/msg/String"), 1);

// send
bool sent <- ros_publish(requests, ["data"::to_json(["robot"::"robot1", "speed"::5.0, "heading"::90.0])]);
// receive
map<string, unknown> msg <- ros_read_latest(states);
if msg != nil { map<string, unknown> state <- map<string, unknown>(from_json(string(msg["data"]))); }
```

Only send numbers, strings, lists and maps in `to_json` (a GAML `point` is serialised with extra type
information): write `"x"::p.x, "y"::p.y`.

## Units

By default requests are in **GAMA units** (`logical.requests_in_logical: true`): positions in the GAMA
world, speeds in units per second, headings in degrees, 0 = east, clockwise on screen (GAMA's own
convention). Add `"frame": "physical"` to a request to use metres, m/s and radians (counter-clockwise).

## Requests: `/tango/requests`

| Message | Effect |
|---|---|
| `{"robot": "r1", "speed": 5, "heading": 90}` | drive at 5 units/s towards 90°. Must be resent every cycle (valid `safety.request_timeout` = 1 s) |
| `{"requests": [{"robot": "r1", ...}, {"robot": "r2", ...}]}` | several robots at once |
| `{"r1": {"speed": 5, "heading": 0}, "r2": {...}}` | same, shorter |
| `{"robot": "r1", "stop": true}` | stop now (also cancels a go-to) |
| `{"robot": "r1", "goto": {"x": 50, "y": 50, "heading": 0}}` | drive to a point (heading optional), then stop. No need to resend |
| `{"robot": "r1", "goto": [50, 50]}` | same, short form |
| `{"robot": "r1", "goto": null}` | cancel the go-to |
| `{"place": {"r1": {"x": .., "y": .., "heading": ..}, "r2": {...}}}` | go-to for several robots, **one after the other** |
| `{"place": "home"}` | every robot to its configured home, one after the other |

Any other field of a speed request (e.g. `goal_x`) is passed as is to the robot's controller
(`request.extra`), for custom controllers.

While a go-to is running it has priority over speed requests for that robot; it ends with `arrived`,
`failed` (timeout, `goto.timeout` = 60 s) or `cancelled`. A target outside the field is refused. A robot
waits while another one stands in its way (`goto.clearance`).

## State: `/tango/robots/state`

Published 10 times per second:

```json
{"stamp": 1763500000.1,
 "robots": {
   "robot1": {"visible": true, "ever_seen": true, "tag_id": 10,
              "x": 0.53, "y": 0.50, "theta": 1.57, "speed": 0.0,
              "logical": {"x": 35.3, "y": 66.7, "heading": 270.0, "speed": 0.0},
              "goal": {"status": "arrived", "logical": {"x": 25, "y": 80, "heading": 270}, "distance": 0.01}}}}
```

`x`, `y`, `theta` are physical (m, rad); `logical` is in GAMA units. `goal` is `null` when the robot never
received a go-to.

## Several cameras

`/tango/world/state` and `/tango/robots/state` carry the **fused** positions (all cameras). The REST
API has the details per camera: `GET /api/cameras/status` (state, homography, estimated position, robots
seen) and, in `GET /api/status`, `fusion` (for each robot: cameras seeing it, `average` or `triangulated`,
measured tag height). Video per camera: `/api/stream.mjpg?camera=<name>`, `/api/snapshot.jpg?camera=<name>`.

## Other topics

| Topic | Type | Content |
|---|---|---|
| `/tango/world/state` | `std_msgs/String` | full JSON: homographies, field outline, robots |
| `/tango/robots/<name>/pose` | `geometry_msgs/PoseStamped` | physical pose (frame `tango_world`) |
| `/tango/robots/<name>/request` | `std_msgs/Float64MultiArray` | `[speed, heading]`, alternative to JSON |
| `/tango/status` | `std_msgs/String` | middleware health, 1 Hz (camera, homography, setup checks, estop) |
| `/tango/estop` | `std_msgs/Bool` | `true` = emergency stop, `false` = release |
| `/<robot>/cmd_vel` | `geometry_msgs/Twist` (configurable) | commands sent to the robots |

The **ROS network** tab of the interface lists them all with what is connected on the other side, and
the REST API of the interface is documented at `http://localhost:8080/api/docs`.
