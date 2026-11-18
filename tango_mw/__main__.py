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
        logging.info("Pas de configuration %s : création avec les valeurs par défaut", cfg_path)
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
        logging.info("Interface de configuration : http://%s:%d", "localhost" if cfg.web.host in ("0.0.0.0", "127.0.0.1") else cfg.web.host, cfg.web.port)
    else:
        server = None
    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        logging.info("Arrêt…")
        if server:
            server.should_exit = True
        app.stop()
    return 0


def cmd_new_controller(args) -> int:
    from .control.registry import ControllerRegistry

    reg = ControllerRegistry([args.dir], base_dir=Path.cwd())
    path = reg.create(args.name)
    print(f"Contrôleur créé : {path}")
    if reg.errors:
        print("Attention :", reg.errors)
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
        print(f"{p} existe déjà (utilisez --force)")
        return 1
    save_config(AppConfig(), p)
    print(f"Configuration par défaut écrite dans {p}")
    return 0


def main(argv=None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(prog="tango-mw", description="Middleware GAMA <-> robots physiques (AprilTag + ROS 2)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="lancer le middleware (défaut)")
    run.add_argument("-c", "--config", help=f"fichier YAML (défaut: {DEFAULT_CONFIG})")
    run.add_argument("--bus", choices=["auto", "ros2", "loopback"])
    run.add_argument("--camera", help="source caméra (index, fichier, URL ou 'synthetic')")
    run.add_argument("--host")
    run.add_argument("--port", type=int)
    run.add_argument("--no-web", action="store_true")
    run.set_defaults(func=cmd_run)

    nc = sub.add_parser("new-controller", help="créer un contrôleur depuis le modèle")
    nc.add_argument("name")
    nc.add_argument("--dir", default="controllers")
    nc.set_defaults(func=cmd_new_controller)

    pt = sub.add_parser("print-tags", help="générer des PNG de tags à imprimer")
    pt.add_argument("ids", type=int, nargs="+")
    pt.add_argument("--family", default="tag36h11")
    pt.add_argument("--size", type=int, default=800)
    pt.add_argument("--out", default="tags")
    pt.set_defaults(func=cmd_print_tags)

    ic = sub.add_parser("init-config", help="écrire une configuration par défaut")
    ic.add_argument("path", nargs="?", default=str(DEFAULT_CONFIG))
    ic.add_argument("--force", action="store_true")
    ic.set_defaults(func=cmd_init_config)

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
