#!/usr/bin/env python3
"""Build one unified summary_metrics JSON/CSV row for an experiment run."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.metrics.run_summary import build_run_summary, write_run_summary


def default_output_dir(run_path: Path) -> Path:
    path = run_path.expanduser().resolve()
    if path.name == "runtime":
        return path.parent / "reports" / "metrics"
    if path.name == "reports":
        return path / "metrics"
    if path.name == "metrics":
        return path
    if (path / "runtime").is_dir() or (path / "reports").is_dir():
        return path / "reports" / "metrics"
    return path / "metrics"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "run",
        type=Path,
        help="experiment output, runtime, run, reports, or reports/metrics directory",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="destination (default: <output>/reports/metrics)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    summary = build_run_summary(args.run)
    output_dir = args.output_dir or default_output_dir(args.run)
    outputs = write_run_summary(summary, output_dir)
    print(
        "[RUN-SUMMARY] "
        f"experiment_id={summary['experiment_id']} "
        f"run_status={summary['run_status']} "
        f"complete={summary['complete']} "
        f"available_sources={sum(bool(summary[f'{name}_available']) for name in ('manifest', 'performance', 'network', 'correctness', 'propagation'))}/5 "
        f"conflicts={summary['conflict_count']}"
    )
    for name, path in outputs.items():
        print(f"[OUTPUT] {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
