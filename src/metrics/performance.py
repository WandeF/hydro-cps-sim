#!/usr/bin/env python3
"""Offline runtime, resource, and communication performance metrics.

The analyzer consumes artifacts that are already emitted by an experiment.  It
does not depend on pandas and deliberately treats every artifact as optional so
that interrupted or older runs can still be summarized.
"""
from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from statistics import fmean, stdev
from typing import Any, Iterable, Mapping, Sequence

from src.core.config import load_yaml
from src.metrics.writer_quality import analyze_metric_writer_stats, required_metric_writers


def _finite_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_int(value: Any) -> int | None:
    number = _finite_float(value)
    if number is None or number <= 0 or not number.is_integer():
        return None
    return int(number)


def percentile(values: Sequence[float], quantile: float) -> float | None:
    """Return a linearly interpolated percentile (the common type-7 method)."""

    if not values:
        return None
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be between zero and one")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _candidate_roots(path: Path | str) -> list[Path]:
    root = Path(path).expanduser().resolve()
    candidates = [root]
    if root.name == "runtime":
        candidates.append(root.parent)
    elif (root / "runtime").is_dir():
        candidates.append(root / "runtime")
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def resolve_performance_artifacts(path: Path | str) -> dict[str, Path | None]:
    """Resolve performance inputs from a run, output, or runtime directory."""

    roots = _candidate_roots(path)
    root = roots[0]

    def first(relative_paths: Iterable[Path]) -> Path | None:
        seen: set[Path] = set()
        for candidate in relative_paths:
            candidate = candidate.resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file():
                return candidate
        return None

    def first_dir(relative_paths: Iterable[Path]) -> Path | None:
        seen: set[Path] = set()
        for candidate in relative_paths:
            candidate = candidate.resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_dir():
                return candidate
        return None

    def under_roots(*relative_paths: str) -> list[Path]:
        return [base / relative for base in roots for relative in relative_paths]

    cycle_candidates = under_roots(
        "cycle_timing.csv",
        "csv/cycle_timing.csv",
        "csv/closed_loop_timing.csv",
        "reports/csv/cycle_timing.csv",
        "runtime/csv/cycle_timing.csv",
        "runtime/csv/closed_loop_timing.csv",
    )
    resource_candidates = under_roots(
        "resources.csv",
        "csv/resources.csv",
        "reports/csv/resources.csv",
        "runtime/csv/resources.csv",
    )
    communication_candidates = under_roots(
        "communication.csv",
        "csv/communication.csv",
        "reports/csv/communication.csv",
        "runtime/csv/communication.csv",
    )
    stage_candidates = under_roots(
        "run_all_timing.csv",
        "timing/run_all_timing.csv",
    )
    manifest_candidates = under_roots(
        "manifest.json",
        "reports/manifest.json",
        "runtime/manifest.json",
    )
    event_candidates = under_roots(
        "events.csv",
        "csv/events.csv",
        "reports/csv/events.csv",
        "runtime/csv/events.csv",
    )
    config_candidates = under_roots(
        "config_resolved.yaml",
        "reports/config_resolved.yaml",
        "runtime/config_resolved.yaml",
    )
    writer_stats_candidates = under_roots(
        "metric_writer_stats",
        "raw/metric_writer_stats",
        "reports/metric_writer_stats",
        "runtime/raw/metric_writer_stats",
    )

    # A runtime path is commonly passed directly while timing lives beside it.
    if root.name == "runtime":
        stage_candidates.insert(0, root.parent / "timing" / "run_all_timing.csv")
        manifest_candidates.insert(0, root / "manifest.json")
        event_candidates.insert(0, root / "csv" / "events.csv")
        config_candidates.insert(0, root / "config_resolved.yaml")
        writer_stats_candidates.insert(0, root / "raw" / "metric_writer_stats")
    elif (root / "runtime").is_dir():
        manifest_candidates.insert(0, root / "runtime" / "manifest.json")
        event_candidates.insert(0, root / "runtime" / "csv" / "events.csv")
        config_candidates.insert(0, root / "runtime" / "config_resolved.yaml")
        writer_stats_candidates.insert(0, root / "runtime" / "raw" / "metric_writer_stats")

    return {
        "cycle_timing_csv": first(cycle_candidates),
        "resources_csv": first(resource_candidates),
        "communication_csv": first(communication_candidates),
        "run_all_timing_csv": first(stage_candidates),
        "manifest_json": first(manifest_candidates),
        "events_csv": first(event_candidates),
        "config_resolved_yaml": first(config_candidates),
        "writer_stats_dir": first_dir(writer_stats_candidates),
    }


