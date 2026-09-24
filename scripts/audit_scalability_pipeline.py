#!/usr/bin/env python3
"""Audit every data-feed/source/SCADA/execution record in a scale run."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config import load_yaml


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _output_dir(config_path: Path, cfg: dict[str, Any]) -> Path:
    raw = Path(str(cfg["output_path"])).expanduser()
    return raw.resolve() if raw.is_absolute() else (config_path.parent / raw).resolve()


def _pairs(cfg: dict[str, Any]) -> list[dict[str, str]]:
    sources = {
        str(plc["name"]): plc
        for plc in cfg.get("plcs", []) or []
        if isinstance(plc, dict) and plc.get("scale_role") == "source"
    }
    executions = {
        str(plc["name"]): plc
        for plc in cfg.get("plcs", []) or []
        if isinstance(plc, dict) and plc.get("scale_role") == "execution"
    }
    pairs: list[dict[str, str]] = []
    for source_name, source in sorted(
        sources.items(), key=lambda item: int(item[0].removeprefix("PLC"))
    ):
        execution_name = str(source.get("paired_plc", ""))
        execution = executions.get(execution_name)
        sensors = [str(tag) for tag in source.get("sensors", []) or []]
        actuators = [str(tag) for tag in (execution or {}).get("actuators", []) or []]
        if execution is None or len(sensors) != 1 or len(actuators) != 1:
            raise ValueError(f"invalid scale pair {source_name}->{execution_name}")
        tag = sensors[0]
        pairs.append({
            "source_plc": source_name,
            "execution_plc": execution_name,
            "tag": tag,
            "source_variable": f"{source_name}_{tag}",
            "destination_variable": f"{source_name}_{tag}",
            "actuator": actuators[0],
        })
    if len(pairs) * 2 != len(cfg.get("plcs", []) or []):
        raise ValueError("every PLC must belong to exactly one source/execution pair")
    return pairs


def audit(config_path: Path, *, tolerance: float = 1e-5) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config_path = config_path.expanduser().resolve()
    cfg = load_yaml(config_path)
    output = _output_dir(config_path, cfg)
    runtime_json = output / "runtime/json"
    pairs = _pairs(cfg)
    iterations = int(cfg.get("iterations", 0))
    rows: list[dict[str, Any]] = []

    for iteration in range(iterations):
        feed = _read_json(runtime_json / f"physics_{iteration:04d}.json")
        poll = _read_json(runtime_json / f"scada_poll_{iteration:04d}.json")
        downlink = _read_json(runtime_json / f"scada_downlink_{iteration:04d}.json")
        actuators = _read_json(runtime_json / f"actuator_state_{iteration:04d}.json")
        feed_values = feed.get("values", {}) or {}
        poll_plcs = poll.get("plcs", {}) or {}
        downlink_plcs = downlink.get("plcs", {}) or {}

        for pair_index, pair in enumerate(pairs, start=1):
            feed_value = _number(feed_values.get(pair["tag"]))
            source_value = _number(
                ((poll_plcs.get(pair["source_plc"], {}) or {}).get("md", {}) or {}).get(
                    pair["source_variable"]
                )
            )
            destination_meta = (
                ((downlink_plcs.get(pair["execution_plc"], {}) or {}).get("written", {}) or {}).get(
                    pair["destination_variable"], {}
                )
                or {}
            )
            destination_value = _number(destination_meta.get("value"))
            actual_raw = actuators.get(pair["actuator"])
            actual_state = bool(actual_raw) if isinstance(actual_raw, bool) else None
            expected_state = feed_value >= 5.0 if feed_value is not None else None
            source_error = (
                abs(source_value - feed_value)
                if source_value is not None and feed_value is not None else None
            )
            destination_error = (
                abs(destination_value - feed_value)
                if destination_value is not None and feed_value is not None else None
            )
            source_match = source_error is not None and source_error <= tolerance
            destination_match = destination_error is not None and destination_error <= tolerance
            logic_correct = (
                actual_state is not None
                and expected_state is not None
                and actual_state == expected_state
            )
            rows.append({
                "iteration": iteration,
                "pair_index": pair_index,
                **pair,
                "feed_value": feed_value,
                "source_plc_observed_value": source_value,
                "destination_received_value": destination_value,
                "source_absolute_error": source_error,
                "destination_absolute_error": destination_error,
                "source_match": source_match,
                "destination_match": destination_match,
                "expected_actuator_state": expected_state,
                "actual_actuator_state": actual_state,
                "logic_correct": logic_correct,
                "end_to_end_correct": source_match and destination_match and logic_correct,
            })

    expected = iterations * len(pairs)
    complete_iterations = sum(
        all(row["end_to_end_correct"] for row in rows if row["iteration"] == iteration)
        and sum(1 for row in rows if row["iteration"] == iteration) == len(pairs)
        for iteration in range(iterations)
    )

    def count(field: str) -> int:
        return sum(bool(row[field]) for row in rows)

    def rate(field: str) -> float | None:
        return count(field) / expected if expected else None

    values = [float(row["feed_value"]) for row in rows if row["feed_value"] is not None]
    summary = {
        "schema_version": 1,
        "experiment_id": (cfg.get("experiment", {}) or {}).get("id"),
        "plc_count": len(cfg.get("plcs", []) or []),
        "source_plc_count": len(pairs),
        "execution_plc_count": len(pairs),
        "communication_pair_count": len(pairs),
        "iteration_count": iterations,
        "expected_pair_iteration_records": expected,
        "observed_pair_iteration_records": len(rows),
        "source_match_count": count("source_match"),
        "source_match_rate": rate("source_match"),
        "destination_match_count": count("destination_match"),
        "destination_match_rate": rate("destination_match"),
        "logic_correct_count": count("logic_correct"),
        "logic_correct_rate": rate("logic_correct"),
        "end_to_end_correct_count": count("end_to_end_correct"),
        "end_to_end_correct_rate": rate("end_to_end_correct"),
        "complete_iteration_count": complete_iterations,
        "feed_below_5_count": sum(value < 5.0 for value in values),
        "feed_at_or_above_5_count": sum(value >= 5.0 for value in values),
        "feed_minimum_observed": min(values, default=None),
        "feed_maximum_observed": max(values, default=None),
        "comparison_tolerance": tolerance,
        "pass": (
            len(rows) == expected
            and complete_iterations == iterations
            and count("source_match") == expected
            and count("destination_match") == expected
            and count("logic_correct") == expected
        ),
        "source_files": {
            "data_feed_snapshots": str(runtime_json / "physics_XXXX.json"),
            "scada_poll": str(runtime_json / "scada_poll_XXXX.json"),
            "scada_downlink": str(runtime_json / "scada_downlink_XXXX.json"),
            "actuator_state": str(runtime_json / "actuator_state_XXXX.json"),
        },
    }
    return rows, summary


def write_outputs(config_path: Path, rows: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path]:
    cfg = load_yaml(config_path)
    output = _output_dir(config_path, cfg)
    destination = output / "reports/advisor"
    destination.mkdir(parents=True, exist_ok=True)
    csv_path = destination / "scalability_pipeline_audit.csv"
    json_path = destination / "scalability_pipeline_audit_summary.json"
    fields = list(rows[0]) if rows else ["iteration", "pair_index"]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return csv_path, json_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--tolerance", type=float, default=1e-5)
    args = parser.parse_args()
    rows, summary = audit(args.config, tolerance=args.tolerance)
    csv_path, json_path = write_outputs(args.config.resolve(), rows, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[AUDIT] rows={csv_path}")
    print(f"[AUDIT] summary={json_path}")
    return 0 if summary["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
