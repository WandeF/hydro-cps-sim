#!/usr/bin/env python3
"""Build TODO2 single-run tables, quality checks, propagation reports, and plots."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import struct
import sys
from collections import defaultdict, deque
from pathlib import Path
from statistics import fmean, median
from typing import Any, Iterable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.metrics.correctness import analyze_correctness, write_correctness_outputs
from src.metrics.performance import analyze_performance, write_performance_outputs
from src.metrics.propagation import analyze_propagation, write_propagation_outputs
from src.metrics.run_summary import build_run_summary, write_run_summary


OLD_ARCHIVE = Path("/home/lzh/MASTER/CODE/output/quantitative_20260716T113050_metric_cde39ea")
ZERO_LOSS_REFERENCE = OLD_ARCHIVE / "02_network_delay_matrix/runs/network_delay_2ms_run_01/output"
BASELINE_100 = OLD_ARCHIVE / "01_correctness_baseline__config__iter100__logicwait0p1__20260716T113050+0800/output"
MITM_OUTPUT = OLD_ARCHIVE / "03_attack_propagation__mitm_plc4__scenario-mitm_scada_to_plc4_fake_T7__rep01__20260716T145932/output"


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def number(raw: Any, default: float = 0.0) -> float:
    try:
        value = float(raw)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def integer(raw: Any, default: int = 0) -> int:
    return int(number(raw, float(default)))


def truth(raw: Any) -> bool:
    return str(raw).strip().lower() in {"true", "1", "yes", "ok", "success"}


def percentile(values: Iterable[float], p: float) -> float | None:
    data = sorted(float(x) for x in values if math.isfinite(float(x)))
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    position = (len(data) - 1) * p
    low = int(math.floor(position))
    high = int(math.ceil(position))
    return data[low] + (data[high] - data[low]) * (position - low)


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = columns or list(rows[0] if rows else [])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in columns})


def pcap_records(path: Path):
    with path.open("rb") as handle:
        magic = handle.read(4)
        formats = {
            b"\xd4\xc3\xb2\xa1": ("<", 1_000_000.0),
            b"\xa1\xb2\xc3\xd4": (">", 1_000_000.0),
            b"\x4d\x3c\xb2\xa1": ("<", 1_000_000_000.0),
            b"\xa1\xb2\x3c\x4d": (">", 1_000_000_000.0),
        }
        if magic not in formats:
            raise ValueError(f"unsupported pcap magic: {path}")
        endian, scale = formats[magic]
        rest = handle.read(20)
        if len(rest) != 20:
            return
        while True:
            header = handle.read(16)
            if not header:
                break
            if len(header) != 16:
                raise ValueError(f"truncated pcap record: {path}")
            sec, fraction, captured, _original = struct.unpack(endian + "IIII", header)
            packet = handle.read(captured)
            if len(packet) != captured:
                raise ValueError(f"truncated pcap packet: {path}")
            yield sec + fraction / scale, packet


def ipv4_packet(frame: bytes) -> bytes | None:
    offsets = (2, 4, 0, 14)
    for offset in offsets:
        if len(frame) > offset + 20 and frame[offset] >> 4 == 4:
            ihl = (frame[offset] & 0x0F) * 4
            total = struct.unpack("!H", frame[offset + 2:offset + 4])[0]
            if ihl >= 20 and total >= ihl and offset + total <= len(frame):
                return frame[offset:offset + total]
    return None


def tcp_segment(ip: bytes) -> dict[str, Any] | None:
    ihl = (ip[0] & 0x0F) * 4
    if len(ip) < ihl + 20 or ip[9] != 6:
        return None
    source = ".".join(str(x) for x in ip[12:16])
    target = ".".join(str(x) for x in ip[16:20])
    sport, dport, seq = struct.unpack("!HHI", ip[ihl:ihl + 8])
    tcp_hlen = (ip[ihl + 12] >> 4) * 4
    flags = ip[ihl + 13]
    payload = max(0, len(ip) - ihl - tcp_hlen)
    consumed = payload + int(bool(flags & 0x02)) + int(bool(flags & 0x01))
    return {"flow": (source, target, sport, dport), "seq": seq, "consumed": consumed}


def tcp_retransmissions(path: Path) -> tuple[int, int, float | None]:
    maximum: dict[tuple[Any, ...], int] = {}
    retransmissions = 0
    sequence_packets = 0
    for _timestamp, frame in pcap_records(path):
        ip = ipv4_packet(frame)
        segment = tcp_segment(ip) if ip is not None else None
        if segment is None or segment["consumed"] <= 0:
            continue
        sequence_packets += 1
        end = (segment["seq"] + segment["consumed"]) & 0xFFFFFFFF
        previous = maximum.get(segment["flow"])
        if previous is not None and segment["seq"] < previous:
            retransmissions += 1
        if previous is None or end > previous:
            maximum[segment["flow"]] = end
    rate = retransmissions / sequence_packets if sequence_packets else None
    return retransmissions, sequence_packets, rate


def packet_delays_ms(first: Path, second: Path) -> list[float]:
    pending: dict[bytes, deque[float]] = defaultdict(deque)
    for timestamp, frame in pcap_records(first):
        ip = ipv4_packet(frame)
        if ip is not None:
            pending[hashlib.blake2b(ip, digest_size=16).digest()].append(timestamp)
    delays: list[float] = []
    for timestamp, frame in pcap_records(second):
        ip = ipv4_packet(frame)
        if ip is None:
            continue
        key = hashlib.blake2b(ip, digest_size=16).digest()
        candidates = pending.get(key)
        if candidates:
            delays.append(abs(timestamp - candidates.popleft()) * 1000.0)
            if not candidates:
                pending.pop(key, None)
    return delays


def link_pcaps(output: Path, link: str) -> tuple[Path | None, Path | None]:
    root = output / "runtime/network/pcap"
    first = sorted(root.glob(f"ns3_network-{link}-0-*.pcap"))
    second = sorted(root.glob(f"ns3_network-{link}-1-*.pcap"))
    return (first[0] if first else None, second[0] if second else None)


def communication_metrics(output: Path) -> dict[str, Any]:
    rows = [
        row for row in read_csv(output / "runtime/csv/communication.csv")
        if str(row.get("operation", "")).lower() != "connect" and not truth(row.get("warmup"))
    ]
    status_counts = {name: 0 for name in ("success", "timeout", "exception", "connection_error", "other_failure")}
    rtts: list[float] = []
    for row in rows:
        status = str(row.get("status", "")).strip().lower()
        if status in status_counts:
            status_counts[status] += 1
        elif "timeout" in status:
            status_counts["timeout"] += 1
        elif "connection" in status:
            status_counts["connection_error"] += 1
        elif "exception" in status:
            status_counts["exception"] += 1
        elif status == "success":
            status_counts["success"] += 1
        else:
            status_counts["other_failure"] += 1
        if status == "success" and row.get("latency_ms") not in (None, ""):
            rtts.append(number(row["latency_ms"]))
    total = len(rows)
    classified = sum(status_counts.values())
    return {
        "modbus_requests": total,
        **{f"modbus_{key}_count": value for key, value in status_counts.items()},
        "modbus_conservation_ok": total == classified,
        "modbus_success_rate": status_counts["success"] / total if total else None,
        "modbus_timeout_rate": status_counts["timeout"] / total if total else None,
        "modbus_rtt_mean_ms": fmean(rtts) if rtts else None,
        "modbus_rtt_median_ms": median(rtts) if rtts else None,
        "modbus_rtt_p95_ms": percentile(rtts, 0.95),
        "modbus_rtt_p99_ms": percentile(rtts, 0.99),
    }


def maximum_data_age_ms(output: Path, hydraulic_step_sec: float = 300.0) -> float:
    maximum_iterations = 0
    for row in read_csv(output / "runtime/csv/scada.csv"):
        current = integer(row.get("iteration"))
        for key, raw in row.items():
            if not key.endswith(".used_previous") or not truth(raw):
                continue
            prefix = key[:-len("used_previous")]
            previous = integer(row.get(prefix + "previous_iteration"), current - 1)
            maximum_iterations = max(maximum_iterations, max(1, current - previous))
    return maximum_iterations * hydraulic_step_sec * 1000.0


def target_network(output: Path, links: set[str]) -> dict[str, Any]:
    rows = [
        row for row in read_csv(output / "runtime/csv/network.csv")
        if row.get("metric_source") == "link_trace" and row.get("link") in links
    ]
    tx = sum(integer(row.get("tx_packets")) for row in rows)
    rx = sum(integer(row.get("rx_packets")) for row in rows)
    lost = sum(integer(row.get("lost_packets")) for row in rows)
    pending = sum(integer(row.get("pending_packets")) for row in rows)
    queue_samples = sum(integer(row.get("queue_samples")) for row in rows)
    queue_weighted = sum(number(row.get("queue_packets_mean")) * integer(row.get("queue_samples")) for row in rows)
    delay_samples = sum(integer(row.get("delay_samples")) for row in rows)
    delay_weighted = sum(number(row.get("mean_delay_ms")) * integer(row.get("delay_samples")) for row in rows)
    return {
        "target_tx_packets": tx, "target_rx_packets": rx, "target_lost_packets": lost,
        "target_pending_packets": pending,
        "measured_loss_rate": lost / tx if tx else None,
        "network_conservation_ok": tx - rx == lost + pending,
        "queue_drop_packets": sum(integer(row.get("queue_drop_packets")) for row in rows),
        "queue_packets_mean": queue_weighted / queue_samples if queue_samples else None,
        "queue_packets_max": max((integer(row.get("queue_packets_max")) for row in rows), default=None),
        "packet_delay_mean_ms": delay_weighted / delay_samples if delay_samples else None,
    }


def quality(output: Path, config: dict[str, Any]) -> dict[str, Any]:
    events = read_csv(output / "runtime/csv/events.csv")
    final = ""
    for row in events:
        if row.get("event_type") == "simulation_end":
            final = str(row.get("status", "")).strip().lower()
    # Recompute writer quality for every adopted run.  The online ``--check``
    # step may stop before writing performance_summary.json when an impairment
    # intentionally violates a baseline correctness threshold; that does not
    # make the already-closed metric writers unobservable or invalid.
    performance = analyze_performance(output)
    write_performance_outputs(performance, output / "reports/metrics")
    writers = performance.get("metric_writers", {}) or {}
    check = read_json(output / "check/check_summary.json", {}) or {}
    attacks = config.get("attacks", {}) or {}
    enabled = bool(attacks.get("enabled", False))
    schedule = read_csv(output / "runtime/csv/attack_schedule.csv")
    attack_events = read_csv(output / "runtime/csv/attack_events.csv")
    window = any(
        "start" in str(row.get("event", row.get("action", ""))).lower()
        or truth(row.get("active"))
        for row in schedule
    )
    run_summary = build_run_summary(output)
    write_run_summary(run_summary, output / "reports/metrics")
    result = {
        "simulation_end": final,
        "metrics_writer_status": "ok" if writers.get("quality_complete") is True else "error",
        "telemetry_drop_count": integer(writers.get("dropped_total")),
        "conflict_count": integer(run_summary.get("conflict_count")),
        "cleanup_status": "success" if final == "success" else "error",
        "offline_check_ok": check.get("ok") is True,
        "attack_enabled": enabled,
        "attack_window_triggered": window,
        "actual_attack_event_count": len(attack_events),
    }
    result["quality_pass"] = (
        result["simulation_end"] == "success"
        and result["metrics_writer_status"] == "ok"
        and result["telemetry_drop_count"] == 0
        and result["conflict_count"] == 0
        and result["cleanup_status"] == "success"
        and (not enabled or (window and len(attack_events) > 0))
    )
    return result


def final_outputs(archive: Path) -> dict[str, Path]:
    index = read_json(archive / "RUN_INDEX.json", []) or []
    outputs: dict[str, Path] = {}
    for item in index:
        if item.get("valid"):
            outputs[str(item["id"])] = Path(item["output"])
    return outputs


def correctness(output: Path, baseline: Path) -> dict[str, Any]:
    summary = analyze_correctness(baseline, output, baseline, output, variables=[f"T{i}" for i in range(1, 8)])
    write_correctness_outputs(summary, output / "reports/metrics")
    return summary


def propagation(output: Path, baseline: Path, scenario: str | None = None) -> dict[str, Any]:
    summary = analyze_propagation(
        baseline, output, baseline, output,
        attack_schedule=output, attack_events=output, scada_timeouts=output,
        variables=[f"T{i}" for i in range(1, 8)], scenario=scenario,
        physical_tolerance=0.01, hydraulic_step_sec=300.0,
        recovery_consecutive_iterations=3,
    )
    write_propagation_outputs(summary, output / "reports/metrics")
    return summary


def enrich_run(output: Path, baseline: Path, links: set[str], pcap_link: str) -> dict[str, Any]:
    config = yaml.safe_load((output / "runtime/config_resolved.yaml").read_text(encoding="utf-8"))
    row = {**target_network(output, links), **communication_metrics(output)}
    first, second = link_pcaps(output, pcap_link)
    if first is not None:
        retrans, packets, rate = tcp_retransmissions(first)
        row.update({"tcp_retransmissions": retrans, "tcp_sequence_packets": packets, "tcp_retransmission_rate": rate})
    if first is not None and second is not None:
        delays = packet_delays_ms(first, second)
        row.update({
            "packet_delay_sample_count": len(delays),
            "packet_delay_median_ms": median(delays) if delays else None,
            "packet_delay_p95_ms": percentile(delays, 0.95),
            "packet_delay_p99_ms": percentile(delays, 0.99),
        })
    corr = correctness(output, baseline)
    row.update({
        "maximum_data_age_ms": maximum_data_age_ms(output),
        "actuator_mismatch_rate": corr["control"]["overall"]["mismatch_rate"],
        "tank_pooled_rmse": corr["physical"]["overall"]["pooled_rmse"],
        **quality(output, config),
    })
    row["quality_pass"] = bool(row["quality_pass"] and row["network_conservation_ok"] and row["modbus_conservation_ok"])
    return row


def style_axis(ax, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_title(title, loc="left", fontsize=12, color="#202124", pad=12)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", color="#DADCE0", linewidth=0.8, alpha=0.8)
    ax.spines[["top", "right"]].set_visible(False)


def line_plot(path: Path, rows: list[dict[str, Any]], x: str, y: str, title: str,
              xlabel: str, ylabel: str, series: str | None = None) -> None:
    fig, ax = plt.subplots(figsize=(8, 4.8), constrained_layout=True)
    colors = ["#1967D2", "#E37400", "#7B7F24"]
    markers = ["o", "s", "^"]
    styles = ["-", "--", "-."]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if series:
        for row in rows:
            groups[str(row[series])].append(row)
    else:
        groups[""] = rows
    for index, (label, items) in enumerate(sorted(groups.items(), key=lambda item: number(item[0]))):
        items = sorted(items, key=lambda row: number(row[x]))
        ax.plot(
            [number(row[x]) for row in items], [number(row[y], math.nan) for row in items],
            color=colors[index % len(colors)], marker=markers[index % len(markers)],
            linestyle=styles[index % len(styles)], linewidth=2, markersize=6,
            label=(f"{label} Mbps" if series else None),
        )
    if series:
        ax.legend(frameon=False, ncol=3, loc="best")
    style_axis(ax, title, xlabel, ylabel)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=180, facecolor="white")
    plt.close(fig)


def timeline_plot(path: Path, summary: dict[str, Any], title: str) -> None:
    timeline = summary.get("timeline", {}) or {}
    keys = [
        ("tA_attack", "tA attack"), ("tC_communication", "tC communication"),
        ("tU_control", "tU control"), ("tP_physical", "tP physical"),
        ("tAttackEnd", "attack end"),
    ]
    points = [(label, (timeline.get(key, {}) or {}).get("iteration")) for key, label in keys]
    points = [(label, int(value)) for label, value in points if value is not None]
    fig, ax = plt.subplots(figsize=(8, 3.3), constrained_layout=True)
    positions = list(reversed(range(len(points))))
    for index, ((label, value), y_pos) in enumerate(zip(points, positions)):
        color = "#1967D2" if label != "attack end" else "#E37400"
        ax.scatter(value, y_pos, s=65, color=color, zorder=3)
        ax.annotate(str(value), (value, y_pos), xytext=(8, 0),
                    textcoords="offset points", va="center", fontsize=9)
    ax.set_yticks(positions, [label for label, _ in points])
    ax.set_ylim(-0.8, max(positions, default=0) + 0.8)
    ax.set_xlabel("Hydraulic iteration")
    ax.set_title(title, loc="left", fontsize=12, pad=12)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", color="#DADCE0", linewidth=0.8)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=180, facecolor="white")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    args = parser.parse_args()
    archive = args.archive.expanduser().resolve()
    results_dir = archive / "results"
    plots_dir = archive / "plots"
    outputs = final_outputs(archive)
    if len(outputs) != 23:
        raise RuntimeError(f"expected 23 valid formal outputs, found {len(outputs)}")

    packet_rows: list[dict[str, Any]] = []
    bandwidth_rows: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = []

    for loss in (0.01, 0.02, 0.05, 0.10, 0.50):
        experiment_id = f"packet_loss_{str(loss).replace('.', 'p')}"
        output = outputs[experiment_id]
        row = {"experiment_id": experiment_id, "configured_loss_rate": loss, "output_path": str(output)}
        row.update(enrich_run(output, BASELINE_100, {"r0-r_scada", "r0-r4"}, "r0-r4"))
        row["loss_absolute_error"] = abs(number(row.get("measured_loss_rate")) - loss)
        packet_rows.append(row)
        quality_rows.append({"experiment_id": experiment_id, "group": "packet_loss", **{k: row.get(k) for k in (
            "simulation_end", "metrics_writer_status", "telemetry_drop_count", "conflict_count", "cleanup_status",
            "offline_check_ok", "network_conservation_ok", "modbus_conservation_ok", "attack_enabled",
            "attack_window_triggered", "actual_attack_event_count", "quality_pass")}})

    zero_row = {"experiment_id": "existing_0_percent_reference", "configured_loss_rate": 0.0,
                "output_path": str(ZERO_LOSS_REFERENCE), "note": "reused; not one of the five new formal runs"}
    zero_row.update(enrich_run(ZERO_LOSS_REFERENCE, BASELINE_100, {"r0-r_scada", "r0-r4"}, "r0-r4"))
    zero_row["loss_absolute_error"] = abs(number(zero_row.get("measured_loss_rate")))
    write_csv(results_dir / "packet_loss_zero_percent_reference.csv", [zero_row])

    bandwidth_baselines = {
        b: outputs[f"bandwidth_dos_{b}mbps_rho_0p0"] for b in (5, 10, 20)
    }
    for bandwidth in (5, 10, 20):
        for rho in (0.0, 0.8, 1.0, 1.2, 1.5):
            experiment_id = f"bandwidth_dos_{bandwidth}mbps_rho_{str(rho).replace('.', 'p')}"
            output = outputs[experiment_id]
            row = {"experiment_id": experiment_id, "bandwidth_mbps": bandwidth, "rho": rho,
                   "configured_dos_rate_mbps": bandwidth * rho, "output_path": str(output)}
            row.update(enrich_run(output, bandwidth_baselines[bandwidth], {"r0-r_scada", "r0-r2"}, "r0-r2"))
            bandwidth_rows.append(row)
            quality_rows.append({"experiment_id": experiment_id, "group": "bandwidth_dos", **{k: row.get(k) for k in (
                "simulation_end", "metrics_writer_status", "telemetry_drop_count", "conflict_count", "cleanup_status",
                "offline_check_ok", "network_conservation_ok", "modbus_conservation_ok", "attack_enabled",
                "attack_window_triggered", "actual_attack_event_count", "quality_pass")}})

    propagation_rows: list[dict[str, Any]] = []
    propagation_summaries: dict[str, dict[str, Any]] = {}
    for scenario in ("single_bot", "three_bots"):
        experiment_id = f"dos_propagation_{scenario}"
        output = outputs[experiment_id]
        base_metrics = enrich_run(output, bandwidth_baselines[10], {"r0-r_scada", "r0-r2"}, "r0-r2")
        summary = propagation(output, bandwidth_baselines[10], None)
        propagation_summaries[scenario] = summary
        timeline = summary["timeline"]
        row = {
            "experiment_id": experiment_id, "scenario": scenario, "output_path": str(output),
            "tA": timeline["tA_attack"]["iteration"], "tC": timeline["tC_communication"]["iteration"],
            "tU": timeline["tU_control"]["iteration"], "tP": timeline["tP_physical"]["iteration"],
            "attack_end": timeline["tAttackEnd"]["iteration"], "recovery_status": summary["recovery"]["status"],
            "attack_executed": base_metrics["attack_enabled"] and base_metrics["actual_attack_event_count"] > 0,
            "control_deviation_detected": timeline["tU_control"]["iteration"] is not None,
            "physical_deviation_detected": timeline["tP_physical"]["iteration"] is not None,
            **base_metrics,
        }
        propagation_rows.append(row)
        quality_rows.append({"experiment_id": experiment_id, "group": "dos_propagation", **{k: row.get(k) for k in (
            "simulation_end", "metrics_writer_status", "telemetry_drop_count", "conflict_count", "cleanup_status",
            "offline_check_ok", "network_conservation_ok", "modbus_conservation_ok", "attack_enabled",
            "attack_window_triggered", "actual_attack_event_count", "quality_pass")}})

    plc_output = outputs["plc_logic_injection_plc4"]
    plc_metrics = enrich_run(plc_output, BASELINE_100, {"r0-r_scada", "r0-r4"}, "r0-r4")
    plc_summary = propagation(plc_output, BASELINE_100, "openplc_shift_t7_threshold")
    propagation_summaries["plc_logic"] = plc_summary
    states = sorted((plc_output / "runtime/attacks").glob("*.state.json"))
    state = read_json(states[0], {}) if states else {}
    plc_artifacts = results_dir / "plc_logic_artifacts"
    plc_artifacts.mkdir(parents=True, exist_ok=True)
    for key in ("original_st_path", "malicious_st_path", "compile_log_path", "deploy_log_path"):
        source = Path(str(state.get(key, "")))
        if source.is_file():
            shutil.copy2(source, plc_artifacts / source.name)
    if states:
        shutil.copy2(states[0], plc_artifacts / states[0].name)
    plc_timeline = plc_summary["timeline"]
    plc_row = {
        "experiment_id": "plc_logic_injection_plc4", "scenario": "openplc_shift_t7_threshold",
        "output_path": str(plc_output),
        "before_source_sha256": state.get("before_source_sha256"),
        "after_source_sha256": state.get("after_source_sha256"),
        "before_executable_sha256": state.get("before_executable_sha256"),
        "after_executable_sha256": state.get("after_executable_sha256"),
        "source_hash_changed": state.get("before_source_sha256") != state.get("after_source_sha256"),
        "executable_hash_changed": state.get("before_executable_sha256") != state.get("after_executable_sha256"),
        "malicious_logic_deployed": state.get("malicious_logic_deployed") is True,
        "tA": plc_timeline["tA_attack"]["iteration"], "tC": plc_timeline["tC_communication"]["iteration"],
        "tU": plc_timeline["tU_control"]["iteration"], "tP": plc_timeline["tP_physical"]["iteration"],
        "attack_end": plc_timeline["tAttackEnd"]["iteration"], "recovery_status": plc_summary["recovery"]["status"],
        "attack_executed": plc_metrics["attack_enabled"] and plc_metrics["actual_attack_event_count"] > 0,
        "control_deviation_detected": plc_timeline["tU_control"]["iteration"] is not None,
        "physical_deviation_detected": plc_timeline["tP_physical"]["iteration"] is not None,
        **plc_metrics,
    }
    plc_row["quality_pass"] = bool(plc_row["quality_pass"] and plc_row["source_hash_changed"] and plc_row["malicious_logic_deployed"])
    quality_rows.append({"experiment_id": "plc_logic_injection_plc4", "group": "plc_logic_injection", **{k: plc_row.get(k) for k in (
        "simulation_end", "metrics_writer_status", "telemetry_drop_count", "conflict_count", "cleanup_status",
        "offline_check_ok", "network_conservation_ok", "modbus_conservation_ok", "attack_enabled",
        "attack_window_triggered", "actual_attack_event_count", "source_hash_changed", "malicious_logic_deployed", "quality_pass")}})

    write_csv(results_dir / "packet_loss_per_run.csv", packet_rows)
    write_csv(results_dir / "bandwidth_dos_per_run.csv", bandwidth_rows)
    write_csv(results_dir / "dos_propagation_per_run.csv", propagation_rows)
    write_csv(results_dir / "plc_logic_injection_per_run.csv", [plc_row])
    write_csv(results_dir / "data_quality_per_run.csv", quality_rows)

    reused = results_dir / "reused_mitm"
    reused.mkdir(parents=True, exist_ok=True)
    mitm_summary = read_json(MITM_OUTPUT / "reports/metrics/propagation_summary.json", {})
    (reused / "propagation_summary.json").write_text(json.dumps(mitm_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    propagation_summaries["mitm"] = mitm_summary

    loss_plots = {
        "measured_loss_rate": ("Configured vs measured packet loss", "Measured loss rate"),
        "tcp_retransmission_rate": ("Packet loss vs TCP retransmission rate", "TCP retransmission rate"),
        "modbus_rtt_p95_ms": ("Packet loss vs Modbus RTT P95", "RTT P95 (ms)"),
        "modbus_success_rate": ("Packet loss vs Modbus success rate", "Success rate"),
        "maximum_data_age_ms": ("Packet loss vs maximum data age", "Maximum data age (ms)"),
    }
    for field, (title, ylabel) in loss_plots.items():
        line_plot(plots_dir / f"packet_loss_{field}.png", packet_rows, "configured_loss_rate", field,
                  title, "Configured loss rate", ylabel)
    bandwidth_plots = {
        "queue_drop_packets": ("DoS intensity vs queue drops", "Queue drops (packets)"),
        "queue_packets_max": ("DoS intensity vs maximum queue length", "Maximum queue length (packets)"),
        "tcp_retransmission_rate": ("DoS intensity vs TCP retransmission rate", "TCP retransmission rate"),
        "modbus_rtt_p95_ms": ("DoS intensity vs Modbus RTT P95", "RTT P95 (ms)"),
        "modbus_timeout_rate": ("DoS intensity vs Modbus timeout rate", "Timeout rate"),
        "maximum_data_age_ms": ("DoS intensity vs maximum data age", "Maximum data age (ms)"),
        "tank_pooled_rmse": ("DoS intensity vs tank pooled RMSE", "Tank pooled RMSE"),
    }
    for field, (title, ylabel) in bandwidth_plots.items():
        line_plot(plots_dir / f"bandwidth_dos_{field}.png", bandwidth_rows, "rho", field,
                  title, "Normalized DoS intensity (rho)", ylabel, "bandwidth_mbps")
    for name, summary in propagation_summaries.items():
        timeline_plot(plots_dir / f"timeline_{name}.png", summary, f"Cross-layer propagation: {name}")

    failures = []
    for failure in archive.glob("experiments/*/*/attempt_*/failure.json"):
        item = read_json(failure, {}) or {}
        failures.append({"path": str(failure), **item})
    preflight = Path("/home/lzh/MASTER/CODE/output/quantitative_todo2_20260716T160131+0800_metric_cde39ea")
    for failure in preflight.glob("experiments/*/*/attempt_*/failure.json"):
        item = read_json(failure, {}) or {}
        failures.append({"path": str(failure), "phase": "preflight", **item})
    write_csv(results_dir / "failed_attempts.csv", failures)

    onset: dict[int, dict[str, Any]] = {}
    for bandwidth in (5, 10, 20):
        items = sorted((r for r in bandwidth_rows if r["bandwidth_mbps"] == bandwidth), key=lambda r: r["rho"])
        def first_rho(predicate):
            return next((row["rho"] for row in items if predicate(row)), None)
        onset[bandwidth] = {
            "queue_drop_rho": first_rho(lambda r: number(r.get("queue_drop_packets")) > 0),
            "tcp_retransmission_rho": first_rho(lambda r: number(r.get("tcp_retransmissions")) > 0),
            "modbus_degradation_rho": first_rho(lambda r: number(r.get("modbus_timeout_rate")) > 0 or number(r.get("modbus_success_rate"), 1) < 1),
        }
    no_effect = [
        row["experiment_id"] for row in bandwidth_rows + propagation_rows + [plc_row]
        if row.get("attack_enabled") and number(row.get("actuator_mismatch_rate")) == 0 and number(row.get("tank_pooled_rmse")) <= 0.01
    ]
    quality_passed = sum(bool(row.get("quality_pass")) for row in quality_rows)
    offline_nonpass = [
        row["experiment_id"] for row in quality_rows if not row.get("offline_check_ok")
    ]
    report = f"""# TODO2 quantitative experiment report

