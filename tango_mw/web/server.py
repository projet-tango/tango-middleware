"""Web configuration / administration interface (FastAPI)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import cv2
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import ValidationError

from .. import deploy, tagsheet
from ..app import App
from ..bus.base import topic_map
from ..config import AppConfig
from ..control.calibration import DEFAULT_OPTIONS, PROCEDURES
from ..vision.camera import probe_cameras
from ..vision.detector import render_tag
from ..vision.mobile import qr_png

STATIC = Path(__file__).parent / "static"


def _bad(exc: Exception, code: int = 400):
    if isinstance(exc, ValidationError):
        detail = [{"loc": ".".join(str(x) for x in e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        raise HTTPException(code, detail=detail)
    raise HTTPException(code, detail=str(exc))


def create_web_app(app: App) -> FastAPI:
    api = FastAPI(title="Tango middleware", docs_url="/api/docs")
    api.mount("/static", StaticFiles(directory=STATIC), name="static")

    @api.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # ------------------------------------------------------------------ status
    @api.get("/api/status")
    def status():
        return JSONResponse(json.loads(json.dumps(app.status(), default=float)))

    @api.get("/api/checks")
    def setup_checks():
        """Lighting / image quality checks (ok, warn, bad, unknown) with advice."""
        return app.checks.result

    @api.get("/api/logs")
    def logs(after: int = 0):
        return [r for r in list(app.logs.records) if r["id"] > after]

    @api.get("/api/network")
    def network():
        info = app.bus.network_info()
        return {"info": info, "topic_map": topic_map(app.cfg), "bus": app.bus.kind, "bus_error": app.bus_error}

    # ------------------------------------------------------------------ config
    @api.get("/api/config")
    def get_config():
        return app.cfg.model_dump(mode="json")

    @api.get("/api/config/schema")
    def get_schema():
        return AppConfig.model_json_schema()

    @api.put("/api/config")
    def put_config(data: Dict[str, Any] = Body(...), save: bool = False):
        try:
            cfg = AppConfig.model_validate(data)
        except ValidationError as exc:
            _bad(exc)
        app.apply_config(cfg)
        if save:
            app.save()
        return {"ok": True, "saved": save}

    @api.patch("/api/config/{section}")
    def patch_section(section: str, data: Any = Body(...), save: bool = False):
        cur = app.cfg.model_dump(mode="json")
        if section not in cur:
            raise HTTPException(404, f"unknown section: {section}")
        cur[section] = data
        try:
            cfg = AppConfig.model_validate(cur)
        except ValidationError as exc:
            _bad(exc)
        app.apply_config(cfg)
        if save:
            app.save()
        return {"ok": True}

    @api.post("/api/config/save")
    def save_cfg():
        return {"path": str(app.save())}

    # ------------------------------------------------------------------ video
    def _camera(name: Optional[str]) -> Optional[str]:
        if name and name not in app.units:
            raise HTTPException(404, f"unknown camera: {name}")
        return name

    @api.get("/api/cameras/status")
    def cameras_status():
        """Every camera: state, homography, estimated position, robots it sees."""
        return app.cameras_status()

    @api.get("/api/snapshot.jpg")
    def snapshot(camera: Optional[str] = None):
        jpg = app.annotated_jpeg(camera=_camera(camera))
        if jpg is None:
            raise HTTPException(503, "no image")
        return Response(jpg, media_type="image/jpeg")

    @api.get("/api/stream.mjpg")
    def stream(camera: Optional[str] = None):
        cam = _camera(camera)

        def gen():
            period = 1.0 / max(app.cfg.web.preview_fps, 0.5)
            last_seq = -1
            while True:
                t0 = time.time()
                u = app.unit(cam)
                if u is not None and u.frame_seq != last_seq:
                    last_seq = u.frame_seq
                    jpg = app.annotated_jpeg(camera=cam)
                    if jpg:
                        yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
                time.sleep(max(0.01, period - (time.time() - t0)))

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    @api.get("/api/cameras")
    def cameras():
        return probe_cameras()

    @api.get("/api/tags/{tag_id}.png")
    def tag_png(tag_id: int, family: Optional[str] = None, size: int = Query(600, ge=50, le=4000)):
        fam = family or app.cfg.detector.family
        try:
            img = render_tag(fam, tag_id, size, quiet_zone=0.2)
        except Exception as exc:
            _bad(exc)
        cv2.putText(img, f"{fam} #{tag_id}", (8, img.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX, size / 1200, 0, 1, cv2.LINE_AA)
        ok, buf = cv2.imencode(".png", img)
        return Response(buf.tobytes(), media_type="image/png")

    # ------------------------------------------------------------------ phone camera
    @api.get("/api/mobile")
    def mobile_status():
        any_mobile = any(c.is_mobile for c in app.cfg.all_cameras())
        if app.mobile is None:
            return {"running": False, "source_is_mobile": any_mobile, "error": app.camera_error}
        return {**app.mobile.describe(), "source_is_mobile": True}

    @api.post("/api/mobile/new_code")
    def mobile_new_code():
        return {"pairing_code": app.new_pairing_code()}

    @api.post("/api/mobile/disconnect")
    def mobile_disconnect(camera: Optional[str] = None):
        if app.mobile:
            app.mobile.kick(camera)
        return {"ok": True}

    @api.post("/api/mobile/forget")
    def mobile_forget(camera: str):
        """Unbind the phone of a camera: the next phone that connects takes it."""
        if app.mobile:
            app.mobile.forget(camera)
        return {"ok": True}

    @api.get("/api/mobile/qr.png")
    def mobile_qr(data: str = Query(..., max_length=400)):
        try:
            return Response(qr_png(data), media_type="image/png", headers={"Cache-Control": "max-age=3600"})  # content depends only on the URL
        except Exception as exc:
            _bad(exc)

    # ------------------------------------------------------------------ printable tags
    def _tag_items(which: str, ids: Optional[str], size_mm: Optional[float], robot_size_mm: Optional[float]):
        id_list = [int(x) for x in (ids or "").replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()]
        if which not in ("world", "robots", "all", "custom"):
            raise HTTPException(400, "which must be world, robots, all or custom")
        items = tagsheet.items_for(app.cfg, which, id_list, size_mm, robot_size_mm)
        if not items:
            raise HTTPException(404, "no tag to print")
        return items

    @api.get("/api/tags/list")
    def tag_list():
        """Tags of the configuration (boundary + robots) with their default print size."""
        return {"family": app.cfg.detector.family, "items": tagsheet.items_for(app.cfg, "all")}

    @api.get("/api/tags/{tag_id}.svg")
    def tag_svg(tag_id: int, size_mm: float = Query(100, gt=5, le=1000), front: bool = False, caption: Optional[str] = None):
        """Vector tag at real size (black square side in mm)."""
        return Response(tagsheet.tag_svg(app.cfg.detector.family, tag_id, size_mm, caption, front), media_type="image/svg+xml")

    @api.get("/print/tags", response_class=HTMLResponse)
    def print_tags(which: str = "all", ids: Optional[str] = None, size_mm: Optional[float] = Query(None, gt=5, le=1000),
                   robot_size_mm: Optional[float] = Query(None, gt=5, le=1000)):
        """A4 print sheet (print at 100 %), with a 100 mm ruler to check the scale."""
        items = _tag_items(which, ids, size_mm, robot_size_mm)
        return HTMLResponse(tagsheet.print_page(app.cfg.detector.family, items, f"Tango tags ({which})"))

    @api.get("/api/tags/archive.zip")
    def tag_archive(which: str = "all", ids: Optional[str] = None, size_mm: Optional[float] = Query(None, gt=5, le=1000),
                    robot_size_mm: Optional[float] = Query(None, gt=5, le=1000)):
        items = _tag_items(which, ids, size_mm, robot_size_mm)
        return Response(tagsheet.archive(app.cfg.detector.family, items), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="tango-tags-{which}.zip"'})

    # ------------------------------------------------------------------ go to position / placement
    @api.post("/api/robots/{name}/goto")
    def robot_goto(name: str, data: Dict[str, Any] = Body(...)):
        """{"x", "y", "heading"?, "frame": "logical"|"physical"} — logical = GAMA units."""
        if app.cfg.robot(name) is None:
            raise HTTPException(404, f"unknown robot: {name}")
        try:
            goal = app.make_goal(data, data.get("frame", "logical"), "web")
        except ValueError as exc:
            _bad(exc)
        app.manager.set_goal(name, goal)
        return {"ok": True}

    @api.delete("/api/robots/{name}/goto")
    def robot_goto_cancel(name: str):
        app.manager.set_goal(name, None)
        return {"ok": True}

    @api.post("/api/place")
    def place(data: Dict[str, Any] = Body(default={})):
        """Bring robots to their home (default) or to {"robots": {name: pose}}, one after the other."""
        names = app.place(data.get("robots") or "home", data.get("frame", "logical"), "web")
        if not names:
            raise HTTPException(400, "nothing to place: set a home position on the robots (Robots tab)")
        return {"placing": names}

    @api.post("/api/robots/{name}/home_here")
    def home_here(name: str):
        """Use the robot's current position and heading as its home (GAMA coordinates)."""
        cfg = app.cfg.model_copy(deep=True)
        r = cfg.robot(name)
        o = app.snapshot.robots.get(name)
        if r is None:
            raise HTTPException(404, f"unknown robot: {name}")
        if o is None or not o.visible or app.snapshot.H is None:
            raise HTTPException(409, f"{name} is not visible")
        lg = app.logical.pose_to_logical(o.x, o.y, o.theta)
        from ..config import HomePose

        r.home = HomePose(x=round(lg["x"], 2), y=round(lg["y"], 2), heading=round(lg["heading"], 1))
        app.apply_config(cfg)
        if app.config_path:
            app.save()
        return r.home.model_dump()

    @api.post("/api/world/relock")
    def relock():
        app.relock_homography()
        return {"ok": True}

    # ------------------------------------------------------------------ control
    @api.post("/api/estop")
    def estop(on: bool = Body(..., embed=True)):
        app.manager.set_estop(on)
        return {"estop": on}

    @api.post("/api/robots/{name}/request")
    def manual_request(name: str, data: Dict[str, Any] = Body(...)):
        if not app.handle_request(name, data, "web"):
            raise HTTPException(404, f"unknown robot or invalid request: {name}")
        return {"ok": True}

    @api.get("/api/calibration/procedures")
    def procedures():
        return {"procedures": PROCEDURES, "default_options": DEFAULT_OPTIONS}

    @api.post("/api/robots/{name}/calibrate")
    def calibrate(name: str, data: Dict[str, Any] = Body(...)):
        try:
            job = app.manager.start_calibration(name, data.get("procedure", "full"), data.get("options") or {})
        except KeyError:
            raise HTTPException(404, f"unknown robot: {name}")
        except Exception as exc:
            _bad(exc)
        return job.describe()

    @api.delete("/api/robots/{name}/calibrate")
    def cancel_calibration(name: str):
        app.manager.cancel_calibration(name)
        return {"ok": True}

    # ------------------------------------------------------------------ deployment
    @api.get("/api/setup/scan")
    def setup_scan(duration: float = Query(1.0, ge=0.1, le=10.0)):
        return deploy.scan_tags(app, duration)

    @api.post("/api/setup/propose_world")
    def setup_propose_world(data: Dict[str, Any] = Body(...)):
        """Corner tags -> world corners. Width/height (m, centre to centre) are optional: when left
        out they are estimated from the printed tag size (world.tag_size), no measuring needed."""
        scan = deploy.scan_tags(app, float(data.get("duration", 1.0)))
        rot, mirror, ids = int(data.get("rotation", 0)), bool(data.get("mirror", False)), data.get("ids")
        estimated = None
        try:
            width, height = data.get("width"), data.get("height")
            if not width or not height:
                estimated = deploy.estimate_field(scan, app.cfg.world.tag_size, rot, mirror, ids)
                width, height = width or estimated["width"], height or estimated["height"]
            tags = deploy.propose_world_corners(scan["tags"], float(width), float(height), rot, mirror, ids)
        except (ValueError, KeyError) as exc:
            _bad(exc)
        return {"tags": tags, "scan": scan, "estimated": estimated}

    @api.post("/api/setup/measure_tags")
    def setup_measure(data: Dict[str, Any] = Body(default={})):
        scan = deploy.scan_tags(app, float(data.get("duration", 1.0)))
        try:
            return {"tags": deploy.measure_tags(scan, data.get("ids")), "scan": scan}
        except ValueError as exc:
            _bad(exc)

    @api.get("/api/setup/discover_robots")
    def setup_discover(template: Optional[str] = None):
        tpl = template or (app.cfg.robots[0].output.topic if app.cfg.robots else "/{name}/cmd_vel")
        try:
            return {**deploy.discover_robots(app.bus.network_info(), tpl, app.cfg), "template": tpl}
        except ValueError as exc:
            _bad(exc)

    @api.post("/api/robots/{name}/identify")
    def identify(name: str, data: Dict[str, Any] = Body(default={})):
        try:
            job = app.manager.start_identify(name, data.get("options") or {})
        except KeyError:
            raise HTTPException(404, f"unknown robot: {name}")
        except Exception as exc:
            _bad(exc)
        return job.describe()

    @api.post("/api/robots/{name}/assign_tag")
    def assign_tag(name: str, tag_id: int = Body(..., embed=True)):
        if app.cfg.robot(name) is None:
            raise HTTPException(404, f"unknown robot: {name}")
        try:
            app.assign_tag(name, tag_id)
        except Exception as exc:
            _bad(exc)
        return {"ok": True}

    # ------------------------------------------------------------------ controllers
    @api.get("/api/controllers")
    def controllers():
        return {**app.registry.describe(), "allow_code_edit": app.cfg.web.allow_code_edit}

    @api.post("/api/controllers/reload")
    def reload_controllers():
        app.registry.reload(force=True)
        return app.registry.describe()

    def _check_edit():
        if not app.cfg.web.allow_code_edit:
            raise HTTPException(403, "code editing disabled (web.allow_code_edit)")

    @api.get("/api/controllers/source/{filename}")
    def get_source(filename: str):
        _check_edit()
        try:
            return {"filename": filename, "code": app.registry.read_source(filename)}
        except FileNotFoundError:
            raise HTTPException(404, filename)
        except Exception as exc:
            _bad(exc)

    @api.put("/api/controllers/source/{filename}")
    def put_source(filename: str, data: Dict[str, str] = Body(...)):
        _check_edit()
        try:
            path = app.registry.write_source(filename, data["code"])
        except SyntaxError as exc:
            raise HTTPException(400, f"Syntax error line {exc.lineno}: {exc.msg}")
        except Exception as exc:
            _bad(exc)
        return {"path": str(path), "errors": app.registry.errors.get(str(path))}

    @api.post("/api/controllers/new")
    def new_controller(data: Dict[str, str] = Body(...)):
        _check_edit()
        try:
            path = app.registry.create(data["name"])
        except FileExistsError as exc:
            raise HTTPException(409, f"already exists: {exc}")
        except Exception as exc:
            _bad(exc)
        return {"path": str(path), "filename": path.name}

    return api
