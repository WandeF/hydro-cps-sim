#!/usr/bin/env python3
"""Prepare and run the single-observation experiment matrix from TODO2.md."""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config import load_yaml
from src.experiment.runner import experiment_completed


LOSS_LEVELS = (0.01, 0.02, 0.05, 0.10, 0.50)
BANDWIDTHS_MBPS = (10, 5, 20)  # TODO2 precheck order.
DOS_RHOS = (0.0, 0.8, 1.0, 1.2, 1.5)
BOTTLENECK_LINKS = ("r0-r_scada", "r0-r2")
BOTTLENECK_LANS = ("scada_lan", "plc2_lan")


def _timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def _set_link(cfg: dict[str, Any], name: str, **updates: Any) -> None:
    for link in cfg["network"]["backbone_links"]:
        if link.get("name") == name:
            link.update(updates)
            return
    raise KeyError(name)


def _set_lan_rate(cfg: dict[str, Any], name: str, rate: str) -> None:
    for lan in cfg["network"]["lans"]:
        if lan.get("name") == name:
            lan["data_rate"] = rate
            return
    raise KeyError(name)


def _measurement(cfg: dict[str, Any]) -> None:
    metrics = cfg.setdefault("metrics", {})
    metrics.update({"enabled": True, "event_log": True, "communication": True, "resource_monitor": True})
    network = cfg.setdefault("network", {})
    network["pcap"] = True
    measurement = network.setdefault("measurement", {})
    measurement.update({"enabled": True, "flow_monitor": True, "link_metrics": True, "pcap": True})


def _experiment(cfg: dict[str, Any], experiment_id: str, group: str, parameter: str,
                value: Any, seed: int, **extra: Any) -> None:
    cfg["experiment"] = {
        "id": experiment_id,
        "name": experiment_id,
        "group": group,
        "parameter": parameter,
        "value": value,
        "repetition": 1,
        "random_seed": seed,
        **extra,
    }
    _measurement(cfg)


def _specs() -> list[dict[str, Any]]:
    base = load_yaml(PROJECT_ROOT / "examples/c_town/config.yaml")
    dos_single = load_yaml(PROJECT_ROOT / "examples/c_town/config_dos_plc2_single.yaml")
    dos_three = load_yaml(PROJECT_ROOT / "examples/c_town/config_dos_plc2_three_bots.yaml")
    plc_logic = load_yaml(PROJECT_ROOT / "examples/c_town/config_openplc_logic_plc4.yaml")
    specs: list[dict[str, Any]] = []
    seed = 2026071600

    # TODO2 enumerates 0% plus five nonzero rates but also explicitly requires
    # five new runs/rows. The existing 2 ms, 100 Mbps no-loss run is retained as
    # the 0% reference; these are the five new random-loss configurations.
    for loss in LOSS_LEVELS:
        seed += 1
        cfg = deepcopy(base)
        cfg["iterations"] = 100
        for link_name in ("r0-r_scada", "r0-r4"):
            _set_link(
                cfg,
                link_name,
                data_rate="100Mbps",
                delay="2ms",
                error_model={"type": "rate", "unit": "packet", "error_rate": loss},
            )
        cfg["attacks"] = {"enabled": False}
        label = str(loss).replace(".", "p")
        experiment_id = f"packet_loss_{label}"
        _experiment(
            cfg, experiment_id, "packet_loss", "loss_rate", loss, seed,
            target_links=["r0-r_scada", "r0-r4"], baseline_reference="existing_0_percent_run",
        )
        specs.append({"id": experiment_id, "group": "packet_loss", "config": cfg})

    for bandwidth in BANDWIDTHS_MBPS:
        for rho in DOS_RHOS:
            seed += 1
            cfg = deepcopy(dos_single)
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
            scenarios = attacks.get("scenarios", [])
            if scenarios:
                scenarios[0]["enabled"] = rho > 0
                scenarios[0]["name"] = f"dos_plc2_b{bandwidth}_rho_{str(rho).replace('.', 'p')}"
                scenarios[0]["traffic"]["rate"] = f"{bandwidth * rho:g}Mbps" if rho > 0 else "0Mbps"
            rho_label = str(rho).replace(".", "p")
            experiment_id = f"bandwidth_dos_{bandwidth}mbps_rho_{rho_label}"
            _experiment(
                cfg, experiment_id, "bandwidth_dos", "bandwidth_mbps_rho",
                {"bandwidth_mbps": bandwidth, "rho": rho}, seed,
                bandwidth_mbps=bandwidth, rho=rho, queue_packets=100,
                target_links=list(BOTTLENECK_LINKS),
            )
            specs.append({"id": experiment_id, "group": "bandwidth_dos", "config": cfg})

    for scenario, cfg_source in (("single_bot", dos_single), ("three_bots", dos_three)):
        seed += 1
        cfg = deepcopy(cfg_source)
        cfg["iterations"] = 100
        experiment_id = f"dos_propagation_{scenario}"
        _experiment(
            cfg, experiment_id, "dos_propagation", "scenario", scenario, seed,
            scenario=scenario,
        )
        specs.append({"id": experiment_id, "group": "dos_propagation", "config": cfg})

    seed += 1
    cfg = deepcopy(plc_logic)
    cfg["iterations"] = 100
    experiment_id = "plc_logic_injection_plc4"
    _experiment(
        cfg, experiment_id, "plc_logic_injection", "scenario",
        "openplc_shift_t7_threshold", seed, scenario="openplc_shift_t7_threshold",
    )
    specs.append({"id": experiment_id, "group": "plc_logic_injection", "config": cfg})
    return specs


