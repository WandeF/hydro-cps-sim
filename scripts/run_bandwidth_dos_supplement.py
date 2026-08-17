#!/usr/bin/env python3
"""Run missing bandwidth-DoS rho levels under the original TODO2 conditions.

The historical bandwidth matrix contains 5/10/20 Mbps x
rho={0, 0.8, 1.0, 1.2, 1.5}.  This supplement runs the missing levels needed
for a 0..2 grid with spacing 0.25.  rho=2.0 is included because it is absent
from the historical bandwidth matrix (the separate three-bot queue experiment
at rho=2.0 is not interchangeable with this two-link bandwidth experiment).
"""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.config import load_yaml
from scripts.run_todo2_experiments import (
    PROJECT_ROOT,
    _attempt_config,
    _experiment,
    _set_lan_rate,
    _set_link,
    run,
)


BANDWIDTHS_MBPS = (5, 10, 20)
# The user-requested five values plus 2.0, which is required to close the
# stated 0..2, 0.25-spaced grid and is not present in the historical matrix.
SUPPLEMENT_RHOS = (0.25, 0.5, 0.75, 1.25, 1.75, 2.0)
BOTTLENECK_LINKS = ("r0-r_scada", "r0-r2")
BOTTLENECK_LANS = ("scada_lan", "plc2_lan")


def timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def build_specs() -> list[dict[str, Any]]:
    source = load_yaml(PROJECT_ROOT / "examples/c_town/config_dos_plc2_single.yaml")
    specs: list[dict[str, Any]] = []
    seed = 2026072300
    for bandwidth in BANDWIDTHS_MBPS:
        for rho in SUPPLEMENT_RHOS:
            seed += 1
            cfg = deepcopy(source)
            cfg["iterations"] = 100
            rate = f"{bandwidth}Mbps"
            for link_name in BOTTLENECK_LINKS:
                _set_link(
                    cfg,
                    link_name,
                    data_rate=rate,
                    delay="2ms",
                    queue={"type": "DropTailQueue", "max_packets": 100},
                )
            for lan_name in BOTTLENECK_LANS:
                _set_lan_rate(cfg, lan_name, rate)
            attacks = cfg.setdefault("attacks", {})
            attacks["enabled"] = rho > 0
            scenarios = attacks.get("scenarios", []) or []
            if scenarios:
                scenario = scenarios[0]
                label = str(rho).replace(".", "p")
                scenario["enabled"] = rho > 0
                scenario["name"] = f"dos_plc2_b{bandwidth}_rho_{label}"
                scenario.setdefault("traffic", {})["rate"] = f"{bandwidth * rho:g}Mbps"
            rho_label = str(rho).replace(".", "p")
            experiment_id = f"bandwidth_dos_{bandwidth}mbps_rho_{rho_label}"
            _experiment(
                cfg,
                experiment_id,
                "bandwidth_dos_supplement",
                "bandwidth_mbps_rho",
                {"bandwidth_mbps": bandwidth, "rho": rho},
                seed,
                bandwidth_mbps=bandwidth,
                rho=rho,
                queue_packets=100,
                target_links=list(BOTTLENECK_LINKS),
                supplement=True,
                source_matrix="TODO2 bandwidth_dos_per_run.csv",
            )
            specs.append({"id": experiment_id, "group": "bandwidth_dos_supplement", "config": cfg})
    return specs


def prepare(archive: Path) -> list[dict[str, Any]]:
    archive.mkdir(parents=True, exist_ok=True)
    specs = build_specs()
    for spec in specs:
        config_path, output_dir = _attempt_config(archive, spec, 1)
        spec["config_path"] = str(config_path)
        spec["output_dir"] = str(output_dir)
    plan = {
        "schema_version": 1,
        "created_at": timestamp(),
        "project_root": str(PROJECT_ROOT),
        "bandwidth_mbps": list(BANDWIDTHS_MBPS),
        "supplement_rho_levels": list(SUPPLEMENT_RHOS),
        "conditions": {
            "iterations": 100,
            "bottleneck_links": list(BOTTLENECK_LINKS),
            "queue": "DropTailQueue max_packets=100",
            "link_delay": "2ms",
            "attack": "single UDP DoS to PLC2, rate=bandwidth*rho",
        },
        "experiments": [
            {"id": s["id"], "group": s["group"], "config": s["config_path"], "output": s["output_dir"]}
            for s in specs
        ],
    }
    (archive / "EXPERIMENT_PLAN.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return specs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    archive = (args.archive or (Path("/home/lzh/MASTER/CODE/output") / f"bandwidth_dos_supplement_{timestamp()}"))
    archive = archive.expanduser().resolve()
    specs = prepare(archive)
    print(f"[BANDWIDTH-SUPPLEMENT] archive={archive} prepared={len(specs)}", flush=True)
    if args.prepare_only:
        return 0
    return run(archive, specs)


if __name__ == "__main__":
    raise SystemExit(main())
