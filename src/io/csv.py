#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small CSV helpers for runtime telemetry.

The runtime still writes JSON snapshots because they are convenient for replay
and synchronization debugging. CSV files are the operator-facing logs: one row
per simulation cycle, or one row per PLC-cycle where a per-PLC view is clearer.

Performance note:
    The first CSV implementation re-read the whole CSV file on every append in
    order to detect header expansion. That becomes O(N^2) and makes wide logs
    such as scada.csv slow after tens/hundreds of cycles. This version reads
    only the header on the normal append path, and rewrites the file only when
    genuinely new columns appear.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any


def csv_dir(runtime_dir: Path) -> Path:
    path = runtime_dir / "csv"
    path.mkdir(parents=True, exist_ok=True)
    return path


def json_dir(runtime_dir: Path) -> Path:
    path = runtime_dir / "json"
    path.mkdir(parents=True, exist_ok=True)
    return path


def check_dir(output_dir: Path) -> Path:
    path = output_dir / "check"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _to_cell(value: Any) -> str | int | float | bool | None:
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return str(value)
    if isinstance(value, (str, bytes)):
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    return str(value)


def flatten(prefix: str, data: Any, out: dict[str, Any] | None = None) -> dict[str, Any]:
    """Flatten nested dict/list data into CSV-friendly columns."""
    if out is None:
        out = {}

    if isinstance(data, dict):
        for key, value in data.items():
            safe_key = str(key).replace(".", "_").replace(" ", "_")
            child = f"{prefix}.{safe_key}" if prefix else safe_key
            flatten(child, value, out)
    elif isinstance(data, list):
        for idx, value in enumerate(data):
            child = f"{prefix}_{idx}" if prefix else str(idx)
            flatten(child, value, out)
    else:
        out[prefix] = _to_cell(data)

    return out


def _read_header_only(path: Path) -> list[str]:
    """Read only the CSV header. Do not scan historical rows."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            return list(next(reader))
        except StopIteration:
            return []


def _read_all_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Slow path used only when new columns appear and the file must be rewritten."""
    if not path.exists() or path.stat().st_size == 0:
        return [], []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    return header, rows


def append_row(path: Path, row: dict[str, Any], *, fixed_columns: list[str] | None = None) -> None:
    """Append one row to a CSV file, expanding the header only when necessary.

    Normal case after the first cycle is O(1): read header line, append row.
    If a later row introduces new columns, the file is rewritten once to keep a
    valid CSV header. To avoid that slow path during long runs, callers should
    keep their row schema stable when possible.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    clean_row = {str(k): _to_cell(v) for k, v in row.items()}

    fixed_columns = fixed_columns or []
    existing_header = _read_header_only(path)

    header: list[str] = []
    for col in fixed_columns:
        if col not in header:
            header.append(col)
    for col in existing_header:
        if col not in header:
            header.append(col)
    for col in sorted(k for k in clean_row.keys() if k not in fixed_columns):
        if col not in header:
            header.append(col)

    # New file: write header and first row.
    if not existing_header:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
            writer.writeheader()
            writer.writerow({col: clean_row.get(col, "") for col in header})
        return

    # Fast path: no header expansion, append only.
    if header == existing_header:
        with path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=existing_header, extrasaction="ignore")
            writer.writerow({col: clean_row.get(col, "") for col in existing_header})
        return

    # Slow path: header expansion. This should be rare.
    _, existing_rows = _read_all_rows(path)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for old in existing_rows:
            writer.writerow({col: old.get(col, "") for col in header})
        writer.writerow({col: clean_row.get(col, "") for col in header})
