#!/usr/bin/env python3
"""Offline cross-layer attack propagation metrics."""
from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable, Mapping, Sequence

from src.metrics.correctness import (
    ACTUATOR_CSV,
    PHYSICS_CSV,
    _write_csv,
    aligned_iterations,
    as_bool,
    as_float,
    common_columns,
    read_iteration_rows,
    resolve_csv_artifact,
    resolve_optional_csv_artifact,
)


ATTACK_SCHEDULE_CSV = "attack_schedule.csv"
ATTACK_EVENTS_CSV = "attack_events.csv"
SCADA_TIMEOUT_CSV = "scada_timeout_events.csv"

ATTACK_START_EVENTS = {
    "attack_packet_sent",
    "attack_triggered",
    "attack_on",
    "attack_logic_loaded",
    "dos_start",
    "openplc_logic_start",
    "logic_injection_start",
    "attack_start",
}
ATTACK_END_EVENTS = {
    "attack_stopped",
    "attack_off",
    "dos_stop",
    "openplc_logic_restore",
    "logic_injection_restore",
    "attack_stop",
    "stop",
}


def read_csv_rows(path: Path | str | None) -> list[dict[str, str]]:
    if path is None:
        return []
    csv_path = Path(path)
    if not csv_path.is_file():
        return []
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _event_name(row: Mapping[str, Any]) -> str:
    return str(row.get("event") or row.get("action") or "").strip().lower()


def _scenario_name(row: Mapping[str, Any]) -> str:
    return str(row.get("scenario") or row.get("attack") or "").strip()


def _matches_scenario(
    row: Mapping[str, Any],
    scenario: str | None,
    *,
    allow_unlabeled: bool = False,
) -> bool:
    name = _scenario_name(row)
    return (
        scenario is None
        or name == scenario
        or (allow_unlabeled and not name)
    )


def _iteration_or_none(row: Mapping[str, Any]) -> int | None:
    raw = row.get("iteration")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(float(str(raw)))
    except ValueError:
        return None


def _epoch_or_none(row: Mapping[str, Any]) -> float | None:
    return as_float(row.get("timestamp_epoch"))


def _is_modification_event(row: Mapping[str, Any]) -> bool:
    if str(row.get("direction", "")).strip():
        return True
    old_value = row.get("old_value", row.get("original_value"))
    new_value = row.get("new_value", row.get("modified_value"))
    return str(old_value or "").strip() != "" and str(new_value or "").strip() != ""


def _is_attack_entry_event(row: Mapping[str, Any]) -> bool:
    return _event_name(row) in ATTACK_START_EVENTS or _is_modification_event(row)


def _after_start(
    row: Mapping[str, Any],
    start_iteration: int | None,
    start_epoch: float | None,
    *,
    strict_iteration: bool = False,
) -> bool:
    iteration = _iteration_or_none(row)
    epoch = _epoch_or_none(row)
    if start_iteration is not None and iteration is not None:
        if strict_iteration and iteration <= start_iteration:
            return False
        if not strict_iteration and iteration < start_iteration:
            return False
    if start_epoch is not None and epoch is not None and epoch < start_epoch:
        return False
    return True


def _choose_time(
    candidates: Sequence[tuple[Mapping[str, Any], str]],
) -> dict[str, Any]:
    iteration_candidates = [
        (_iteration_or_none(row), source)
        for row, source in candidates
        if _iteration_or_none(row) is not None
    ]
    epoch_candidates = [
        (_epoch_or_none(row), source)
        for row, source in candidates
        if _epoch_or_none(row) is not None
    ]
    iteration_value, iteration_source = (
        min(iteration_candidates, key=lambda item: int(item[0]))
        if iteration_candidates
        else (None, None)
    )
    epoch_value, epoch_source = (
        min(epoch_candidates, key=lambda item: float(item[0]))
        if epoch_candidates
        else (None, None)
    )
    return {
        "iteration": iteration_value,
        "epoch": epoch_value,
        "iteration_source": iteration_source,
        "epoch_source": epoch_source,
    }