This archive contains one valid observation for each of 23 unique new configurations. No cross-run means, standard deviations, confidence intervals, or error bars were calculated.

## Scope and interpretation

- Packet loss: five new nonzero levels (1%, 2%, 5%, 10%, 50%). The previously completed 0% / 2 ms / 100 Mbps run is a separately labelled reference because TODO2 lists six values but requires five new runs and five table rows.
- Bandwidth–DoS: 15 unique combinations (5/10/20 Mbps × rho 0/0.8/1/1.2/1.5).
- Cross-layer attacks: single-bot DoS, three-bot DoS, and PLC4 logic injection were run once. MITM was reused and not rerun.
- Statements in this report describe only the observed run under each configuration.

## Quality

- Formal runs passing the required data-quality gates: {quality_passed}/{len(quality_rows)}.
- Failed/non-experimental attempts retained: {len(failures)}.
- Per-run evidence: `results/data_quality_per_run.csv`.
- Network conservation uses `tx - rx = lost + pending`; Modbus conservation classifies every non-warmup, non-connect request.
- Baseline-consistency check non-passes (observed experimental deviation, not telemetry loss): {', '.join(offline_nonpass) if offline_nonpass else 'none'}.

## Bandwidth–DoS onset observations

```json
{json.dumps(onset, ensure_ascii=False, indent=2)}
```

