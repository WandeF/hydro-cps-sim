#!/usr/bin/env python3
"""Export user-facing reports from Hydro-CPS-Sim runtime raw/json data."""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config import load_runtime_config
from src.io.dhalsim import dhalsim_tag_columns, snapshot_to_dhalsim_row


PLC_ORDER = ["PLC1", "PLC2", "PLC3", "PLC4", "PLC5", "PLC7", "PLC8", "PLC9"]
KEY_SCADA_COLUMNS = [
    "PLC9.PLC9_T7",
    "PLC4.PLC4_T3",
    "PLC4.PLC4_T4",
    "PLC7.PLC7_T5",
    "PLC8.PLC8_T6",
]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [dict(row) for row in csv.DictReader(f)]


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})


def _fmt(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    try:
        if isinstance(value, str) and value.strip() == "":
            return ""
        num = float(value)
    except (TypeError, ValueError):
        return value
    return f"{num:.6f}"


def _iteration(row: dict[str, Any]) -> int:
    raw = row.get("iteration", 0)
    return int(float(raw)) if raw not in {None, ""} else 0


def _load_physics_snapshots(runtime_dir: Path) -> list[dict[str, Any]]:
    raw_rows = _read_jsonl(runtime_dir / "raw" / "physics.jsonl")
    if raw_rows:
        by_iter = {_iteration(row): row for row in raw_rows}
        return [by_iter[i] for i in sorted(by_iter)]

    snapshots: list[dict[str, Any]] = []
    for path in sorted((runtime_dir / "json").glob("physics_*.json")):
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            snapshots.append(data)
    return sorted(snapshots, key=_iteration)


def export_physics(rt, runtime_dir: Path, reports_csv_dir: Path) -> Path:  # type: ignore[no-untyped-def]
    rows: list[dict[str, Any]] = []
    columns = ["iteration"] + dhalsim_tag_columns(rt)
    for snapshot in _load_physics_snapshots(runtime_dir):
        row = snapshot_to_dhalsim_row(rt, snapshot)
        rows.append({col: (_fmt(row.get(col)) if col != "iteration" else row.get(col, "")) for col in columns})
    out = reports_csv_dir / "physics.csv"
    _write_csv(out, rows, columns)
    return out


def export_actuator_state(runtime_dir: Path, reports_csv_dir: Path) -> Path:
    raw_rows = _read_jsonl(runtime_dir / "raw" / "actuator_state.jsonl")
    if not raw_rows:
        for path in sorted((runtime_dir / "json").glob("actuator_state_*.json")):
            match = re.search(r"actuator_state_(\d+)\.json$", path.name)
            if not match:
                continue
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = {"iteration": int(match.group(1)), **data}
                raw_rows.append(data)
    by_iter = {_iteration(row): row for row in raw_rows}
    names = sorted({k for row in raw_rows for k in row if k != "iteration"})
    columns = ["iteration"] + names
    rows = []
    for iteration in sorted(by_iter):
        row = by_iter[iteration]
        rows.append({"iteration": iteration, **{name: _fmt(row.get(name, "")) for name in names}})
    out = reports_csv_dir / "actuator_state.csv"
    _write_csv(out, rows, columns)
    return out


def _load_scada_observed(runtime_dir: Path) -> list[dict[str, Any]]:
    rows = _read_jsonl(runtime_dir / "raw" / "scada_observed.jsonl")
    if rows:
        return rows
    return [dict(row) for row in _read_csv(runtime_dir / "csv" / "scada_observed.csv")]


def _is_local_scada_observation(row: dict[str, Any]) -> bool:
    plc = str(row.get("plc", "")).strip()
    variable = str(row.get("variable", "")).strip()
    if not plc or not variable:
        return False
    owner, _, _name = variable.partition("_")
    return owner == plc


def export_scada_observed(runtime_dir: Path, reports_csv_dir: Path) -> list[Path]:
    raw_rows = _load_scada_observed(runtime_dir)
    normalized: list[dict[str, Any]] = []
    for row in raw_rows:
        if not _is_local_scada_observation(row):
            continue
        normalized.append({
            "iteration": _iteration(row),
            "plc": row.get("plc", ""),
            "variable": row.get("variable", ""),
            "value": _fmt(row.get("value")),
            "source": row.get("source", ""),
            "direction": row.get("direction", ""),
            "kind": row.get("kind", ""),
            "timestamp_epoch": row.get("timestamp_epoch", ""),
        })
    normalized.sort(key=lambda r: (int(r["iteration"]), str(r["plc"]), str(r["variable"])))

    long_cols = ["iteration", "plc", "variable", "value", "source", "direction", "kind", "timestamp_epoch"]
    long_path = reports_csv_dir / "scada_observed_long.csv"
    _write_csv(long_path, normalized, long_cols)

    wide_by_iter: dict[int, dict[str, Any]] = {}
    wide_cols_set: set[str] = set()
    for row in normalized:
        iteration = int(row["iteration"])
        key = f"{row['plc']}.{row['variable']}"
        wide_by_iter.setdefault(iteration, {"iteration": iteration})[key] = row["value"]
        wide_cols_set.add(key)

    def col_key(col: str) -> tuple[int, str]:
        plc, _, var = col.partition(".")
        try:
            plc_idx = PLC_ORDER.index(plc)
        except ValueError:
            plc_idx = len(PLC_ORDER)
        return plc_idx, var

    wide_cols = ["iteration"] + sorted(wide_cols_set, key=col_key)
    wide_rows = [wide_by_iter[i] for i in sorted(wide_by_iter)]
    wide_path = reports_csv_dir / "scada_observed_wide.csv"
    _write_csv(wide_path, wide_rows, wide_cols)

    key_cols = ["iteration"] + KEY_SCADA_COLUMNS
    key_rows = [{col: row.get(col, "") for col in key_cols} for row in wide_rows]
    key_path = reports_csv_dir / "scada_observed_key.csv"
    _write_csv(key_path, key_rows, key_cols)
    return [long_path, wide_path, key_path]


def export_attack_events(runtime_dir: Path, reports_csv_dir: Path) -> Path:
    rows = _read_jsonl(runtime_dir / "raw" / "attack_events.jsonl")
    if not rows:
        for row in _read_csv(runtime_dir / "csv" / "attack_events.csv"):
            rows.append({
                "timestamp_epoch": row.get("timestamp_epoch", ""),
                "iteration": row.get("iteration", ""),
                "scenario": row.get("attack", ""),
                "rule": row.get("rule", ""),
                "target": row.get("target", ""),
                "variable": row.get("variable", ""),
                "direction": row.get("direction", ""),
                "function_code": row.get("function_code", ""),
                "transaction_id": row.get("transaction_id", ""),
                "old_value": row.get("original_value", ""),
                "new_value": row.get("modified_value", ""),
            })
    columns = [
        "iteration",
        "scenario",
        "attack",
        "event",
        "rule",
        "source",
        "target",
        "target_ip",
        "target_port",
        "protocol",
        "rate",
        "packet_size",
        "packets",
        "bytes",
        "variable",
        "direction",
        "function_code",
        "old_value",
        "new_value",
        "timestamp_epoch",
        "transaction_id",
        "message",
    ]
    out_rows = []
    for row in rows:
        scenario = row.get("scenario", row.get("attack", ""))
        out_rows.append({
            **row,
            "scenario": scenario,
            "attack": row.get("attack", scenario),
            "iteration": "" if row.get("iteration") in {None, ""} else _iteration(row),
            "old_value": _fmt(row.get("old_value")),
            "new_value": _fmt(row.get("new_value")),
        })
    out_rows.sort(key=lambda r: (_iteration(r), str(r.get("scenario", "")), str(r.get("target", ""))))
    out = reports_csv_dir / "attack_events.csv"
    _write_csv(out, out_rows, columns)
    return out


def export_attack_schedule(runtime_dir: Path, reports_csv_dir: Path) -> Path:
    rows = _read_jsonl(runtime_dir / "raw" / "attack_schedule.jsonl")
    if not rows:
        for row in _read_csv(runtime_dir / "csv" / "attack_schedule.csv"):
            event = row.get("action", "")
            rows.append({
                "timestamp_epoch": row.get("timestamp_epoch", ""),
                "iteration": row.get("iteration", ""),
                "scenario": row.get("attack", ""),
                "target": row.get("target", ""),
                "event": event,
                "active": event == "attack_on",
                "active_window": row.get("active_window", ""),
                "proxy_pid": row.get("proxy_pid", ""),
                "message": row.get("message", ""),
            })
    columns = ["iteration", "scenario", "target", "event", "active", "timestamp_epoch", "active_window", "proxy_pid", "message"]
    rows.sort(key=lambda r: (_iteration(r), str(r.get("scenario", "")), str(r.get("target", ""))))
    out = reports_csv_dir / "attack_schedule.csv"
    _write_csv(out, rows, columns)
    return out


def export_scada_timeout_events(runtime_dir: Path, reports_csv_dir: Path) -> Path:
    rows = _read_jsonl(runtime_dir / "raw" / "scada_timeout_events.jsonl")
    if not rows:
        rows = [dict(row) for row in _read_csv(runtime_dir / "csv" / "scada_timeout_events.csv")]
    columns = [
        "timestamp_epoch",
        "iteration",
        "phase",
        "plc",
        "ip",
        "status",
        "warmup",
        "used_previous",
        "previous_iteration",
        "message",
    ]
    rows.sort(key=lambda r: (_iteration(r), str(r.get("phase", "")), str(r.get("plc", ""))))
    out = reports_csv_dir / "scada_timeout_events.csv"
    _write_csv(out, rows, columns)
    return out


def export_cycle_timing(runtime_dir: Path, reports_csv_dir: Path) -> Path:
    rows = _read_jsonl(runtime_dir / "raw" / "cycle_timing.jsonl")
    if not rows:
        rows = [dict(row) for row in _read_csv(runtime_dir / "csv" / "closed_loop_timing.csv")]
    base_cols = [
        "iteration",
        "input_physics_iteration",
        "output_physics_iteration",
        "wait_local_write_sec",
        "wait_scada_downlink_sec",
        "logic_wait_sec",
        "signal_read_actuators_sec",
        "wait_actuator_read_sec",
        "merge_actuator_state_sec",
        "physics_step_publish_sec",
        "write_cycle_logs_sec",
        "cycle_total_sec",
        "local_plc_count",
        "actuator_count",
        "physics_value_count",
    ]
    extra = sorted({k for row in rows for k in row if k not in base_cols})
    columns = base_cols + extra
    out_rows = []
    for row in sorted(rows, key=_iteration):
        out_rows.append({col: (_fmt(row.get(col)) if col.endswith("_sec") else row.get(col, "")) for col in columns})
    out = reports_csv_dir / "cycle_timing.csv"
    _write_csv(out, out_rows, columns)
    return out


def export_metric_artifacts(runtime_dir: Path, reports_dir: Path) -> dict[str, Path]:
    """Copy fixed-schema quantitative outputs without reshaping their rows."""
    outputs: dict[str, Path] = {}
    reports_csv_dir = reports_dir / "csv"
    reports_csv_dir.mkdir(parents=True, exist_ok=True)
    for name in ("events.csv", "communication.csv", "resources.csv", "network.csv"):
        source = runtime_dir / "csv" / name
        target = reports_csv_dir / name
        if target.exists():
            target.unlink()
        if not source.exists():
            continue
        shutil.copy2(source, target)
        outputs[Path(name).stem] = target

    for source, relative_target in (
        (runtime_dir / "manifest.json", Path("manifest.json")),
        (runtime_dir / "config_resolved.yaml", Path("config_resolved.yaml")),
        (runtime_dir / "network" / "network-aggregate.json", Path("network") / "network-aggregate.json"),
        (runtime_dir / "network" / "flow-monitor.xml", Path("network") / "flow-monitor.xml"),
        (runtime_dir / "network" / "link-metrics.csv", Path("network") / "link-metrics.csv"),
    ):
        target = reports_dir / relative_target
        if target.exists():
            target.unlink()
        if not source.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        outputs[f"artifact_{source.stem}"] = target

    writer_stats_source = runtime_dir / "raw" / "metric_writer_stats"
    writer_stats_target = reports_dir / "metric_writer_stats"
    if writer_stats_target.exists():
        shutil.rmtree(writer_stats_target)
    if writer_stats_source.is_dir():
        shutil.copytree(writer_stats_source, writer_stats_target)
        outputs["metric_writer_stats"] = writer_stats_target
    return outputs


def sync_compat_csv(reports_csv_dir: Path, runtime_dir: Path) -> None:
    compat_dir = runtime_dir / "csv"
    compat_dir.mkdir(parents=True, exist_ok=True)
    for src in reports_csv_dir.glob("*.csv"):
        try:
            shutil.copy2(src, compat_dir / src.name)
        except OSError as exc:
            print(f"[EXPORT][WARN] compatibility copy skipped {src.name}: {exc}")
    aliases = {
        "scada_observed_long.csv": "scada_observed.csv",
        "cycle_timing.csv": "closed_loop_timing.csv",
    }
    for src_name, alias_name in aliases.items():
        src = reports_csv_dir / src_name
        if src.exists():
            try:
                shutil.copy2(src, compat_dir / alias_name)
            except OSError as exc:
                print(f"[EXPORT][WARN] compatibility alias skipped {alias_name}: {exc}")


def export_all(config: Path, runtime_dir: Path | None = None, reports_dir: Path | None = None) -> dict[str, Path]:
    rt = load_runtime_config(config)
    runtime_dir = runtime_dir or (rt.output_dir / "runtime")
    reports_dir = reports_dir or (rt.output_dir / "reports")
    reports_csv_dir = reports_dir / "csv"
    reports_csv_dir.mkdir(parents=True, exist_ok=True)

    outputs: dict[str, Path] = {}
    outputs["physics"] = export_physics(rt, runtime_dir, reports_csv_dir)
    outputs["actuator_state"] = export_actuator_state(runtime_dir, reports_csv_dir)
    for path in export_scada_observed(runtime_dir, reports_csv_dir):
        outputs[path.stem] = path
    outputs["attack_events"] = export_attack_events(runtime_dir, reports_csv_dir)
    outputs["attack_schedule"] = export_attack_schedule(runtime_dir, reports_csv_dir)
    outputs["scada_timeout_events"] = export_scada_timeout_events(runtime_dir, reports_csv_dir)
    outputs["cycle_timing"] = export_cycle_timing(runtime_dir, reports_csv_dir)
    outputs.update(export_metric_artifacts(runtime_dir, reports_dir))
    sync_compat_csv(reports_csv_dir, runtime_dir)
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Hydro-CPS-Sim reports from runtime raw/json data")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--runtime-dir", type=Path, default=None)
    parser.add_argument("--reports-dir", type=Path, default=None)
    args = parser.parse_args()

    outputs = export_all(args.config.resolve(), args.runtime_dir, args.reports_dir)
    for name, path in sorted(outputs.items()):
        print(f"[EXPORT] {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
