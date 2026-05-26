#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manual post-run checker for Hydro-CPS-Sim."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from src.check.actuators import load_actuator_history, verify_actuator_history
from src.check.open_loop import replay_open_loop
from src.check.plots import write_actuator_outputs, write_tank_curve_outputs
from src.core.config import load_runtime_config, write_json
from src.io.csv import check_dir as default_check_dir


def _clean_check_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for pattern in ("*.csv", "*.json", "*.png"):
        for old in path.glob(pattern):
            old.unlink()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Check a completed Hydro-CPS-Sim run")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--runtime-dir", type=Path, help="default: <output_path>/runtime")
    p.add_argument("--check-dir", type=Path, help="default: <output_path>/check")
    p.add_argument("--iterations", type=int, help="default: config iterations")
    p.add_argument("--init-style", choices=["dhalsim", "current"], default="dhalsim")
    p.add_argument("--tolerance", type=float, default=1e-6)
    p.add_argument("--no-plots", action="store_true")
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

    _clean_check_dir(check_root)
    print(f"[CHECK] runtime={runtime_dir}")
    print(f"[CHECK] output ={check_root}")
    print(f"[CHECK] iterations={iterations} init_style={args.init_style}")

    actuator_summary = verify_actuator_history(
        rt,
        runtime_dir,
        check_root,
        first_control_iteration=first_control_iteration,
        final_physics_iteration=final_physics_iteration,
    )
    print(f"[ACTUATOR] ok={actuator_summary.get('ok')} rows={actuator_summary.get('rows')} -> {actuator_summary.get('actuator_check_csv')}")

    actuator_history = load_actuator_history(runtime_dir, first_control_iteration, final_physics_iteration)
    open_loop_summary = replay_open_loop(
        rt,
        runtime_dir,
        check_root,
        actuator_history,
        iterations=iterations,
        init_style=args.init_style,
        tolerance=args.tolerance,
    )
    print(f"[OPEN-LOOP] ok={open_loop_summary.get('ok')} rows={open_loop_summary.get('rows')} -> {open_loop_summary.get('open_loop_physics_csv')}")

    plot_summary: dict[str, Any] = {"enabled": not args.no_plots}
    if not args.no_plots:
        tank_plots = write_tank_curve_outputs(rt, runtime_dir, check_root)
        actuator_plots = write_actuator_outputs(check_root)
        plot_summary.update({"tank": tank_plots, "actuator": actuator_plots})
        print(f"[PLOTS] tank={len(tank_plots.get('outputs', []))} actuator={len(actuator_plots.get('outputs', []))}")

    summary = {
        "ok": bool(actuator_summary.get("ok")) and bool(open_loop_summary.get("ok")),
        "runtime_dir": str(runtime_dir),
        "check_dir": str(check_root),
        "iterations": iterations,
        "init_style": args.init_style,
        "actuator": actuator_summary,
        "open_loop": open_loop_summary,
        "plots": plot_summary,
    }
    write_json(check_root / "check_summary.json", summary)
    print(f"[SUMMARY] ok={summary['ok']} -> {check_root / 'check_summary.json'}")
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
