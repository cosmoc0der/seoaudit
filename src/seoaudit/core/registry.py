from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import pkgutil
import sys
from importlib.metadata import entry_points
from pathlib import Path
from types import ModuleType

from seoaudit.core.check import Check
from seoaudit.core.config import ConfigError

log = logging.getLogger(__name__)

BUILTIN_PACKAGE = "seoaudit.checks"
ENTRY_POINT_GROUP = "seoaudit.checks"


def discover_checks(plugin_paths: list[Path] | None = None) -> dict[str, type[Check]]:
    """Finds checks from the built-in package, installed entry points and local plugin dirs."""
    found: dict[str, type[Check]] = {}

    package = importlib.import_module(BUILTIN_PACKAGE)
    for info in pkgutil.iter_modules(package.__path__, f"{BUILTIN_PACKAGE}."):
        _register(found, _checks_in(importlib.import_module(info.name)))

    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            _register(found, [ep.load()])
        except Exception:
            log.exception("Failed to load check plugin %s", ep.name)

    for path in plugin_paths or []:
        for module in _load_dir(Path(path)):
            _register(found, _checks_in(module))
    return found


def _checks_in(module: ModuleType) -> list[type[Check]]:
    return [
        obj
        for obj in vars(module).values()
        if inspect.isclass(obj) and issubclass(obj, Check) and obj is not Check and obj.__module__ == module.__name__
    ]


def _register(found: dict[str, type[Check]], classes: list[type[Check]]) -> None:
    for cls in classes:
        if not getattr(cls, "id", None):
            raise ConfigError(f"Check {cls.__qualname__} has no id")
        if cls.id in found and found[cls.id] is not cls:
            log.warning("Check '%s' from %s overrides %s", cls.id, cls.__module__, found[cls.id].__module__)
        found[cls.id] = cls


def _load_dir(path: Path) -> list[ModuleType]:
    if not path.is_dir():
        raise ConfigError(f"Plugin path not found: {path}")
    modules = []
    for init in sorted(path.glob("*/__init__.py")):
        name = f"seoaudit_plugins.{init.parent.name}"
        spec = importlib.util.spec_from_file_location(name, init, submodule_search_locations=[str(init.parent)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        modules.append(module)
    return modules
