"""Physical snapshots with explicit time/backend metadata for every domain."""
from __future__ import annotations

from typing import Any
from src.io.dhalsim import dhalsim_tag_columns, snapshot_to_dhalsim_row, write_physics_row


def state_columns(rt: Any) -> list[str]:
    metadata = ["iteration", "sim_time", "sim_time_start", "sim_time_end", "backend", "solver_converged"]
    tags = list(dict.fromkeys(dhalsim_tag_columns(rt) + list(rt.raw.get("physics", {}).get("sensors", {}))))
    if set(tags) & set(metadata):
        raise ValueError("Physical sensor tags must not shadow snapshot metadata")
    return metadata + tags


def state_row(rt: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    row = snapshot_to_dhalsim_row(rt, snapshot)
    row.update(snapshot.get("values", {}))
    for key in state_columns(rt)[:6]:
        row[key] = snapshot.get(key, "")
    return row
