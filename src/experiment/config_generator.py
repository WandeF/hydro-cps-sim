#!/usr/bin/env python3
"""Generate isolated experiment configurations from a base YAML file."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

import yaml

from src.core.config import load_yaml


def set_named_link(config: dict[str, Any], link_name: str, **updates: Any) -> None:
    links = (config.get("network", {}) or {}).get("backbone_links", []) or []
    for link in links:
        if isinstance(link, dict) and str(link.get("name")) == link_name:
            link.update(updates)
            return
    raise KeyError(f"Unknown backbone link: {link_name}")


def _enable_metrics(config: dict[str, Any]) -> None:
    metrics = config.setdefault("metrics", {})
    metrics.update(
        {
            "enabled": True,
            "event_log": True,
            "communication": True,
            "resource_monitor": True,
        }
    )
    network = config.setdefault("network", {})
    measurement = network.setdefault("measurement", {})
    measurement.update(
        {
            "enabled": True,
            "flow_monitor": True,
            "link_metrics": True,
            "pcap": bool(network.get("pcap", True)),
        }
    )


def generate_delay_configs(
    base_config: Path,
    output_dir: Path,
    *,
    link_names: Iterable[str],
    delays_ms: Iterable[float],
    repetitions: int,
    results_root: Path,
    seed_base: int = 1000,
) -> list[Path]:
    """Generate one self-contained YAML per delay/repetition.

    Every generated file receives a unique ``output_path`` so ``run_all.sh``
    cannot overwrite an earlier repetition.
    """
    base_config = base_config.resolve()
    output_dir = output_dir.resolve()
    results_root = results_root.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    results_root.mkdir(parents=True, exist_ok=True)
    base = load_yaml(base_config)
    names = [str(name) for name in link_names]
    if not names:
        raise ValueError("At least one target link is required")
    if repetitions < 1:
        raise ValueError("repetitions must be >= 1")

    generated: list[Path] = []
    for raw_delay in delays_ms:
        delay = float(raw_delay)
        delay_label = f"{delay:g}"
        filename_label = delay_label.replace(".", "p")
        for repetition in range(1, repetitions + 1):
            config = deepcopy(base)
            for link_name in names:
                set_named_link(config, link_name, delay=f"{delay_label}ms")
            experiment_id = f"network_delay_{filename_label}ms_run_{repetition:02d}"
            result_dir = results_root / experiment_id
            config["output_path"] = str(result_dir / "output")
            config["experiment"] = {
                "id": experiment_id,
                "name": experiment_id,
                "group": "network_delay",
                "parameter": "delay_ms",
                "value": delay,
                "target_links": names,
                "repetition": repetition,
                "random_seed": seed_base + repetition,
                "base_config": str(base_config),
            }
            _enable_metrics(config)
            attacks = config.get("attacks")
            if isinstance(attacks, dict):
                attacks["enabled"] = False
            path = output_dir / f"{experiment_id}.yaml"
            with path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
            generated.append(path)
    return generated
