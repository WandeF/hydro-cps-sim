#!/usr/bin/env python3
"""Run one generated experiment through the existing run_all pipeline."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.experiment.runner import run_experiment


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one Hydro-CPS-Sim experiment")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("run_all_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    extra = args.run_all_args
    if extra and extra[0] == "--":
        extra = extra[1:]
    result = run_experiment(
        args.config,
        project_root=PROJECT_ROOT,
        iterations=args.iterations,
        check=args.check,
        extra_args=extra,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["returncode"])


if __name__ == "__main__":
    raise SystemExit(main())
