"""Physics selection without importing optional numerical dependencies."""
from __future__ import annotations

import math
from typing import Any

from src.physics.registry import ALIASES


def physics_options(raw: dict[str, Any]) -> dict[str, Any]:
    options = raw.get("physics", {})
    if not isinstance(options, dict):
        raise ValueError("physics must be a mapping")
    return options


def physics_backend(raw: dict[str, Any], override: str | None = None) -> str:
    options = physics_options(raw)
    requested = override if override not in (None, "auto") else options.get("backend", "dhalsim_epynet")
    if raw.get("schema_version") == 2 and "backend" not in options and override in (None,"auto"):
        raise ValueError("schema_version 2 requires physics.backend")
    name = ALIASES.get(str(requested), str(requested))
    from src.physics.registry import configure_plugins, registration
    registry = configure_plugins(raw)
    registration(name, registry)
    return name


def physics_timestep(raw: dict[str, Any]) -> float | None:
    options = physics_options(raw)
    timing = options.get("timing", {})
    if not isinstance(timing, dict):
        raise ValueError("physics.timing must be a mapping")
    value = timing.get("step_seconds", options.get("timestep"))
    if "step_seconds" in timing and "timestep" in options and timing["step_seconds"] != options["timestep"]:
        raise ValueError("Conflicting physical sampling intervals")
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("physics.timestep must be finite and positive")
    try:
        step = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("physics.timestep must be finite and positive") from exc
    if not math.isfinite(step) or step <= 0:
        raise ValueError("physics.timestep must be finite and positive")
    from src.physics.registry import registration
    name = physics_backend(raw)
    from src.physics.registry import configure_plugins
    quantum = registration(name, configure_plugins(raw)).time_quantum_s
    if quantum is not None and not (step/quantum).is_integer():
        raise ValueError(f"Selected backend requires physics.timestep in multiples of {quantum} seconds")
    return step


def physics_init_style(raw: dict[str, Any], backend: str | None = None) -> str:
    backend = backend or physics_backend(raw)
    from src.physics.registry import registration
    from src.physics.registry import configure_plugins
    spec = registration(backend, configure_plugins(raw))
    default = "current" if raw.get("schema_version") == 2 else spec.initialization
    style = str(physics_options(raw).get("init_style", default))
    if style not in ("dhalsim", "current"):
        raise ValueError("physics.init_style must be dhalsim or current")
    if style == "dhalsim" and spec.initialization != "dhalsim":
        raise ValueError(f"{backend} requires current initialization, not DHALSIM dummy water states")
    return style
