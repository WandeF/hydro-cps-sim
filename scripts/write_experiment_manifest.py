#!/usr/bin/env python3
"""Write the current run's reproducibility manifest before runtime startup."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.experiment.manifest import write_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--experiment-id")
    args = parser.parse_args()
    manifest_path, resolved_config_path = write_manifest(
        args.config.expanduser().resolve(),
        args.runtime_dir.expanduser().resolve() if args.runtime_dir else None,
        experiment_id=args.experiment_id,
        project_root=PROJECT_ROOT,
    )
    print(f"[MANIFEST] {manifest_path}")
    print(f"[CONFIG-RESOLVED] {resolved_config_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