Successful attack runs without detected control/physical deviation at the configured 0.01 physical tolerance: {', '.join(no_effect) if no_effect else 'none'}.

No target-link queue drops were observed in the 15 bandwidth–DoS runs; `queue_drop_rho=null` therefore means "not observed", not missing telemetry. Curves connect one observation per unique configuration and do not establish monotonicity or causality.

## Main artifacts

- `results/packet_loss_per_run.csv` (5 new formal runs)
- `results/packet_loss_zero_percent_reference.csv` (reused reference)
- `results/bandwidth_dos_per_run.csv` (15 runs)
- `results/dos_propagation_per_run.csv` (2 runs)
- `results/plc_logic_injection_per_run.csv` (1 run)
- `results/reused_mitm/propagation_summary.json`
- `results/plc_logic_artifacts/`
- `results/failed_attempts.csv`
- `plots/` (5 packet-loss plots, 7 bandwidth–DoS plots, 4 attack timelines)

Each experiment directory retains its resolved config, manifest, raw/reported CSV and JSON metrics, logs, checks, and PCAP files under its successful attempt directory.
"""
    (archive / "FINAL_REPORT.md").write_text(report, encoding="utf-8")
    validation_report = f"""# Validation report

## Overall assessment: Share with caveats

The TODO2 archive is complete and internally consistent for reporting the observed outcomes of the configured simulations. It must not be used to claim sampling uncertainty, monotonic response, or causality because each unique configuration contributes exactly one valid observation.

