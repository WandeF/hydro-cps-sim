"""Explicit, lazy UPSI plugins. Configuration is trusted executable input."""
from __future__ import annotations
from dataclasses import dataclass
from importlib import import_module, metadata
from typing import Callable
from src.physics.base import PhysicsBackend


@dataclass(frozen=True)
class Registration:
    factory: str | Callable
    dependency: str | None = None
    initialization: str = "current"
    legacy_factory: str | None = None
    time_quantum_s: float | None = None


_BACKENDS: dict[str, Registration] = {}
ALIASES = {"epanet": "dhalsim_epynet", "epynet": "dhalsim_epynet"}


def register_backend(name, factory, *, dependency=None, initialization="current",
                     legacy_factory=None, time_quantum_s=None):
    if not isinstance(name, str) or not name or name in _BACKENDS or name in ALIASES:
        raise ValueError(f"Duplicate or invalid backend: {name}")
    if initialization not in {"current", "dhalsim"}:
        raise ValueError("Unsupported initialization policy")
    _BACKENDS[name] = Registration(factory, dependency, initialization, legacy_factory, time_quantum_s)


def configure_plugins(raw):
    plugins = raw.get("physics", {}).get("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("physics.plugins must be a mapping of trusted plugin names")
    for name, item in plugins.items():
        if not isinstance(item, dict) or set(item) - {"factory", "dependency", "entry_point"}:
            raise ValueError(f"Invalid plugin declaration: {name}")
        if ("factory" in item) == ("entry_point" in item):
            raise ValueError("Plugin needs exactly one factory or entry_point")
        source = item.get("factory", "entrypoint:" + str(item.get("entry_point")))
        if not isinstance(source, str) or (not source.startswith("entrypoint:") and ":" not in source):
            raise ValueError("Plugin factory must be an explicit module:class")
        desired = Registration(source, item.get("dependency"))
        if name in _BACKENDS:
            if _BACKENDS[name] != desired:
                raise ValueError(f"Duplicate backend with different source: {name}")
        else:
            register_backend(name, source, dependency=item.get("dependency"))


def registration(name):
    name = ALIASES.get(name, name)
    try:
        return _BACKENDS[name]
    except KeyError as exc:
        raise ValueError(f"Unsupported physics backend {name!r}; declare a trusted physics.plugins entry") from exc


def resolve_factory(source):
    if callable(source):
        return source
    if source.startswith("entrypoint:"):
        name = source.split(":", 1)[1]
        matches = list(metadata.entry_points(group="hydro_cps.physics", name=name))
        if len(matches) != 1:
            raise ValueError(f"Expected one installed hydro_cps.physics entry point: {name}")
        return matches[0].load()
    module, symbol = source.split(":", 1)
    return getattr(import_module(module), symbol)


def create_backend(name, config):
    backend = resolve_factory(registration(name).factory)(config)
    if not isinstance(backend, PhysicsBackend):
        raise TypeError("Physics plugins must implement PhysicsBackend")
    return backend


register_backend("dhalsim_epynet", "src.physics.adapters.epanet:EpanetBackend",
                 dependency="epynet.epanet2", initialization="dhalsim",
                 legacy_factory="src.physics.adapters.epanet:EpanetAdapter", time_quantum_s=1.0)
register_backend("pandapower", "src.physics.adapters.pandapower:PandapowerBackend", dependency="pandapower",
                 legacy_factory="src.physics.adapters.pandapower:PandapowerAdapter")
