#!/usr/bin/env python3
"""Generate a reproducible network-delay experiment matrix."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.experiment.config_generator import generate_delay_configs


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate isolated network-delay YAML configurations")
    parser.add_argument("--base-config", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "experiments" / "network_validation" / "generated")
    parser.add_argument("--results-root", type=Path, default=PROJECT_ROOT / "results")
    parser.add_argument("--link", action="append", dest="links", required=True, help="Backbone link name; repeat to update a path")
    parser.add_argument("--delays-ms", nargs="+", type=float, default=[0, 2, 5, 10, 20, 50, 100])
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--seed-base", type=int, default=1000)
    args = parser.parse_args()

    paths = generate_delay_configs(
        args.base_config,
        args.output_dir,
        link_names=args.links,
        delays_ms=args.delays_ms,
        repetitions=args.repetitions,
        results_root=args.results_root,
        seed_base=args.seed_base,
    )
    for path in paths:
        print(path)
    print(f"[MATRIX] generated={len(paths)} output={args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
