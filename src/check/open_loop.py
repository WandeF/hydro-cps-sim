#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Post-run open-loop replay against closed-loop physics snapshots."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from src.check.actuators import load_actuator_history
from src.core.config import RuntimeConfig, load_runtime_config, read_json, write_json
from src.io.csv import append_row, check_dir as default_check_dir, json_dir
from src.io.dhalsim import BASE_COLUMNS, dhalsim_tag_columns, snapshot_to_dhalsim_row
from src.physics.engine import PhysicsEngine


def _write_check_physics_row(check_root: Path, rt: RuntimeConfig, snapshot: dict[str, Any]) -> None:
    columns = BASE_COLUMNS + dhalsim_tag_columns(rt)
    append_row(check_root / "open_loop_physics.csv", snapshot_to_dhalsim_row(rt, snapshot), fixed_columns=columns)


def max_snapshot_diff(a: dict[str, Any], b: dict[str, Any]) -> tuple[int, float]:
    diff_count = 0
    max_abs = 0.0
    for section in ("values", "link_status", "link_flow"):
        av = a.get(section, {}) or {}
        bv = b.get(section, {}) or {}
        for key in sorted(set(av) | set(bv)):
            try:
                x = float(av.get(key, 0.0))
                y = float(bv.get(key, 0.0))
                d = abs(x - y)
            except Exception:
                d = 0.0 if av.get(key) == bv.get(key) else float("inf")
            if d > 1e-6:
                diff_count += 1
                max_abs = max(max_abs, d if d != float("inf") else 1e300)
    return diff_count, max_abs


def replay_open_loop(
    rt: RuntimeConfig,
    runtime_dir: Path,
    check_root: Path,
    actuator_history: dict[int, dict[str, bool]],
    *,
    iterations: int,
    init_style: str = "dhalsim",
    tolerance: float = 1e-6,
) -> dict[str, Any]:
    """Replay recorded actuator states through a fresh DHALSIM-epynet simulator."""
    check_root.mkdir(parents=True, exist_ok=True)
    closed_json_dir = json_dir(runtime_dir)
    replay_work_dir = check_root / "epanet_work"
    replay_work_dir.mkdir(parents=True, exist_ok=True)
    replay = PhysicsEngine(rt, mode="dhalsim_epynet", work_dir=replay_work_dir)
    mismatched_iterations: list[int] = []
    rows = 0

    if init_style == "dhalsim":
        zero = replay.dhalsim_zero_snapshot(iteration=0)
        _write_check_physics_row(check_root, rt, zero)
        initial_iteration = 1
        first_control_iteration = 1
        final_physics_iteration = iterations
        initial = replay.dhalsim_initial_snapshot(iteration=initial_iteration)
        _write_check_physics_row(check_root, rt, initial)
        rows += 2
    else:
        initial_iteration = 0
        first_control_iteration = 0
        final_physics_iteration = iterations
        initial = replay.current_snapshot(iteration=initial_iteration)
        _write_check_physics_row(check_root, rt, initial)
        rows += 1

    for i in range(first_control_iteration, final_physics_iteration):
        state = actuator_history.get(i)
        if state is None:
            raise RuntimeError(f"missing actuator_state for iteration {i}")
        snap = replay.step(state, iteration=i + 1)
        _write_check_physics_row(check_root, rt, snap)
        rows += 1

        closed_path = closed_json_dir / f"physics_{i + 1:04d}.json"
        if closed_path.exists():
            closed = read_json(closed_path)
            diff_count, max_abs = max_snapshot_diff(closed, snap)
            ok = diff_count == 0 or max_abs <= tolerance
            append_row(
                check_root / "open_loop_check.csv",
                {
                    "iteration": i + 1,
                    "ok": ok,
                    "diff_count": diff_count,
                    "max_abs_diff": max_abs,
                    "closed_loop_json": str(closed_path),
                },
                fixed_columns=["iteration", "ok", "diff_count", "max_abs_diff", "closed_loop_json"],
            )
            if not ok:
                mismatched_iterations.append(i + 1)

    # Do not explicitly close the native epynet handle. Some builds segfault in
    # ENclose/ENcloseH after a long run; process exit will reclaim resources.
    summary = {
        "ok": not mismatched_iterations,
        "rows": rows,
        "init_style": init_style,
        "mismatched_iterations": mismatched_iterations,
        "open_loop_physics_csv": str(check_root / "open_loop_physics.csv"),
        "open_loop_check_csv": str(check_root / "open_loop_check.csv"),
    }
    write_json(check_root / "open_loop_check_summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Replay recorded actuator outputs through DHALSIM-epynet")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--runtime-dir", type=Path)
    p.add_argument("--check-dir", type=Path)
    p.add_argument("--iterations", type=int)
    p.add_argument("--init-style", choices=["dhalsim", "current"], default="dhalsim")
    p.add_argument("--tolerance", type=float, default=1e-6)
    return p


def main() -> int:
    args = build_parser().parse_args()
    rt = load_runtime_config(args.config.resolve())
    runtime_dir = (args.runtime_dir or (rt.output_dir / "runtime")).resolve()
    check_root = (args.check_dir or default_check_dir(rt.output_dir)).resolve()
    iterations = args.iterations if args.iterations is not None else rt.iterations

    if args.init_style == "dhalsim":
        first_control_iteration = 1
        final_physics_iteration = iterations
    else:
        first_control_iteration = 0
        final_physics_iteration = iterations

    history = load_actuator_history(runtime_dir, first_control_iteration, final_physics_iteration)
    summary = replay_open_loop(
        rt,
        runtime_dir,
        check_root,
        history,
        iterations=iterations,
        init_style=args.init_style,
        tolerance=args.tolerance,
    )
    print(f"[OPEN-LOOP] ok={summary.get('ok')} rows={summary.get('rows')} -> {summary.get('open_loop_physics_csv')}")
    return 0 if summary.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
