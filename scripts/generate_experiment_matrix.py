#!/usr/bin/env python3
"""Generate reproducible one-factor experiment matrices."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.experiment.config_generator import (
    NETWORK_PARAMETERS,
    SUPPORTED_PARAMETERS,
    generate_parameter_configs,
)


DEFAULT_DELAYS_MS = [0, 2, 5, 10, 20, 50, 100]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate isolated YAML configurations for one network or "
            "simulation factor"
        )
    )
    parser.add_argument("--base-config", required=True, type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "experiments" / "network_validation" / "generated",
    )
    parser.add_argument("--results-root", type=Path, default=PROJECT_ROOT / "results")
    parser.add_argument(
        "--parameter",
        choices=SUPPORTED_PARAMETERS,
        default="delay_ms",
        help="Factor to vary (default: delay_ms for compatibility)",
    )
    parser.add_argument(
        "--values",
        nargs="+",
        metavar="VALUE",
        help="Values for --parameter; required except for the default delay matrix",
    )
    parser.add_argument(
        "--link",
        action="append",
        dest="links",
        default=[],
        help="Backbone link name; repeat to update a path (required for network factors)",
    )
    parser.add_argument(
        "--delays-ms",
        nargs="+",
        type=float,
        help="Legacy alias for '--parameter delay_ms --values ...'",
    )
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--seed-base", type=int, default=1000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.values is not None and args.delays_ms is not None:
        parser.error("--values and --delays-ms cannot be used together")
    if args.delays_ms is not None and args.parameter != "delay_ms":
        parser.error("--delays-ms can only be used with --parameter delay_ms")

    values = args.values
    if args.delays_ms is not None:
        values = args.delays_ms
    elif values is None:
        if args.parameter == "delay_ms":
            values = DEFAULT_DELAYS_MS
        else:
            parser.error(f"--values is required for --parameter {args.parameter}")

    if args.parameter in NETWORK_PARAMETERS and not args.links:
        parser.error(f"at least one --link is required for --parameter {args.parameter}")
    if args.parameter == "iterations" and args.links:
        parser.error("--link cannot be used with --parameter iterations")

    try:
        paths = generate_parameter_configs(
            args.base_config,
            args.output_dir,
            parameter=args.parameter,
            values=values,
            link_names=args.links,
            repetitions=args.repetitions,
            results_root=args.results_root,
            seed_base=args.seed_base,
        )
    except (KeyError, ValueError) as exc:
        parser.error(str(exc))

    for path in paths:
        print(path)
    print(
        f"[MATRIX] parameter={args.parameter} generated={len(paths)} "
        f"output={args.output_dir.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
