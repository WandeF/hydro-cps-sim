#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""File-marker synchronization helpers for simple cross-process co-simulation.

Network namespaces do not isolate the filesystem in the current Hydro-CPS-Sim
setup, so processes started via `ip netns exec` can use a shared runtime/sync
folder as a first-stage synchronization mechanism.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable, Any


def default_poll_interval() -> float:
    """Return the default marker polling interval in seconds.

    A short interval reduces filesystem-marker synchronization latency in the
    persistent closed-loop runtime. It is intentionally configurable so large
    experiments can trade CPU usage for lower synchronization delay.
    """
    raw = os.environ.get("HYDRO_CPS_POLL_INTERVAL", "0.005")
    try:
        value = float(raw)
    except ValueError:
        value = 0.005
    return max(0.001, value)


DEFAULT_POLL_INTERVAL = default_poll_interval()


class SyncTimeoutError(TimeoutError):
    pass


def marker_path(sync_dir: Path | str, stage: str, iteration: int | None = None, entity: str | None = None) -> Path:
    root = Path(sync_dir)
    if iteration is None:
        name = f"{stage}.ready"
    elif entity:
        name = f"{stage}_{iteration:04d}_{entity}.ready"
    else:
        name = f"{stage}_{iteration:04d}.ready"
    return root / name


def atomic_write_json(path: Path | str, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, p)


def touch_marker(path: Path | str, payload: dict[str, Any] | None = None) -> None:
    data = dict(payload or {})
    data.setdefault("pid", os.getpid())
    data.setdefault("wall_time", time.time())
    atomic_write_json(path, data)


def read_marker(path: Path | str) -> Any:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def remove_marker(path: Path | str) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def clear_ready_files(sync_dir: Path | str) -> None:
    root = Path(sync_dir)
    root.mkdir(parents=True, exist_ok=True)
    for p in root.glob("*.ready"):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def stop_requested(sync_dir: Path | str) -> bool:
    return marker_path(sync_dir, "stop").exists()


def wait_for_marker(
    path: Path | str,
    *,
    timeout: float | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    stop_dir: Path | str | None = None,
) -> Path:
    p = Path(path)
    start = time.monotonic()
    while True:
        if p.exists():
            return p
        if stop_dir is not None and stop_requested(stop_dir):
            raise SyncTimeoutError(f"stop requested while waiting for {p}")
        if timeout is not None and (time.monotonic() - start) > timeout:
            raise SyncTimeoutError(f"timeout waiting for marker: {p}")
        time.sleep(max(0.001, poll_interval))


def wait_for_markers(
    paths: Iterable[Path | str],
    *,
    timeout: float | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    stop_dir: Path | str | None = None,
) -> list[Path]:
    pending = {Path(p) for p in paths}
    start = time.monotonic()
    while pending:
        pending = {p for p in pending if not p.exists()}
        if not pending:
            break
        if stop_dir is not None and stop_requested(stop_dir):
            raise SyncTimeoutError(f"stop requested while waiting for markers: {[str(p) for p in sorted(pending)]}")
        if timeout is not None and (time.monotonic() - start) > timeout:
            raise SyncTimeoutError(f"timeout waiting for markers: {[str(p) for p in sorted(pending)]}")
        time.sleep(max(0.001, poll_interval))
    return [Path(p) for p in paths]
