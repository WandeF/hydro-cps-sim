#!/usr/bin/env python3
"""Prepare/run the dense delay and packet-loss traceability supplement.

The original TODO3 archive is immutable. Existing matching endpoints are reused
from it; only missing levels are executed in a new archive. The final paper
tables contain the r0-r_scada link only, with delay in ms and loss in percent.

Running experiments requires a valid sudo timestamp because the platform uses
Linux network namespaces::

    sudo -v
    /home/lzh/anaconda3/bin/python scripts/run_network_traceability_supplement.py --run
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any


PAPER_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path("/home/lzh/MASTER/CODE/hydro-cps-sim")
OLD_ARCHIVE = Path(
    "/home/lzh/MASTER/CODE/output/quantitative_supplement_20260721T232042+0800_metric_cde39ea"
)
DEFAULT_ARCHIVE = Path(
    "/home/lzh/MASTER/CODE/output/paper_network_traceability_20260903"
)
DELAY_LEVELS_MS = tuple(range(0, 101, 5))
LOSS_LEVELS = tuple(round(value / 100, 2) for value in range(10, 51, 2))
REUSED_DELAY_LEVELS = {0, 5, 10, 20, 50, 100}
REUSED_LOSS_LEVELS = {0.50}
TARGET_LINK = "r0-r_scada"


def load_todo3():
    path = PROJECT_ROOT / "scripts/run_todo3_experiments.py"
    spec = importlib.util.spec_from_file_location("hydrocps_todo3", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


todo3 = load_todo3()


def loss_label(loss_rate: float) -> str:
    return f"{loss_rate * 100:g}".replace(".", "p")


def build_specs() -> list[dict[str, Any]]:
    base = todo3.load_yaml(PROJECT_ROOT / "examples/c_town/config.yaml")
    specs: list[dict[str, Any]] = []
    seed = 2026090300
    ns3_run = 900

    for delay_ms in DELAY_LEVELS_MS:
        if delay_ms in REUSED_DELAY_LEVELS:
            continue
        seed += 1
        ns3_run += 1
        cfg = deepcopy(base)
        cfg["iterations"] = 100
        todo3.configure_target_links(cfg, delay_ms=delay_ms)
        todo3.disable_attacks(cfg)
        todo3.instrument(cfg, pcap_links=[TARGET_LINK])
        experiment_id = f"paper_delay_{delay_ms}ms"
        todo3.set_experiment(
            cfg,
            experiment_id=experiment_id,
            group="delay",
            parameter="delay_ms",
            value=delay_ms,
            seed=seed,
            ns3_run=ns3_run,
            target_links=[TARGET_LINK],
            paper_supplement=True,
        )
        specs.append(
            {"id": experiment_id, "group": "delay", "config": cfg, "timeout_sec": 1200}
        )

    for index, loss_rate in enumerate(LOSS_LEVELS):
        if loss_rate in REUSED_LOSS_LEVELS:
            continue
        seed += 1
        ns3_run += 1
        cfg = deepcopy(base)
        # At 10--50% loss, 100 cycles still provide thousands of target-link
        # packets, while avoiding the unnecessary 300-cycle low-loss precheck.
        cfg["iterations"] = 100
        todo3.configure_target_links(
            cfg,
            loss_rate=loss_rate,
            stream_base=9000 + index * 100,
        )
        todo3.disable_attacks(cfg)
        todo3.instrument(cfg, pcap_links=[TARGET_LINK])
        label = loss_label(loss_rate)
        experiment_id = f"paper_loss_{label}pct"
        todo3.set_experiment(
            cfg,
            experiment_id=experiment_id,
            group="loss",
            parameter="loss_rate",
            value=loss_rate,
            seed=seed,
            ns3_run=ns3_run,
            target_links=[TARGET_LINK],
            paper_supplement=True,
            selected_iterations=100,
            per_request_timeout_sec=2.0,
            maximum_connection_retries=60,
            maximum_experiment_wall_clock_sec=1800,
        )
        specs.append(
            {
                "id": experiment_id,
                "group": "loss",
                "config": cfg,
                "timeout_sec": 1800,
                "env": {"MODBUS_TIMEOUT": "2.0", "CONNECT_RETRIES": "60"},
            }
        )
    return specs


def prepare(archive: Path, specs: list[dict[str, Any]]) -> None:
    for section in ("01_delay_path_validation", "02_packet_loss_21_levels"):
        (archive / section).mkdir(parents=True, exist_ok=True)
    plan_rows = []
    for item in specs:
        config_path, output_dir = todo3.attempt_paths(archive, item, 1)
        item["config_path"] = str(config_path)
        item["output_dir"] = str(output_dir)
        plan_rows.append(
            {
                "id": item["id"],
                "group": item["group"],
                "config": str(config_path),
                "output": str(output_dir),
            }
        )
    plan = {
        "schema_version": 1,
        "purpose": "dense paper network-parameter traceability figure",
        "target_link": TARGET_LINK,
        "delay_levels_ms": list(DELAY_LEVELS_MS),
        "loss_levels_fraction": list(LOSS_LEVELS),
        "reused_delay_levels_ms": sorted(REUSED_DELAY_LEVELS),
        "reused_loss_levels_fraction": sorted(REUSED_LOSS_LEVELS),
        "new_run_count": len(specs),
        "experiments": plan_rows,
    }
    (archive / "EXPERIMENT_PLAN.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def valid_outputs(archive: Path) -> dict[str, Path]:
    path = archive / "RUN_INDEX.json"
    if not path.is_file():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(row["id"]): Path(row["output"])
        for row in rows
        if row.get("valid") and row.get("output")
    }


def old_valid_outputs() -> dict[str, Path]:
    rows = json.loads((OLD_ARCHIVE / "RUN_INDEX.json").read_text(encoding="utf-8"))
    return {
        str(row["id"]): Path(row["output"])
        for row in rows
        if row.get("valid") and row.get("output")
    }


def link_rows(output: Path) -> list[dict[str, str]]:
    candidates = (
        output / "reports/network/link-metrics.csv",
        output / "runtime/network/link-metrics.csv",
    )
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        raise FileNotFoundError(f"No link-metrics.csv under {output}")
    with path.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle) if row.get("link") == TARGET_LINK]


def weighted_delay(output: Path) -> tuple[float, int]:
    rows = link_rows(output)
    samples = sum(int(float(row["delay_samples"])) for row in rows)
    assert samples > 0
    value = sum(
        float(row["mean_delay_ms"]) * int(float(row["delay_samples"])) for row in rows
    ) / samples
    return value, samples


def measured_loss(output: Path) -> tuple[float, int, int]:
    rows = link_rows(output)
    tx = sum(int(float(row["tx_packets"])) for row in rows)
    drops = sum(int(float(row["error_model_drop_packets"])) for row in rows)
    assert tx > 0 and drops <= tx
    return drops / tx, tx, drops


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def collect(archive: Path) -> None:
    old = old_valid_outputs()
    new = valid_outputs(archive)
    delay_rows = []
    for level in DELAY_LEVELS_MS:
        experiment_id = f"delay_path_{level}ms" if level in REUSED_DELAY_LEVELS else f"paper_delay_{level}ms"
        source = old.get(experiment_id) if level in REUSED_DELAY_LEVELS else new.get(experiment_id)
        if source is None:
            raise RuntimeError(f"Missing completed delay run: {experiment_id}")
        measured, samples = weighted_delay(source)
        delay_rows.append(
            {
                "configured_delay_ms": level,
                "measured_delay_ms": f"{measured:.9f}",
                "delay_samples": samples,
                "target_link": TARGET_LINK,
                "source_output": str(source),
            }
        )

    loss_rows = []
    for level in LOSS_LEVELS:
        if level == 0.50:
            source = old["packet_loss_50pct"]
        else:
            source = new.get(f"paper_loss_{loss_label(level)}pct")
        if source is None:
            raise RuntimeError(f"Missing completed loss run: {level:.0%}")
        measured, tx, drops = measured_loss(source)
        loss_rows.append(
            {
                "configured_loss_percent": f"{100 * level:.0f}",
                "measured_loss_percent": f"{100 * measured:.6f}",
                "tx_packets": tx,
                "error_model_drop_packets": drops,
                "target_link": TARGET_LINK,
                "source_output": str(source),
            }
        )

    write_csv(PAPER_ROOT / "data/network_delay_traceability.csv", delay_rows)
    write_csv(PAPER_ROOT / "data/network_loss_traceability.csv", loss_rows)
    print(f"Collected {len(delay_rows)} delay levels and {len(loss_rows)} loss levels.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--group", choices=("delay", "loss"), action="append")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args()
    archive = args.archive.expanduser().resolve()
    specs = build_specs()
    if args.group:
        specs = [item for item in specs if item["group"] in set(args.group)]
    if args.collect_only:
        collect(archive)
        return 0
    prepare(archive, specs)
    print(f"Prepared {len(specs)} missing configurations in {archive}")
    if not args.run:
        print("Preparation only. Authenticate with 'sudo -v', then rerun with --run.")
        return 0
    status = todo3.run(archive, specs)
    if status == 0:
        collect(archive)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
