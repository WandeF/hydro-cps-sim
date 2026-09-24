#!/usr/bin/env python3
"""Sequential experiment runner used by the command-line scripts."""
from __future__ import annotations

import csv
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

from src.core.config import load_yaml
from src.experiment.manifest import resolve_output_dir


def experiment_completed(output_dir: Path) -> bool:
    """Return true only when the unified timeline records a successful end."""

    events_path = Path(output_dir) / "runtime" / "csv" / "events.csv"
    try:
        with events_path.open(encoding="utf-8", newline="") as handle:
            final_status: str | None = None
            for row in csv.DictReader(handle):
                if str(row.get("event_type", "")).strip() == "simulation_end":
                    final_status = str(row.get("status", "")).strip().lower()
    except (OSError, UnicodeError, csv.Error):
        return False
    return final_status == "success"


def run_experiment(
    config: Path,
    *,
    project_root: Path | None = None,
    iterations: int | None = None,
    check: bool = False,
    extra_args: Iterable[str] = (),
) -> dict[str, Any]:
    project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    config = config.resolve()
    command = ["bash", str(project_root / "scripts" / "run_all.sh"), "--config", str(config)]
    if iterations is not None:
        command.extend(["--iterations", str(iterations)])
    if check:
        command.append("--check")
    command.extend(str(arg) for arg in extra_args)
    started = time.time()
    result = subprocess.run(command, cwd=str(project_root), check=False)
    ended = time.time()
    output_dir = resolve_output_dir(config, load_yaml(config))
    return {
        "config": str(config),
        "output_dir": str(output_dir),
        "command": command,
        "returncode": result.returncode,
        "runtime_sec": ended - started,
    }
