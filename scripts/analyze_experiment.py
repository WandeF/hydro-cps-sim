#!/usr/bin/env python3
"""Run offline correctness or cross-layer propagation analysis."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.metrics.correctness import analyze_correctness, write_correctness_outputs
from src.metrics.propagation import analyze_propagation, write_propagation_outputs


def _csv_input(explicit: Path | None, root: Path) -> Path:
    return explicit if explicit is not None else root


def _default_output_dir(run_path: Path) -> Path:
    path = run_path.expanduser().resolve()
    if path.is_file():
        return path.parent / "metrics"
    if path.name == "csv" and path.parent.name in {"reports", "runtime"}:
        return path.parent / "metrics"
    if path.name == "reports":
        return path / "metrics"
    if (path / "reports").is_dir() or (path / "runtime").is_dir():
        return path / "reports" / "metrics"
    return path / "metrics"


def _add_common_pair_inputs(
    parser: argparse.ArgumentParser,
    *,
    run_flag: str,
    run_help: str,
) -> None:
    parser.add_argument("--baseline", required=True, type=Path, help="baseline run/output/report directory")
    parser.add_argument(run_flag, required=True, type=Path, help=run_help)
    parser.add_argument("--baseline-physics", type=Path, help="explicit baseline physics.csv")
    parser.add_argument("--baseline-actuators", type=Path, help="explicit baseline actuator_state.csv")
    run_name = run_flag.lstrip("-").replace("-", "_")
    parser.add_argument(f"--{run_name.replace('_', '-')}-physics", dest=f"{run_name}_physics", type=Path)
    parser.add_argument(f"--{run_name.replace('_', '-')}-actuators", dest=f"{run_name}_actuators", type=Path)
    parser.add_argument("--variables", nargs="+", help="physical columns; default: all comparable columns (or tanks for propagation)")
    parser.add_argument("--actuators", nargs="+", help="actuator columns; default: all comparable columns")
    parser.add_argument("--include-row-zero", action="store_true", help="include DHALSIM dummy row 0")
    parser.add_argument("--output-dir", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)

    correctness = subparsers.add_parser("correctness", help="baseline/platform correctness metrics")
    _add_common_pair_inputs(
        correctness,
        run_flag="--platform",
        run_help="platform run/output/report directory",
    )

    propagation = subparsers.add_parser("propagation", help="attack propagation and recovery metrics")
    _add_common_pair_inputs(
        propagation,
        run_flag="--attack",
        run_help="attack run/output/report directory",
    )
    propagation.add_argument("--attack-schedule", type=Path, help="explicit attack_schedule.csv")
    propagation.add_argument("--attack-events", type=Path, help="explicit attack_events.csv")
    propagation.add_argument("--scada-timeouts", type=Path, help="explicit scada_timeout_events.csv")
    propagation.add_argument("--scenario", help="only use events belonging to this attack scenario")
    propagation.add_argument("--attack-start", type=int, help="override inferred attack start iteration")
    propagation.add_argument("--attack-end", type=int, help="override inferred attack end iteration")
    propagation.add_argument("--epsilon", type=float, default=0.01, help="physical deviation/recovery tolerance")
    propagation.add_argument("--hydraulic-step-sec", type=float, default=300.0)
    propagation.add_argument("--recovery-k", type=int, default=3, help="consecutive in-tolerance iterations required")
    return parser


def run_correctness(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Path]]:
    excluded = () if args.include_row_zero else (0,)
    summary = analyze_correctness(
        _csv_input(args.baseline_physics, args.baseline),
        _csv_input(args.platform_physics, args.platform),
        _csv_input(args.baseline_actuators, args.baseline),
        _csv_input(args.platform_actuators, args.platform),
        variables=args.variables,
        actuators=args.actuators,
        exclude_iterations=excluded,
    )
    output_dir = args.output_dir or _default_output_dir(args.platform)
    return summary, write_correctness_outputs(summary, output_dir)


def run_propagation(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Path]]:
    excluded = () if args.include_row_zero else (0,)
    summary = analyze_propagation(
        _csv_input(args.baseline_physics, args.baseline),
        _csv_input(args.attack_physics, args.attack),
        _csv_input(args.baseline_actuators, args.baseline),
        _csv_input(args.attack_actuators, args.attack),
        attack_schedule=args.attack_schedule or args.attack,
        attack_events=args.attack_events or args.attack,
        scada_timeouts=args.scada_timeouts or args.attack,
        variables=args.variables,
        actuators=args.actuators,
        scenario=args.scenario,
        attack_start_iteration=args.attack_start,
        attack_end_iteration=args.attack_end,
        physical_tolerance=args.epsilon,
        hydraulic_step_sec=args.hydraulic_step_sec,
        recovery_consecutive_iterations=args.recovery_k,
        exclude_iterations=excluded,
    )
    output_dir = args.output_dir or _default_output_dir(args.attack)
    return summary, write_propagation_outputs(summary, output_dir)


def main() -> int:
    args = build_parser().parse_args()
    if args.mode == "correctness":
        summary, outputs = run_correctness(args)
        print(
            "[CORRECTNESS] "
            f"pooled_rmse={summary['physical']['overall']['pooled_rmse']} "
            f"actuator_mismatch_rate={summary['control']['overall']['mismatch_rate']}"
        )
    else:
        summary, outputs = run_propagation(args)
        timeline = summary["timeline"]
        print(
            "[PROPAGATION] "
            f"tA={timeline['tA_attack']['iteration']} "
            f"tC={timeline['tC_communication']['iteration']} "
            f"tU={timeline['tU_control']['iteration']} "
            f"tP={timeline['tP_physical']['iteration']} "
            f"recovery={summary['recovery']['status']}"
        )
    for name, path in outputs.items():
        print(f"[OUTPUT] {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
