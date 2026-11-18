"""Phone camera source: a phone pushes JPEG frames to the middleware.

Everything lives in :class:`MobileHub`, which is independent of the vision loop
so that a phone can connect, drop and reconnect at any time:

* an HTTP/WebSocket server on ``camera.mobile.port`` (Android app), plus an
  optional HTTPS server on ``camera.mobile.https_port`` (browsers need HTTPS
  to access the camera, e.g. Safari on iPhone);
* a UDP discovery responder on the same port number, so the app finds the
  middleware on its own.

Routes: ``GET /`` web camera page, ``GET /info`` service description,
``WS /ws`` frame stream.

WebSocket protocol (v1)::

    phone  -> server  text   {"type": "hello", "code": "123456", "device": "<stable id>",
                              "name": "Pixel 7", "platform": "android", "app_version": "1.0"}
    server -> phone   text   {"type": "welcome", ...} then {"type": "settings", ...}
                             or {"type": "error", "reason": "bad_code" | "busy"} then close
    phone  -> server  binary one JPEG image per message
    phone  -> server  text   {"type": "stats", "fps": .., "battery": .., "charging": .., ...}
    server -> phone   text   {"type": "settings", ...} again whenever the configuration changes

Close codes: 4400 bad hello, 4401 bad pairing code, 4409 another phone is
streaming, 4000 replaced by a newer connection from the same phone.

UDP discovery: the phone broadcasts ``{"type": "tango_discover"}`` to the port,
the middleware answers (unicast) with ``{"type": "tango_here", "port": .., ...}``.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import ipaddress
import json
import logging
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket  # module level: FastAPI resolves the (string) annotations here
from fastapi.responses import FileResponse, JSONResponse
from starlette.websockets import WebSocketDisconnect

from ..config import AppConfig
from .camera import FrameSource

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
STATIC = Path(__file__).resolve().parent.parent / "web" / "static"


def new_pairing_code() -> str:
    return f"{secrets.randbelow(10 ** 6):06d}"


def lan_addresses() -> List[str]:
    """Best guess of the IPv4 addresses a phone on the LAN can reach us at (primary first)."""
    found: List[str] = []
    try:  # no packet is sent: this only asks the OS which interface routes outside
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return [ip for ip in found if not ip.startswith(("127.", "169.254."))]


def ensure_certificate(directory: Path) -> Tuple[Path, Path]:
    """Self-signed certificate for the HTTPS camera page (created once, reused afterwards)."""
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / "mobile_cert.pem", directory / "mobile_key.pem"
    if cert.exists() and key.exists():
        return cert, key
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    pk = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname()
    sans: List[x509.GeneralName] = [x509.DNSName(host), x509.DNSName("localhost")]
    for ip in lan_addresses() + ["127.0.0.1"]:
        sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Tango middleware ({host})")])
    now = _dt.datetime.now(_dt.timezone.utc)
    crt = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(pk.public_key())
           .serial_number(x509.random_serial_number())
           .not_valid_before(now - _dt.timedelta(days=1)).not_valid_after(now + _dt.timedelta(days=3650))
           .add_extension(x509.SubjectAlternativeName(sans), critical=False)
           .sign(pk, hashes.SHA256()))
    key.write_bytes(pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    cert.write_bytes(crt.public_bytes(serialization.Encoding.PEM))
    log.info("Self-signed certificate created for the camera page: %s", cert)
    return cert, key


@dataclass
class _Client:
    ws: Any
    loop: asyncio.AbstractEventLoop
    device: str
    name: str
    platform: str
    app_version: str
    address: str
    secure: bool
    connected_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    last_frame: float = 0.0
    frames: int = 0
    bytes: int = 0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    stats: Dict[str, Any] = field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "device": self.device, "name": self.name, "platform": self.platform, "app_version": self.app_version,
            "address": self.address, "secure": self.secure, "connected_for": now - self.connected_at,
            "frame_age": (now - self.last_frame) if self.last_frame else None, "frames": self.frames,
            "fps": self.fps, "kbytes": self.bytes // 1024, "width": self.width, "height": self.height, "stats": self.stats,
        }


class _Server:
    """One uvicorn server (HTTP or HTTPS) running in its own thread on a socket we bound ourselves."""

    def __init__(self, asgi, host: str, port: int, ssl: Optional[Tuple[Path, Path]] = None):
        import uvicorn

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.bind((host, port))  # raises cleanly if the port is taken
        except OSError:
            self.sock.close()
            raise
        kw = {"ssl_certfile": str(ssl[0]), "ssl_keyfile": str(ssl[1])} if ssl else {}
        self.server = uvicorn.Server(uvicorn.Config(asgi, log_level="warning", access_log=False, ws="auto",
                                                    ws_ping_interval=5.0, ws_ping_timeout=10.0, **kw))
        self.server.install_signal_handlers = lambda: None
        self.thread = threading.Thread(target=self._run, name=f"mobile-{port}", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self.server.run(sockets=[self.sock])
        except BaseException as exc:  # pragma: no cover - SystemExit from uvicorn on startup errors
            log.error("Phone server stopped: %r", exc)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=3.0)
        try:
            self.sock.close()
        except OSError:
            pass


class _Slot:
    """A camera whose source is 'mobile': at most one phone feeds it."""

    def __init__(self, name: str):
        self.name = name
        self.client: Optional[_Client] = None
        self.jpeg: Optional[bytes] = None
        self.stamp = 0.0
        self.seq = 0


class MobileHub:
    """Receives the phones. Each camera configured with source 'mobile' is a slot; a phone is
    assigned to a slot when it connects: the slot it fed last time (remembered in phones.json), else
    a free slot, else a slot whose phone has been silent for 'stale_after' seconds."""

    def __init__(self, cfg: AppConfig, cert_dir: Path):
        self.cfg = cfg
        self.cert_dir = Path(cert_dir)
        self.error: Optional[str] = None
        self.https_error: Optional[str] = None
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._slots: Dict[str, _Slot] = {}
        self._bindings: Dict[str, str] = self._load_bindings()   # camera name -> device id
        self._servers: List[_Server] = []
        self._udp: Optional[socket.socket] = None
        self._udp_thread: Optional[threading.Thread] = None
        self._running = False
        self._bound: Tuple[Any, ...] = ()
        self.instance = secrets.token_hex(4)
        self._sync_slots(cfg)

    # ------------------------------------------------------------------ slots
    @staticmethod
    def mobile_cameras(cfg: AppConfig) -> List[Any]:
        return [c for c in cfg.all_cameras() if c.is_mobile]

    def _sync_slots(self, cfg: AppConfig) -> None:
        names = [c.name for c in self.mobile_cameras(cfg)]
        with self._lock:
            for n in names:
                self._slots.setdefault(n, _Slot(n))
            gone = [n for n in self._slots if n not in names]
        for n in gone:
            self.kick(n, reason="camera_removed")
            with self._lock:
                self._slots.pop(n, None)

    def slot_names(self) -> List[str]:
        return list(self._slots)

    def _bindings_path(self) -> Path:
        return self.cert_dir / "phones.json"

    def _load_bindings(self) -> Dict[str, str]:
        try:
            return {str(k): str(v) for k, v in json.loads(self._bindings_path().read_text(encoding="utf-8")).items()}
        except Exception:
            return {}

    def _save_bindings(self) -> None:
        try:
            self.cert_dir.mkdir(parents=True, exist_ok=True)
            self._bindings_path().write_text(json.dumps(self._bindings, indent=1), encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            log.warning("phones.json not saved: %s", exc)

    def _assign(self, device: str, now: float, stale_after: float) -> Tuple[Optional[str], Optional[_Client]]:
        """Pick the slot of a connecting phone (called with the lock held). Returns (slot, replaced client)."""
        slots = self._slots
        for name, dev in self._bindings.items():          # the camera this phone fed last time
            if dev == device and name in slots:
                return name, slots[name].client
        for name, sl in slots.items():                    # a camera nobody has fed yet
            if name not in self._bindings and sl.client is None:
                return name, None
        for name, sl in slots.items():                    # a camera whose phone is gone
            c = sl.client
            if c is None or now - max(c.last_activity, c.connected_at) >= stale_after:
                return name, c
        return None, None

    def forget(self, camera: str) -> None:
        """Unbind the phone of a camera: the next phone that connects can take it."""
        self._bindings.pop(camera, None)
        self._save_bindings()
        self.kick(camera, reason="forgotten")

    # ------------------------------------------------------------------ lifecycle
    def _binding(self, cfg: AppConfig) -> Tuple[Any, ...]:
        m = cfg.camera.mobile
        return (m.host, m.port, m.https_port)

    def start(self) -> None:
        m = self.cfg.camera.mobile
        self._running = True
        self._bound = self._binding(self.cfg)
        asgi = self._make_asgi()
        try:
            self._servers.append(_Server(asgi, m.host, m.port))
        except OSError as exc:
            self.error = f"port {m.port} unavailable: {exc}"
            raise RuntimeError(f"Phone receiver: {self.error}") from exc
        self.error = None
        if m.https_port:
            try:
                self._servers.append(_Server(asgi, m.host, m.https_port, ensure_certificate(self.cert_dir)))
                self.https_error = None
            except ImportError:
                self.https_error = "'cryptography' package missing: HTTPS camera page disabled"
                log.warning(self.https_error)
            except Exception as exc:
                self.https_error = f"HTTPS unavailable (port {m.https_port}): {exc}"
                log.warning(self.https_error)
        self._start_udp(m.port)
        log.info("Phone receiver ready: ws://%s:%d/ws%s — pairing code %s — camera(s): %s",
                 (lan_addresses() or ["?"])[0], m.port,
                 f", web page https://…:{m.https_port}/" if m.https_port and not self.https_error else "",
                 m.pairing_code if m.require_code else "(disabled)", ", ".join(self._slots) or "none")

    def stop(self) -> None:
        self._running = False
        for n in list(self._slots):
            self.kick(n, reason="server_stopped", code=1001)
        for srv in self._servers:
            srv.stop()
        self._servers = []
        if self._udp is not None:
            try:
                self._udp.close()
            except OSError:
                pass
            self._udp = None
        if self._udp_thread:
            self._udp_thread.join(timeout=2.0)
        with self._cond:
            self._cond.notify_all()

    def update_config(self, cfg: AppConfig) -> None:
        old_code = self.cfg.camera.mobile.pairing_code
        self.cfg = cfg
        self._sync_slots(cfg)
        if self._binding(cfg) != self._bound:
            log.info("Phone receiver: address/ports changed, restarting")
            self.stop()
            self.start()
            return
        m = cfg.camera.mobile
        if m.require_code and m.pairing_code != old_code:
            for n in list(self._slots):
                self.kick(n, reason="bad_code", code=4401)  # the phones have to pair again
        else:
            self.push_settings()

    # ------------------------------------------------------------------ frames
    def wait_frame(self, slot: str, after_seq: int, timeout: float) -> Tuple[int, Optional[bytes], float]:
        with self._cond:
            sl = self._slots.get(slot)
            if sl is not None and sl.seq == after_seq:
                self._cond.wait(timeout)
                sl = self._slots.get(slot)
            if sl is None:
                return after_seq, None, 0.0
            return sl.seq, sl.jpeg, sl.stamp

    def _push(self, slot: str, client: _Client, data: bytes) -> None:
        now = time.time()
        with self._lock:
            sl = self._slots.get(slot)
            if sl is None or sl.client is not client:
                return
            dt = now - client.last_frame if client.last_frame else 0.0
            client.last_frame = client.last_activity = now
            client.frames += 1
            client.bytes += len(data)
            if 0 < dt < 5:
                client.fps = 0.9 * client.fps + 0.1 / dt if client.fps else 1.0 / dt
        with self._cond:
            sl.jpeg, sl.stamp = data, now
            sl.seq += 1
            self._cond.notify_all()

    # ------------------------------------------------------------------ phone control
    def settings(self, slot: Optional[str] = None) -> Dict[str, Any]:
        cam = self.cfg.camera_named(slot) or self.cfg.camera
        m = cam.mobile
        return {"type": "settings", "camera": cam.name, "width": cam.width, "height": cam.height, "fps": cam.fps,
                "jpeg_quality": m.jpeg_quality, "lens": m.lens, "focus": m.focus,
                "exposure_compensation": m.exposure_compensation, "torch": m.torch,
                "max_exposure_ms": m.max_exposure_ms}

    def _send(self, client: _Client, coro_factory) -> None:
        try:
            asyncio.run_coroutine_threadsafe(coro_factory(), client.loop)
        except RuntimeError:  # loop closed
            pass

    def push_settings(self) -> None:
        for name, sl in list(self._slots.items()):
            c = sl.client
            if c is not None:
                msg = self.settings(name)
                self._send(c, lambda c=c, msg=msg: c.ws.send_json(msg))

    def kick(self, slot: Optional[str] = None, reason: str = "kicked", code: int = 4000) -> None:
        """Disconnect the phone of one camera (default: every phone)."""
        names = [slot] if slot else list(self._slots)
        for n in names:
            with self._lock:
                sl = self._slots.get(n)
                c = sl.client if sl else None
                if sl is not None:
                    sl.client = None
            if c is not None:
                async def bye(c=c):
                    try:
                        await c.ws.send_json({"type": "error", "reason": reason})
                        await c.ws.close(code)
                    except Exception:
                        pass
                self._send(c, bye)

    # ------------------------------------------------------------------ status
    @property
    def client(self) -> Optional[_Client]:
        """The phone of the first phone camera (single-camera setups)."""
        sl = next(iter(self._slots.values()), None)
        return sl.client if sl else None

    def client_of(self, slot: str) -> Optional[_Client]:
        sl = self._slots.get(slot)
        return sl.client if sl else None

    def urls(self) -> Dict[str, Any]:
        m = self.cfg.camera.mobile
        ips = lan_addresses() if m.host in ("0.0.0.0", "") else [m.host]
        frag = f"#c={m.pairing_code}" if m.require_code else ""
        # One link for everything: the iPhone camera app opens it in Safari, the Android app reads
        # host + ?app=<port> + #c=<code> from it when the QR code is scanned.
        if m.https_port and not self.https_error:
            web = [f"https://{ip}:{m.https_port}/?app={m.port}{frag}" for ip in ips]
        else:
            web = [f"http://{ip}:{m.port}/?app={m.port}{frag}" for ip in ips]
        return {"addresses": ips, "app": [f"{ip}:{m.port}" for ip in ips], "web": web}

    def describe(self) -> Dict[str, Any]:
        m = self.cfg.camera.mobile
        c = self.client
        cams = [{"camera": n, "device": self._bindings.get(n), "client": sl.client.describe() if sl.client else None}
                for n, sl in self._slots.items()]
        return {
            "running": bool(self._servers), "error": self.error, "https_error": self.https_error,
            "port": m.port, "https_port": m.https_port if not self.https_error else None,
            "require_code": m.require_code, "pairing_code": m.pairing_code,
            "discovery": self._udp is not None, "client": c.describe() if c else None, "cameras": cams, **self.urls(),
        }

    def waiting_reason(self, slot: Optional[str] = None) -> Optional[str]:
        if self.error:
            return self.error
        c = self.client_of(slot) if slot else self.client
        if c is None:
            return "waiting for a phone"
        if not c.last_frame or time.time() - c.last_frame > 2.0:
            return f"phone {c.name} connected, no frames"
        return None

    def _all_busy(self) -> bool:
        return bool(self._slots) and all(sl.client is not None for sl in self._slots.values())

    # ------------------------------------------------------------------ UDP discovery
    def _start_udp(self, port: int) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("", port))
        except OSError as exc:
            log.warning("Automatic discovery disabled (UDP %d: %s)", port, exc)
            sock.close()
            return
        sock.settimeout(0.5)
        self._udp = sock
        self._udp_thread = threading.Thread(target=self._udp_loop, args=(sock,), name="mobile-discovery", daemon=True)
        self._udp_thread.start()

    def _udp_loop(self, sock: socket.socket) -> None:
        while self._running and self._udp is sock:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if not isinstance(msg, dict) or msg.get("type") != "tango_discover":
                continue
            m = self.cfg.camera.mobile
            reply = {"type": "tango_here", "service": "tango-mw", "v": PROTOCOL_VERSION, "id": self.instance,
                     "name": socket.gethostname(), "port": m.port,
                     "https_port": m.https_port if not self.https_error else None,
                     "code_required": m.require_code, "busy": self._all_busy()}
            try:
                sock.sendto(json.dumps(reply).encode("utf-8"), addr)
            except OSError:
                pass

    # ------------------------------------------------------------------ ASGI app
    def _make_asgi(self):
        api = FastAPI(title="Tango mobile camera", docs_url=None, redoc_url=None, openapi_url=None)
        hub = self

        @api.get("/")
        def page():
            return FileResponse(STATIC / "mobile.html", headers={"Cache-Control": "no-store"})

        @api.get("/manifest.webmanifest")
        def manifest():
            return JSONResponse({"name": "Tango camera", "short_name": "Tango cam", "start_url": "/",
                                 "display": "fullscreen", "background_color": "#000000", "theme_color": "#000000"},
                                media_type="application/manifest+json")

        @api.get("/info")
        def info():
            m = hub.cfg.camera.mobile
            return {"service": "tango-mw", "v": PROTOCOL_VERSION, "id": hub.instance, "name": socket.gethostname(),
                    "code_required": m.require_code, "busy": hub._all_busy(), "cameras": hub.slot_names()}

        @api.websocket("/ws")
        async def ws_endpoint(ws: WebSocket):
            await ws.accept()
            try:
                hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10.0))
                assert isinstance(hello, dict) and hello.get("type") == "hello"
            except Exception:
                await _refuse(ws, "bad_hello", 4400)
                return
            m = hub.cfg.camera.mobile
            peer = f"{ws.client.host}:{ws.client.port}" if ws.client else "?"
            if m.require_code and str(hello.get("code", "")).strip() != m.pairing_code:
                log.warning("Phone %s (%s) refused: wrong pairing code", hello.get("name", "?"), peer)
                await _refuse(ws, "bad_code", 4401)
                return
            c = _Client(ws=ws, loop=asyncio.get_running_loop(), device=str(hello.get("device") or peer)[:80],
                        name=str(hello.get("name") or "phone")[:60], platform=str(hello.get("platform") or "?")[:20],
                        app_version=str(hello.get("app_version") or "")[:20], address=peer,
                        secure=ws.url.scheme == "wss")
            now = time.time()
            with hub._lock:
                slot, cur = hub._assign(c.device, now, m.stale_after)
                if slot is not None:
                    hub._slots[slot].client = c
                    changed = hub._bindings.get(slot) != c.device
                    hub._bindings[slot] = c.device
                else:
                    changed = False
            if slot is None:
                log.warning("Phone %s refused: every phone camera already has a phone (%s)", c.name,
                            ", ".join(hub.slot_names()) or "no camera set to 'mobile'")
                await _refuse(ws, "busy", 4409)
                return
            if changed:
                hub._save_bindings()
            if cur is not None and cur is not c:
                log.info("Camera %s: phone %s replaced by %s", slot, cur.name, c.name)
                hub._send(cur, lambda: _refuse(cur.ws, "replaced", 4000))
            log.info("Phone connected: %s (%s, %s) -> camera %s", c.name, c.platform, peer, slot)
            try:
                await ws.send_json({"type": "welcome", "server": socket.gethostname(), "camera": slot, "v": PROTOCOL_VERSION})
                await ws.send_json(hub.settings(slot))
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("bytes"):
                        hub._push(slot, c, msg["bytes"])
                    elif msg.get("text"):
                        c.last_activity = time.time()
                        try:
                            data = json.loads(msg["text"])
                        except ValueError:
                            continue
                        if isinstance(data, dict) and data.get("type") == "stats":
                            c.stats = {k: v for k, v in data.items() if k != "type"}
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                with hub._lock:
                    sl = hub._slots.get(slot)
                    mine = sl is not None and sl.client is c
                    if mine:
                        sl.client = None
                if mine:
                    log.warning("Phone disconnected: %s (camera %s)", c.name, slot)

        async def _refuse(ws, reason: str, code: int):
            try:
                await ws.send_json({"type": "error", "reason": reason})
                await ws.close(code)
            except Exception:
                pass

        return api


class MobileSource(FrameSource):
    """FrameSource view on one camera slot of the hub. Closing it does not disconnect the phone."""

    reopen_on_failure = False

    def __init__(self, hub: MobileHub, slot: Optional[str] = None):
        self.hub = hub
        self.slot = slot or (hub.slot_names() or ["main"])[0]
        sl = hub._slots.get(self.slot)
        self._seq = sl.seq if sl else 0

    def read(self):
        seq, jpg, stamp = self.hub.wait_frame(self.slot, self._seq, timeout=0.5)
        if seq == self._seq or jpg is None:
            return False, None, time.time()
        self._seq = seq
        frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return False, None, stamp
        c = self.hub.client_of(self.slot)
        if c is not None:
            c.height, c.width = frame.shape[:2]
        return True, frame, stamp

    def status_error(self) -> Optional[str]:
        return self.hub.waiting_reason(self.slot)

    def phone_stats(self) -> Optional[Dict[str, Any]]:
        c = self.hub.client_of(self.slot)
        return c.stats if c else None

    def info(self) -> dict:
        c = self.hub.client_of(self.slot)
        d = {"backend": "mobile", "width": c.width if c else 0, "height": c.height if c else 0,
             "fps": c.fps if c else 0.0}
        if c:
            d["device"] = c.name
        return d


def qr_png(text: str, scale: int = 8) -> bytes:
    """QR code PNG (OpenCV encoder, no extra dependency)."""
    enc = cv2.QRCodeEncoder.create()
    img = enc.encode(text)
    img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    img = cv2.copyMakeBorder(img, 4 * scale, 4 * scale, 4 * scale, 4 * scale, cv2.BORDER_CONSTANT, value=255)
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("QR encoding failed")
    return buf.tobytes()
