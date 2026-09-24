#!/usr/bin/env python3
"""Summarize performance artifacts from one experiment run."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.metrics.performance import analyze_performance, write_performance_outputs


def _default_output_dir(run_path: Path) -> Path:
    path = run_path.expanduser().resolve()
    if path.name == "runtime":
        return path.parent / "reports" / "metrics"
    if (path / "runtime").is_dir() or (path / "reports").is_dir():
        return path / "reports" / "metrics"
    return path / "metrics"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "run",
        type=Path,
        help="run, output, or runtime directory containing performance artifacts",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="destination directory (default: <output>/reports/metrics)",
    )
    parser.add_argument(
        "--hydraulic-step-sec",
        type=float,
        help="override manifest hydraulic_step_sec when calculating real-time factor",
    )
    parser.add_argument(
        "--log-root",
        action="append",
        type=Path,
        dest="log_roots",
        help="directory included in log-volume accounting; repeat to supply multiple roots",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    summary = analyze_performance(
        args.run,
        hydraulic_step_sec=args.hydraulic_step_sec,
        log_roots=args.log_roots,
    )
    output_dir = args.output_dir or _default_output_dir(args.run)
    outputs = write_performance_outputs(summary, output_dir)
    print(
        "[PERFORMANCE] "
        f"iterations={summary['iteration_time']['count']} "
        f"mean_iteration_ms={summary['iteration_time']['mean_ms']} "
        f"real_time_factor={summary['runtime']['real_time_factor']} "
        f"peak_rss_mb={summary['resources']['peak_aggregate_rss_mb']}"
    )
    for name, path in outputs.items():
        print(f"[OUTPUT] {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
