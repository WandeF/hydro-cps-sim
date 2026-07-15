#!/usr/bin/env python3
"""Convert ns-3 FlowMonitor/link snapshots to network.csv and JSON summary."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config import load_yaml
from src.experiment.manifest import sha256_file
from src.metrics.network_metrics import analyze_network
from src.network.ns3_generation import resolve_output_dir


def _simulation_status(output_dir: Path | None) -> str | None:
    if output_dir is None:
        return None
    path = output_dir / "runtime" / "csv" / "events.csv"
    status: str | None = None
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if str(row.get("event_type", "")).strip() == "simulation_end":
                    status = str(row.get("status", "")).strip().lower() or None
    except (OSError, UnicodeError, csv.Error):
        return None
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze Hydro-CPS-Sim ns-3 network telemetry")
    parser.add_argument("--config", type=Path, help="Config used to resolve the default output directory")
    parser.add_argument("--network-dir", type=Path, help="Directory containing flow-monitor.xml/link-metrics.csv")
    parser.add_argument("--flow-monitor", type=Path, help="Explicit FlowMonitor XML path")
    parser.add_argument("--link-metrics", type=Path, help="Explicit P2P link metrics CSV path")
    parser.add_argument("--output-csv", type=Path, help="Output network.csv path")
    parser.add_argument("--aggregate-json", type=Path, help="Output aggregate JSON path")
    args = parser.parse_args()

    output_dir: Path | None = None
    cfg: dict = {}
    if args.config is not None:
        config_path = args.config.expanduser().resolve()
        cfg = load_yaml(config_path)
        output_dir = resolve_output_dir(config_path, cfg)

    if args.network_dir is not None:
        network_dir = args.network_dir.expanduser().resolve()
    elif output_dir is not None:
        network_dir = output_dir / "runtime" / "network"
    else:
        network_dir = Path.cwd()

    flow_monitor = args.flow_monitor.expanduser().resolve() if args.flow_monitor else network_dir / "flow-monitor.xml"
    link_metrics = args.link_metrics.expanduser().resolve() if args.link_metrics else network_dir / "link-metrics.csv"
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else ((output_dir / "runtime" / "csv" / "network.csv") if output_dir else network_dir / "network.csv")
    )
    aggregate_json = (
        args.aggregate_json.expanduser().resolve()
        if args.aggregate_json
        else network_dir / "network-aggregate.json"
    )

    summary = analyze_network(
        flow_monitor_xml=flow_monitor,
        link_metrics_csv=link_metrics,
        output_csv=output_csv,
        aggregate_json=aggregate_json,
    )
    manifest: dict = {}
    if output_dir is not None:
        manifest_path = output_dir / "runtime" / "manifest.json"
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
        except (OSError, ValueError, TypeError):
            pass
        if manifest and args.config is not None:
            try:
                manifest_config = Path(str(manifest.get("config_file", ""))).expanduser().resolve()
                manifest_hash = str(manifest.get("config_sha256", ""))
                if manifest_config != config_path or (manifest_hash and manifest_hash != sha256_file(config_path)):
                    manifest = {}
            except (OSError, ValueError, TypeError):
                manifest = {}
    experiment = cfg.get("experiment", {}) if isinstance(cfg, dict) else {}
    if not isinstance(experiment, dict):
        experiment = {}
    summary.update({
        "metric_type": "network",
        "experiment_id": manifest.get("experiment_id") or experiment.get("id") or experiment.get("name"),
        "group": manifest.get("group") or experiment.get("group"),
        "parameter": manifest.get("parameter") or experiment.get("parameter"),
        "parameter_value": manifest.get("parameter_value") if manifest.get("parameter_value") not in (None, "") else experiment.get("value"),
        "repetition": manifest.get("repetition") if manifest.get("repetition") not in (None, "") else experiment.get("repetition"),
        "run_status": _simulation_status(output_dir),
    })
    summary["complete"] = summary["run_status"] == "success" if summary["run_status"] is not None else None
    aggregate_json.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"[NETWORK-METRICS] status={summary['status']} rows={summary['row_count']}")
    print(f"[NETWORK-METRICS] csv={output_csv}")
    print(f"[NETWORK-METRICS] aggregate={aggregate_json}")
    for warning in summary["warnings"]:
        print(f"[NETWORK-METRICS][WARN] {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