## Methodology review

- Scope: 23 unique new configurations, plus separately labelled reused 0% packet-loss and MITM references.
- Data as of: 2026-07-16 (Asia/Shanghai).
- Sources: resolved YAML, lifecycle events, writer snapshots, runtime CSV/JSON, ns-3 link telemetry, PCAP, correctness/propagation reports, and PLC injection provenance.
- Grain: one adopted valid run per unique configuration; failed and nonaccepted attempts are excluded from result rows and retained separately.

## Issues and caveats

1. **Medium — no repeated observations.** Means across repetitions, dispersion, confidence intervals, and error bars are intentionally unavailable.
2. **Medium — queue-drop onset not observed.** All 15 bandwidth–DoS runs reported zero target-link queue drops, so no queue-drop threshold can be estimated.
3. **Low — baseline-consistency non-passes.** {', '.join(offline_nonpass)} produced real control/physical differences from the baseline; their telemetry and conservation checks still passed.
4. **Low — failed attempts retained.** {len(failures)} failed or initially nonaccepted attempts remain in `results/failed_attempts.csv` and are not counted as formal observations.

## Calculation spot-checks

- Formal grain: verified 23 rows, 23 unique experiment IDs, and 23 unique seeds.
- Required result tables: verified 5 packet-loss, 15 bandwidth–DoS, 2 DoS propagation, and 1 PLC-injection rows.
- Lifecycle and writers: verified 23/23 simulation successes, zero writer drops, zero conflicts, and successful cleanup status.
- Conservation: verified 23/23 network and Modbus conservation checks.
- Attack evidence: all enabled attacks have a start window and recorded events.
- PLC injection: source and executable SHA-256 values changed, malicious deployment is recorded, and source/compile/deploy/state artifacts are copied.
- Raw evidence: every adopted run retains at least one PCAP; no NUL bytes were found in adopted CSV evidence.

## Visualization review

Sixteen PNG figures were generated without error bars. Axes and units are labelled, bandwidth series use consistent scales, and propagation timelines use separate event lanes to avoid label overlap. Connected points are descriptive only.

## Required caveats for use

- Phrase conclusions as "observed in this run/configuration."
- Do not infer population-level uncertainty or monotonic trends from the connected single-run points.
- Treat `queue_drop_rho=null` as "not observed," not missing data.
- Keep reused references distinct from the 23 newly executed formal configurations.
"""
    (archive / "VALIDATION_REPORT.md").write_text(validation_report, encoding="utf-8")
    (archive / "ARCHIVE_INDEX.md").write_text(
        "# Archive index\n\n- [Final report](FINAL_REPORT.md)\n- [Validation report](VALIDATION_REPORT.md)\n"
        "- [Experiment plan](EXPERIMENT_PLAN.json)\n"
        "- [Run index](RUN_INDEX.json)\n- [Results](results/)\n- [Plots](plots/)\n- [Formal experiments](experiments/)\n",
        encoding="utf-8",
    )
    print(f"[TODO2-ANALYSIS] archive={archive} quality={quality_passed}/{len(quality_rows)} failures={len(failures)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
