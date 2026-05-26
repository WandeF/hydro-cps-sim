#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DHALSIM-style CSV export helpers.

DHALSIM's C-Town CSVs use flat tag columns, for example:
    iteration,timestamp,PU1F,PU2F,J280,J269,PU1,PU2,T1,...

The runtime snapshots are richer JSON objects.  This module converts those
snapshots back to the flat DHALSIM-style table so the project has one physics
CSV format regardless of whether the row comes from closed-loop execution or
open-loop replay.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.io.csv import append_row, csv_dir
from src.core.config import RuntimeConfig


BASE_COLUMNS = ["iteration", "timestamp"]


def dhalsim_tag_columns(rt: RuntimeConfig) -> list[str]:
    """Return flat tag columns in the same order as the C-Town PLC config.

    The order is: for each PLC in config order, list its sensors, then its
    actuators.  Duplicate tags are emitted once.
    """
    columns: list[str] = []
    seen: set[str] = set()

    for plc in rt.raw.get("plcs", []) or []:
        if not isinstance(plc, dict):
            continue
        for group in ("sensors", "actuators"):
            for tag in plc.get(group, []) or []:
                name = str(tag)
                if name and name not in seen:
                    seen.add(name)
                    columns.append(name)

    return columns


def _to_dhalsim_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def snapshot_to_dhalsim_row(rt: RuntimeConfig, snapshot: dict[str, Any]) -> dict[str, Any]:
    values = snapshot.get("values", {}) or {}
    actuators = snapshot.get("actuators_applied", {}) or {}
    link_status = snapshot.get("link_status", {}) or {}
    link_flow = snapshot.get("link_flow", {}) or {}

    row: dict[str, Any] = {
        "iteration": snapshot.get("iteration", ""),
        "timestamp": snapshot.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
    }

    for name in dhalsim_tag_columns(rt):
        value: Any = ""
        if name in values:
            value = values[name]
        elif name in actuators:
            value = actuators[name]
        elif name.endswith("F") and name[:-1] in link_flow:
            value = link_flow[name[:-1]]
        elif name in link_status:
            value = link_status[name]
        elif name in rt.actuator_initial_state:
            value = False
        else:
            value = 0
        row[name] = _to_dhalsim_cell(value)

    return row


def write_physics_row(
    runtime_dir: Path,
    rt: RuntimeConfig,
    snapshot: dict[str, Any],
    *,
    filename: str = "physics.csv",
) -> None:
    columns = BASE_COLUMNS + dhalsim_tag_columns(rt)
    append_row(csv_dir(runtime_dir) / filename, snapshot_to_dhalsim_row(rt, snapshot), fixed_columns=columns)
