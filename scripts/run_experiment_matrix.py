#!/usr/bin/env python3
"""Run generated experiment YAML files sequentially without overwriting runs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config import load_yaml
from src.experiment.manifest import resolve_output_dir
from src.experiment.runner import experiment_completed, run_experiment


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a directory of generated experiment configs")
    parser.add_argument("--config-dir", required=True, type=Path)
    parser.add_argument("--pattern", default="*.yaml")
    parser.add_argument("--resume", action="store_true", help="Skip only runs whose timeline ends successfully")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    configs = sorted(args.config_dir.resolve().glob(args.pattern))
    results = []
    for config in configs:
        output_dir = resolve_output_dir(config, load_yaml(config))
        if args.resume and experiment_completed(output_dir):
            results.append({"config": str(config), "output_dir": str(output_dir), "status": "skipped"})
            continue
        result = run_experiment(config, project_root=PROJECT_ROOT, check=args.check)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False))
        if result["returncode"] and args.stop_on_error:
            break

    summary = args.config_dir.resolve() / "matrix_run_summary.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [item for item in results if int(item.get("returncode", 0) or 0) != 0]
    print(f"[MATRIX] total={len(results)} failed={len(failed)} summary={summary}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
