"""Physics selection without importing optional numerical dependencies."""
from __future__ import annotations

import math
from typing import Any

ALIASES = {"epynet": "dhalsim_epynet", "epanet": "dhalsim_epynet"}
# (adapter module, class, optional solver import, default initialization).
BACKEND_SPECS = {
    "dhalsim_epynet": ("src.physics.adapters.epanet", "EpanetAdapter", "epynet.epanet2", "dhalsim"),
    "pandapower": ("src.physics.adapters.pandapower", "PandapowerAdapter", "pandapower", "current"),
}
BACKENDS = tuple(BACKEND_SPECS)


def physics_options(raw: dict[str, Any]) -> dict[str, Any]:
    options = raw.get("physics", {})
    if not isinstance(options, dict):
        raise ValueError("physics must be a mapping")
    return options


def physics_backend(raw: dict[str, Any], override: str | None = None) -> str:
    options = physics_options(raw)
    requested = override if override not in (None, "auto") else options.get("backend", "dhalsim_epynet")
    name = ALIASES.get(str(requested), str(requested))
    if name not in BACKENDS:
        raise ValueError(f"Unsupported physics backend {requested!r}; expected {', '.join(BACKENDS)}")
    return name


def physics_timestep(raw: dict[str, Any]) -> float | None:
    value = physics_options(raw).get("timestep")
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
    if physics_backend(raw) == "dhalsim_epynet" and not step.is_integer():
        raise ValueError("EPANET requires an integer physics.timestep in seconds")
    return step


def physics_init_style(raw: dict[str, Any], backend: str | None = None) -> str:
    backend = backend or physics_backend(raw)
    style = str(physics_options(raw).get("init_style", BACKEND_SPECS[backend][3]))
    if style not in ("dhalsim", "current"):
        raise ValueError("physics.init_style must be dhalsim or current")
    if style == "dhalsim" and backend != "dhalsim_epynet":
        raise ValueError(f"{backend} requires current initialization, not DHALSIM dummy water states")
    return style
