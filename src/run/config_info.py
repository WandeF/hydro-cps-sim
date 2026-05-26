#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Print shell-safe values derived from config.yaml for orchestration scripts."""
from __future__ import annotations

import argparse
import shlex
from pathlib import Path
from typing import Any

from src.core.config import load_yaml


def _resolve_path(config_path: Path, value: Any, *, default: str | None = None) -> Path:
    raw = value if value not in (None, "") else default
    if raw is None:
        raise ValueError("missing path value")
    p = Path(str(raw)).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (config_path.parent / p).resolve()


def _resolve_output_dir(config_path: Path, value: Any) -> Path:
    p = _resolve_path(config_path, value, default="output")
    if p.exists():
        return p
    local_output = (config_path.parent / "output").resolve()
    if local_output.exists():
        return local_output
    return p


def main() -> int:
    parser = argparse.ArgumentParser(description="Print shell exports for Hydro-CPS config paths")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()

    config_path = args.config.expanduser().resolve()
    cfg = load_yaml(config_path)
    output_dir = _resolve_output_dir(config_path, cfg.get("output_path"))
    openplc_path = _resolve_path(config_path, cfg.get("openplc_path"), default="../OpenPLC_v3")
    ns3_path = _resolve_path(config_path, cfg.get("ns3_path"), default="../ns-3-dev-git")
    iterations = int(cfg.get("iterations", 100) or 100)

    values = {
        "CONFIG_ABS": str(config_path),
        "OUTPUT_DIR": str(output_dir),
        "OPENPLC_PATH": str(openplc_path),
        "NS3_PATH": str(ns3_path),
        "ITERATIONS_FROM_CONFIG": str(iterations),
    }
    for key, value in values.items():
        print(f"{key}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
