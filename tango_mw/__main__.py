"""Command line entry point: ``python -m tango_mw`` or ``tango-mw``."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

DEFAULT_CONFIG = Path("config") / "config.yaml"


def _utf8_console() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def cmd_run(args) -> int:
    from .app import App
    from .config import load_config, save_config

    cfg_path = Path(args.config) if args.config else DEFAULT_CONFIG
    if not cfg_path.exists():
        logging.info("No configuration %s: creating it with default values", cfg_path)
        save_config(load_config(None), cfg_path)
    cfg = load_config(cfg_path)
    if args.bus:
        cfg.ros.bus = args.bus
    if args.camera is not None:
        cfg.camera.source = args.camera
    if args.host:
        cfg.web.host = args.host
    if args.port:
        cfg.web.port = args.port

    app = App(cfg, cfg_path, base_dir=Path.cwd())
    app.start()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    except Exception:  # pragma: no cover - not on every platform
        pass

    if cfg.web.enabled and not args.no_web:
        import uvicorn

        from .web.server import create_web_app

        server = uvicorn.Server(uvicorn.Config(create_web_app(app), host=cfg.web.host, port=cfg.web.port,
                                               log_level="warning", access_log=False, ws="none"))
        server.install_signal_handlers = lambda: None  # we handle signals ourselves
        t = threading.Thread(target=server.run, name="web", daemon=True)
        t.start()
        logging.info("Web interface: http://%s:%d", "localhost" if cfg.web.host in ("0.0.0.0", "127.0.0.1") else cfg.web.host, cfg.web.port)
    else:
        server = None
    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        logging.info("Stopping…")
        if server:
            server.should_exit = True
        app.stop()
    return 0


def cmd_new_controller(args) -> int:
    from .control.registry import ControllerRegistry

    reg = ControllerRegistry([args.dir], base_dir=Path.cwd())
    path = reg.create(args.name)
    print(f"Controller created: {path}")
    if reg.errors:
        print("Warning:", reg.errors)
    return 0


def cmd_print_tags(args) -> int:
    import cv2

    from .vision.detector import render_tag

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for i in args.ids:
        img = render_tag(args.family, i, args.size, quiet_zone=0.2)
        cv2.putText(img, f"{args.family} #{i}", (8, img.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX, args.size / 1200, 0, 1, cv2.LINE_AA)
        p = out / f"{args.family}_{i}.png"
        cv2.imwrite(str(p), img)
        print(p)
    return 0


def cmd_init_config(args) -> int:
    from .config import AppConfig, save_config

    p = Path(args.path)
    if p.exists() and not args.force:
        print(f"{p} already exists (use --force)")
        return 1
    save_config(AppConfig(), p)
    print(f"Default configuration written to {p}")
    return 0


def cmd_service(args) -> int:
    from . import service

    if args.action == "install":
        spec = service.spec_from_args(args)
        if not spec.config.exists() and not args.dry_run:
            from .config import load_config, save_config

            save_config(load_config(None), spec.config)
            print(f"Default configuration created: {spec.config}")
        return service.install(spec, dry_run=args.dry_run)
    if args.action == "uninstall":
        return service.uninstall()
    return service.status()


def main(argv=None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(prog="tango-mw", description="GAMA <-> physical robots middleware (AprilTag + ROS 2)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="run the middleware (default)")
    run.add_argument("-c", "--config", help=f"YAML file (default: {DEFAULT_CONFIG})")
    run.add_argument("--bus", choices=["auto", "ros2", "loopback"])
    run.add_argument("--camera", help="camera source (index, file, URL, 'mobile' or 'synthetic')")
    run.add_argument("--host")
    run.add_argument("--port", type=int)
    run.add_argument("--no-web", action="store_true")
    run.set_defaults(func=cmd_run)

    nc = sub.add_parser("new-controller", help="create a controller from the template")
    nc.add_argument("name")
    nc.add_argument("--dir", default="controllers")
    nc.set_defaults(func=cmd_new_controller)

    pt = sub.add_parser("print-tags", help="generate tag PNGs to print")
    pt.add_argument("ids", type=int, nargs="+")
    pt.add_argument("--family", default="tag36h11")
    pt.add_argument("--size", type=int, default=800)
    pt.add_argument("--out", default="tags")
    pt.set_defaults(func=cmd_print_tags)

    ic = sub.add_parser("init-config", help="write a default configuration")
    ic.add_argument("path", nargs="?", default=str(DEFAULT_CONFIG))
    ic.add_argument("--force", action="store_true")
    ic.set_defaults(func=cmd_init_config)

    sv = sub.add_parser("service", help="start automatically at boot (systemd / scheduled task / launchd)")
    sv.add_argument("action", choices=["install", "uninstall", "status"])
    sv.add_argument("-c", "--config", help=f"YAML file (default: <workdir>/{DEFAULT_CONFIG})")
    sv.add_argument("--workdir", help="working directory (default: current directory; contains controllers/)")
    sv.add_argument("--ros-setup", help="ROS 2 script to source (e.g. /opt/ros/jazzy/setup.bash, C:/dev/ros2/local_setup.bat)")
    sv.add_argument("--python", help="Python interpreter (default: this one)")
    sv.add_argument("--args", help="extra options for 'run', e.g. \"--bus ros2\"")
    sv.add_argument("--dry-run", action="store_true", help="print the generated file without installing anything")
    sv.set_defaults(func=cmd_service)

    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help", "-v", "--verbose"):
        argv = ["run", *argv]
    args = parser.parse_args(argv)
    if args.cmd is None:
        args = parser.parse_args([*argv, "run"]) if argv else parser.parse_args(["run"])
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