def infer_attack_times(
    schedule_rows: Sequence[Mapping[str, Any]],
    attack_event_rows: Sequence[Mapping[str, Any]],
    *,
    scenario: str | None = None,
    start_iteration: int | None = None,
    end_iteration: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    scheduled_start_candidates: list[tuple[Mapping[str, Any], str]] = []
    for row in schedule_rows:
        if _matches_scenario(row, scenario) and _event_name(row) in ATTACK_START_EVENTS:
            scheduled_start_candidates.append((row, f"attack_schedule:{_event_name(row)}"))
    runtime_start_candidates: list[tuple[Mapping[str, Any], str]] = []
    for row in attack_event_rows:
        if _matches_scenario(row, scenario) and _is_attack_entry_event(row):
            label = _event_name(row) or "modification"
            runtime_start_candidates.append((row, f"attack_events:{label}"))

    # Prefer the first observed packet/modification/logic load.  The schedule
    # is a fallback for older runs and must not predate the actual attack entry.
    attack_start = _choose_time(runtime_start_candidates or scheduled_start_candidates)
    if start_iteration is not None:
        attack_start["iteration"] = int(start_iteration)
        attack_start["iteration_source"] = "argument"

    scheduled_end_candidates: list[tuple[Mapping[str, Any], str]] = []
    for row in schedule_rows:
        if not _matches_scenario(row, scenario):
            continue
        if _event_name(row) not in ATTACK_END_EVENTS:
            continue
        if not _after_start(
            row,
            attack_start.get("iteration"),
            attack_start.get("epoch"),
            strict_iteration=True,
        ):
            continue
        scheduled_end_candidates.append((row, f"attack_schedule:{_event_name(row)}"))
    runtime_end_candidates: list[tuple[Mapping[str, Any], str]] = []
    for row in attack_event_rows:
        if not _matches_scenario(row, scenario):
            continue
        if _event_name(row) not in ATTACK_END_EVENTS:
            continue
        if not _after_start(
            row,
            attack_start.get("iteration"),
            attack_start.get("epoch"),
            strict_iteration=True,
        ):
            continue
        runtime_end_candidates.append((row, f"attack_events:{_event_name(row)}"))

    attack_end = _choose_time(runtime_end_candidates or scheduled_end_candidates)
    if end_iteration is not None:
        attack_end["iteration"] = int(end_iteration)
        attack_end["iteration_source"] = "argument"
    return attack_start, attack_end


def infer_communication_anomaly(
    attack_event_rows: Sequence[Mapping[str, Any]],
    timeout_rows: Sequence[Mapping[str, Any]],
    attack_start: Mapping[str, Any],
    *,
    scenario: str | None = None,
) -> dict[str, Any]:
    candidates: list[tuple[Mapping[str, Any], str]] = []
    for row in attack_event_rows:
        if not _matches_scenario(row, scenario) or not _is_modification_event(row):
            continue
        if _after_start(row, attack_start.get("iteration"), attack_start.get("epoch")):
            candidates.append((row, "attack_events:modification"))
    for row in timeout_rows:
        # Existing SCADA timeout telemetry is global and has no scenario field.
        # Attribute an unlabeled timeout by temporal overlap; keep labels strict
        # when a future exporter provides them.
        if not _matches_scenario(row, scenario, allow_unlabeled=True):
            continue
        if _after_start(row, attack_start.get("iteration"), attack_start.get("epoch")):
            phase = str(row.get("phase", "unknown"))
            candidates.append((row, f"scada_timeout:{phase}"))
    return _choose_time(candidates)


def _default_physical_variables(
    baseline_rows: Mapping[int, Mapping[str, Any]],
    attack_rows: Mapping[int, Mapping[str, Any]],
    actuator_names: Sequence[str],
) -> list[str]:
    shared = common_columns(baseline_rows, attack_rows)
    tanks = [name for name in shared if re.fullmatch(r"T\d+", name, flags=re.IGNORECASE)]
    if tanks:
        return tanks
    actuator_set = set(actuator_names)
    return [name for name in shared if name not in actuator_set]


def _tolerance(variable: str, tolerances: float | Mapping[str, float]) -> float:
    if isinstance(tolerances, Mapping):
        value = tolerances.get(variable, tolerances.get("*", 0.01))
    else:
        value = tolerances
    result = float(value)
    if result < 0:
        raise ValueError("physical tolerances must be non-negative")
    return result


def first_control_deviation(
    baseline: Mapping[int, Mapping[str, Any]],
    attack: Mapping[int, Mapping[str, Any]],
    iterations: Sequence[int],
    actuators: Sequence[str],
    *,
    start_iteration: int | None,
) -> tuple[int | None, list[dict[str, Any]]]:
    first_overall: int | None = None
    rows: list[dict[str, Any]] = []
    considered = [iteration for iteration in iterations if start_iteration is None or iteration >= start_iteration]
    for actuator in actuators:
        compared = 0
        mismatches: list[int] = []
        for iteration in considered:
            baseline_state = as_bool(baseline[iteration].get(actuator))
            attack_state = as_bool(attack[iteration].get(actuator))
            if baseline_state is None or attack_state is None:
                continue
            compared += 1
            if baseline_state != attack_state:
                mismatches.append(iteration)
        first = min(mismatches) if mismatches else None
        if first is not None and (first_overall is None or first < first_overall):
            first_overall = first
        rows.append(
            {
                "actuator": actuator,
                "count": compared,
                "mismatch_count": len(mismatches),
                "mismatch_rate": None if compared == 0 else len(mismatches) / compared,
                "first_deviation_iteration": first,
                "last_deviation_iteration": max(mismatches) if mismatches else None,
            }
        )
    return first_overall, rows


def first_physical_deviation(
    baseline: Mapping[int, Mapping[str, Any]],
    attack: Mapping[int, Mapping[str, Any]],
    iterations: Sequence[int],
    variables: Sequence[str],
    *,
    start_iteration: int | None,
    tolerances: float | Mapping[str, float],
) -> int | None:
    for iteration in iterations:
        if start_iteration is not None and iteration < start_iteration:
            continue
        for variable in variables:
            baseline_value = as_float(baseline[iteration].get(variable))
            attack_value = as_float(attack[iteration].get(variable))
            if baseline_value is None or attack_value is None:
                continue
            if abs(attack_value - baseline_value) > _tolerance(variable, tolerances):
                return iteration
    return None


def physical_impact_metrics(
    baseline: Mapping[int, Mapping[str, Any]],
    attack: Mapping[int, Mapping[str, Any]],
    iterations: Sequence[int],
    variables: Sequence[str],
    *,
    start_iteration: int | None,
    end_iteration: int | None,
    hydraulic_step_sec: float,
) -> list[dict[str, Any]]:
    window = [
        iteration
        for iteration in iterations
        if (start_iteration is None or iteration >= start_iteration)
        and (end_iteration is None or iteration <= end_iteration)
    ]
    rows: list[dict[str, Any]] = []
    for variable in variables:
        differences: list[float] = []
        for iteration in window:
            baseline_value = as_float(baseline[iteration].get(variable))
            attack_value = as_float(attack[iteration].get(variable))
            if baseline_value is None or attack_value is None:
                continue
            differences.append(attack_value - baseline_value)
        absolute = [abs(value) for value in differences]
        squared_sum = math.fsum(value * value for value in differences)
        rows.append(
            {
                "variable": variable,
                "count": len(differences),
                "rmse": math.sqrt(squared_sum / len(differences)) if differences else None,
                "peak_abs_deviation": max(absolute) if absolute else None,
                "auc_abs_deviation": math.fsum(absolute) * hydraulic_step_sec
                if absolute
                else None,
                "mean_abs_deviation": fmean(absolute) if absolute else None,
                "window_start_iteration": start_iteration,
                "window_end_iteration": end_iteration,
            }
        )
    return rows


def _recovery_for_variables(
    baseline: Mapping[int, Mapping[str, Any]],
    attack: Mapping[int, Mapping[str, Any]],
    iterations: Sequence[int],
    variables: Sequence[str],
    *,
    end_iteration: int | None,
    tolerances: float | Mapping[str, float],
    consecutive_iterations: int,
) -> dict[str, Any]:
    if end_iteration is None:
        return {
            "status": "unknown_attack_end",
            "not_recovered": None,
            "recovery_iteration": None,
            "recovery_iterations": None,
        }
    if consecutive_iterations <= 0:
        raise ValueError("consecutive recovery iterations must be positive")

    post = [iteration for iteration in iterations if iteration > end_iteration]
    for index, start in enumerate(post):
        window = post[index : index + consecutive_iterations]
        if len(window) < consecutive_iterations:
            break
        if any(right != left + 1 for left, right in zip(window, window[1:])):
            continue
        recovered = True
        for iteration in window:
            for variable in variables:
                baseline_value = as_float(baseline[iteration].get(variable))
                attack_value = as_float(attack[iteration].get(variable))
                if baseline_value is None or attack_value is None:
                    recovered = False
                    break
                if abs(attack_value - baseline_value) > _tolerance(variable, tolerances):
                    recovered = False
                    break
            if not recovered:
                break
        if recovered:
            return {
                "status": "recovered",
                "not_recovered": False,
                "recovery_iteration": start,
                "recovery_iterations": start - end_iteration,
            }
    return {
        "status": "not_recovered",
        "not_recovered": True,
        "recovery_iteration": None,
        "recovery_iterations": None,
    }


def _phase_delay(
    start: Mapping[str, Any],
    end: Mapping[str, Any],
    hydraulic_step_sec: float,
) -> dict[str, Any]:
    start_iteration = start.get("iteration")
    end_iteration = end.get("iteration")
    iteration_delta = (
        int(end_iteration) - int(start_iteration)
        if start_iteration is not None and end_iteration is not None
        else None
    )
    start_epoch = start.get("epoch")
    end_epoch = end.get("epoch")
    wall_clock_delta = (
        float(end_epoch) - float(start_epoch)
        if start_epoch is not None and end_epoch is not None
        else None
    )
    return {
        "iterations": iteration_delta,
        "hydraulic_time_sec": None
        if iteration_delta is None
        else iteration_delta * hydraulic_step_sec,
        "wall_clock_sec": wall_clock_delta,
    }


def _point(iteration: int | None, source: str) -> dict[str, Any]:
    return {
        "iteration": iteration,
        "epoch": None,
        "iteration_source": source if iteration is not None else None,
        "epoch_source": None,
    }


def analyze_propagation(
    baseline_physics: Path | str,
    attack_physics: Path | str,
    baseline_actuators: Path | str,
    attack_actuators: Path | str,
    *,
    attack_schedule: Path | str | None = None,
    attack_events: Path | str | None = None,
    scada_timeouts: Path | str | None = None,
    variables: Sequence[str] | None = None,
    actuators: Sequence[str] | None = None,
    scenario: str | None = None,
    attack_start_iteration: int | None = None,
    attack_end_iteration: int | None = None,
    physical_tolerance: float | Mapping[str, float] = 0.01,
    hydraulic_step_sec: float = 300.0,
    recovery_consecutive_iterations: int = 3,
    exclude_iterations: Iterable[int] = (0,),
) -> dict[str, Any]:
    if hydraulic_step_sec <= 0:
        raise ValueError("hydraulic_step_sec must be positive")

    baseline_physics_path = resolve_csv_artifact(baseline_physics, PHYSICS_CSV)
    attack_physics_path = resolve_csv_artifact(attack_physics, PHYSICS_CSV)
    baseline_actuator_path = resolve_csv_artifact(baseline_actuators, ACTUATOR_CSV)
    attack_actuator_path = resolve_csv_artifact(attack_actuators, ACTUATOR_CSV)
    schedule_path = resolve_optional_csv_artifact(attack_schedule, ATTACK_SCHEDULE_CSV)
    events_path = resolve_optional_csv_artifact(attack_events, ATTACK_EVENTS_CSV)
    timeout_path = resolve_optional_csv_artifact(scada_timeouts, SCADA_TIMEOUT_CSV)

    baseline_physics_rows = read_iteration_rows(baseline_physics_path)
    attack_physics_rows = read_iteration_rows(attack_physics_path)
    physics_iterations, physics_alignment = aligned_iterations(
        baseline_physics_rows,
        attack_physics_rows,
        exclude_iterations=exclude_iterations,
    )
    baseline_actuator_rows = read_iteration_rows(baseline_actuator_path)
    attack_actuator_rows = read_iteration_rows(attack_actuator_path)
    control_iterations, control_alignment = aligned_iterations(
        baseline_actuator_rows,
        attack_actuator_rows,
        exclude_iterations=exclude_iterations,
    )
    selected_actuators = common_columns(
        baseline_actuator_rows, attack_actuator_rows, actuators
    )
    selected_variables = (
        common_columns(baseline_physics_rows, attack_physics_rows, variables)
        if variables is not None
        else _default_physical_variables(
            baseline_physics_rows, attack_physics_rows, selected_actuators
        )
    )
    if not selected_variables:
        raise ValueError("no comparable physical variables selected")

    schedule_rows = read_csv_rows(schedule_path)
    event_rows = read_csv_rows(events_path)
    timeout_rows = read_csv_rows(timeout_path)
    attack_start, attack_end = infer_attack_times(
        schedule_rows,
        event_rows,
        scenario=scenario,
        start_iteration=attack_start_iteration,
        end_iteration=attack_end_iteration,
    )
    communication = infer_communication_anomaly(
        event_rows,
        timeout_rows,
        attack_start,
        scenario=scenario,
    )
    control_iteration, control_rows = first_control_deviation(
        baseline_actuator_rows,
        attack_actuator_rows,
        control_iterations,
        selected_actuators,
        start_iteration=attack_start.get("iteration"),
    )
    control = _point(control_iteration, "actuator_baseline_deviation")
    # A physical deviation that predates the first control deviation cannot be
    # attributed to the control-to-physics leg of this attack chain.  It is
    # usually baseline run-to-run drift.  Runtime control iteration i is applied
    # to physics row i+1, so once tU exists search tP from that next row;
    # otherwise retain the attack-entry fallback for attacks with no observable
    # actuator-state change.
    physical_search_start = (
        control_iteration + 1
        if control_iteration is not None
        else attack_start.get("iteration")
    )
    physical_iteration = first_physical_deviation(
        baseline_physics_rows,
        attack_physics_rows,
        physics_iterations,
        selected_variables,
        start_iteration=physical_search_start,
        tolerances=physical_tolerance,
    )
    physical = _point(physical_iteration, "physical_baseline_deviation")

    physical_metrics = physical_impact_metrics(
        baseline_physics_rows,
        attack_physics_rows,
        physics_iterations,
        selected_variables,
        start_iteration=attack_start.get("iteration"),
        end_iteration=attack_end.get("iteration"),
        hydraulic_step_sec=hydraulic_step_sec,
    )
    recovery = _recovery_for_variables(
        baseline_physics_rows,
        attack_physics_rows,
        physics_iterations,
        selected_variables,
        end_iteration=attack_end.get("iteration"),
        tolerances=physical_tolerance,
        consecutive_iterations=recovery_consecutive_iterations,
    )
    recovery["hydraulic_time_sec"] = (
        None
        if recovery["recovery_iterations"] is None
        else recovery["recovery_iterations"] * hydraulic_step_sec
    )
    recovery["consecutive_iterations_required"] = recovery_consecutive_iterations

    return {
        "schema_version": 1,
        "metric_type": "propagation",
        "scenario": scenario,
        "inputs": {
            "baseline_physics_csv": str(baseline_physics_path),
            "attack_physics_csv": str(attack_physics_path),
            "baseline_actuator_csv": str(baseline_actuator_path),
            "attack_actuator_csv": str(attack_actuator_path),
            "attack_schedule_csv": str(schedule_path) if schedule_path else None,
            "attack_events_csv": str(events_path) if events_path else None,
            "scada_timeout_events_csv": str(timeout_path) if timeout_path else None,
        },
        "settings": {
            "hydraulic_step_sec": hydraulic_step_sec,
            "physical_tolerance": physical_tolerance,
            "recovery_consecutive_iterations": recovery_consecutive_iterations,
            "variables": selected_variables,
            "actuators": selected_actuators,
        },
        "alignment": {
            "physical": physics_alignment,
            "control": control_alignment,
        },
        "timeline": {
            "tA_attack": attack_start,
            "tAttackEnd": attack_end,
            "tC_communication": communication,
            "tU_control": control,
            "tP_physical": physical,
        },
        "delays": {
            "attack_to_communication": _phase_delay(
                attack_start, communication, hydraulic_step_sec
            ),
            "communication_to_control": _phase_delay(
                communication, control, hydraulic_step_sec
            ),
            "control_to_physical": _phase_delay(
                control, physical, hydraulic_step_sec
            ),
            "attack_to_control": _phase_delay(
                attack_start, control, hydraulic_step_sec
            ),
            "attack_to_physical": _phase_delay(
                attack_start, physical, hydraulic_step_sec
            ),
        },
        "physical": {
            "variables": physical_metrics,
            "overall": {
                "variable_count": len(physical_metrics),
                "mean_rmse": fmean(
                    float(row["rmse"])
                    for row in physical_metrics
                    if row["rmse"] is not None
                )
                if any(row["rmse"] is not None for row in physical_metrics)
                else None,
                "peak_abs_deviation": max(
                    (
                        float(row["peak_abs_deviation"])
                        for row in physical_metrics
                        if row["peak_abs_deviation"] is not None
                    ),
                    default=None,
                ),
                "auc_abs_deviation": math.fsum(
                    float(row["auc_abs_deviation"])
                    for row in physical_metrics
                    if row["auc_abs_deviation"] is not None
                ),
            },
        },
        "control": {
            "actuators": control_rows,
            "overall": {
                "actuator_count": len(control_rows),
                "mismatch_count": sum(
                    int(row["mismatch_count"]) for row in control_rows
                ),
                "first_deviation_iteration": control_iteration,
            },
        },
        "recovery": recovery,
    }


def analyze_propagation_roots(
    baseline: Path | str,
    attack: Path | str,
    **kwargs: Any,
) -> dict[str, Any]:
    kwargs.setdefault("attack_schedule", attack)
    kwargs.setdefault("attack_events", attack)
    kwargs.setdefault("scada_timeouts", attack)
    return analyze_propagation(
        baseline,
        attack,
        baseline,
        attack,
        **kwargs,
    )


def write_propagation_outputs(summary: Mapping[str, Any], output_dir: Path | str) -> dict[str, Path]:
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "propagation_summary.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)

    physical_path = output / "propagation_physical.csv"
    physical_columns = [
        "variable",
        "count",
        "rmse",
        "peak_abs_deviation",
        "auc_abs_deviation",
        "mean_abs_deviation",
        "window_start_iteration",
        "window_end_iteration",
    ]
    _write_csv(physical_path, summary["physical"]["variables"], physical_columns)

    control_path = output / "propagation_control.csv"
    control_columns = [
        "actuator",
        "count",
        "mismatch_count",
        "mismatch_rate",
        "first_deviation_iteration",
        "last_deviation_iteration",
    ]
    _write_csv(control_path, summary["control"]["actuators"], control_columns)

    timeline = summary["timeline"]
    delays = summary["delays"]
    recovery = summary["recovery"]
    physical_overall = summary["physical"]["overall"]
    summary_row: dict[str, Any] = {
        "metric_type": "propagation",
        "scenario": summary.get("scenario"),
        "tA_iteration": timeline["tA_attack"]["iteration"],
        "tA_epoch": timeline["tA_attack"]["epoch"],
        "tC_iteration": timeline["tC_communication"]["iteration"],
        "tC_epoch": timeline["tC_communication"]["epoch"],
        "tU_iteration": timeline["tU_control"]["iteration"],
        "tP_iteration": timeline["tP_physical"]["iteration"],
        "attack_end_iteration": timeline["tAttackEnd"]["iteration"],
        "recovery_status": recovery["status"],
        "not_recovered": recovery["not_recovered"],
        "recovery_iteration": recovery["recovery_iteration"],
        "recovery_iterations": recovery["recovery_iterations"],
        "recovery_hydraulic_time_sec": recovery["hydraulic_time_sec"],
        **{f"physical_{key}": value for key, value in physical_overall.items()},
    }
    for phase_name, values in delays.items():
        for key, value in values.items():
            summary_row[f"{phase_name}_{key}"] = value
    summary_csv_path = output / "propagation_summary.csv"
    _write_csv(summary_csv_path, [summary_row], list(summary_row))
    return {
        "json": json_path,
        "summary_csv": summary_csv_path,
        "physical_csv": physical_path,
        "control_csv": control_path,
    }
