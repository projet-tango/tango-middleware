---
title: 7. Troubleshooting
nav_order: 8
---

# 7. Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| The phone stays on *Looking for the middleware* | not on the same network, or the network isolates devices → same Wi-Fi, a dedicated router; on Windows, network set as *private* and Python allowed through the firewall. You can also type the PC's address (`192.168.x.y:47800`) in the app |
| *Wrong pairing code* on the phone | type the code shown in the Camera tab, or scan the QR code. *New code* in the middleware disconnects paired phones on purpose |
| *Another phone is already streaming* | only one phone at a time: stop the other one (or wait 3 s after it stopped) |
| The phone stops streaming after a while | battery optimisation → *Disable battery optimization* in the app; some vendors also need the app allowed in their "auto-start" list |
| Low frame rate | dim light (the *Light level* check), Wi-Fi congestion, or a too high resolution; 1280×960 at 30 fps is a good default |
| *Homography missing* | fewer than 4 boundary tags visible → check the live view; a robot over a corner tag hides it |
| Large reprojection error | the tag positions do not match the floor → measure again between **centres**; check the field is square (diagonals) |
| A robot is *not visible* | tag hidden, tilted, too small, glare → see the setup checks; check the robot's `tag_id` |
| Robot positions jump when they move | motion blur → more light and `max_exposure_ms` 10, or slower robots |
| Robot *no subscriber* / not reachable | its ROS driver is not running, other namespace, other `ROS_DOMAIN_ID` → `ros2 topic list` from the PC |
| A robot drives the wrong way | `invert_linear` / `invert_angular` in its calibration, or wrong FRONT arrow → re-run the calibration |
| A robot curves or overshoots | run the **full** calibration again (battery level matters); tune the `heading_pid` gains |
| *Placement failed (timeout)* | the robot could not reach its home: blocked by another robot, target too close to the edge, or calibration off |
| GAMA shows *GAMA silent* in the middleware | GAMA's ROS plugin not on the same domain, or the experiment not running → check `ros2 topic echo /tango/requests` |
| Brightness pulses between frames (*Light flicker*) | short exposure under mains lights → `max_exposure_ms` 10 or 20 (8.3 / 16.7 in 60 Hz countries) |
| Robots stop every second | GAMA does not resend its requests every cycle (they expire after `safety.request_timeout`) |

Still stuck? The **System** tab shows the log; the REST API (`/api/docs`) exposes everything the interface
shows, and `GET /api/checks` returns the setup checks as JSON.
