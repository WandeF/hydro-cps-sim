#!/usr/bin/env python3
"""Offline closed-loop correctness metrics.

The runtime already exports stable ``physics.csv`` and ``actuator_state.csv``
files.  This module deliberately consumes those files after a run instead of
adding instrumentation to the time-sensitive closed loop.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable, Mapping, Sequence


PHYSICS_CSV = "physics.csv"
ACTUATOR_CSV = "actuator_state.csv"
METADATA_COLUMNS = {"iteration", "timestamp", "timestamp_epoch", "time"}


def resolve_csv_artifact(path: Path | str, filename: str) -> Path:
    """Resolve a CSV passed directly or through a run/output/report directory."""

    root = Path(path).expanduser()
    if root.is_file():
        return root.resolve()

    candidates = (
        root / filename,
        root / "csv" / filename,
        root / "reports" / "csv" / filename,
        root / "runtime" / "csv" / filename,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    checked = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"{filename} not found from {root}; checked: {checked}")


def resolve_optional_csv_artifact(path: Path | str | None, filename: str) -> Path | None:
    if path is None:
        return None
    try:
        return resolve_csv_artifact(path, filename)
    except FileNotFoundError:
        return None


def read_iteration_rows(path: Path | str) -> dict[int, dict[str, str]]:
    """Read a CSV into an iteration-indexed mapping and reject duplicate rows."""

    csv_path = Path(path)
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "iteration" not in reader.fieldnames:
            raise ValueError(f"CSV has no iteration column: {csv_path}")
        rows: dict[int, dict[str, str]] = {}
        for line_number, row in enumerate(reader, start=2):
            raw = str(row.get("iteration", "")).strip()
            if not raw:
                continue
            try:
                iteration = int(float(raw))
            except ValueError as exc:
                raise ValueError(
                    f"invalid iteration {raw!r} at {csv_path}:{line_number}"
                ) from exc
            if iteration in rows:
                raise ValueError(f"duplicate iteration {iteration} in {csv_path}")
            rows[iteration] = dict(row)
    return rows


def as_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "open", "opened", "on", "yes"}:
        return True
    if text in {"0", "false", "closed", "close", "off", "no"}:
        return False
    try:
        number = float(text)
    except ValueError:
        return None
    if number == 1.0:
        return True
    if number == 0.0:
        return False
    return None


def aligned_iterations(
    reference: Mapping[int, Mapping[str, Any]],
    candidate: Mapping[int, Mapping[str, Any]],
    *,
    exclude_iterations: Iterable[int] = (0,),
) -> tuple[list[int], dict[str, Any]]:
    excluded = {int(value) for value in exclude_iterations}
    reference_ids = set(reference) - excluded
    candidate_ids = set(candidate) - excluded
    common = sorted(reference_ids & candidate_ids)
    if not common:
        raise ValueError("reference and candidate have no aligned iterations")
    return common, {
        "iterations_compared": len(common),
        "first_iteration": common[0],
        "last_iteration": common[-1],
        "excluded_iterations": sorted(excluded),
        "reference_only_iterations": sorted(reference_ids - candidate_ids),
        "candidate_only_iterations": sorted(candidate_ids - reference_ids),
        "complete_alignment": reference_ids == candidate_ids,
    }


def common_columns(
    reference: Mapping[int, Mapping[str, Any]],
    candidate: Mapping[int, Mapping[str, Any]],
    requested: Sequence[str] | None = None,
) -> list[str]:
    reference_columns = {key for row in reference.values() for key in row}
    candidate_columns = {key for row in candidate.values() for key in row}
    if requested is not None:
        columns = [str(column) for column in requested]
        missing = [
            column
            for column in columns
            if column not in reference_columns or column not in candidate_columns
        ]
        if missing:
            raise ValueError(f"columns missing from reference or candidate: {', '.join(missing)}")
        return columns
    return sorted((reference_columns & candidate_columns) - METADATA_COLUMNS)


def numeric_error_metrics(
    reference: Mapping[int, Mapping[str, Any]],
    candidate: Mapping[int, Mapping[str, Any]],
    iterations: Sequence[int],
    variable: str,
) -> dict[str, Any]:
    differences: list[float] = []
    for iteration in iterations:
        reference_value = as_float(reference[iteration].get(variable))
        candidate_value = as_float(candidate[iteration].get(variable))
        if reference_value is None or candidate_value is None:
            continue
        differences.append(candidate_value - reference_value)

    if not differences:
        return {
            "variable": variable,
            "count": 0,
            "rmse": None,
            "mae": None,
            "max_abs_error": None,
            "mean_error": None,
            "sum_squared_error": 0.0,
        }

    squared_sum = math.fsum(value * value for value in differences)
    absolute = [abs(value) for value in differences]
    return {
        "variable": variable,
        "count": len(differences),
        "rmse": math.sqrt(squared_sum / len(differences)),
        "mae": fmean(absolute),
        "max_abs_error": max(absolute),
        "mean_error": fmean(differences),
        "sum_squared_error": squared_sum,
    }


def state_switch_events(
    rows: Mapping[int, Mapping[str, Any]],
    actuator: str,
    *,
    exclude_iterations: Iterable[int] = (0,),
) -> list[tuple[int, bool]]:
    excluded = {int(value) for value in exclude_iterations}
    events: list[tuple[int, bool]] = []
    previous: bool | None = None
    for iteration in sorted(set(rows) - excluded):
        state = as_bool(rows[iteration].get(actuator))
        if state is None:
            continue
        if previous is not None and state != previous:
            events.append((iteration, state))
        previous = state
    return events


def match_switch_events(
    reference_events: Sequence[tuple[int, bool]],
    candidate_events: Sequence[tuple[int, bool]],
) -> dict[str, Any]:
    """Pair same-direction switch events in chronological order.

    Pairing by the new Boolean state avoids matching an ``on`` transition with
    an ``off`` transition when one trajectory contains an extra event.
    """

    pairs: list[tuple[int, int, bool]] = []
    for state in (False, True):
        reference_state_events = [iteration for iteration, value in reference_events if value is state]
        candidate_state_events = [iteration for iteration, value in candidate_events if value is state]
        pairs.extend(
            (reference_iteration, candidate_iteration, state)
            for reference_iteration, candidate_iteration in zip(
                reference_state_events, candidate_state_events
            )
        )
    pairs.sort(key=lambda item: (item[0], item[1]))
    errors = [abs(candidate_iteration - reference_iteration) for reference_iteration, candidate_iteration, _ in pairs]
    denominator = max(len(reference_events), len(candidate_events))
    exact = sum(error == 0 for error in errors)
    return {
        "reference_switch_count": len(reference_events),
        "candidate_switch_count": len(candidate_events),
        "matched_switch_count": len(pairs),
        "unmatched_reference_switch_count": len(reference_events) - len(pairs),
        "unmatched_candidate_switch_count": len(candidate_events) - len(pairs),
        "switch_match_rate": 1.0 if denominator == 0 else len(pairs) / denominator,
        "switch_exact_match_rate": 1.0 if denominator == 0 else exact / denominator,
        "switch_mean_abs_error_iterations": fmean(errors) if errors else None,
        "switch_max_abs_error_iterations": max(errors) if errors else None,
    }


def actuator_metrics(
    reference: Mapping[int, Mapping[str, Any]],
    candidate: Mapping[int, Mapping[str, Any]],
    iterations: Sequence[int],
    actuator: str,
    *,
    exclude_iterations: Iterable[int] = (0,),
) -> dict[str, Any]:
    compared = 0
    mismatches = 0
    for iteration in iterations:
        reference_state = as_bool(reference[iteration].get(actuator))
        candidate_state = as_bool(candidate[iteration].get(actuator))
        if reference_state is None or candidate_state is None:
            continue
        compared += 1
        mismatches += int(reference_state != candidate_state)

    switches = match_switch_events(
        state_switch_events(reference, actuator, exclude_iterations=exclude_iterations),
        state_switch_events(candidate, actuator, exclude_iterations=exclude_iterations),
    )
    return {
        "actuator": actuator,
        "count": compared,
        "mismatch_count": mismatches,
        "mismatch_rate": None if compared == 0 else mismatches / compared,
        **switches,
    }


def analyze_correctness(
    baseline_physics: Path | str,
    platform_physics: Path | str,
    baseline_actuators: Path | str,
    platform_actuators: Path | str,
    *,
    variables: Sequence[str] | None = None,
    actuators: Sequence[str] | None = None,
    exclude_iterations: Iterable[int] = (0,),
) -> dict[str, Any]:
    baseline_physics_path = resolve_csv_artifact(baseline_physics, PHYSICS_CSV)
    platform_physics_path = resolve_csv_artifact(platform_physics, PHYSICS_CSV)
    baseline_actuator_path = resolve_csv_artifact(baseline_actuators, ACTUATOR_CSV)
    platform_actuator_path = resolve_csv_artifact(platform_actuators, ACTUATOR_CSV)

    baseline_physics_rows = read_iteration_rows(baseline_physics_path)
    platform_physics_rows = read_iteration_rows(platform_physics_path)
    physics_iterations, physics_alignment = aligned_iterations(
        baseline_physics_rows,
        platform_physics_rows,
        exclude_iterations=exclude_iterations,
    )
    selected_variables = common_columns(
        baseline_physics_rows, platform_physics_rows, variables
    )
    physical_rows = [
        numeric_error_metrics(
            baseline_physics_rows,
            platform_physics_rows,
            physics_iterations,
            variable,
        )
        for variable in selected_variables
    ]
    physical_rows = [row for row in physical_rows if row["count"] > 0]
    physical_count = sum(int(row["count"]) for row in physical_rows)
    physical_squared_sum = math.fsum(float(row["sum_squared_error"]) for row in physical_rows)
    physical_rmse_values = [float(row["rmse"]) for row in physical_rows if row["rmse"] is not None]

    baseline_actuator_rows = read_iteration_rows(baseline_actuator_path)
    platform_actuator_rows = read_iteration_rows(platform_actuator_path)
    actuator_iterations, actuator_alignment = aligned_iterations(
        baseline_actuator_rows,
        platform_actuator_rows,
        exclude_iterations=exclude_iterations,
    )
    selected_actuators = common_columns(
        baseline_actuator_rows, platform_actuator_rows, actuators
    )
    control_rows = [
        actuator_metrics(
            baseline_actuator_rows,
            platform_actuator_rows,
            actuator_iterations,
            actuator,
            exclude_iterations=exclude_iterations,
        )
        for actuator in selected_actuators
    ]
    control_rows = [row for row in control_rows if row["count"] > 0]
    control_count = sum(int(row["count"]) for row in control_rows)
    control_mismatches = sum(int(row["mismatch_count"]) for row in control_rows)
    switch_denominator = sum(
        max(int(row["reference_switch_count"]), int(row["candidate_switch_count"]))
        for row in control_rows
    )
    switch_matches = sum(int(row["matched_switch_count"]) for row in control_rows)
    exact_matches = math.fsum(
        float(row["switch_exact_match_rate"])
        * max(int(row["reference_switch_count"]), int(row["candidate_switch_count"]))
        for row in control_rows
    )
    switch_error_total = math.fsum(
        float(row["switch_mean_abs_error_iterations"])
        * int(row["matched_switch_count"])
        for row in control_rows
        if row["switch_mean_abs_error_iterations"] is not None
    )
    switch_max_errors = [
        int(row["switch_max_abs_error_iterations"])
        for row in control_rows
        if row["switch_max_abs_error_iterations"] is not None
    ]

    return {
        "schema_version": 1,
        "metric_type": "correctness",
        "inputs": {
            "baseline_physics_csv": str(baseline_physics_path),
            "platform_physics_csv": str(platform_physics_path),
            "baseline_actuator_csv": str(baseline_actuator_path),
            "platform_actuator_csv": str(platform_actuator_path),
        },
        "alignment": {
            "physical": physics_alignment,
            "control": actuator_alignment,
        },
        "physical": {
            "variables": physical_rows,
            "overall": {
                "variable_count": len(physical_rows),
                "comparable_value_count": physical_count,
                "pooled_rmse": None
                if physical_count == 0
                else math.sqrt(physical_squared_sum / physical_count),
                "mean_variable_rmse": fmean(physical_rmse_values)
                if physical_rmse_values
                else None,
                "max_abs_error": max(
                    (float(row["max_abs_error"]) for row in physical_rows),
                    default=None,
                ),
            },
        },
        "control": {
            "actuators": control_rows,
            "overall": {
                "actuator_count": len(control_rows),
                "comparable_state_count": control_count,
                "mismatch_count": control_mismatches,
                "mismatch_rate": None
                if control_count == 0
                else control_mismatches / control_count,
                "reference_switch_count": sum(
                    int(row["reference_switch_count"]) for row in control_rows
                ),
                "candidate_switch_count": sum(
                    int(row["candidate_switch_count"]) for row in control_rows
                ),
                "matched_switch_count": switch_matches,
                "switch_match_rate": 1.0
                if switch_denominator == 0
                else switch_matches / switch_denominator,
                "switch_exact_match_rate": 1.0
                if switch_denominator == 0
                else exact_matches / switch_denominator,
                "mean_actuator_switch_error_iterations": switch_error_total
                / switch_matches
                if switch_matches
                else None,
                "max_switch_error_iterations": max(switch_max_errors, default=None),
            },
        },
    }


def analyze_correctness_roots(
    baseline: Path | str,
    platform: Path | str,
    **kwargs: Any,
) -> dict[str, Any]:
    return analyze_correctness(
        baseline,
        platform,
        baseline,
        platform,
        **kwargs,
    )


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: "" if row.get(column) is None else row.get(column) for column in columns})


def write_correctness_outputs(summary: Mapping[str, Any], output_dir: Path | str) -> dict[str, Path]:
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "correctness_summary.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)

    physical_path = output / "correctness_physical.csv"
    physical_columns = [
        "variable",
        "count",
        "rmse",
        "mae",
        "max_abs_error",
        "mean_error",
        "sum_squared_error",
    ]
    _write_csv(physical_path, summary["physical"]["variables"], physical_columns)

    actuator_path = output / "correctness_actuators.csv"
    actuator_columns = [
        "actuator",
        "count",
        "mismatch_count",
        "mismatch_rate",
        "reference_switch_count",
        "candidate_switch_count",
        "matched_switch_count",
        "unmatched_reference_switch_count",
        "unmatched_candidate_switch_count",
        "switch_match_rate",
        "switch_exact_match_rate",
        "switch_mean_abs_error_iterations",
        "switch_max_abs_error_iterations",
    ]
    _write_csv(actuator_path, summary["control"]["actuators"], actuator_columns)

    physical_overall = summary["physical"]["overall"]
    control_overall = summary["control"]["overall"]
    physical_alignment = summary["alignment"]["physical"]
    control_alignment = summary["alignment"]["control"]
    summary_row = {
        "metric_type": "correctness",
        "physical_iterations": physical_alignment["iterations_compared"],
        "control_iterations": control_alignment["iterations_compared"],
        **{f"physical_{key}": value for key, value in physical_overall.items()},
        **{f"control_{key}": value for key, value in control_overall.items()},
    }
    summary_csv_path = output / "correctness_summary.csv"
    _write_csv(summary_csv_path, [summary_row], list(summary_row))
    return {
        "json": json_path,
        "summary_csv": summary_csv_path,
        "physical_csv": physical_path,
        "actuator_csv": actuator_path,
    }
