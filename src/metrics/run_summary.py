#!/usr/bin/env python3
"""Build one stable, traceable metric row for a single experiment run.

The individual analyzers intentionally own their detailed JSON schemas.  This
module selects a small set of paper-facing scalar metrics from those schemas
and places them in one fixed-width row.  Missing sources remain ``None`` (and
therefore blank in CSV); they are never converted to synthetic zeroes.

Source precedence is explicit:

* manifest metadata wins over copies embedded in performance/network output;
* performance owns lifecycle, runtime, resource, and Modbus measurements;
* the network aggregate prefers ``link_trace`` over FlowMonitor because real
  TapBridge traffic may not be observed by an ns-3 FlowProbe;
* correctness and propagation own their respective offline metrics.

Any lower-priority value that disagrees with the selected value is retained in
``conflicts`` so a consolidated row never hides provenance problems.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SOURCE_NAMES = ("manifest", "performance", "network", "correctness", "propagation")

# Paths are ordered from the canonical runtime/report layout to compatibility
# locations used by standalone analyzers and older runs.
SOURCE_LAYOUTS: dict[str, tuple[str, ...]] = {
    "manifest": (
        "runtime/manifest.json",
        "reports/manifest.json",
        "manifest.json",
    ),
    "performance": (
        "reports/metrics/performance_summary.json",
        "metrics/performance_summary.json",
        "runtime/metrics/performance_summary.json",
        "runtime/performance_summary.json",
        "performance_summary.json",
    ),
    "network": (
        "runtime/network/network-aggregate.json",
        "reports/network/network-aggregate.json",
        "reports/metrics/network-aggregate.json",
        "metrics/network-aggregate.json",
        "network/network-aggregate.json",
        "network-aggregate.json",
    ),
    "correctness": (
        "reports/metrics/correctness_summary.json",
        "metrics/correctness_summary.json",
        "runtime/metrics/correctness_summary.json",
        "runtime/correctness_summary.json",
        "correctness_summary.json",
    ),
    "propagation": (
        "reports/metrics/propagation_summary.json",
        "metrics/propagation_summary.json",
        "runtime/metrics/propagation_summary.json",
        "runtime/propagation_summary.json",
        "propagation_summary.json",
    ),
}

SOURCE_FILENAMES = {
    source: layouts[-1].split("/")[-1] for source, layouts in SOURCE_LAYOUTS.items()
}

# This tuple is also the CSV contract.  Additions must be deliberate: every run
# emits every column, with unavailable values represented by None/blank.
SUMMARY_COLUMNS = (
    "schema_version",
    "metric_type",
    "experiment_id",
    "group",
    "parameter",
    "parameter_value",
    "repetition",
    "timestamp",
    "random_seed",
    "configured_iterations",
    "hydraulic_step_sec",
    "delay_ms",
    "loss_rate",
    "bot_count",
    "config_file",
    "config_sha256",
    "git_commit",
    "git_branch",
    "git_dirty",
    "run_status",
    "complete",
    "quality_complete",
    "runtime_sec",
    "simulation_runtime_sec",
    "simulated_time_sec",
    "real_time_factor",
    "iteration_count",
    "mean_iteration_ms",
    "p95_iteration_ms",
    "peak_rss_mb",
    "mean_cpu_percent",
    "peak_cpu_percent",
    "modbus_requests",
    "modbus_success_count",
    "modbus_timeout_count",
    "modbus_success_rate",
    "modbus_timeout_rate",
    "modbus_rtt_ms",
    "modbus_rtt_p95_ms",
    "network_status",
    "network_row_count",
    "network_metric_source",
    "network_tx_packets",
    "network_rx_packets",
    "network_lost_packets",
    "network_drop_packets",
    "network_mean_delay_ms",
    "network_loss_rate",
    "network_throughput_bps",
    "network_mean_abs_delay_error_ms",
    "network_max_abs_delay_error_ms",
    "network_mean_loss_error",
    "network_max_loss_error",
    "correctness_physical_iterations",
    "correctness_control_iterations",
    "correctness_physical_complete_alignment",
    "correctness_control_complete_alignment",
    "physical_variable_count",
    "physical_comparable_value_count",
    "physical_pooled_rmse",
    "physical_rmse_mean",
    "physical_max_deviation",
    "actuator_count",
    "actuator_comparable_state_count",
    "actuator_mismatch_count",
    "actuator_mismatch_rate",
    "actuator_switch_match_rate",
    "actuator_switch_exact_match_rate",
    "actuator_switch_mean_error_iterations",
    "actuator_switch_max_error_iterations",
    "attack_scenario",
    "attack_start_iteration",
    "communication_anomaly_iteration",
    "control_deviation_iteration",
    "physical_deviation_iteration",
    "attack_end_iteration",
    "attack_to_communication_iterations",
    "attack_to_comm_ms",
    "communication_to_control_iterations",
    "control_to_physics_iterations",
    "attack_to_physical_iterations",
    "propagation_mean_rmse",
    "propagation_peak_abs_deviation",
    "propagation_auc_abs_deviation",
    "recovery_status",
    "not_recovered",
    "recovery_iteration",
    "recovery_iterations",
    "recovery_hydraulic_time_sec",
    "manifest_available",
    "performance_available",
    "network_available",
    "correctness_available",
    "propagation_available",
    "conflict_count",
    "unavailable_field_count",
    "source_paths",
    "source_errors",
    "conflicts",
    "unavailable_fields",
)

_DIAGNOSTIC_COLUMNS = {
    "source_paths",
    "source_errors",
    "conflicts",
    "unavailable_fields",
}
_NON_METRIC_COLUMNS = {
    "schema_version",
    "metric_type",
    "manifest_available",
    "performance_available",
    "network_available",
    "correctness_available",
    "propagation_available",
    "conflict_count",
    "unavailable_field_count",
    *_DIAGNOSTIC_COLUMNS,
}
_MISSING = object()


def _output_root(path: Path) -> Path:
    """Normalize an output, runtime, reports, or reports/metrics directory."""

    if path.name == "runtime":
        return path.parent
    if path.name == "reports":
        return path.parent
    if path.name == "metrics" and path.parent.name == "reports":
        return path.parent.parent
    return path


def discover_run_summary_artifacts(run_path: Path | str) -> dict[str, list[Path]]:
    """Return prioritized candidate JSON files for one run directory."""

    requested = Path(run_path).expanduser().resolve()
    if not requested.is_dir():
        raise FileNotFoundError(requested)
    root = _output_root(requested)
    discovered: dict[str, list[Path]] = {}
    for source in SOURCE_NAMES:
        candidates: list[Path] = []
        for relative in SOURCE_LAYOUTS[source]:
            candidate = (root / relative).resolve()
            if candidate.is_file() and candidate not in candidates:
                candidates.append(candidate)
        direct = (requested / SOURCE_FILENAMES[source]).resolve()
        if direct.is_file() and direct not in candidates:
            candidates.append(direct)
        # A custom run layout remains supported, but canonical locations above
        # always win.  Sorting makes fallback selection deterministic.
        if root.is_dir():
            for candidate in sorted(root.rglob(SOURCE_FILENAMES[source])):
                resolved = candidate.resolve()
                if resolved.is_file() and resolved not in candidates:
                    candidates.append(resolved)
        discovered[source] = candidates
    return discovered


def _read_first_valid_json(
    candidates: Sequence[Path],
) -> tuple[dict[str, Any] | None, Path | None, list[str]]:
    errors: list[str] = []
    if not candidates:
        return None, None, ["not_found"]
    for path in candidates:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        if not isinstance(loaded, Mapping):
            errors.append(f"{path}: root_is_not_object")
            continue
        return dict(loaded), path, errors
    return None, None, errors


def _nested(data: Mapping[str, Any] | None, path: str) -> Any:
    value: Any = data
    for key in path.split("."):
        if not isinstance(value, Mapping) or key not in value:
            return _MISSING
        value = value[key]
    return value


def _clean_scalar(value: Any) -> Any:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _number(value: Any) -> int | float | None:
    value = _clean_scalar(value)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _boolean(value: Any) -> bool | None:
    value = _clean_scalar(value)
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    return None


def _status(value: Any) -> str | None:
    value = _clean_scalar(value)
    return str(value).lower() if value is not None else None


def _equivalent(left: Any, right: Any) -> bool:
    if type(left) is type(right):
        return left == right
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    left_number = _number(left)
    right_number = _number(right)
    if left_number is not None and right_number is not None:
        return float(left_number) == float(right_number)
    return str(left).strip() == str(right).strip()


def _milliseconds(value: Any) -> int | float | None:
    number = _number(value)
    if number is None:
        return None
    milliseconds = float(number) * 1000.0
    return int(milliseconds) if milliseconds.is_integer() else milliseconds


Candidate = tuple[str, str]


def _choose(
    field: str,
    sources: Mapping[str, Mapping[str, Any] | None],
    candidates: Sequence[Candidate],
    conflicts: list[dict[str, Any]],
    transform: Callable[[Any], Any] = _clean_scalar,
) -> Any:
    available: list[tuple[str, Any]] = []
    for source, path in candidates:
        raw = _nested(sources.get(source), path)
        value = transform(raw)
        if value is not None:
            available.append((source, value))
    if not available:
        return None
    selected_source, selected_value = available[0]
    for conflicting_source, conflicting_value in available[1:]:
        if not _equivalent(selected_value, conflicting_value):
            conflicts.append(
                {
                    "field": field,
                    "selected_source": selected_source,
                    "selected_value": selected_value,
                    "conflicting_source": conflicting_source,
                    "conflicting_value": conflicting_value,
                }
            )
    return selected_value


def _network_measurement(
    network: Mapping[str, Any] | None,
) -> tuple[str | None, Mapping[str, Any] | None]:
    by_source = _nested(network, "by_source")
    if not isinstance(by_source, Mapping):
        return None, None
    for name in ("link_trace", "flow_monitor"):
        value = by_source.get(name)
        if isinstance(value, Mapping):
            return name, value
    for name in sorted(str(key) for key in by_source):
        value = by_source.get(name)
        if isinstance(value, Mapping):
            return name, value
    return None, None


def _derived_experiment_values(summary: dict[str, Any]) -> None:
    parameter = str(summary.get("parameter") or "").strip().lower()
    parameter = re.sub(r"[^a-z0-9]+", "_", parameter).strip("_")
    value = summary.get("parameter_value")
    if parameter in {"delay", "delay_ms", "network_delay", "latency_ms"}:
        summary["delay_ms"] = _number(value)
    elif parameter in {"loss", "loss_rate", "packet_loss", "packet_loss_rate"}:
        summary["loss_rate"] = _number(value)
    elif parameter in {"bot_count", "bots", "dos_bots", "dos_bot_count"}:
        summary["bot_count"] = _number(value)


def build_run_summary(run_path: Path | str) -> dict[str, Any]:
    """Merge available per-run analyzer outputs into one fixed-schema row."""

    candidates = discover_run_summary_artifacts(run_path)
    sources: dict[str, dict[str, Any] | None] = {}
    selected_paths: dict[str, str | None] = {}
    source_errors: dict[str, list[str]] = {}
    for source in SOURCE_NAMES:
        data, selected, errors = _read_first_valid_json(candidates[source])
        sources[source] = data
        selected_paths[source] = str(selected) if selected is not None else None
        source_errors[source] = errors

    summary: dict[str, Any] = {column: None for column in SUMMARY_COLUMNS}
    summary["schema_version"] = 1
    summary["metric_type"] = "run_summary"
    conflicts: list[dict[str, Any]] = []

    def select(
        field: str,
        choices: Sequence[Candidate],
        transform: Callable[[Any], Any] = _clean_scalar,
    ) -> None:
        summary[field] = _choose(field, sources, choices, conflicts, transform)

    metadata = {
        "experiment_id": (
            ("manifest", "experiment_id"),
            ("performance", "experiment_id"),
            ("network", "experiment_id"),
        ),
        "group": (
            ("manifest", "group"),
            ("performance", "group"),
            ("network", "group"),
        ),
        "parameter": (
            ("manifest", "parameter"),
            ("performance", "parameter"),
            ("network", "parameter"),
        ),
        "parameter_value": (
            ("manifest", "parameter_value"),
            ("performance", "parameter_value"),
            ("network", "parameter_value"),
        ),
        "repetition": (
            ("manifest", "repetition"),
            ("performance", "repetition"),
            ("network", "repetition"),
        ),
    }
    for field, choices in metadata.items():
        select(field, choices)

    for field, path, transform in (
        ("timestamp", "timestamp", _clean_scalar),
        ("random_seed", "random_seed", _clean_scalar),
        ("configured_iterations", "iterations", _number),
        ("hydraulic_step_sec", "hydraulic_step_sec", _number),
        ("config_file", "config_file", _clean_scalar),
        ("config_sha256", "config_sha256", _clean_scalar),
        ("git_commit", "git.commit", _clean_scalar),
        ("git_branch", "git.branch", _clean_scalar),
        ("git_dirty", "git.dirty", _boolean),
    ):
        select(field, (("manifest", path),), transform)

    select(
        "run_status",
        (("performance", "run_status"), ("network", "run_status")),
        _status,
    )
    select(
        "complete",
        (("performance", "complete"), ("network", "complete")),
        _boolean,
    )
    select("quality_complete", (("performance", "quality_complete"),), _boolean)

    performance_fields: dict[str, tuple[str, Callable[[Any], Any]]] = {
        "runtime_sec": ("runtime.wall_clock_sec", _number),
        "simulation_runtime_sec": ("runtime.simulation_wall_clock_sec", _number),
        "simulated_time_sec": ("runtime.simulated_time_sec", _number),
        "real_time_factor": ("runtime.real_time_factor", _number),
        "iteration_count": ("iteration_time.count", _number),
        "mean_iteration_ms": ("iteration_time.mean_ms", _number),
        "p95_iteration_ms": ("iteration_time.p95_ms", _number),
        "peak_rss_mb": ("resources.peak_aggregate_rss_mb", _number),
        "mean_cpu_percent": ("resources.mean_aggregate_cpu_percent", _number),
        "peak_cpu_percent": ("resources.peak_aggregate_cpu_percent", _number),
        "modbus_requests": ("communication.request_count", _number),
        "modbus_success_count": ("communication.success_count", _number),
        "modbus_timeout_count": ("communication.timeout_count", _number),
        "modbus_success_rate": ("communication.success_rate", _number),
        "modbus_timeout_rate": ("communication.timeout_rate", _number),
        "modbus_rtt_ms": ("communication.mean_rtt_ms", _number),
        "modbus_rtt_p95_ms": ("communication.p95_rtt_ms", _number),
    }
    for field, (path, transform) in performance_fields.items():
        select(field, (("performance", path),), transform)

    network = sources.get("network")
    summary["network_metric_source"], selected_network = _network_measurement(network)
    summary["network_status"] = _status(_nested(network, "status"))
    summary["network_row_count"] = _number(_nested(network, "row_count"))
    network_fields = {
        "network_tx_packets": "tx_packets",
        "network_rx_packets": "rx_packets",
        "network_lost_packets": "lost_packets",
        "network_drop_packets": "drop_packets",
        "network_mean_delay_ms": "mean_delay_ms",
        "network_loss_rate": "packet_loss_rate",
        "network_throughput_bps": "throughput_bps_sum",
        "network_mean_abs_delay_error_ms": "mean_abs_delay_error_ms",
        "network_max_abs_delay_error_ms": "max_abs_delay_error_ms",
        "network_mean_loss_error": "mean_loss_error",
        "network_max_loss_error": "max_loss_error",
    }
    for field, path in network_fields.items():
        summary[field] = _number(_nested(selected_network, path))

    correctness_fields = {
        "correctness_physical_iterations": "alignment.physical.iterations_compared",
        "correctness_control_iterations": "alignment.control.iterations_compared",
        "correctness_physical_complete_alignment": "alignment.physical.complete_alignment",
        "correctness_control_complete_alignment": "alignment.control.complete_alignment",
        "physical_variable_count": "physical.overall.variable_count",
        "physical_comparable_value_count": "physical.overall.comparable_value_count",
        "physical_pooled_rmse": "physical.overall.pooled_rmse",
        "physical_rmse_mean": "physical.overall.mean_variable_rmse",
        "physical_max_deviation": "physical.overall.max_abs_error",
        "actuator_count": "control.overall.actuator_count",
        "actuator_comparable_state_count": "control.overall.comparable_state_count",
        "actuator_mismatch_count": "control.overall.mismatch_count",
        "actuator_mismatch_rate": "control.overall.mismatch_rate",
        "actuator_switch_match_rate": "control.overall.switch_match_rate",
        "actuator_switch_exact_match_rate": "control.overall.switch_exact_match_rate",
        "actuator_switch_mean_error_iterations": "control.overall.mean_actuator_switch_error_iterations",
        "actuator_switch_max_error_iterations": "control.overall.max_switch_error_iterations",
    }
    boolean_correctness = {
        "correctness_physical_complete_alignment",
        "correctness_control_complete_alignment",
    }
    for field, path in correctness_fields.items():
        select(
            field,
            (("correctness", path),),
            _boolean if field in boolean_correctness else _number,
        )

    select("attack_scenario", (("propagation", "scenario"),))
    propagation_fields: dict[str, tuple[str, Callable[[Any], Any]]] = {
        "attack_start_iteration": ("timeline.tA_attack.iteration", _number),
        "communication_anomaly_iteration": ("timeline.tC_communication.iteration", _number),
        "control_deviation_iteration": ("timeline.tU_control.iteration", _number),
        "physical_deviation_iteration": ("timeline.tP_physical.iteration", _number),
        "attack_end_iteration": ("timeline.tAttackEnd.iteration", _number),
        "attack_to_communication_iterations": ("delays.attack_to_communication.iterations", _number),
        "attack_to_comm_ms": ("delays.attack_to_communication.wall_clock_sec", _milliseconds),
        "communication_to_control_iterations": ("delays.communication_to_control.iterations", _number),
        "control_to_physics_iterations": ("delays.control_to_physical.iterations", _number),
        "attack_to_physical_iterations": ("delays.attack_to_physical.iterations", _number),
        "propagation_mean_rmse": ("physical.overall.mean_rmse", _number),
        "propagation_peak_abs_deviation": ("physical.overall.peak_abs_deviation", _number),
        "propagation_auc_abs_deviation": ("physical.overall.auc_abs_deviation", _number),
        "recovery_status": ("recovery.status", _clean_scalar),
        "not_recovered": ("recovery.not_recovered", _boolean),
        "recovery_iteration": ("recovery.recovery_iteration", _number),
        "recovery_iterations": ("recovery.recovery_iterations", _number),
        "recovery_hydraulic_time_sec": ("recovery.hydraulic_time_sec", _number),
    }
    for field, (path, transform) in propagation_fields.items():
        select(field, (("propagation", path),), transform)

    _derived_experiment_values(summary)
    for source in SOURCE_NAMES:
        summary[f"{source}_available"] = sources[source] is not None
    summary["source_paths"] = selected_paths
    summary["source_errors"] = source_errors
    summary["conflicts"] = conflicts
    unavailable = [
        field
        for field in SUMMARY_COLUMNS
        if field not in _NON_METRIC_COLUMNS and summary.get(field) is None
    ]
    summary["unavailable_fields"] = unavailable
    summary["conflict_count"] = len(conflicts)
    summary["unavailable_field_count"] = len(unavailable)
    return summary


def _csv_value(field: str, value: Any) -> Any:
    if value is None:
        return ""
    if field in _DIAGNOSTIC_COLUMNS:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return value


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_run_summary(
    summary: Mapping[str, Any], output_dir: Path | str
) -> dict[str, Path]:
    """Write ``summary_metrics.json`` and a single-row stable-schema CSV."""

    output = Path(output_dir).expanduser().resolve()
    unknown = sorted(set(summary) - set(SUMMARY_COLUMNS))
    if unknown:
        raise ValueError(f"run summary contains fields outside the stable schema: {unknown}")
    row = {column: summary.get(column) for column in SUMMARY_COLUMNS}

    json_path = output / "summary_metrics.json"
    json_content = json.dumps(
        row,
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
        allow_nan=False,
    ) + "\n"
    _atomic_write(json_path, json_content)

    csv_path = output / "summary_metrics.csv"
    # StringIO avoids exposing a header-only/partial CSV if the process stops.
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(SUMMARY_COLUMNS))
    writer.writeheader()
    writer.writerow(
        {column: _csv_value(column, row.get(column)) for column in SUMMARY_COLUMNS}
    )
    _atomic_write(csv_path, buffer.getvalue())
    return {"json": json_path, "csv": csv_path}


__all__ = [
    "SOURCE_LAYOUTS",
    "SOURCE_NAMES",
    "SUMMARY_COLUMNS",
    "build_run_summary",
    "discover_run_summary_artifacts",
    "write_run_summary",
]