def _attempt_config(archive: Path, spec: dict[str, Any], attempt: int) -> tuple[Path, Path]:
    attempt_dir = archive / "experiments" / spec["group"] / spec["id"] / f"attempt_{attempt:02d}"
    output_dir = attempt_dir / "output"
    config_path = attempt_dir / "config.yaml"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    cfg = deepcopy(spec["config"])
    cfg["output_path"] = str(output_dir)
    cfg["experiment"]["attempt"] = attempt
    cfg["experiment"]["base_config"] = str(PROJECT_ROOT / "TODO2.md")
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(cfg, handle, allow_unicode=True, sort_keys=False)
    return config_path, output_dir


def prepare(archive: Path) -> list[dict[str, Any]]:
    archive.mkdir(parents=True, exist_ok=True)
    specs = _specs()
    for spec in specs:
        config_path, output_dir = _attempt_config(archive, spec, 1)
        spec["config_path"] = str(config_path)
        spec["output_dir"] = str(output_dir)
    plan = {
        "schema_version": 1,
        "created_at": _timestamp(),
        "project_root": str(PROJECT_ROOT),
        "formal_new_run_count": len(specs),
        "run_policy": "one valid run per unique parameter combination; at most two infrastructure retries",
        "packet_loss_resolution": {
            "new_formal_levels": list(LOSS_LEVELS),
            "zero_percent_reference": (
                "/home/lzh/MASTER/CODE/output/quantitative_20260716T113050_metric_cde39ea/"
                "02_network_delay_matrix/runs/network_delay_2ms_run_01/output"
            ),
            "reason": "TODO2 lists six values but explicitly requires five new runs and five result rows.",
        },
        "experiments": [
            {"id": x["id"], "group": x["group"], "config": x["config_path"], "output": x["output_dir"]}
            for x in specs
        ],
    }
    (archive / "EXPERIMENT_PLAN.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return specs


def _final_status(output_dir: Path) -> str:
    path = output_dir / "runtime/csv/events.csv"
    status = ""
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("event_type") == "simulation_end":
                    status = str(row.get("status", "")).strip().lower()
    except (OSError, csv.Error, UnicodeError):
        pass
    return status


def run(archive: Path, specs: list[dict[str, Any]]) -> int:
    loaded = []
    try:
        loaded = json.loads((archive / "RUN_INDEX.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    results: list[dict[str, Any]] = loaded if isinstance(loaded, list) else []
    for position, spec in enumerate(specs, start=1):
        known_attempts = {int(item.get("attempt", 0)) for item in results if item.get("id") == spec["id"]}
        experiment_dir = archive / "experiments" / spec["group"] / spec["id"]
        for attempt_dir in sorted(experiment_dir.glob("attempt_*")):
            try:
                attempt_number = int(attempt_dir.name.rsplit("_", 1)[1])
            except (IndexError, ValueError):
                continue
            if attempt_number in known_attempts or not (attempt_dir / "run.log").exists():
                continue
            output_dir = attempt_dir / "output"
            status = _final_status(output_dir)
            results.append({
                "id": spec["id"], "group": spec["group"], "attempt": attempt_number,
                "config": str(attempt_dir / "config.yaml"), "output": str(output_dir),
                "log": str(attempt_dir / "run.log"), "returncode": None,
                "simulation_end": status, "valid": status == "success" and experiment_completed(output_dir),
                "elapsed_sec": None, "reconciled_after_interruption": True,
            })
        prior = [
            item for item in results
            if item.get("id") == spec["id"]
            and Path(str(item.get("config", ""))).parent.exists()
        ]
        successful_prior = next(
            (
                item for item in prior
                if item.get("simulation_end") == "success"
                and experiment_completed(Path(str(item.get("output", ""))))
            ),
            None,
        )
        if successful_prior is not None:
            successful_prior["valid"] = True
            print(f"[TODO2] reuse successful id={spec['id']} attempt={successful_prior.get('attempt')}", flush=True)
            continue
        success = False
        first_attempt = max((int(item.get("attempt", 0)) for item in prior), default=0) + 1
        for attempt in range(first_attempt, 4):
            config_path, output_dir = _attempt_config(archive, spec, attempt)
            log_path = config_path.parent / "run.log"
            command = ["bash", str(PROJECT_ROOT / "scripts/run_all.sh"), "--config", str(config_path), "--check"]
            child_env = os.environ.copy()
            child_env["PYTHON_BIN"] = sys.executable
            child_env["PATH"] = f"{Path(sys.executable).parent}:{child_env.get('PATH', '')}"
            child_env["PYTHONDONTWRITEBYTECODE"] = "1"
            child_env["SYNC_TIMEOUT"] = "180.0"
            started = time.time()
            print(f"[TODO2] {position:02d}/{len(specs)} id={spec['id']} attempt={attempt}", flush=True)
            with log_path.open("w", encoding="utf-8") as log:
                proc = subprocess.run(
                    command, cwd=str(PROJECT_ROOT), stdout=log, stderr=subprocess.STDOUT,
                    check=False, env=child_env,
                )
            elapsed = time.time() - started
            status = _final_status(output_dir)
            # Offline correctness/check diagnostics are allowed to return
            # nonzero when an experimental impairment creates a real
            # deviation. Lifecycle success, not diagnostic similarity to the
            # baseline, determines whether this is a valid formal run.
            success = status == "success" and experiment_completed(output_dir)
            record = {
                "id": spec["id"], "group": spec["group"], "attempt": attempt,
                "config": str(config_path), "output": str(output_dir), "log": str(log_path),
                "returncode": proc.returncode, "simulation_end": status,
                "valid": success, "elapsed_sec": elapsed,
            }
            results.append(record)
            if success:
                print(f"[TODO2] success id={spec['id']} elapsed={elapsed:.1f}s", flush=True)
                break
            failure = {
                **record,
                "reason": "non-experimental run failure; see run.log",
                "retry_allowed": attempt < 3,
            }
            (config_path.parent / "failure.json").write_text(
                json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(
                f"[TODO2][WARN] failed id={spec['id']} attempt={attempt} "
                f"returncode={proc.returncode} simulation_end={status or 'missing'}",
                flush=True,
            )
        if not success:
            print(f"[TODO2][ERROR] exhausted retries id={spec['id']}", flush=True)
        (archive / "RUN_INDEX.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    failed_ids = sorted({r["id"] for r in results if not any(x["id"] == r["id"] and x["valid"] for x in results)})
    print(f"[TODO2] completed valid={len(specs)-len(failed_ids)}/{len(specs)} failed={failed_ids}", flush=True)
    return 1 if failed_ids else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    archive = (args.archive or (Path("/home/lzh/MASTER/CODE/output") / f"quantitative_todo2_{_timestamp()}"))
    archive = archive.expanduser().resolve()
    specs = prepare(archive)
    print(f"[TODO2] archive={archive} prepared={len(specs)}", flush=True)
    if args.prepare_only:
        return 0
    return run(archive, specs)


if __name__ == "__main__":
    raise SystemExit(main())
