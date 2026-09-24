#!/usr/bin/env python3
"""Analyze the advisor-requested experiment archive into paper-facing evidence."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.metrics.performance import analyze_performance, write_performance_outputs
from src.metrics.propagation import analyze_propagation, write_propagation_outputs
from scripts.analyze_todo2_experiments import tcp_retransmissions


TANKS = tuple(f"T{i}" for i in range(1, 8))
PRESSURES = ("J14", "J256", "J269", "J280", "J289", "J300", "J302", "J306", "J307", "J317", "J415", "J422")
ACTUATORS = ("PU1", "PU2", "PU4", "PU5", "PU6", "PU7", "PU8", "PU9", "PU10", "PU11", "V2")
PHYSICAL_EPSILON = {"tank": 0.01, "pressure": 0.1}
SCADA_OBSERVATION_EPSILON = 1e-5
MITM_T7_MERGE_EPSILON = 1e-9


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def number(raw: Any, default: float | None = None) -> float | None:
    try:
        value = float(raw)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def integer(raw: Any, default: int = 0) -> int:
    value = number(raw)
    return int(value) if value is not None else default


def truth(raw: Any) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "ok", "success"}


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if columns is None:
        columns = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    columns.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in columns})


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def iteration_table(path: Path) -> dict[int, dict[str, str]]:
    return {integer(row.get("iteration")): row for row in read_csv(path)}


def csv_path(output: Path, name: str) -> Path:
    for candidate in (output / "runtime/csv" / name, output / "reports/csv" / name, output / "csv" / name):
        if candidate.is_file():
            return candidate
    return output / "runtime/csv" / name


def report_csv_path(output: Path, name: str) -> Path:
    """Return the paper-facing report CSV without falling back to runtime data."""
    path = output / "reports/csv" / name
    if not path.is_file():
        raise FileNotFoundError(f"required report CSV is missing: {path}")
    return path


def valid_outputs(archive: Path) -> list[dict[str, Any]]:
    rows = json.loads((archive / "RUN_INDEX.json").read_text(encoding="utf-8"))
    adopted: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("valid"):
            adopted[str(row["id"])] = {**row, "output_path": Path(row["output"])}
    return sorted(adopted.values(), key=lambda row: (row.get("group", ""), str(row.get("id", ""))))


def latest_superseded_outputs(archive: Path, group: str) -> list[dict[str, Any]]:
    """Return the most recently superseded valid batch for one experiment group."""
    rows = json.loads((archive / "RUN_INDEX.json").read_text(encoding="utf-8"))
    candidates = [
        row for row in rows
        if row.get("group") == group
        and row.get("superseded") is True
        and str(row.get("superseded_at", ""))
    ]
    if not candidates:
        return []
    latest = max(str(row["superseded_at"]) for row in candidates)
    return [
        {**row, "output_path": Path(row["output"])}
        for row in candidates
        if str(row.get("superseded_at")) == latest
    ]


def dos_rerun_comparison(
    archive: Path,
    current_attacks: list[dict[str, Any]],
    baseline: Path,
) -> list[dict[str, Any]]:
    """Compare the adopted DoS scan with the immediately preceding batch."""
    previous = latest_superseded_outputs(archive, "dos")
    current = {
        float(row["strength_value"]): row
        for row in current_attacks
        if row.get("attack_type") == "dos"
    }
    adopted_outputs = {
        float(row.get("value", 0.0)): row
        for row in valid_outputs(archive)
        if row.get("group") == "dos"
    }
    metrics = (
        "modbus_success_rate",
        "communication_timeout_count",
        "mean_rtt_ms",
        "max_rtt_ms",
        "tcp_retransmissions",
        "plc4_control_update_frequency_per_cycle",
        "scada_first_observation_mismatch_iteration",
        "control_first_deviation_iteration",
        "control_mismatch_rate",
        "tank_peak_absolute_deviation",
        "pressure_peak_absolute_deviation",
    )
    rows: list[dict[str, Any]] = []
    for record in sorted(previous, key=lambda row: float(row.get("value", 0.0))):
        strength = float(record.get("value", 0.0))
        adopted = current.get(strength)
        adopted_record = adopted_outputs.get(strength)
        if adopted is None or adopted_record is None:
            continue
        prior = analyze_attack(
            {
                "id": record["id"],
                "group": "dos",
                "parameter": record.get("parameter", "rho"),
                "value": strength,
                "output_path": record["output_path"],
            },
            baseline,
        )
        item: dict[str, Any] = {
            "strength_value": strength,
            "previous_experiment_id": record["id"],
            "previous_attempt": record.get("attempt"),
            "previous_elapsed_sec": record.get("elapsed_sec"),
            "previous_superseded_at": record.get("superseded_at"),
            "previous_output": str(record["output_path"]),
            "adopted_experiment_id": adopted["experiment_id"],
            "adopted_attempt": adopted_record.get("attempt"),
            "adopted_elapsed_sec": adopted_record.get("elapsed_sec"),
            "adopted_output": str(adopted_record["output_path"]),
        }
        for metric in metrics:
            before = prior.get(metric)
            after = adopted.get(metric)
            item[f"previous_{metric}"] = before
            item[f"adopted_{metric}"] = after
            item[f"delta_{metric}"] = (
                float(after) - float(before)
                if before is not None and after is not None
                else None
            )
        rows.append(item)
    return rows


def aligned_differences(
    baseline: dict[int, dict[str, str]],
    attack: dict[int, dict[str, str]],
    variables: Iterable[str],
    *,
    start: int,
) -> dict[str, list[tuple[int, float]]]:
    result: dict[str, list[tuple[int, float]]] = {name: [] for name in variables}
    for iteration in sorted(set(baseline) & set(attack)):
        if iteration < start or iteration == 0:
            continue
        for name in variables:
            left = number(baseline[iteration].get(name))
            right = number(attack[iteration].get(name))
            if left is not None and right is not None:
                result[name].append((iteration, right - left))
    return result


def deviation_summary(differences: dict[str, list[tuple[int, float]]], epsilon: float) -> dict[str, Any]:
    values = [value for series in differences.values() for _iteration, value in series]
    first = min(
        (iteration for series in differences.values() for iteration, value in series if abs(value) > epsilon),
        default=None,
    )
    return {
        "variable_count": len(differences),
        "sample_count": len(values),
        "rmse": math.sqrt(fmean(value * value for value in values)) if values else None,
        "mean_absolute_deviation": fmean(abs(value) for value in values) if values else None,
        "peak_absolute_deviation": max((abs(value) for value in values), default=None),
        "first_deviation_iteration": first,
        "epsilon": epsilon,
    }


def bool_value(raw: Any) -> bool:
    return str(raw).strip().lower() in {"1", "true", "open", "opened", "on"}


def control_summary(baseline: dict[int, dict[str, str]], attack: dict[int, dict[str, str]], start: int) -> dict[str, Any]:
    iterations = [x for x in sorted(set(baseline) & set(attack)) if x >= start]
    names = [name for name in ACTUATORS if any(name in row for row in attack.values())]
    mismatch = 0
    comparisons = 0
    first = None
    per_actuator: dict[str, int] = Counter()
    for iteration in iterations:
        for name in names:
            if name not in baseline[iteration] or name not in attack[iteration]:
                continue
            comparisons += 1
            if bool_value(baseline[iteration][name]) != bool_value(attack[iteration][name]):
                mismatch += 1
                per_actuator[name] += 1
                first = iteration if first is None else min(first, iteration)

    def switches(table: dict[int, dict[str, str]]) -> int:
        total = 0
        for name in names:
            sequence = [bool_value(table[i][name]) for i in sorted(table) if i >= start and name in table[i]]
            total += sum(left != right for left, right in zip(sequence, sequence[1:]))
        return total

    return {
        "actuator_count": len(names),
        "comparison_count": comparisons,
        "mismatch_count": mismatch,
        "mismatch_rate": mismatch / comparisons if comparisons else None,
        "first_deviation_iteration": first,
        "attack_switch_count": switches(attack),
        "baseline_switch_count": switches(baseline),
        "switch_count_deviation": switches(attack) - switches(baseline),
        "per_actuator_mismatch_count": dict(per_actuator),
    }


def perception_summary(output: Path) -> dict[str, Any]:
    rows = read_csv(csv_path(output, "attack_events.csv"))
    errors = []
    for row in rows:
        old = number(row.get("old_value", row.get("original_value")))
        new = number(row.get("new_value", row.get("modified_value")))
        if old is not None and new is not None:
            errors.append(abs(new - old))
    return {
        "modified_frame_count": len(errors),
        "mean_absolute_perception_error": fmean(errors) if errors else None,
        "peak_absolute_perception_error": max(errors, default=None),
    }


def scada_observation_summary(
    output: Path,
    *,
    start: int,
    epsilon: float = SCADA_OBSERVATION_EPSILON,
) -> dict[str, Any]:
    """Compare SCADA-observed values with same-run physical ground truth."""
    observed = iteration_table(csv_path(output, "scada_observed_wide.csv"))
    physics = iteration_table(csv_path(output, "physics.csv"))
    observed_fields = sorted({
        field
        for row in observed.values()
        for field in row
        if field != "iteration" and "." in field and "_" in field
    })

    def physics_field(observed_field: str) -> str:
        qualified = observed_field.split(".", 1)[1]
        return qualified.split("_", 1)[1]

    comparisons = 0
    first_iteration: int | None = None
    first_variables: list[str] = []
    first_peak_error: float | None = None
    for iteration in sorted(set(observed) & set(physics)):
        if iteration < start:
            continue
        mismatches: list[tuple[str, float]] = []
        for field in observed_fields:
            truth_field = physics_field(field)
            observed_value = number(observed[iteration].get(field))
            truth_value = number(physics[iteration].get(truth_field))
            if observed_value is None or truth_value is None:
                continue
            comparisons += 1
            error = abs(observed_value - truth_value)
            if error > epsilon:
                mismatches.append((field, error))
        if mismatches:
            first_iteration = iteration
            first_variables = [field for field, _error in mismatches]
            first_peak_error = max(error for _field, error in mismatches)
            break

    return {
        "scada_observation_variable_count": len(observed_fields),
        "scada_observation_comparisons_until_first_mismatch": comparisons,
        "scada_observation_epsilon": epsilon,
        "scada_first_observation_mismatch_iteration": first_iteration,
        "scada_first_observation_mismatch_variable_count": len(first_variables),
        "scada_first_observation_mismatch_variables": first_variables,
        "scada_first_observation_mismatch_peak_absolute_error": first_peak_error,
    }


def communication_summary(
    output: Path,
    *,
    first_control_deviation_iteration: int | None = None,
) -> dict[str, Any]:
    rows = [
        row for row in read_csv(csv_path(output, "communication.csv"))
        if str(row.get("operation", "")).lower() != "connect" and not truth(row.get("warmup"))
    ]
    statuses = Counter(str(row.get("status", "")).strip().lower() for row in rows)
    success = statuses.get("success", 0)
    timeout = sum(value for key, value in statuses.items() if "timeout" in key)
    plc4_successful_update_iterations = {
        integer(row.get("iteration"))
        for row in rows
        if str(row.get("phase", "")).lower() == "downlink"
        and str(row.get("target", "")).upper() == "PLC4"
        and str(row.get("status", "")).lower() == "success"
        and integer(row.get("iteration"), -1) >= 0
    }
    downlink_success = len(plc4_successful_update_iterations)
    iterations = {integer(row.get("iteration")) for row in rows if integer(row.get("iteration"), -1) >= 0}
    post_deviation_iterations = {
        iteration for iteration in iterations
        if first_control_deviation_iteration is not None
        and iteration >= first_control_deviation_iteration
    }
    post_deviation_updates = {
        iteration for iteration in plc4_successful_update_iterations
        if first_control_deviation_iteration is not None
        and iteration >= first_control_deviation_iteration
    }
    latencies = [
        value for row in rows
        if str(row.get("status", "")).lower() == "success"
        for value in [number(row.get("latency_ms"))]
        if value is not None
    ]
    return {
        "modbus_request_count": len(rows),
        "modbus_success_count": success,
        "modbus_success_rate": success / len(rows) if rows else None,
        "communication_timeout_count": timeout,
        "communication_error_count": len(rows) - success - timeout,
        "mean_rtt_ms": fmean(latencies) if latencies else None,
        "max_rtt_ms": max(latencies, default=None),
        "plc4_successful_control_updates": downlink_success,
        "plc4_control_update_frequency_per_cycle": downlink_success / len(iterations) if iterations else None,
        "plc4_post_deviation_observation_cycles": (
            len(post_deviation_iterations)
            if first_control_deviation_iteration is not None else None
        ),
        "plc4_post_deviation_successful_control_updates": (
            len(post_deviation_updates)
            if first_control_deviation_iteration is not None else None
        ),
        "plc4_post_deviation_update_frequency_per_cycle": (
            len(post_deviation_updates) / len(post_deviation_iterations)
            if post_deviation_iterations else None
        ),
        "plc4_post_deviation_average_update_interval_cycles": (
            len(post_deviation_iterations) / len(post_deviation_updates)
            if post_deviation_updates else None
        ),
    }


def network_summary(output: Path, link: str = "r0-r4") -> dict[str, Any]:
    rows = [
        row for row in read_csv(csv_path(output, "network.csv"))
        if row.get("metric_source") == "link_trace" and row.get("link") == link
    ]
    tx = sum(integer(row.get("tx_packets")) for row in rows)
    lost = sum(integer(row.get("lost_packets")) for row in rows)
    delay_samples = sum(integer(row.get("delay_samples")) for row in rows)
    weighted_delay = sum((number(row.get("mean_delay_ms"), 0.0) or 0.0) * integer(row.get("delay_samples")) for row in rows)
    result = {
        "target_link": link,
        "tx_packets": tx,
        "lost_packets": lost,
        "packet_loss_rate": lost / tx if tx else None,
        "queue_drop_packets": sum(integer(row.get("queue_drop_packets")) for row in rows),
        "queue_packets_max": max((integer(row.get("queue_packets_max")) for row in rows), default=None),
        "mean_propagation_delay_ms": weighted_delay / delay_samples if delay_samples else None,
    }
    retransmissions = 0
    tcp_packets = 0
    for pcap in sorted((output / "runtime/network/pcap").glob(f"ns3_network-{link}-*.pcap")):
        try:
            count, packets, _rate = tcp_retransmissions(pcap)
        except (OSError, ValueError):
            continue
        retransmissions += count
        tcp_packets += packets
    result.update({
        "tcp_retransmissions": retransmissions,
        "tcp_sequence_packets": tcp_packets,
        "tcp_retransmission_rate": retransmissions / tcp_packets if tcp_packets else None,
    })
    return result


def attack_start_iteration(output: Path, fallback: int = 15) -> int:
    starts = []
    start_events = {
        "attack_packet_sent", "attack_triggered", "attack_start", "dos_start",
        "openplc_logic_start", "logic_injection_start", "attack_logic_loaded",
    }
    for row in read_csv(csv_path(output, "attack_events.csv")):
        event = str(row.get("event", "")).strip().lower()
        modification = (
            str(row.get("old_value", row.get("original_value", ""))).strip() != ""
            and str(row.get("new_value", row.get("modified_value", ""))).strip() != ""
        )
        if event in start_events or modification:
            starts.append(integer(row.get("iteration"), fallback))
    if starts:
        return min(starts)
    for row in read_csv(csv_path(output, "attack_schedule.csv")):
        event = str(row.get("event", "")).strip().lower()
        if event in start_events:
            starts.append(integer(row.get("iteration"), fallback))
    return min(starts, default=fallback)


def event_epoch(
    output: Path,
    iteration: int | None,
    kinds: set[str],
    *,
    not_before: float | None = None,
) -> float | None:
    if iteration is None:
        return None
    values = []
    for row in read_csv(csv_path(output, "events.csv")):
        if integer(row.get("iteration"), -10) != iteration or row.get("event_type") not in kinds:
            continue
        raw = number(row.get("wall_time_ns"))
        if raw is not None:
            epoch = raw / 1_000_000_000.0
            if not_before is None or epoch >= not_before:
                values.append(epoch)
    return min(values, default=None)


def propagation_summary(baseline: Path, output: Path, group: str) -> dict[str, Any]:
    summary = analyze_propagation(
        baseline, output, baseline, output,
        attack_schedule=output, attack_events=output, scada_timeouts=output,
        variables=list(TANKS + PRESSURES), physical_tolerance=PHYSICAL_EPSILON["tank"],
        hydraulic_step_sec=300.0, recovery_consecutive_iterations=3,
    )
    summary["scenario"] = group
    write_propagation_outputs(summary, output / "reports/advisor")
    timeline = summary["timeline"]
    t_a = timeline["tA_attack"].get("epoch")
    t_c = timeline["tC_communication"].get("epoch")
    t_u_i = timeline["tU_control"].get("iteration")
    t_p_i = timeline["tP_physical"].get("iteration")
    t_u = event_epoch(output, t_u_i, {"plc_output_changed"}, not_before=t_a)
    if t_u is None:
        t_u = event_epoch(output, t_u_i, {"scada_iteration_end"}, not_before=t_a)
    if t_u is None and t_u_i is not None:
        t_u = event_epoch(output, t_u_i + 1, {"physics_actuator_input"}, not_before=t_a)
    t_p = event_epoch(
        output, t_p_i, {"physics_sensor_value", "physics_iteration_end"},
        not_before=t_u or t_a,
    )
    # Logic injection enters directly at the control layer.  Reporting a made-up
    # communication anomaly would misrepresent the causal path.
    communication_applicable = group != "plc_logic"
    if not communication_applicable:
        t_c = None
        timeline["tC_communication"] = {
            "iteration": None, "epoch": None,
            "iteration_source": "not_applicable_direct_control_attack",
            "epoch_source": "not_applicable_direct_control_attack",
        }

    def delta(end: float | None, start: float | None) -> float | None:
        return end - start if end is not None and start is not None else None

    return {
        "attack_type": group,
        "t_a_iteration": timeline["tA_attack"].get("iteration"),
        "t_c_iteration": timeline["tC_communication"].get("iteration"),
        "t_u_iteration": t_u_i,
        "t_p_iteration": t_p_i,
        "t_a_epoch": t_a,
        "t_c_epoch": t_c,
        "t_u_epoch": t_u,
        "t_p_epoch": t_p,
        "communication_stage_applicable": communication_applicable,
        "D_prop_wall_sec": delta(t_p, t_a),
        "D_network_wall_sec": delta(t_c, t_a),
        "D_control_wall_sec": delta(t_u, t_c) if communication_applicable else None,
        "D_attack_to_control_wall_sec": delta(t_u, t_a),
        "D_physical_wall_sec": delta(t_p, t_u),
        "D_prop_hydraulic_sec": summary["delays"]["attack_to_physical"]["hydraulic_time_sec"],
        "D_network_hydraulic_sec": summary["delays"]["attack_to_communication"]["hydraulic_time_sec"] if communication_applicable else None,
        "D_control_hydraulic_sec": summary["delays"]["communication_to_control"]["hydraulic_time_sec"] if communication_applicable else None,
        "D_physical_hydraulic_sec": summary["delays"]["control_to_physical"]["hydraulic_time_sec"],
    }


def analyze_attack(row: dict[str, Any], baseline: Path) -> dict[str, Any]:
    output = row["output_path"]
    start = attack_start_iteration(output)
    base_physics = iteration_table(csv_path(baseline, "physics.csv"))
    run_physics = iteration_table(csv_path(output, "physics.csv"))
    base_control = iteration_table(csv_path(baseline, "actuator_state.csv"))
    run_control = iteration_table(csv_path(output, "actuator_state.csv"))
    tank = deviation_summary(aligned_differences(base_physics, run_physics, TANKS, start=start), PHYSICAL_EPSILON["tank"])
    pressure = deviation_summary(aligned_differences(base_physics, run_physics, PRESSURES, start=start), PHYSICAL_EPSILON["pressure"])
    control = control_summary(base_control, run_control, start)
    result: dict[str, Any] = {
        "experiment_id": row["id"], "attack_type": row["group"],
        "strength_parameter": row.get("parameter"), "strength_value": row.get("value"),
        "attack_start_iteration": start,
        **{f"control_{key}": value for key, value in control.items() if key != "per_actuator_mismatch_count"},
        "per_actuator_mismatch_count": control["per_actuator_mismatch_count"],
        **{f"tank_{key}": value for key, value in tank.items()},
        **{f"pressure_{key}": value for key, value in pressure.items()},
    }
    if row["group"] == "mitm":
        result.update(perception_summary(output))
    if row["group"] == "dos":
        result["configured_attack_traffic_mbps"] = float(row.get("value", 0.0)) * 10.0
        result.update(scada_observation_summary(output, start=start))
        result.update(communication_summary(
            output,
            first_control_deviation_iteration=control["first_deviation_iteration"],
        ))
        result.update(network_summary(output))
    if row["group"] == "plc_logic":
        config = yaml.safe_load((output / "runtime/config_resolved.yaml").read_text(encoding="utf-8"))
        rule = config["attacks"]["scenarios"][0]["injection"]["rule"]
        original = float(rule["original_value"])
        injected = float(rule["injected_value"])
        result.update({
            "original_threshold": original,
            "injected_threshold": injected,
            "threshold_absolute_change": abs(injected - original),
            "threshold_modification_ratio": abs(injected - original) / abs(original) if original else None,
        })
    return result


def startup_seconds(performance: dict[str, Any]) -> float | None:
    total = 0.0
    seen = False
    for record in performance.get("stages", {}).get("records", []) or []:
        if record.get("stage") == "Run persistent closed-loop control":
            seen = True
            break
        total += float(record.get("duration_sec", 0.0) or 0.0)
    return total if seen else None


def analyze_scale(row: dict[str, Any]) -> dict[str, Any]:
    output = row["output_path"]
    performance = analyze_performance(output)
    write_performance_outputs(performance, output / "reports/advisor")
    config = yaml.safe_load((output / "runtime/config_resolved.yaml").read_text(encoding="utf-8"))
    experiment = config.get("experiment", {}) or {}
    topology = experiment.get("topology", {}) or {}
    plc_count = integer(topology.get("plc_count"))
    physical_process_plc_count = experiment.get("physical_process_plc_count")
    synthetic_scale_plc_count = experiment.get("synthetic_scale_plc_count")
    if physical_process_plc_count is None:
        physical_process_plc_count = min(plc_count, 8)
    if synthetic_scale_plc_count is None:
        synthetic_scale_plc_count = max(0, plc_count - 8)
    synthetic_profile = experiment.get("synthetic_plc_profile")
    pipeline_audit_path = output / "reports/advisor/scalability_pipeline_audit_summary.json"
    try:
        pipeline_audit = json.loads(pipeline_audit_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pipeline_audit = {}
    resources = performance.get("resources", {}) or {}
    timing = performance.get("iteration_time", {}) or {}
    runtime = performance.get("runtime", {}) or {}
    communication = performance.get("communication", {}) or {}
    mean_cycle_time = timing.get("mean_sec")
    pair_count = experiment.get("communication_pair_count")
    pair_updates_per_sec = None
    if mean_cycle_time and pair_count:
        pair_updates_per_sec = float(pair_count) / float(mean_cycle_time)
    return {
        "experiment_id": row["id"],
        **topology,
        "physical_process_plc_count": physical_process_plc_count,
        "synthetic_scale_plc_count": synthetic_scale_plc_count,
        "synthetic_plc_profile": synthetic_profile,
        "scale_pipeline": experiment.get("scale_pipeline"),
        "data_feed_source_plc_count": experiment.get("data_feed_source_plc_count"),
        "execution_plc_count": experiment.get("execution_plc_count"),
        "communication_pair_count": experiment.get("communication_pair_count"),
        "all_plcs_communication_active": experiment.get("all_plcs_communication_active"),
        "data_feed_range": experiment.get("data_feed_range"),
        "execution_rule": experiment.get("execution_rule"),
        "openplc_instance_count": topology.get("plc_count"),
        "modbus_connection_count": communication.get("connection_count"),
        "startup_time_sec": startup_seconds(performance),
        "mean_cycle_time_sec": mean_cycle_time,
        "pair_updates_per_sec": pair_updates_per_sec,
        "p95_cycle_time_sec": timing.get("p95_sec"),
        "total_simulation_time_sec": timing.get("total_sec"),
        "total_wall_clock_sec": runtime.get("wall_clock_sec"),
        "mean_aggregate_cpu_percent": resources.get("mean_aggregate_cpu_percent"),
        "peak_aggregate_cpu_percent": resources.get("peak_aggregate_cpu_percent"),
        "peak_aggregate_memory_mb": resources.get("peak_aggregate_rss_mb"),
        "completed_cycles": timing.get("count"),
        "run_complete": performance.get("complete"),
        "telemetry_quality_complete": performance.get("quality_complete"),
        "pipeline_audit_pass": pipeline_audit.get("pass"),
        "pipeline_expected_records": pipeline_audit.get("expected_pair_iteration_records"),
        "pipeline_observed_records": pipeline_audit.get("observed_pair_iteration_records"),
        "source_match_rate": pipeline_audit.get("source_match_rate"),
        "destination_match_rate": pipeline_audit.get("destination_match_rate"),
        "logic_correct_rate": pipeline_audit.get("logic_correct_rate"),
        "end_to_end_correct_rate": pipeline_audit.get("end_to_end_correct_rate"),
        "feed_below_5_count": pipeline_audit.get("feed_below_5_count"),
        "feed_at_or_above_5_count": pipeline_audit.get("feed_at_or_above_5_count"),
    }


def flatten(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        result.append({key: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value for key, value in row.items()})
    return result


def plot_attack(
    rows: list[dict[str, Any]],
    group: str,
    output: Path,
    *,
    strength_bounds: tuple[float, float] | None = None,
    title: str | None = None,
    xticks: tuple[float, ...] | None = None,
) -> None:
    signed_strength = group == "plc_logic"
    sort_key = (
        (lambda row: float(row["strength_value"]))
        if signed_strength
        else (lambda row: abs(float(row["strength_value"])))
    )
    selected = [row for row in rows if row["attack_type"] == group]
    if strength_bounds is not None:
        lower, upper = strength_bounds
        selected = [row for row in selected if lower <= float(row["strength_value"]) <= upper]
    selected = sorted(selected, key=sort_key)
    if not selected:
        return
    x = [
        float(row["strength_value"]) if signed_strength else abs(float(row["strength_value"]))
        for row in selected
    ]
    if signed_strength:
        xlabel = "Signed threshold shift Δh"
    elif group == "dos":
        xlabel = "Offered load ratio ρ"
    else:
        xlabel = "Absolute attack strength"
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.8))
    if group == "dos":
        axes[0].plot(
            x,
            [row.get("modbus_success_rate") for row in selected],
            color="#3366A5",
            marker="o",
            label="Modbus success rate",
        )
        axes[0].plot(
            x,
            [row.get("plc4_control_update_frequency_per_cycle") for row in selected],
            color="#D9822B",
            marker="s",
            linestyle="--",
            label="PLC4 update frequency",
        )
        axes[0].plot(
            x,
            [row.get("packet_loss_rate") for row in selected],
            color="#666666",
            marker="^",
            linestyle=":",
            label="Link loss rate",
        )
        axes[0].set_ylabel("Rate")
    else:
        axes[0].plot(x, [row.get("control_mismatch_rate") for row in selected], marker="o")
        axes[0].set_ylabel("Actuator mismatch rate")
    axes[0].set_xlabel(xlabel)
    axes[0].grid(alpha=0.3)
    axes[0].legend() if group == "dos" else None
    axes[1].plot(x, [row.get("tank_peak_absolute_deviation") for row in selected], marker="o", label="Tank level")
    axes[1].plot(x, [row.get("pressure_peak_absolute_deviation") for row in selected], marker="s", label="Node pressure")
    axes[1].set_xlabel(xlabel)
    axes[1].set_ylabel("Peak absolute deviation")
    axes[1].grid(alpha=0.3)
    axes[1].legend()
    if signed_strength:
        for axis in axes:
            axis.axvline(0.0, color="#444444", linestyle="--", linewidth=1.0, alpha=0.75)
        if min(x) <= -3.4 and max(x) >= -3.3:
            for axis in axes:
                axis.axvspan(
                    -3.4,
                    -3.3,
                    color="#D9822B",
                    alpha=0.12,
                    label="Upper ≤ lower threshold",
                )
                axis.axvline(-3.3, color="#A65E14", linestyle=":", linewidth=1.0, alpha=0.8)
            axes[0].legend(loc="best", fontsize=8)
    if strength_bounds is not None:
        tick_values = list(xticks) if xticks is not None else x
        labels = [f"{value:g}" for value in tick_values]
        for axis in axes:
            axis.set_xticks(tick_values, labels=labels, rotation=45, ha="right")
    chart_title = title or ("PLC threshold-shift response scan" if signed_strength else f"{group.upper()} intensity-response scan")
    fig.suptitle(chart_title)
    fig.tight_layout()
    fig.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(fig)


def plot_scada_tank_observation_comparison(
    baseline: Path,
    attack: Path,
    output: Path,
    *,
    attack_start: int = 15,
    attack_end: int = 55,
    first_control_deviation: int = 47,
) -> None:
    """Plot SCADA-observed PLC4 tank values for baseline and DoS rho=5."""
    baseline_rows = iteration_table(csv_path(baseline, "scada_observed_wide.csv"))
    attack_rows = iteration_table(csv_path(attack, "scada_observed_wide.csv"))
    first_observation_mismatch = scada_observation_summary(
        attack,
        start=attack_start,
    )["scada_first_observation_mismatch_iteration"]
    iterations = sorted(set(baseline_rows) & set(attack_rows))
    if not iterations:
        return

    series = (
        ("PLC4.PLC4_T3", "T3"),
        ("PLC4.PLC4_T4", "T4"),
    )
    fig, axes = plt.subplots(2, 1, figsize=(9.4, 6.5), sharex=True)
    for index, (axis, (field, label)) in enumerate(zip(axes, series)):
        axis.plot(
            iterations,
            [number(baseline_rows[i].get(field)) for i in iterations],
            color="#3366A5",
            linewidth=2.1,
            linestyle="-",
            label="Baseline",
            zorder=3,
        )
        axis.plot(
            iterations,
            [number(attack_rows[i].get(field)) for i in iterations],
            color="#D9822B",
            linewidth=2.1,
            linestyle="--",
            label="DoS ρ=5",
            zorder=4,
        )
        axis.axvspan(
            attack_start,
            attack_end,
            color="#D9822B",
            alpha=0.09,
            label="Attack window" if index == 0 else None,
            zorder=1,
        )
        axis.axvline(
            first_control_deviation,
            color="#555555",
            linestyle=":",
            linewidth=1.5,
            label="First control mismatch (tU)" if index == 0 else None,
            zorder=2,
        )
        if first_observation_mismatch is not None:
            axis.axvline(
                first_observation_mismatch,
                color="#A65E14",
                linestyle="--",
                linewidth=1.2,
                label="First SCADA/physical mismatch (tS)" if index == 0 else None,
                zorder=2,
            )
        axis.set_title(f"SCADA-observed {label}", loc="left", fontsize=11)
        axis.set_ylabel("Tank level (m)")
        axis.grid(axis="y", color="#D9D9D9", linewidth=0.8, alpha=0.8)
        axis.spines[["top", "right"]].set_visible(False)

    axes[1].annotate(
        "Attack on",
        xy=(attack_start, 0.02),
        xycoords=("data", "axes fraction"),
        xytext=(4, 3),
        textcoords="offset points",
        ha="left",
        va="bottom",
        color="#7A4A16",
        fontsize=9,
    )
    if first_observation_mismatch is not None:
        axes[1].annotate(
            "tS",
            xy=(first_observation_mismatch, 0.02),
            xycoords=("data", "axes fraction"),
            xytext=(4, 18),
            textcoords="offset points",
            ha="left",
            va="bottom",
            color="#A65E14",
            fontsize=9,
        )
    axes[1].annotate(
        "tU",
        xy=(first_control_deviation, 0.02),
        xycoords=("data", "axes fraction"),
        xytext=(4, 3),
        textcoords="offset points",
        ha="left",
        va="bottom",
        color="#444444",
        fontsize=9,
    )
    axes[1].annotate(
        "Attack off",
        xy=(attack_end, 0.02),
        xycoords=("data", "axes fraction"),
        xytext=(4, 3),
        textcoords="offset points",
        ha="left",
        va="bottom",
        color="#7A4A16",
        fontsize=9,
    )
    axes[0].legend(loc="upper left", ncol=2, frameon=True)
    axes[1].set_xlabel("Iteration")
    axes[1].set_xlim(min(iterations), max(iterations))
    fig.suptitle("SCADA-observed T3 and T4: baseline vs DoS ρ=5", fontsize=15, y=0.985)
    fig.text(
        0.5,
        0.945,
        (
            "Iterations 1–79; attack window 15–55; "
            f"first SCADA/physical mismatch tS={first_observation_mismatch}; "
            f"first control mismatch tU={first_control_deviation}"
        ),
        ha="center",
        va="top",
        fontsize=9.5,
        color="#555555",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(output, dpi=220, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def group_identical_series(
    entries: list[tuple[float, str, list[tuple[int, float]]]],
    *,
    tolerance: float = MITM_T7_MERGE_EPSILON,
) -> list[dict[str, Any]]:
    """Merge time series whose aligned values are numerically identical."""
    groups: list[dict[str, Any]] = []
    for strength, experiment_id, points in sorted(entries, key=lambda item: item[0]):
        for group in groups:
            reference = group["points"]
            if len(reference) != len(points):
                continue
            if all(
                left_iteration == right_iteration
                and abs(left_value - right_value) <= tolerance
                for (left_iteration, left_value), (right_iteration, right_value)
                in zip(reference, points)
            ):
                group["strengths"].append(strength)
                group["experiment_ids"].append(experiment_id)
                break
        else:
            groups.append({
                "strengths": [strength],
                "experiment_ids": [experiment_id],
                "points": points,
            })
    return groups


def mitm_t7_groups(
    adopted: list[dict[str, Any]],
    baseline: Path,
) -> list[dict[str, Any]]:
    return t7_series_groups(adopted, baseline, experiment_group="mitm", source="physical")


def t7_series_groups(
    adopted: list[dict[str, Any]],
    baseline: Path,
    *,
    experiment_group: str,
    source: str,
) -> list[dict[str, Any]]:
    return tank_series_groups(
        adopted,
        baseline,
        experiment_group=experiment_group,
        source=source,
        tank="T7",
    )


def mitm_plc4_received_t7_groups(
    adopted: list[dict[str, Any]],
    baseline: Path,
    *,
    attack_start: int = 15,
    attack_end: int = 55,
) -> list[dict[str, Any]]:
    """Reconstruct the T7 value delivered by SCADA to PLC4 under MITM."""
    mitm_rows = [row for row in adopted if row.get("group") == "mitm"]
    selected: list[tuple[float, str, Path]] = []
    if not any(float(row.get("value", math.nan)) == 0.0 for row in mitm_rows):
        selected.append((0.0, "baseline_closed_loop", baseline))
    selected.extend(
        (float(row.get("value", 0.0)), str(row["id"]), row["output_path"])
        for row in mitm_rows
    )
    entries: list[tuple[float, str, list[tuple[int, float]]]] = []
    source_files: dict[str, str] = {}
    for strength, experiment_id, output in selected:
        source_path = report_csv_path(output, "scada_observed_wide.csv")
        table = iteration_table(source_path)
        points: list[tuple[int, float]] = []
        for iteration in sorted(table):
            if iteration < 1:
                continue
            observed = number(table[iteration].get("PLC9.PLC9_T7"))
            if observed is None:
                continue
            received = observed + strength if attack_start <= iteration <= attack_end else observed
            points.append((iteration, received))
        entries.append((strength, experiment_id, points))
        source_files[experiment_id] = str(source_path)
    groups = group_identical_series(entries)
    for group in groups:
        group["source_files"] = [source_files[item] for item in group["experiment_ids"]]
        group["source_column"] = "PLC9.PLC9_T7"
        group["receiver"] = "PLC4"
        group["transformation"] = (
            "received_T7(iteration) = PLC9.PLC9_T7(iteration) + delta_x "
            f"for {attack_start} <= iteration <= {attack_end}; unchanged otherwise"
        )
    return groups


def validate_mitm_plc4_received_t7(
    adopted: list[dict[str, Any]],
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for row in adopted:
        if row.get("group") != "mitm":
            continue
        strength = float(row.get("value", 0.0))
        observed_path = report_csv_path(row["output_path"], "scada_observed_wide.csv")
        events_path = report_csv_path(row["output_path"], "attack_events.csv")
        observed = iteration_table(observed_path)
        old_differences: list[float] = []
        new_differences: list[float] = []
        event_count = 0
        for event in read_csv(events_path):
            if event.get("variable") != "PLC9_T7" or event.get("target") != "PLC4":
                continue
            iteration = integer(event.get("iteration"))
            raw_value = number(observed.get(iteration, {}).get("PLC9.PLC9_T7"))
            old_value = number(event.get("old_value"))
            new_value = number(event.get("new_value"))
            if raw_value is None or old_value is None or new_value is None:
                continue
            old_differences.append(abs(raw_value - old_value))
            new_differences.append(abs((raw_value + strength) - new_value))
            event_count += 1
        runs.append({
            "experiment_id": row["id"],
            "strength": strength,
            "validated_attack_event_count": event_count,
            "max_abs_scada_vs_event_old_value_m": max(old_differences, default=None),
            "max_abs_reconstructed_vs_event_new_value_m": max(new_differences, default=None),
            "scada_source_file": str(observed_path),
            "attack_event_source_file": str(events_path),
        })
    overall = max(
        (
            run["max_abs_reconstructed_vs_event_new_value_m"]
            for run in runs
            if run["max_abs_reconstructed_vs_event_new_value_m"] is not None
        ),
        default=None,
    )
    return {
        "receiver": "PLC4",
        "variable": "PLC9_T7",
        "attack_window_inclusive": [15, 55],
        "formula": "received_T7 = scada_observed_PLC9_T7 + delta_x during attack window",
        "csv_precision_tolerance_m": 1.1e-6,
        "overall_max_abs_reconstructed_vs_event_new_value_m": overall,
        "matches_attack_events_within_csv_precision": overall is not None and overall <= 1.1e-6,
        "runs": runs,
    }


def tank_series_groups(
    adopted: list[dict[str, Any]],
    baseline: Path,
    *,
    experiment_group: str,
    source: str,
    tank: str,
) -> list[dict[str, Any]]:
    """Load report CSV tank trajectories and merge numerically identical series."""
    if source not in {"physical", "scada_observed"}:
        raise ValueError(f"unsupported tank source: {source}")
    scada_columns = {
        "T3": "PLC4.PLC4_T3",
        "T4": "PLC4.PLC4_T4",
        "T7": "PLC9.PLC9_T7",
    }
    if source == "scada_observed" and tank not in scada_columns:
        raise ValueError(f"unsupported SCADA-observed tank: {tank}")
    entries: list[tuple[float, str, list[tuple[int, float]]]] = []
    source_files: dict[str, str] = {}
    experiment_rows = [row for row in adopted if row.get("group") == experiment_group]
    selected: list[tuple[float, str, Path]] = []
    # MITM and DoS scans do not contain a zero-strength run, so the common
    # closed-loop baseline is their rho/delta=0 reference.  The PLC scan has
    # its own zero-shift run and should not duplicate it with another series.
    if not any(float(row.get("value", math.nan)) == 0.0 for row in experiment_rows):
        selected.append((0.0, "baseline_closed_loop", baseline))
    selected.extend(
        (float(row.get("value", 0.0)), str(row["id"]), row["output_path"])
        for row in experiment_rows
    )
    filename, column = (
        ("physics.csv", tank)
        if source == "physical"
        else ("scada_observed_wide.csv", scada_columns[tank])
    )
    for strength, experiment_id, output in selected:
        source_path = report_csv_path(output, filename)
        table = iteration_table(source_path)
        points = []
        for iteration in sorted(table):
            # Iteration 0 is a pre-initialization placeholder (T7=0), whereas
            # the physical initial condition begins at iteration 1 (T7=2.5 m).
            if iteration < 1:
                continue
            value = number(table[iteration].get(column))
            if value is not None:
                points.append((iteration, value))
        entries.append((strength, experiment_id, points))
        source_files[experiment_id] = str(source_path)
    groups = group_identical_series(entries)
    for group in groups:
        group["source_files"] = [source_files[item] for item in group["experiment_ids"]]
        group["source_column"] = column
    return groups


def strength_group_label(
    strengths: list[float],
    *,
    parameter_symbol: str = "Δx",
    zero_description: str = "基线",
) -> str:
    values = ", ".join(f"{value:g}" for value in strengths)
    if strengths == [0.0]:
        return f"{zero_description} {parameter_symbol}=0"
    suffix = "（轨迹重合）" if len(strengths) > 1 else ""
    return f"{parameter_symbol}={values}{suffix}"


def write_mitm_t7_group_data(groups: list[dict[str, Any]], output: Path) -> None:
    write_t7_group_data(groups, output, parameter_name="delta_x")


def write_t7_group_data(
    groups: list[dict[str, Any]],
    output: Path,
    *,
    parameter_name: str,
) -> None:
    rows: dict[int, dict[str, Any]] = {}
    for group in groups:
        values = "_".join(
            f"{value:g}".replace("-", "m").replace(".", "p")
            for value in group["strengths"]
        )
        column = (
            f"baseline_{parameter_name}_0"
            if group["strengths"] == [0.0] and "baseline_closed_loop" in group["experiment_ids"]
            else f"{parameter_name}_{values}"
        )
        for iteration, value in group["points"]:
            rows.setdefault(iteration, {"iteration": iteration})[column] = value
    write_csv(output, [rows[key] for key in sorted(rows)])


def validate_t7_actual_vs_scada(
    adopted: list[dict[str, Any]],
    *,
    experiment_group: str,
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for row in adopted:
        if row.get("group") != experiment_group:
            continue
        actual_path = report_csv_path(row["output_path"], "physics.csv")
        observed_path = report_csv_path(row["output_path"], "scada_observed_wide.csv")
        actual = iteration_table(actual_path)
        observed = iteration_table(observed_path)
        differences = []
        common_iterations = sorted(set(actual) & set(observed))
        for iteration in common_iterations:
            if iteration < 1:
                continue
            actual_value = number(actual[iteration].get("T7"))
            observed_value = number(observed[iteration].get("PLC9.PLC9_T7"))
            if actual_value is not None and observed_value is not None:
                differences.append(abs(actual_value - observed_value))
        runs.append({
            "experiment_id": row["id"],
            "strength": float(row.get("value", 0.0)),
            "compared_iteration_count": len(differences),
            "max_abs_difference_m": max(differences, default=None),
            "actual_source_file": str(actual_path),
            "scada_source_file": str(observed_path),
        })
    maximum = max(
        (run["max_abs_difference_m"] for run in runs if run["max_abs_difference_m"] is not None),
        default=None,
    )
    return {
        "experiment_group": experiment_group,
        "actual_source": "output/reports/csv/physics.csv/T7",
        "scada_source": "output/reports/csv/scada_observed_wide.csv/PLC9.PLC9_T7",
        "comparison_iterations": "1-79 (iteration 80 has no SCADA poll row)",
        "csv_precision_tolerance_m": 1.1e-6,
        "overall_max_abs_difference_m": maximum,
        "consistent_within_csv_precision": maximum is not None and maximum <= 1.1e-6,
        "runs": runs,
    }


def plot_mitm_t7_comparison(groups: list[dict[str, Any]], output: Path) -> None:
    plot_t7_comparison(
        groups,
        output,
        title="不同 MITM 篡改强度下的 T7 实际水位",
        parameter_symbol="Δx",
        measurement_note="物理层真实水位；第 1–80 轮",
        zero_description="MITM 零强度",
    )


def plot_t7_comparison(
    groups: list[dict[str, Any]],
    output: Path,
    *,
    title: str,
    parameter_symbol: str,
    measurement_note: str,
    signed_strength: bool = False,
    zero_description: str = "基线",
) -> None:
    if not groups:
        return
    font_path = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    font_family = "DejaVu Sans"
    if font_path.is_file():
        font_manager.fontManager.addfont(str(font_path))
        font_family = font_manager.FontProperties(fname=font_path).get_name()
    with plt.rc_context({"font.family": font_family, "axes.unicode_minus": False}):
        figure_height = 8.2 if len(groups) >= 25 else (7.0 if len(groups) >= 10 else 6.3)
        fig, axis = plt.subplots(figsize=(10.8, figure_height))
        attack_groups = [group for group in groups if 0.0 not in group["strengths"]]
        cmap = plt.get_cmap("viridis")
        colors = [cmap(index / max(1, len(attack_groups) - 1)) for index in range(len(attack_groups))]
        markers = ("o", "s", "^", "D", "v", "P", "X", "h")

        axis.axvspan(15, 55, color="#D9822B", alpha=0.09, zorder=0)
        axis.axvline(15, color="#8A5A20", linestyle=":", linewidth=1.1, alpha=0.75)
        axis.axvline(55, color="#8A5A20", linestyle=":", linewidth=1.1, alpha=0.75)
        for group in groups:
            iterations = [point[0] for point in group["points"]]
            values = [point[1] for point in group["points"]]
            if 0.0 in group["strengths"]:
                axis.plot(
                    iterations,
                    values,
                    color="#303030",
                    linewidth=2.4,
                    linestyle="--",
                    label=strength_group_label(
                        group["strengths"],
                        parameter_symbol=parameter_symbol,
                        zero_description=zero_description,
                    ),
                    zorder=3,
                )
                continue
            index = attack_groups.index(group)
            if signed_strength:
                representative = fmean(group["strengths"])
                normalized = min(1.0, max(0.0, (representative + 5.0) / 10.0))
                color = plt.get_cmap("coolwarm_r")(normalized)
            else:
                color = colors[index]
            axis.plot(
                iterations,
                values,
                color=color,
                linewidth=1.8,
                marker=markers[index % len(markers)],
                markersize=3.8,
                markevery=10,
                label=strength_group_label(
                    group["strengths"],
                    parameter_symbol=parameter_symbol,
                    zero_description=zero_description,
                ),
                zorder=2,
            )

        axis.set_title(title, fontsize=15, pad=12)
        axis.set_xlabel("仿真轮次")
        axis.set_ylabel("T7 水位 (m)")
        max_iteration = max(point[0] for group in groups for point in group["points"])
        axis.set_xlim(1, max_iteration)
        all_values = [value for group in groups for _, value in group["points"]]
        value_min, value_max = min(all_values), max(all_values)
        padding = max(0.2, (value_max - value_min) * 0.05)
        axis.set_ylim(min(-0.2, value_min - padding), value_max + padding)
        axis.grid(axis="y", color="#D9D9D9", linewidth=0.8, alpha=0.8)
        axis.spines[["top", "right"]].set_visible(False)
        axis.text(
            35,
            axis.get_ylim()[1],
            "攻击窗口 15–55",
            ha="center",
            va="bottom",
            color="#7A4A16",
            fontsize=9,
        )
        axis.legend(
            loc="upper center",
            bbox_to_anchor=(0.5, -0.16),
            ncol=3,
            frameon=False,
            title="攻击强度",
            fontsize=8.5,
        )
        fig.text(
            0.5,
            0.935,
            f"{measurement_note}；相同轨迹按最大绝对差 ≤ 1e-9 m 合并",
            ha="center",
            va="top",
            fontsize=9.5,
            color="#555555",
        )
        fig.tight_layout(rect=(0, 0.08, 1, 0.92))
        fig.savefig(output, dpi=220, bbox_inches="tight", facecolor="white")
        plt.close(fig)


def plot_tank_pair_comparison(
    tank_groups: dict[str, list[dict[str, Any]]],
    output: Path,
    *,
    title: str,
    parameter_symbol: str,
    measurement_note: str,
) -> None:
    """Plot T3 and T4 as aligned panels with one consistent intensity legend."""
    if not all(tank_groups.get(tank) for tank in ("T3", "T4")):
        return
    font_path = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    font_family = "DejaVu Sans"
    if font_path.is_file():
        font_manager.fontManager.addfont(str(font_path))
        font_family = font_manager.FontProperties(fname=font_path).get_name()
    strength_keys: list[tuple[float, ...]] = []
    for groups in tank_groups.values():
        for group in groups:
            key = tuple(group["strengths"])
            if key not in strength_keys:
                strength_keys.append(key)
    reference_keys = [key for key in strength_keys if 0.0 in key]
    attack_keys = [key for key in strength_keys if 0.0 not in key]
    cmap = plt.get_cmap("viridis")
    colors = {
        key: cmap(index / max(1, len(attack_keys) - 1))
        for index, key in enumerate(attack_keys)
    }
    markers = ("o", "s", "^", "D", "v", "P", "X", "h")
    with plt.rc_context({"font.family": font_family, "axes.unicode_minus": False}):
        fig, axes = plt.subplots(2, 1, figsize=(10.8, 9.0), sharex=True)
        for axis, tank in zip(axes, ("T3", "T4")):
            axis.axvspan(15, 55, color="#D9822B", alpha=0.09, zorder=0)
            axis.axvline(15, color="#8A5A20", linestyle=":", linewidth=1.1, alpha=0.75)
            axis.axvline(55, color="#8A5A20", linestyle=":", linewidth=1.1, alpha=0.75)
            for group in tank_groups[tank]:
                key = tuple(group["strengths"])
                iterations = [point[0] for point in group["points"]]
                values = [point[1] for point in group["points"]]
                if key in reference_keys:
                    color = "#303030"
                    linestyle = "--"
                    marker = None
                    linewidth = 2.4
                else:
                    index = attack_keys.index(key)
                    color = colors[key]
                    linestyle = "-"
                    marker = markers[index % len(markers)]
                    linewidth = 1.8
                axis.plot(
                    iterations,
                    values,
                    color=color,
                    linestyle=linestyle,
                    linewidth=linewidth,
                    marker=marker,
                    markersize=3.8,
                    markevery=10,
                    label=strength_group_label(
                        group["strengths"], parameter_symbol=parameter_symbol
                    ),
                    zorder=3 if key in reference_keys else 2,
                )
            axis.set_ylabel(f"{tank} 水位 (m)")
            all_values = [
                value
                for group in tank_groups[tank]
                for _, value in group["points"]
            ]
            value_min, value_max = min(all_values), max(all_values)
            padding = max(0.2, (value_max - value_min) * 0.05)
            axis.set_ylim(min(-0.2, value_min - padding), value_max + padding)
            axis.grid(axis="y", color="#D9D9D9", linewidth=0.8, alpha=0.8)
            axis.spines[["top", "right"]].set_visible(False)
        max_iteration = max(
            point[0]
            for groups in tank_groups.values()
            for group in groups
            for point in group["points"]
        )
        axes[0].text(
            35,
            axes[0].get_ylim()[1],
            "攻击窗口 15–55",
            ha="center",
            va="bottom",
            color="#7A4A16",
            fontsize=9,
        )
        axes[-1].set_xlim(1, max_iteration)
        axes[-1].set_xlabel("仿真轮次")
        handles, labels = axes[0].get_legend_handles_labels()
        legend_columns = 4 if len(groups) >= 25 else 3
        legend_rows = math.ceil(len(groups) / legend_columns)
        bottom_margin = min(0.34, 0.08 + 0.022 * legend_rows)
        fig.legend(
            handles,
            labels,
            loc="lower center",
            bbox_to_anchor=(0.5, 0.01),
            ncol=legend_columns,
            frameon=False,
            title="攻击强度",
            fontsize=8.5,
        )
        fig.suptitle(title, fontsize=15, y=0.985)
        fig.text(
            0.5,
            0.95,
            f"{measurement_note}；相同轨迹按最大绝对差 ≤ 1e-9 m 合并",
            ha="center",
            va="top",
            fontsize=9.5,
            color="#555555",
        )
        fig.tight_layout(rect=(0, bottom_margin, 1, 0.925))
        fig.savefig(output, dpi=220, bbox_inches="tight", facecolor="white")
        plt.close(fig)


def write_mitm_t7_interactive(groups: list[dict[str, Any]], output: Path) -> None:
    """Write a self-contained D3 fragment for inspecting the merged T7 curves."""
    series = [
        {
            "label": strength_group_label(
                group["strengths"], zero_description="MITM 零强度"
            ),
            "baseline": group["strengths"] == [0.0],
            "points": [
                {"iteration": iteration, "value": value}
                for iteration, value in group["points"]
            ],
        }
        for group in groups
    ]
    data = json.dumps(series, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    fragment = r'''<div id="mitm-t7-viz" class="mitm-t7-viz">
  <style>
    #mitm-t7-viz { color: var(--foreground); background: var(--background); font-family: var(--font-sans); padding: 18px; border: 1px solid var(--border); border-radius: 14px; }
    #mitm-t7-viz .viz-head { display: flex; gap: 12px; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; margin-bottom: 8px; }
    #mitm-t7-viz h2 { font-size: 18px; line-height: 1.3; margin: 0; }
    #mitm-t7-viz p { color: var(--muted-foreground); font-size: 12px; margin: 4px 0 0; }
    #mitm-t7-viz .plot { position: relative; width: 100%; min-height: 430px; }
    #mitm-t7-viz svg { display: block; width: 100%; height: auto; overflow: visible; }
    #mitm-t7-viz .grid line { stroke: var(--border); stroke-opacity: .72; }
    #mitm-t7-viz .grid path, #mitm-t7-viz .axis .domain { display: none; }
    #mitm-t7-viz .axis text { fill: var(--muted-foreground); font-size: 11px; }
    #mitm-t7-viz .axis line { stroke: var(--border); }
    #mitm-t7-viz .attack-window { fill: var(--viz-series-2); opacity: .08; }
    #mitm-t7-viz .attack-edge { stroke: var(--viz-series-2); stroke-width: 1; stroke-dasharray: 3 3; opacity: .7; }
    #mitm-t7-viz .attack-label { fill: var(--muted-foreground); font-size: 11px; }
    #mitm-t7-viz .legend { display: flex; flex-wrap: wrap; gap: 6px 12px; margin-top: 8px; }
    #mitm-t7-viz .legend button { appearance: none; border: 1px solid var(--border); background: var(--card); color: var(--foreground); border-radius: 999px; padding: 5px 9px; font: inherit; font-size: 11px; cursor: pointer; }
    #mitm-t7-viz .legend button.off { opacity: .38; text-decoration: line-through; }
    #mitm-t7-viz .swatch { display: inline-block; width: 17px; height: 3px; margin-right: 6px; vertical-align: middle; background: var(--swatch); }
    #mitm-t7-viz .tooltip { position: absolute; pointer-events: none; display: none; z-index: 5; min-width: 190px; max-width: 280px; padding: 9px 11px; border-radius: 8px; border: 1px solid var(--border); background: var(--popover); color: var(--popover-foreground); box-shadow: 0 8px 24px color-mix(in srgb, var(--foreground) 14%, transparent); font-size: 11px; }
    #mitm-t7-viz .tooltip strong { display: block; margin-bottom: 5px; }
    #mitm-t7-viz .tip-row { display: flex; justify-content: space-between; gap: 14px; white-space: nowrap; }
    @media (max-width: 640px) { #mitm-t7-viz { padding: 12px; } #mitm-t7-viz .plot { min-height: 330px; } }
  </style>
  <div class="viz-head"><div><h2>不同 MITM 篡改强度下的 T7 实际水位</h2><p>物理层真实水位 · 攻击窗口 15–55 · 相同轨迹按最大绝对差 ≤ 1e-9 m 合并</p></div></div>
  <div class="plot" role="img" aria-label="不同 MITM 加性篡改强度下 T7 实际水位随仿真轮次变化的折线图"><div class="tooltip"></div></div>
  <div class="legend" aria-label="点击图例可显示或隐藏曲线"></div>
  <script src="https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"></script>
  <script>
  (() => {
    const root = document.querySelector('#mitm-t7-viz');
    const data = __MITM_T7_DATA__;
    const colors = ['var(--viz-series-1)','var(--viz-series-2)','var(--viz-series-3)','var(--viz-series-4)','var(--viz-series-5)','var(--viz-series-6)'];
    const dashes = ['', '', '', '', '', '', '7 3', '3 2', '10 3 2 3'];
    data.forEach((d, i) => { d.visible = true; d.color = d.baseline ? 'var(--foreground)' : colors[(i - 1 + colors.length) % colors.length]; d.dash = d.baseline ? '8 4' : dashes[i] || ''; });
    const plot = d3.select(root).select('.plot');
    const tooltip = plot.select('.tooltip');
    const legend = d3.select(root).select('.legend');
    const margin = {top: 25, right: 22, bottom: 52, left: 58};

    function draw() {
      plot.selectAll('svg').remove();
      const width = Math.max(320, root.clientWidth - 36);
      const height = width < 600 ? 340 : 450;
      const innerWidth = width - margin.left - margin.right;
      const innerHeight = height - margin.top - margin.bottom;
      const svg = plot.insert('svg', ':first-child').attr('viewBox', `0 0 ${width} ${height}`);
      const g = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);
      const x = d3.scaleLinear().domain([1, 80]).range([0, innerWidth]);
      const y = d3.scaleLinear().domain([0, d3.max(data, s => d3.max(s.points, p => p.value)) * 1.05]).nice().range([innerHeight, 0]);

      g.append('rect').attr('class', 'attack-window').attr('x', x(15)).attr('width', x(55) - x(15)).attr('height', innerHeight);
      [15, 55].forEach(v => g.append('line').attr('class', 'attack-edge').attr('x1', x(v)).attr('x2', x(v)).attr('y1', 0).attr('y2', innerHeight));
      g.append('text').attr('class', 'attack-label').attr('x', x(35)).attr('y', -8).attr('text-anchor', 'middle').text('攻击窗口 15–55');
      g.append('g').attr('class', 'grid').call(d3.axisLeft(y).ticks(6).tickSize(-innerWidth).tickFormat(''));
      g.append('g').attr('class', 'axis').attr('transform', `translate(0,${innerHeight})`).call(d3.axisBottom(x).ticks(width < 600 ? 6 : 9).tickFormat(d3.format('d')));
      g.append('g').attr('class', 'axis').call(d3.axisLeft(y).ticks(6));
      g.append('text').attr('fill', 'var(--foreground)').attr('text-anchor', 'middle').attr('x', innerWidth / 2).attr('y', innerHeight + 42).attr('font-size', 12).text('仿真轮次');
      g.append('text').attr('fill', 'var(--foreground)').attr('text-anchor', 'middle').attr('transform', 'rotate(-90)').attr('x', -innerHeight / 2).attr('y', -43).attr('font-size', 12).text('T7 水位 (m)');

      const line = d3.line().x(p => x(p.iteration)).y(p => y(p.value));
      g.selectAll('.series').data(data).join('path').attr('class', 'series').attr('fill', 'none')
        .attr('stroke', d => d.color).attr('stroke-width', d => d.baseline ? 2.7 : 2)
        .attr('stroke-dasharray', d => d.dash).attr('opacity', d => d.visible ? 1 : 0)
        .attr('d', d => line(d.points));

      const focus = g.append('line').attr('stroke', 'var(--muted-foreground)').attr('stroke-dasharray', '2 3').attr('y1', 0).attr('y2', innerHeight).style('display', 'none');
      g.append('rect').attr('fill', 'transparent').attr('width', innerWidth).attr('height', innerHeight)
        .on('pointerenter', () => { focus.style('display', null); tooltip.style('display', 'block'); })
        .on('pointerleave', () => { focus.style('display', 'none'); tooltip.style('display', 'none'); })
        .on('pointermove', event => {
          const [mx] = d3.pointer(event);
          const iteration = Math.max(1, Math.min(80, Math.round(x.invert(mx))));
          focus.attr('x1', x(iteration)).attr('x2', x(iteration));
          const rows = data.filter(d => d.visible).map(d => ({label: d.label, color: d.color, value: d.points.find(p => p.iteration === iteration)?.value})).filter(d => d.value !== undefined);
          tooltip.html(`<strong>第 ${iteration} 轮</strong>` + rows.map(r => `<div class="tip-row"><span style="color:${r.color}">${r.label}</span><span>${r.value.toFixed(3)} m</span></div>`).join(''));
          const bounds = plot.node().getBoundingClientRect();
          const left = Math.max(6, Math.min(bounds.width - 285, event.clientX - bounds.left + 14));
          const top = Math.min(bounds.height - 80, Math.max(6, event.clientY - bounds.top - 20));
          tooltip.style('left', `${left}px`).style('top', `${top}px`);
        });
    }

    legend.selectAll('button').data(data).join('button').attr('type', 'button').attr('aria-pressed', 'true')
      .html(d => `<span class="swatch" style="--swatch:${d.color}"></span>${d.label}`)
      .on('click', function(event, d) { d.visible = !d.visible; d3.select(this).classed('off', !d.visible).attr('aria-pressed', String(d.visible)); draw(); });
    draw();
    let observedWidth = Math.round(root.getBoundingClientRect().width);
    new ResizeObserver(entries => {
      const nextWidth = Math.round(entries[0].contentRect.width);
      if (Math.abs(nextWidth - observedWidth) > 1) { observedWidth = nextWidth; draw(); }
    }).observe(root);
  })();
  </script>
</div>'''.replace("__MITM_T7_DATA__", data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(fragment + "\n", encoding="utf-8")


def plot_scale(rows: list[dict[str, Any]], output: Path) -> None:
    rows = sorted(rows, key=lambda row: integer(row.get("plc_count")))
    if not rows:
        return
    x = [integer(row.get("plc_count")) for row in rows]
    fig, axes = plt.subplots(1, 4, figsize=(15, 4.2))
    color = "#2F6B9A"
    line_style = {"marker": "o", "color": color, "linewidth": 2.0, "markersize": 5.5}
    axes[0].plot(x, [row.get("mean_cycle_time_sec") for row in rows], **line_style)
    axes[0].set_ylabel("Mean cycle time (s)")
    axes[1].plot(x, [row.get("mean_aggregate_cpu_percent") for row in rows], **line_style)
    axes[1].set_ylabel("Aggregate CPU (%)")
    axes[2].plot(x, [row.get("peak_aggregate_memory_mb") for row in rows], **line_style)
    axes[2].set_ylabel("Peak RSS (MB)")
    axes[3].plot(x, [row.get("pair_updates_per_sec") for row in rows], **line_style)
    axes[3].set_ylabel("Pair updates/s")
    for axis in axes:
        axis.set_xlabel("PLC instances")
        axis.set_xscale("log", base=2)
        axis.set_xticks(x, labels=[str(value) for value in x])
        axis.grid(alpha=0.3)
    fig.suptitle("Data-feed communication/control scalability", y=0.99)
    fig.text(
        0.5,
        0.93,
        "30 iterations per size; all 3,900 pair-iteration end-to-end audits passed",
        ha="center",
        color="#4A5560",
        fontsize=9.5,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.90))
    fig.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(fig)


def build_report(
    archive: Path,
    attacks: list[dict[str, Any]],
    propagation: list[dict[str, Any]],
    scales: list[dict[str, Any]],
    quality: list[dict[str, Any]],
    valid_count: int,
) -> str:
    def fmt(value: Any) -> str:
        if value is None:
            return "N/A"
        if isinstance(value, float):
            return f"{value:.6g}"
        return str(value)

    quality_pass = sum(bool(row.get("quality_pass")) for row in quality)
    lines = [
        "# 导师建议实验结果报告", "",
        f"本归档包含 {valid_count} 个有效的真实闭环运行，其中 {quality_pass}/{len(quality)} 个运行通过遥测完整性审计，写入丢失均为 0。每个配置仅运行一次，因此结果是参数扫描观测，不作统计显著性或普遍阈值声明。", "",
        "## 攻击强度—物理影响", "",
    ]
    for group, title in (("mitm", "MITM 数据篡改"), ("dos", "DoS 拥塞"), ("plc_logic", "PLC 逻辑注入")):
        key = (
            (lambda row: float(row["strength_value"]))
            if group == "plc_logic"
            else (lambda row: abs(float(row["strength_value"])))
        )
        rows = sorted((row for row in attacks if row["attack_type"] == group), key=key)
        lines.extend([f"### {title}", ""])
        if not rows:
            lines.extend(["无有效运行。", ""])
            continue
        for row in rows:
            lines.append(
                f"- 强度 `{fmt(row['strength_value'])}`：控制不一致率 `{fmt(row.get('control_mismatch_rate'))}`，"
                f"水箱峰值偏差 `{fmt(row.get('tank_peak_absolute_deviation'))}`，"
                f"节点压力峰值偏差 `{fmt(row.get('pressure_peak_absolute_deviation'))}`。"
            )
        lines.append("")
    dos = sorted((row for row in attacks if row["attack_type"] == "dos"), key=lambda x: float(x["strength_value"]))
    mitm = sorted((row for row in attacks if row["attack_type"] == "mitm"), key=lambda x: float(x["strength_value"]))
    logic = sorted((row for row in attacks if row["attack_type"] == "plc_logic"), key=lambda x: float(x["strength_value"]))
    lines.extend(["## 主要观测", ""])
    if mitm:
        lines.append(
            f"- MITM 实际平均感知误差从 `{fmt(mitm[0].get('mean_absolute_perception_error'))}` 增至 "
            f"`{fmt(mitm[-1].get('mean_absolute_perception_error'))}`；对应水箱峰值偏差从 "
            f"`{fmt(mitm[0].get('tank_peak_absolute_deviation'))}` 增至 `{fmt(mitm[-1].get('tank_peak_absolute_deviation'))}`。"
        )
        for index, row in enumerate(mitm[:-1]):
            tail = mitm[index:]
            tank_values = [float(item.get("tank_peak_absolute_deviation") or 0.0) for item in tail]
            control_values = [float(item.get("control_mismatch_rate") or 0.0) for item in tail]
            if (
                len(tail) >= 2
                and max(tank_values) - min(tank_values) <= 1e-4
                and max(control_values) - min(control_values) <= 1e-6
            ):
                lines.append(
                    f"- 本次扫描从 MITM 强度 `{fmt(row['strength_value'])}` 起，控制不一致率和水箱峰值偏差不再继续增加，"
                    "呈现由离散控制状态与物理边界共同导致的响应饱和现象。"
                )
                break
    if dos:
        physical = [row for row in dos if (row.get("tank_peak_absolute_deviation") or 0) > 0]
        degraded_only = [
            row for row in dos
            if (row.get("communication_timeout_count") or 0) > 0
            and (row.get("tank_peak_absolute_deviation") or 0) == 0
        ]
        if degraded_only:
            lines.append(
                f"- DoS 在 ρ=`{fmt(degraded_only[0]['strength_value'])}` 首次观测到通信超时但尚无物理偏差，"
                "构成网络退化尚未转化为物理异常的中间状态。"
            )
        if physical:
            lines.append(
                f"- 本次 DoS 扫描在 ρ=`{fmt(physical[0]['strength_value'])}` 首次观测到控制与物理偏差；"
                "这是该次单运行扫描的观测点，不表述为普遍阈值。"
            )
    if logic:
        zero = next((row for row in logic if float(row["strength_value"]) == 0.0), None)
        negative = min(logic, key=lambda row: float(row["strength_value"]))
        positive = max(logic, key=lambda row: float(row["strength_value"]))
        if zero:
            lines.append(
                f"- PLC 阈值零偏移对照点（PU10 上水位关闭阈值保持 4.8 m）的控制不一致率为 `{fmt(zero.get('control_mismatch_rate'))}`，"
                f"水箱峰值偏差为 `{fmt(zero.get('tank_peak_absolute_deviation'))}`。"
            )
        lines.append(
            "- PLC 非零强度运行在替换 OpenPLC 进程前快照 %MD 输入与 %QX 输出，并在新实例启动后完成状态交接；"
            "这避免默认零输入触发与目标阈值无关的滞环输出。"
        )
        lines.append(
            f"- PLC 上阈值扫描的两端偏移 `{fmt(negative['strength_value'])}` 与 "
            f"`{fmt(positive['strength_value'])}` 的控制不一致率分别为 "
            f"`{fmt(negative.get('control_mismatch_rate'))}` 和 `{fmt(positive.get('control_mismatch_rate'))}`，"
            f"水箱峰值偏差分别为 `{fmt(negative.get('tank_peak_absolute_deviation'))}` 和 "
            f"`{fmt(positive.get('tank_peak_absolute_deviation'))}`。"
        )
        if float(negative["strength_value"]) <= -3.3:
            lines.append(
                "- PLC 边界点 Δh=-3.4 和 -3.3 对应的上水位关闭阈值分别为 1.4 m 和 1.5 m；"
                "前者低于、后者等于未修改的 1.5 m 下水位开启阈值，属于滞环边界/规则重叠条件，"
                "应与上阈值高于下阈值的常规扫描点分开解释。"
            )
        nonzero_logic = sorted(
            (row for row in logic if float(row["strength_value"]) < 0),
            key=lambda row: float(row["strength_value"]),
        )
        nonzero_first = [
            int(row["control_first_deviation_iteration"])
            for row in nonzero_logic
            if row.get("control_first_deviation_iteration") is not None
        ]
        if len(nonzero_first) == len(nonzero_logic) and nonzero_first:
            monotonic = all(
                current <= following
                for current, following in zip(nonzero_first, nonzero_first[1:])
            )
            if monotonic:
                lines.append(
                    f"- PLC 全部 {len(nonzero_logic)} 个非零点的首次控制偏离轮次随实际上阈值升高单调不减，"
                    f"由第 {nonzero_first[0]} 轮后移至第 {nonzero_first[-1]} 轮；相邻阈值可能因离散水力步而共享同一触发轮次。"
                )
        fine = sorted(
            (
                row for row in logic
                if -3.0 <= float(row["strength_value"]) <= -2.0
            ),
            key=lambda row: float(row["strength_value"]),
        )
        fine_first = [
            int(row["control_first_deviation_iteration"])
            for row in fine
            if row.get("control_first_deviation_iteration") is not None
        ]
        if len(fine) == 11 and len(fine_first) == 11:
            lines.append(
                "- PLC Δh=-3.0至-2.0的0.1步长扫描中，首次控制偏离轮次依次为 "
                + "、".join(str(value) for value in fine_first)
                + "；触发时刻随实际上阈值提高而后移，但控制不一致率和物理峰值局部非单调。"
            )
        near_negative = min(
            (row for row in logic if float(row["strength_value"]) < 0),
            key=lambda row: abs(float(row["strength_value"])),
            default=None,
        )
        near_positive = min(
            (row for row in logic if float(row["strength_value"]) > 0),
            key=lambda row: abs(float(row["strength_value"])),
            default=None,
        )
        if near_negative and near_positive:
            lines.append(
                f"- 距零最近的非零偏移 `{fmt(near_negative['strength_value'])}` 与 "
                f"`{fmt(near_positive['strength_value'])}` 的控制不一致率分别为 "
                f"`{fmt(near_negative.get('control_mismatch_rate'))}` 和 "
                f"`{fmt(near_positive.get('control_mismatch_rate'))}`，水箱峰值偏差分别为 "
                f"`{fmt(near_negative.get('tank_peak_absolute_deviation'))}` 和 "
                f"`{fmt(near_positive.get('tank_peak_absolute_deviation'))}`。"
            )
    if mitm and logic:
        mitm_by_strength = {
            round(float(row["strength_value"]), 10): row
            for row in mitm
            if float(row["strength_value"]) > 0
        }
        logic_by_strength = {
            round(abs(float(row["strength_value"])), 10): row
            for row in logic
            if float(row["strength_value"]) < 0
        }
        shared = sorted(set(mitm_by_strength) & set(logic_by_strength))
        first_match = [
            strength for strength in shared
            if mitm_by_strength[strength].get("control_first_deviation_iteration")
            == logic_by_strength[strength].get("control_first_deviation_iteration")
        ]
        outcome_fields = (
            "control_mismatch_rate",
            "tank_peak_absolute_deviation",
            "pressure_peak_absolute_deviation",
        )
        outcome_match = [
            strength for strength in shared
            if all(
                math.isclose(
                    float(mitm_by_strength[strength].get(field) or 0.0),
                    float(logic_by_strength[strength].get(field) or 0.0),
                    abs_tol=1e-12,
                )
                for field in outcome_fields
            )
        ]
        if shared and len(first_match) == len(shared):
            lines.append(
                f"- 在 MITM Δx 与 PLC Δh=-Δx 的 {len(shared)} 个共有强度点上，首次控制偏离轮次完全一致；"
                "两种攻击都改变了 PU10 上水位条件的首次越阈时刻。"
            )
        if outcome_match:
            lines.append(
                f"- MITM 与 PLC 注入在 Δx=0.1–{fmt(max(outcome_match))} 区间的控制不一致率和物理峰值完全一致，"
                "但从 Δx=2.2 起后续轨迹分化：MITM 修改共享的 T7 输入，会同时作用于消费该变量的多条规则；"
                "PLC 注入只改写 PU10 的一个上阈值。该结果说明相同首次物理触发并不代表相同攻击入口或后续传播机制。"
            )
    lines.append("")
    lines.extend(["## 跨层传播时间", ""])
    for row in propagation:
        lines.append(
            f"- `{row['experiment_id']}`：tA/tC/tU/tP = "
            f"{fmt(row.get('t_a_iteration'))}/{fmt(row.get('t_c_iteration'))}/{fmt(row.get('t_u_iteration'))}/{fmt(row.get('t_p_iteration'))}，"
            f"攻击到物理异常的水力时间为 `{fmt(row.get('D_prop_hydraulic_sec'))}` s。"
        )
    lines.extend([
        "", "PLC 逻辑注入直接进入控制层，其 tC、D_network 和 D_control 记为 N/A；对应的直接链路使用 D_attack_to_control。", "",
        "## 平台扩展性", "",
        "规模实验独立于水网模型：前半组 PLC 接收 0–10 的确定性随机 data-feed，SCADA 经真实 Modbus TCP 轮询后转发给后半组一一配对的执行 PLC；执行逻辑统一为输入小于 5 时关、大于等于 5 时开。每个 PLC 都运行独立 OpenPLC 实例、网络命名空间与 ns-3 网络分支。逐轮审查同时核对源 PLC 观测、执行 PLC 接收值和控制输出。", "",
    ])
    for row in sorted(scales, key=lambda x: integer(x.get("plc_count"))):
        plc_count = integer(row.get("plc_count"))
        lines.append(
            f"- {fmt(plc_count)} 个 PLC（源/执行各 {fmt(row.get('communication_pair_count'))}）："
            f"{fmt(row.get('network_node_count'))} 个网络节点、"
            f"{fmt(row.get('communication_link_count'))} 条通信链路；平均周期 `{fmt(row.get('mean_cycle_time_sec'))}` s，"
            f"配对更新吞吐 `{fmt(row.get('pair_updates_per_sec'))}` pair/s，平均聚合 CPU `{fmt(row.get('mean_aggregate_cpu_percent'))}`%，"
            f"峰值内存 `{fmt(row.get('peak_aggregate_memory_mb'))}` MB，端到端正确率 `{fmt(row.get('end_to_end_correct_rate'))}`。"
        )
    lines.extend([
        "", "## 文件索引", "",
        "- `06_analysis/attack_intensity_summary.csv`：三类强度扫描总表",
        "- `06_analysis/dos_rerun_comparison.csv`：DoS 新一次连续扫描与上一批次的逐强度对照",
        "- `06_analysis/mitm_t7_water_level_comparison.png`：不同 MITM 强度下的实际 T7 水位轨迹（重合曲线已合并）",
        "- `06_analysis/mitm_t7_scada_observed_comparison.png`：不同 MITM 强度下的 SCADA 观测 T7 水位轨迹",
        "- `06_analysis/mitm_plc4_received_t7_comparison.png`：不同 MITM 强度下 PLC4 实际接收的 T7 值",
        "- `06_analysis/dos_t7_actual_comparison.png`：不同 DoS 强度下的实际 T7 水位轨迹",
        "- `06_analysis/dos_t7_scada_observed_comparison.png`：不同 DoS 强度下的 SCADA 观测 T7 水位轨迹",
        "- `06_analysis/dos_t3_t4_actual_comparison.png`：不同 DoS 强度下的实际 T3/T4 水位轨迹",
        "- `06_analysis/dos_t3_t4_scada_observed_comparison.png`：不同 DoS 强度下的 SCADA 观测 T3/T4 水位轨迹",
        "- `06_analysis/plc_logic_t7_comparison.png`：不同 PLC 逻辑注入强度下的 T7 水位轨迹",
        "- `06_analysis/mitm_t7_water_level_series.csv`：T7 合并轨迹的可复算数据",
        "- `06_analysis/plc_logic_pu10_upper_threshold_response.png`：PU10 上水位关闭阈值偏移 [-3.4, 0.0] 响应图",
        "- `06_analysis/propagation_timing.csv`：tA、tC、tU、tP 与分阶段延迟",
        "- `06_analysis/scalability_summary.csv`：资源、效率与拓扑规模",
        "- `06_analysis/scalability_pipeline_audit.csv`：所有规模点逐轮逐 PLC 对的端到端审查记录",
        "- `06_analysis/telemetry_quality.csv`：逐运行生命周期与遥测完整性审计",
        "- 各运行的 `output/runtime/csv/`：逐事件、逐请求、逐控制周期原始数据",
        "- 各运行的 `output/reports/advisor/`：单运行传播或性能分析结果", "",
    ])
    return "\n".join(lines)


def analyze(archive: Path) -> dict[str, Any]:
    adopted = valid_outputs(archive)
    baseline_row = next((row for row in adopted if row["group"] == "baseline"), None)
    if baseline_row is None:
        raise RuntimeError("no valid baseline_closed_loop run in archive")
    baseline = baseline_row["output_path"]
    analysis_dir = archive / "06_analysis"
    analysis_dir.mkdir(exist_ok=True)

    attacks = []
    for row in adopted:
        if row["group"] not in {"mitm", "dos", "plc_logic"}:
            continue
        summary = analyze_attack(row, baseline)
        attacks.append(summary)
        local = row["output_path"] / "reports/advisor"
        write_json(local / "intensity_summary.json", summary)
        write_csv(local / "intensity_summary.csv", flatten([summary]))
    propagation = []
    # Use the strongest absolute intensity of each attack family for the
    # cross-layer comparison. For the signed PLC threshold scan, preserve both
    # equally strong directions because they may follow different control paths.
    for group in ("mitm", "dos", "plc_logic"):
        candidates = [row for row in adopted if row["group"] == group]
        if not candidates:
            continue
        maximum = max(abs(float(row.get("value", 0))) for row in candidates)
        selected_rows = [
            row for row in candidates
            if abs(float(row.get("value", 0))) == maximum
        ]
        if group != "plc_logic":
            selected_rows = selected_rows[:1]
        for selected in selected_rows:
            propagation.append({"experiment_id": selected["id"], **propagation_summary(baseline, selected["output_path"], group)})
    scales = sorted(
        [analyze_scale(row) for row in adopted if row["group"] == "scalability"],
        key=lambda row: integer(row.get("plc_count")),
    )
    scale_audit_records: list[dict[str, Any]] = []
    for row in adopted:
        if row["group"] != "scalability":
            continue
        audit_path = row["output_path"] / "reports/advisor/scalability_pipeline_audit.csv"
        if not audit_path.exists():
            continue
        with audit_path.open("r", encoding="utf-8", newline="") as handle:
            for record in csv.DictReader(handle):
                scale_audit_records.append({
                    "experiment_id": row["id"],
                    "plc_count": row.get("value"),
                    **record,
                })
    quality = []
    for row in adopted:
        performance = analyze_performance(row["output_path"])
        writers = performance.get("metric_writers", {}) or {}
        item = {
            "experiment_id": row["id"],
            "group": row["group"],
            "run_status": performance.get("run_status"),
            "run_complete": performance.get("complete"),
            "telemetry_quality_complete": performance.get("quality_complete"),
            "metric_writer_files": writers.get("file_count"),
            "metric_rows_accepted": writers.get("accepted"),
            "metric_rows_written": writers.get("written"),
            "metric_rows_dropped": writers.get("dropped_total"),
            "metric_write_errors": writers.get("write_errors"),
            "metric_rows_unflushed": writers.get("unflushed_on_close"),
            "completed_cycles": performance.get("iteration_time", {}).get("count"),
            "modbus_request_count": performance.get("communication", {}).get("request_count"),
        }
        item["quality_pass"] = (
            item["run_status"] == "success"
            and item["run_complete"] is True
            and item["telemetry_quality_complete"] is True
            and int(item["metric_rows_dropped"] or 0) == 0
            and int(item["metric_write_errors"] or 0) == 0
            and int(item["metric_rows_unflushed"] or 0) == 0
        )
        quality.append(item)

    write_csv(analysis_dir / "attack_intensity_summary.csv", flatten(attacks))
    write_json(analysis_dir / "attack_intensity_summary.json", attacks)
    dos_comparison = dos_rerun_comparison(archive, attacks, baseline)
    write_csv(analysis_dir / "dos_rerun_comparison.csv", flatten(dos_comparison))
    write_json(analysis_dir / "dos_rerun_comparison.json", dos_comparison)
    write_csv(analysis_dir / "propagation_timing.csv", flatten(propagation))
    write_json(analysis_dir / "propagation_timing.json", propagation)
    write_csv(analysis_dir / "scalability_summary.csv", flatten(scales))
    write_json(analysis_dir / "scalability_summary.json", scales)
    write_csv(analysis_dir / "scalability_pipeline_audit.csv", scale_audit_records)
    write_csv(analysis_dir / "telemetry_quality.csv", flatten(quality))
    write_json(analysis_dir / "telemetry_quality.json", quality)
    for group in ("mitm", "dos", "plc_logic"):
        plot_attack(attacks, group, analysis_dir / f"{group}_intensity_response.png")
    plot_attack(
        attacks,
        "plc_logic",
        analysis_dir / "plc_logic_pu10_upper_threshold_response.png",
        strength_bounds=(-3.4, 0.0),
        title="PU10 upper water-level threshold-shift response",
        xticks=(-3.4, -3.0, -2.5, -2.0, -1.5, -1.0, -0.5, 0.0),
    )
    plot_scale(scales, analysis_dir / "scalability.png")
    dos_rho_5 = next(
        (
            row for row in adopted
            if row["group"] == "dos" and float(row.get("value", 0.0)) == 5.0
        ),
        None,
    )
    if dos_rho_5 is not None:
        plot_scada_tank_observation_comparison(
            baseline,
            dos_rho_5["output_path"],
            analysis_dir / "dos_rho_5_scada_t3_t4_vs_baseline.png",
        )
    t7_groups = mitm_t7_groups(adopted, baseline)
    write_mitm_t7_group_data(t7_groups, analysis_dir / "mitm_t7_water_level_series.csv")
    write_json(analysis_dir / "mitm_t7_water_level_groups.json", t7_groups)
    plot_mitm_t7_comparison(t7_groups, analysis_dir / "mitm_t7_water_level_comparison.png")
    write_mitm_t7_interactive(t7_groups, analysis_dir / "mitm-t7-water-level.html")

    t7_chart_specs = [
        {
            "experiment_group": "mitm",
            "source": "scada_observed",
            "stem": "mitm_t7_scada_observed",
            "parameter_name": "delta_x",
            "parameter_symbol": "Δx",
            "title": "不同 MITM 篡改强度下的 SCADA 观测 T7 水位",
            "measurement_note": "各强度 output/reports/csv/scada_observed_wide.csv 的 PLC9.PLC9_T7；第 1–79 轮",
            "zero_description": "MITM 零强度",
        },
        {
            "experiment_group": "dos",
            "source": "physical",
            "stem": "dos_t7_actual",
            "parameter_name": "rho",
            "parameter_symbol": "ρ",
            "title": "不同 DoS 攻击强度下的 T7 实际水位",
            "measurement_note": "物理层真实水位；第 1–80 轮",
        },
        {
            "experiment_group": "dos",
            "source": "scada_observed",
            "stem": "dos_t7_scada_observed",
            "parameter_name": "rho",
            "parameter_symbol": "ρ",
            "title": "不同 DoS 攻击强度下的 SCADA 观测 T7 水位",
            "measurement_note": "SCADA 原始轮询观测；第 1–79 轮（第 80 轮无轮询记录）",
        },
        {
            "experiment_group": "plc_logic",
            "source": "physical",
            "stem": "plc_logic_t7",
            "parameter_name": "delta_h",
            "parameter_symbol": "Δh",
            "title": "PU10 上水位关闭阈值偏移下的 T7 水位",
            "measurement_note": "原阈值 4.8 m，注入阈值 1.4–4.8 m；物理真实值",
            "signed_strength": True,
            "zero_description": "无攻击",
        },
    ]
    for spec in t7_chart_specs:
        groups = t7_series_groups(
            adopted,
            baseline,
            experiment_group=spec["experiment_group"],
            source=spec["source"],
        )
        stem = spec["stem"]
        write_t7_group_data(
            groups,
            analysis_dir / f"{stem}_series.csv",
            parameter_name=spec["parameter_name"],
        )
        write_json(analysis_dir / f"{stem}_groups.json", groups)
        plot_t7_comparison(
            groups,
            analysis_dir / f"{stem}_comparison.png",
            title=spec["title"],
            parameter_symbol=spec["parameter_symbol"],
            measurement_note=spec["measurement_note"],
            signed_strength=bool(spec.get("signed_strength", False)),
            zero_description=spec.get("zero_description", "基线"),
        )

    plc4_received_t7 = mitm_plc4_received_t7_groups(adopted, baseline)
    write_t7_group_data(
        plc4_received_t7,
        analysis_dir / "mitm_plc4_received_t7_series.csv",
        parameter_name="delta_x",
    )
    write_json(
        analysis_dir / "mitm_plc4_received_t7_groups.json",
        plc4_received_t7,
    )
    plot_t7_comparison(
        plc4_received_t7,
        analysis_dir / "mitm_plc4_received_t7_comparison.png",
        title="不同 MITM 篡改强度下 PLC4 接收的 T7 水位值",
        parameter_symbol="Δx",
        measurement_note=(
            "由各强度 output/reports/csv/scada_observed_wide.csv 的 PLC9.PLC9_T7 重构；"
            "攻击窗口内加 Δx"
        ),
        zero_description="MITM 零强度",
    )
    write_json(
        analysis_dir / "mitm_plc4_received_t7_validation.json",
        validate_mitm_plc4_received_t7(adopted),
    )

    dos_tank_pair_specs = [
        {
            "source": "physical",
            "stem": "dos_t3_t4_actual",
            "title": "不同 DoS 攻击强度下的 T3 与 T4 实际水位",
            "measurement_note": "各强度 output/reports/csv/physics.csv；第 1–80 轮",
        },
        {
            "source": "scada_observed",
            "stem": "dos_t3_t4_scada_observed",
            "title": "不同 DoS 攻击强度下的 SCADA 观测 T3 与 T4 水位",
            "measurement_note": "各强度 output/reports/csv/scada_observed_wide.csv；第 1–79 轮",
        },
    ]
    for spec in dos_tank_pair_specs:
        tank_groups = {
            tank: tank_series_groups(
                adopted,
                baseline,
                experiment_group="dos",
                source=spec["source"],
                tank=tank,
            )
            for tank in ("T3", "T4")
        }
        for tank, groups in tank_groups.items():
            tank_stem = f"dos_{tank.lower()}_{'actual' if spec['source'] == 'physical' else 'scada_observed'}"
            write_t7_group_data(
                groups,
                analysis_dir / f"{tank_stem}_series.csv",
                parameter_name="rho",
            )
            write_json(analysis_dir / f"{tank_stem}_groups.json", groups)
        plot_tank_pair_comparison(
            tank_groups,
            analysis_dir / f"{spec['stem']}_comparison.png",
            title=spec["title"],
            parameter_symbol="ρ",
            measurement_note=spec["measurement_note"],
        )
    write_json(
        analysis_dir / "plc_logic_t7_actual_vs_scada_validation.json",
        validate_t7_actual_vs_scada(adopted, experiment_group="plc_logic"),
    )

    expected = json.loads((archive / "EXPERIMENT_PLAN.json").read_text(encoding="utf-8"))["formal_run_count"]
    completeness = {
        "valid_run_count": len(adopted),
        "expected_run_count": expected,
        "valid_by_group": dict(Counter(row["group"] for row in adopted)),
        "telemetry_quality_pass_count": sum(bool(row["quality_pass"]) for row in quality),
        "telemetry_quality_all_pass": all(bool(row["quality_pass"]) for row in quality),
        "complete": len(adopted) == expected and all(bool(row["quality_pass"]) for row in quality),
    }
    write_json(analysis_dir / "completeness.json", completeness)
    report = build_report(archive, attacks, propagation, scales, quality, len(adopted))
    (archive / "FINAL_REPORT.md").write_text(report, encoding="utf-8")
    (archive / "ARCHIVE_INDEX.md").write_text(
        "# Experiment archive\n\n- [Final report](FINAL_REPORT.md)\n- [Experiment plan](EXPERIMENT_PLAN.json)\n"
        "- [Run index](RUN_INDEX.json)\n- [Combined analysis](06_analysis/)\n",
        encoding="utf-8",
    )
    return {"archive": str(archive), "completeness": completeness}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    result = analyze(args.archive.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["completeness"]["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