def _read_csv(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return []
            return [dict(row) for row in reader if any(str(value or "").strip() for value in row.values())]
    except (OSError, UnicodeError, csv.Error):
        return []


def _read_manifest(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def _read_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        return load_yaml(path)
    except (OSError, UnicodeError, ValueError):
        return {}


def _simulation_status(rows: Sequence[Mapping[str, Any]]) -> str | None:
    status: str | None = None
    for row in rows:
        if str(row.get("event_type", "")).strip() == "simulation_end":
            status = str(row.get("status", "")).strip().lower() or None
    return status


def iteration_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values: list[float] = []
    for row in rows:
        value = _finite_float(
            row.get("cycle_total_sec", row.get("iteration_time_sec", row.get("duration_sec")))
        )
        if value is not None and value >= 0:
            values.append(value)
    total = math.fsum(values) if values else None
    mean = fmean(values) if values else None
    deviation = stdev(values) if len(values) >= 2 else None
    p95 = percentile(values, 0.95)
    return {
        "count": len(values),
        "total_sec": total,
        "mean_sec": mean,
        "std_sec": deviation,
        "p95_sec": p95,
        "min_sec": min(values) if values else None,
        "max_sec": max(values) if values else None,
        "mean_ms": None if mean is None else mean * 1000.0,
        "std_ms": None if deviation is None else deviation * 1000.0,
        "p95_ms": None if p95 is None else p95 * 1000.0,
    }


def resource_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate process rows by timestamp after de-duplicating each PID."""

    samples: dict[str, dict[str, dict[str, float | None]]] = {}
    for index, row in enumerate(rows):
        timestamp = str(row.get("timestamp_ns") or row.get("monotonic_ns") or f"row:{index}")
        pid = str(row.get("pid") or f"{row.get('component', 'unknown')}:{index}")
        cpu = _finite_float(row.get("cpu_percent"))
        rss = _finite_float(row.get("rss_bytes"))
        if cpu is not None and cpu < 0:
            cpu = None
        if rss is not None and rss < 0:
            rss = None
        process = samples.setdefault(timestamp, {}).setdefault(
            pid, {"cpu_percent": None, "rss_bytes": None}
        )
        # A process can appear once under the coordinator tree and once as a
        # configured root.  Max is stable for RSS and avoids summing duplicates.
        if cpu is not None:
            previous_cpu = process["cpu_percent"]
            process["cpu_percent"] = cpu if previous_cpu is None else max(previous_cpu, cpu)
        if rss is not None:
            previous_rss = process["rss_bytes"]
            process["rss_bytes"] = rss if previous_rss is None else max(previous_rss, rss)

    aggregate_cpu: list[float] = []
    aggregate_rss: list[float] = []
    unique_pids: set[str] = set()
    for processes in samples.values():
        unique_pids.update(processes)
        cpu_values = [
            float(values["cpu_percent"])
            for values in processes.values()
            if values["cpu_percent"] is not None
        ]
        rss_values = [
            float(values["rss_bytes"])
            for values in processes.values()
            if values["rss_bytes"] is not None
        ]
        if cpu_values:
            aggregate_cpu.append(math.fsum(cpu_values))
        if rss_values:
            aggregate_rss.append(math.fsum(rss_values))

    peak_rss = max(aggregate_rss) if aggregate_rss else None
    return {
        "row_count": len(rows),
        "sample_count": len(samples),
        "unique_process_count": len(unique_pids),
        "cpu_sample_count": len(aggregate_cpu),
        "mean_aggregate_cpu_percent": fmean(aggregate_cpu) if aggregate_cpu else None,
        "peak_aggregate_cpu_percent": max(aggregate_cpu) if aggregate_cpu else None,
        "rss_sample_count": len(aggregate_rss),
        "peak_aggregate_rss_bytes": peak_rss,
        "peak_aggregate_rss_mb": None if peak_rss is None else peak_rss / (1024.0 * 1024.0),
    }


def communication_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    row_count = len(rows)
    request_count = 0
    warmup_count = 0
    connection_count = 0
    connection_error_count = 0
    success_count = 0
    timeout_count = 0
    error_count = 0
    rtt_values: list[float] = []
    for row in rows:
        status = str(row.get("status", "")).strip().lower()
        warmup_flag = str(row.get("warmup", "")).strip().lower() in {
            "1", "true", "yes", "on"
        }
        error_text = " ".join(
            str(row.get(key, "")).lower() for key in ("error_type", "error")
        )
        is_timeout = (
            status in {"timeout", "timed_out", "timed out"}
            or "timeout" in error_text
            or "timed out" in error_text
        )
        # Grace applies only to startup timeouts.  Older logs marked every
        # request in the grace iteration as warmup, so a successful row with
        # warmup=true must still contribute to request and RTT statistics.
        status_is_warmup_timeout = status.startswith("warmup_") and (
            "timeout" in status or "timed_out" in status
        )
        is_warmup_timeout = status_is_warmup_timeout or (warmup_flag and is_timeout)
        if is_warmup_timeout:
            warmup_count += 1
            continue
        if str(row.get("operation", "")).strip().lower() == "connect":
            connection_count += 1
            if status not in {"success", "ok", "succeeded"}:
                connection_error_count += 1
            continue
        request_count += 1
        is_success = status in {"success", "ok", "succeeded"}
        if is_success:
            success_count += 1
            latency = _finite_float(row.get("latency_ms", row.get("rtt_ms")))
            if latency is not None and latency >= 0:
                rtt_values.append(latency)
        elif is_timeout:
            timeout_count += 1
        else:
            error_count += 1
    return {
        "row_count": row_count,
        "request_count": request_count,
        "warmup_count": warmup_count,
        "connection_count": connection_count,
        "connection_error_count": connection_error_count,
        "success_count": success_count,
        "timeout_count": timeout_count,
        "error_count": error_count,
        "success_rate": None if request_count == 0 else success_count / request_count,
        "timeout_rate": None if request_count == 0 else timeout_count / request_count,
        "error_rate": None if request_count == 0 else error_count / request_count,
        "rtt_sample_count": len(rtt_values),
        "mean_rtt_ms": fmean(rtt_values) if rtt_values else None,
        "p95_rtt_ms": percentile(rtt_values, 0.95),
    }


def stage_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    for row in rows:
        stage = str(row.get("stage", row.get("phase", ""))).strip()
        duration = _finite_float(row.get("duration_sec"))
        if not stage or duration is None or duration < 0:
            continue
        status = str(row.get("status", "")).strip()
        record = {
            "stage": stage,
            "start_epoch_ns": _positive_int(row.get("start_epoch_ns", row.get("start_ns"))),
            "end_epoch_ns": _positive_int(row.get("end_epoch_ns", row.get("end_ns"))),
            "duration_sec": duration,
            "status": status or None,
        }
        records.append(record)
        aggregate = by_name.setdefault(
            stage,
            {"count": 0, "total_sec": 0.0, "mean_sec": None, "last_status": None},
        )
        aggregate["count"] += 1
        aggregate["total_sec"] += duration
        aggregate["mean_sec"] = aggregate["total_sec"] / aggregate["count"]
        aggregate["last_status"] = status or None

    total_names = {"run_all total", "run all total", "total"}
    component_records = [
        record for record in records if str(record["stage"]).strip().lower() not in total_names
    ]
    successful = {"", "0", "success", "succeeded", "ok", "passed"}
    failed_count = sum(
        str(record.get("status") or "").strip().lower() not in successful
        for record in component_records
    )

    overall_runtime = None
    for record in records:
        if str(record["stage"]).strip().lower() in total_names:
            overall_runtime = float(record["duration_sec"])
    if overall_runtime is None:
        starts = [int(record["start_epoch_ns"]) for record in records if record["start_epoch_ns"]]
        ends = [int(record["end_epoch_ns"]) for record in records if record["end_epoch_ns"]]
        if starts and ends and max(ends) >= min(starts):
            overall_runtime = (max(ends) - min(starts)) / 1_000_000_000.0
        elif component_records:
            overall_runtime = math.fsum(float(record["duration_sec"]) for record in component_records)

    simulation_runtime = None
    simulation_stage = None
    priorities = (
        lambda name: name == "run persistent closed-loop control",
        lambda name: "closed-loop" in name and ("run" in name or "simulation" in name),
        lambda name: "simulation" in name and "total" not in name,
    )
    for predicate in priorities:
        matching = [
            record
            for record in records
            if predicate(str(record["stage"]).strip().lower())
        ]
        if matching:
            simulation_runtime = math.fsum(float(record["duration_sec"]) for record in matching)
            simulation_stage = str(matching[0]["stage"])
            break

    return {
        "count": len(component_records),
        "failed_count": failed_count,
        "component_total_sec": math.fsum(
            float(record["duration_sec"]) for record in component_records
        ) if component_records else None,
        "run_all_total_sec": overall_runtime,
        "simulation_runtime_sec": simulation_runtime,
        "simulation_stage": simulation_stage,
        "by_name": by_name,
        "records": records,
    }


def _default_log_roots(run_path: Path | str) -> list[Path]:
    root = Path(run_path).expanduser().resolve()
    runtime = root if root.name == "runtime" else root / "runtime"
    output = root.parent if root.name == "runtime" else root
    candidates = [
        runtime / "raw",
        runtime / "csv",
        runtime / "json",
        runtime / "logs",
        output / "logs",
    ]
    if root.name in {"raw", "csv", "json", "logs"}:
        candidates.insert(0, root)
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir() and resolved not in unique:
            unique.append(resolved)
    return unique


def log_volume_metrics(
    run_path: Path | str,
    iteration_count: int,
    *,
    log_roots: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    roots = (
        [Path(path).expanduser().resolve() for path in log_roots]
        if log_roots is not None
        else _default_log_roots(run_path)
    )
    files: set[Path] = set()
    for root in roots:
        if root.is_file():
            files.add(root)
        elif root.is_dir():
            files.update(path.resolve() for path in root.rglob("*") if path.is_file())
    total_bytes = 0
    readable_count = 0
    for path in files:
        try:
            total_bytes += path.stat().st_size
            readable_count += 1
        except OSError:
            continue
    return {
        "included_roots": [str(path) for path in roots if path.exists()],
        "file_count": readable_count,
        "total_bytes": total_bytes,
        "bytes_per_iteration": None if iteration_count <= 0 else total_bytes / iteration_count,
    }


def analyze_performance(
    run_path: Path | str,
    *,
    hydraulic_step_sec: float | None = None,
    log_roots: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    """Analyze one run/output/runtime directory without requiring every file."""

    root = Path(run_path).expanduser().resolve()
    artifacts = resolve_performance_artifacts(root)
    manifest = _read_manifest(artifacts["manifest_json"])
    config = _read_config(artifacts["config_resolved_yaml"])
    iteration = iteration_metrics(_read_csv(artifacts["cycle_timing_csv"]))
    resources = resource_metrics(_read_csv(artifacts["resources_csv"]))
    communication = communication_metrics(_read_csv(artifacts["communication_csv"]))
    stages = stage_metrics(_read_csv(artifacts["run_all_timing_csv"]))
    simulation_status = _simulation_status(_read_csv(artifacts["events_csv"]))
    writer_quality = analyze_metric_writer_stats(
        artifacts["writer_stats_dir"],
        required_writers=required_metric_writers(config),
    )
    metrics_cfg = config.get("metrics", {}) or {}
    quality_enforced = isinstance(metrics_cfg, Mapping) and bool(metrics_cfg.get("enabled", False))
    writer_quality["quality_enforced"] = quality_enforced
    writer_quality["observed_quality_complete"] = writer_quality["quality_complete"]
    if not quality_enforced:
        writer_quality["quality_complete"] = True
    quality_complete = bool(writer_quality["quality_complete"])
    run_status = simulation_status or "incomplete"
    if not quality_complete and run_status in {"incomplete", "success"}:
        run_status = "cleanup_error"
    complete = run_status == "success" and quality_complete

    configured_iterations = _positive_int(manifest.get("iterations"))
    measured_iterations = int(iteration["count"])
    rtf_iterations = configured_iterations or measured_iterations or None
    step = _finite_float(hydraulic_step_sec)
    if step is None:
        step = _finite_float(manifest.get("hydraulic_step_sec"))
    if step is not None and step <= 0:
        step = None

    wall_runtime = stages["simulation_runtime_sec"]
    runtime_source = None
    if wall_runtime is not None and wall_runtime > 0:
        runtime_source = f"stage:{stages['simulation_stage']}"
    else:
        wall_runtime = iteration["total_sec"]
        if wall_runtime is not None and wall_runtime > 0:
            runtime_source = "cycle_total_sum"
    real_time_factor = (
        None
        if rtf_iterations is None or step is None or wall_runtime is None or wall_runtime <= 0
        else rtf_iterations * step / wall_runtime
    )

    log_denominator = measured_iterations or configured_iterations or 0
    logs = log_volume_metrics(root, log_denominator, log_roots=log_roots)
    inputs = {
        key: str(value) if value is not None else None
        for key, value in artifacts.items()
    }
    return {
        "schema_version": 1,
        "metric_type": "performance",
        "run_status": run_status,
        "complete": complete,
        "quality_complete": quality_complete,
        "experiment_id": manifest.get("experiment_id"),
        "group": manifest.get("group"),
        "parameter": manifest.get("parameter"),
        "parameter_value": manifest.get("parameter_value"),
        "repetition": manifest.get("repetition"),
        "inputs": {"run_path": str(root), **inputs},
        "availability": {key: value is not None for key, value in artifacts.items()},
        "settings": {
            "configured_iterations": configured_iterations,
            "measured_iterations": measured_iterations,
            "hydraulic_step_sec": step,
            "percentile_method": "linear_type_7",
            "rtt_population": "successful_requests",
        },
        "iteration_time": iteration,
        "runtime": {
            "wall_clock_sec": stages["run_all_total_sec"],
            "simulation_wall_clock_sec": wall_runtime,
            "simulation_wall_clock_source": runtime_source,
            "simulated_time_sec": None if rtf_iterations is None or step is None else rtf_iterations * step,
            "real_time_factor": real_time_factor,
        },
        "resources": resources,
        "communication": communication,
        "metric_writers": writer_quality,
        "stages": stages,
        "logs": logs,
    }


def _flatten_scalars(
    value: Mapping[str, Any],
    *,
    prefix: str = "",
    output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = output if output is not None else {}
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, Mapping):
            _flatten_scalars(item, prefix=name, output=result)
        elif item is None or isinstance(item, (str, int, float, bool)):
            result[name] = item
    return result


def _stage_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "unnamed"


def _summary_csv_row(summary: Mapping[str, Any]) -> dict[str, Any]:
    iteration = summary["iteration_time"]
    runtime = summary["runtime"]
    resources = summary["resources"]
    communication = summary["communication"]
    metric_writers = summary["metric_writers"]
    logs = summary["logs"]
    row: dict[str, Any] = {
        "metric_type": "performance",
        "experiment_id": summary.get("experiment_id"),
        "group": summary.get("group"),
        "parameter": summary.get("parameter"),
        "parameter_value": summary.get("parameter_value"),
        "repetition": summary.get("repetition"),
        "run_status": summary.get("run_status"),
        "complete": summary.get("complete"),
        "quality_complete": summary.get("quality_complete"),
        "runtime_sec": runtime["wall_clock_sec"],
        "simulation_runtime_sec": runtime["simulation_wall_clock_sec"],
        "real_time_factor": runtime["real_time_factor"],
        "iteration_count": iteration["count"],
        "total_iteration_sec": iteration["total_sec"],
        "mean_iteration_sec": iteration["mean_sec"],
        "std_iteration_sec": iteration["std_sec"],
        "p95_iteration_sec": iteration["p95_sec"],
        "mean_iteration_ms": iteration["mean_ms"],
        "std_iteration_ms": iteration["std_ms"],
        "p95_iteration_ms": iteration["p95_ms"],
        "peak_aggregate_rss_bytes": resources["peak_aggregate_rss_bytes"],
        "peak_aggregate_rss_mb": resources["peak_aggregate_rss_mb"],
        "mean_aggregate_cpu_percent": resources["mean_aggregate_cpu_percent"],
        "peak_aggregate_cpu_percent": resources["peak_aggregate_cpu_percent"],
        "modbus_requests": communication["request_count"],
        "modbus_warmup_requests": communication["warmup_count"],
        "modbus_connection_attempts": communication["connection_count"],
        "modbus_connection_errors": communication["connection_error_count"],
        "modbus_success_count": communication["success_count"],
        "modbus_timeout_count": communication["timeout_count"],
        "modbus_success_rate": communication["success_rate"],
        "modbus_timeout_rate": communication["timeout_rate"],
        "modbus_rtt_mean_ms": communication["mean_rtt_ms"],
        "modbus_rtt_p95_ms": communication["p95_rtt_ms"],
        "metric_writer_file_count": metric_writers["file_count"],
        "metric_writer_accepted": metric_writers["accepted"],
        "metric_writer_processed": metric_writers["processed"],
        "metric_writer_written": metric_writers["written"],
        "metric_writer_dropped": metric_writers["dropped_total"],
        "metric_writer_write_errors": metric_writers["write_errors"],
        "metric_writer_unflushed": metric_writers["unflushed_on_close"],
        "metric_writer_pending": metric_writers["pending"],
        "metric_writer_thread_alive_count": metric_writers["thread_alive_count"],
        "log_bytes": logs["total_bytes"],
        "log_bytes_per_iteration": logs["bytes_per_iteration"],
    }
    used_slugs: dict[str, int] = {}
    for name, values in summary["stages"]["by_name"].items():
        base = _stage_slug(str(name))
        used_slugs[base] = used_slugs.get(base, 0) + 1
        slug = base if used_slugs[base] == 1 else f"{base}_{used_slugs[base]}"
        row[f"stage_{slug}_sec"] = values["total_sec"]
        row[f"stage_{slug}_status"] = values["last_status"]
    return row


def write_performance_outputs(
    summary: Mapping[str, Any], output_dir: Path | str
) -> dict[str, Path]:
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "performance_summary.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)

    csv_path = output / "performance_summary.csv"
    row = _summary_csv_row(summary)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow({key: "" if value is None else value for key, value in row.items()})
    return {"json": json_path, "summary_csv": csv_path}


__all__ = [
    "analyze_performance",
    "communication_metrics",
    "iteration_metrics",
    "log_volume_metrics",
    "percentile",
    "resolve_performance_artifacts",
    "resource_metrics",
    "stage_metrics",
    "write_performance_outputs",
]
