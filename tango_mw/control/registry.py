"""Controller registry: built-ins, entry points and hot-reloaded plugin files."""
from __future__ import annotations

import importlib.util
import inspect
import logging
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Type

from .base import Controller
from .builtin import BUILTIN_CONTROLLERS

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "tango_mw.controllers"

TEMPLATE = '''"""Contrôleur personnalisé : {name}.

Ce fichier est rechargé automatiquement à chaque sauvegarde.
- state   : RobotState  (x, y, theta [rad], vx, vy, omega, speed, forward_speed, visible, age)
- request : Request     (speed [m/s], heading [rad], extra = champs JSON supplémentaires)
- retour  : BodyCommand (linear [m/s], angular [rad/s], lateral [m/s])
La calibration du robot (gains, zones mortes, dérive) est appliquée ensuite.
"""
import math

from tango_mw.control import BodyCommand, Controller, Param


class {cls}(Controller):
    name = "{name}"
    description = "Décrivez votre contrôleur ici"
    params = {{
        "k": Param(2.0, min=0.0, max=20.0, step=0.1, description="Gain sur l'erreur de cap"),
    }}

    def reset(self):
        # état interne (intégrales, filtres...)
        pass

    def compute(self, state, request, dt):
        err = request.heading_error(state.theta)
        return BodyCommand(
            linear=request.speed * max(0.0, math.cos(err)),
            angular=self.p["k"] * err,
        )
'''


def _controller_classes(module) -> List[Type[Controller]]:
    out = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, Controller) and obj is not Controller and obj.__module__ == module.__name__ and obj.name:
            out.append(obj)
    return out


class ControllerRegistry:
    def __init__(self, plugin_dirs: Optional[List[str]] = None, base_dir: Optional[Path] = None):
        self._lock = threading.RLock()
        self.base_dir = Path(base_dir or Path.cwd())
        self.plugin_dirs: List[Path] = []
        self.classes: Dict[str, Type[Controller]] = {}
        self.origin: Dict[str, str] = {}
        self.errors: Dict[str, str] = {}
        self._mtimes: Dict[Path, float] = {}
        self._file_classes: Dict[Path, List[str]] = {}
        self.generation = 0  # incremented whenever classes change
        for cls in BUILTIN_CONTROLLERS:
            self._add(cls, "builtin")
        self._load_entry_points()
        self.set_dirs(plugin_dirs or [])

    def _add(self, cls: Type[Controller], origin: str) -> None:
        self.classes[cls.name] = cls
        self.origin[cls.name] = origin

    def _load_entry_points(self) -> None:
        try:
            from importlib.metadata import entry_points

            eps = entry_points()
            group = eps.select(group=ENTRY_POINT_GROUP) if hasattr(eps, "select") else eps.get(ENTRY_POINT_GROUP, [])
            for ep in group:
                try:
                    cls = ep.load()
                    self._add(cls, f"entry_point:{ep.value}")
                except Exception as exc:  # pragma: no cover
                    self.errors[f"entry_point:{ep.name}"] = repr(exc)
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------ plugin dirs
    def set_dirs(self, dirs: List[str]) -> None:
        with self._lock:
            self.plugin_dirs = []
            for d in dirs:
                p = Path(d)
                if not p.is_absolute():
                    p = self.base_dir / p
                self.plugin_dirs.append(p)
            self.reload(force=True)

    def plugin_files(self) -> List[Path]:
        files = []
        for d in self.plugin_dirs:
            if d.is_dir():
                files.extend(sorted(p for p in d.glob("*.py") if not p.name.startswith("_")))
        return files

    def _load_file(self, path: Path) -> None:
        mod_name = "tango_mw_plugins." + re.sub(r"\W", "_", path.stem) + f"_{abs(hash(str(path))) % 10**8}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            classes = _controller_classes(module)
            if not classes:
                raise RuntimeError("aucune sous-classe de Controller avec un attribut `name` trouvée")
        except Exception:
            self.errors[str(path)] = traceback.format_exc(limit=5)
            log.error("Erreur de chargement du contrôleur %s:\n%s", path, self.errors[str(path)])
            return  # keep the previous version of the classes, if any
        self.errors.pop(str(path), None)
        for old in self._file_classes.get(path, []):
            if self.origin.get(old) == str(path):
                self.classes.pop(old, None)
                self.origin.pop(old, None)
        names = []
        for cls in classes:
            if self.origin.get(cls.name, "").startswith("builtin"):
                log.warning("%s redéfinit le contrôleur intégré '%s'", path, cls.name)
            self._add(cls, str(path))
            names.append(cls.name)
        self._file_classes[path] = names
        log.info("Contrôleur(s) chargé(s) depuis %s: %s", path.name, ", ".join(names))

    def reload(self, force: bool = False) -> bool:
        """Reload changed plugin files. Returns True if anything changed."""
        with self._lock:
            changed = False
            files = self.plugin_files()
            current = set(files)
            for path in list(self._file_classes):
                if path not in current:  # deleted file
                    for n in self._file_classes.pop(path):
                        if self.origin.get(n) == str(path):
                            self.classes.pop(n, None)
                            self.origin.pop(n, None)
                    self._mtimes.pop(path, None)
                    self.errors.pop(str(path), None)
                    changed = True
            for path in files:
                try:
                    m = path.stat().st_mtime
                except OSError:
                    continue
                if force or self._mtimes.get(path) != m:
                    self._mtimes[path] = m
                    self._load_file(path)
                    changed = True
            if changed:
                self.generation += 1
            return changed

    # ------------------------------------------------------------------ access
    def get(self, name: str) -> Type[Controller]:
        with self._lock:
            if name not in self.classes:
                raise KeyError(f"contrôleur inconnu: {name}")
            return self.classes[name]

    def describe(self) -> dict:
        with self._lock:
            return {
                "controllers": [
                    {**cls.describe(), "origin": self.origin.get(n, "")} for n, cls in sorted(self.classes.items())
                ],
                "errors": dict(self.errors),
                "files": [str(p) for p in self.plugin_files()],
                "dirs": [str(p) for p in self.plugin_dirs],
            }

    # ------------------------------------------------------------------ authoring
    def _safe_path(self, filename: str) -> Path:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.py", filename):
            raise ValueError("nom de fichier invalide (lettres, chiffres, _ et extension .py)")
        if not self.plugin_dirs:
            raise ValueError("aucun dossier de plugins configuré")
        for d in self.plugin_dirs:
            if (d / filename).exists():
                return d / filename
        return self.plugin_dirs[0] / filename

    def read_source(self, filename: str) -> str:
        return self._safe_path(filename).read_text(encoding="utf-8")

    def write_source(self, filename: str, code: str) -> Path:
        path = self._safe_path(filename)
        compile(code, str(path), "exec")  # syntax check before writing
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8")
        self._force_load(path)
        return path

    def _force_load(self, path: Path) -> None:
        with self._lock:
            self._mtimes[path] = path.stat().st_mtime
            self._load_file(path)
            self.generation += 1

    def create(self, name: str) -> Path:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("nom invalide (minuscules, chiffres, _)")
        path = self._safe_path(name + ".py")
        if path.exists():
            raise FileExistsError(str(path))
        cls = "".join(part.capitalize() for part in name.split("_")) or "MyController"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE.format(name=name, cls=cls), encoding="utf-8")
        self._force_load(path)
        return path
