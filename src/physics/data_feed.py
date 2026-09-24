#!/usr/bin/env python3
"""Deterministic random data source for communication/control scale tests.

The scale experiment deliberately has no water-network model. This module
produces one value in ``[minimum, maximum]`` for every source-PLC sensor on
each iteration. The persistent runtime publishes the values to the local PLC
adapters, which perform the actual Modbus register writes; SCADA then polls
the source PLCs and forwards the values to their paired execution PLCs.

Values are derived from ``seed + iteration + tag`` rather than mutable global
random state. A run is random-looking but exactly reproducible and can be
audited independently at any iteration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DataFeedConfig:
    tags: tuple[str, ...]
    minimum: float = 0.0
    maximum: float = 10.0
    seed: int = 0
    precision: int = 6

    def __post_init__(self) -> None:
        if not self.tags:
            raise ValueError("data feed requires at least one tag")
        if not math.isfinite(self.minimum) or not math.isfinite(self.maximum):
            raise ValueError("data-feed bounds must be finite")
        if self.minimum > self.maximum:
            raise ValueError("data-feed minimum cannot exceed maximum")
        if self.precision < 0:
            raise ValueError("data-feed precision cannot be negative")


class DataFeed:
    """Generate reproducible per-tag random values for an iteration."""

    def __init__(self, config: DataFeedConfig) -> None:
        self.config = config

    @staticmethod
    def _derived_seed(seed: int, iteration: int, tag: str) -> int:
        digest = hashlib.sha256(f"{seed}:{iteration}:{tag}".encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big", signed=False)

    def values(self, iteration: int) -> dict[str, float]:
        if iteration < 0:
            raise ValueError("data-feed iteration cannot be negative")
        result: dict[str, float] = {}
        for tag in self.config.tags:
            generator = random.Random(self._derived_seed(self.config.seed, iteration, tag))
            value = generator.uniform(self.config.minimum, self.config.maximum)
            result[tag] = round(value, self.config.precision)
        return result


def source_sensor_tags(raw_config: dict[str, Any]) -> tuple[str, ...]:
    """Return the unique tags owned by PLCs marked as data-feed sources."""
    tags: list[str] = []
    seen: set[str] = set()
    for plc in raw_config.get("plcs", []) or []:
        if not isinstance(plc, dict) or plc.get("scale_role") != "source":
            continue
        for raw_tag in plc.get("sensors", []) or []:
            tag = str(raw_tag)
            if tag and tag not in seen:
                seen.add(tag)
                tags.append(tag)
    return tuple(tags)


def from_runtime_config(raw_config: dict[str, Any]) -> DataFeed:
    physics = raw_config.get("physics", {}) or {}
    options = physics.get("data_feed", {}) if isinstance(physics, dict) else {}
    options = options if isinstance(options, dict) else {}
    experiment = raw_config.get("experiment", {}) or {}
    seed = options.get("seed", experiment.get("random_seed", 0))
    return DataFeed(DataFeedConfig(
        tags=source_sensor_tags(raw_config),
        minimum=float(options.get("minimum", 0.0)),
        maximum=float(options.get("maximum", 10.0)),
        seed=int(seed),
        precision=int(options.get("precision", 6)),
    ))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tags", nargs="+", required=True)
    parser.add_argument("--iterations", type=int, required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--minimum", type=float, default=0.0)
    parser.add_argument("--maximum", type=float, default=10.0)
    parser.add_argument("--precision", type=int, default=6)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    feed = DataFeed(DataFeedConfig(
        tags=tuple(args.tags),
        minimum=args.minimum,
        maximum=args.maximum,
        seed=args.seed,
        precision=args.precision,
    ))
    rows = [{"iteration": iteration, **feed.values(iteration)} for iteration in range(args.iterations)]
    payload = json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
