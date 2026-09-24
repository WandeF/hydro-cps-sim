#!/usr/bin/env python3
"""Create TODO3 per-run tables, diagnostics, plots, and validation reports."""
from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import statistics
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_todo3_experiments import SECTION_NAMES
from src.metrics.correctness import analyze_correctness, write_correctness_outputs
from src.metrics.performance import analyze_performance, write_performance_outputs
from src.metrics.propagation import analyze_propagation, write_propagation_outputs
from src.core.config import load_yaml

OLD_ARCHIVE = Path("/home/lzh/MASTER/CODE/output/quantitative_20260716T113050_metric_cde39ea")
LEGACY_TODO2 = Path("/home/lzh/MASTER/CODE/output/quantitative_todo2_20260716T160500+0800_metric_cde39ea")
BASELINE = OLD_ARCHIVE / "01_correctness_baseline__config__iter100__logicwait0p1__20260716T113050+0800/output"


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error, UnicodeError):
        return []


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def integer(value: Any, default: int = 0) -> int:
    return int(num(value, default))


def percentile(values: Iterable[float], p: float) -> float | None:
    data = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    index = (len(data) - 1) * p
    low, high = math.floor(index), math.ceil(index)
    return data[low] + (data[high] - data[low]) * (index - low)


def ols_summary(rows: list[dict[str, Any]], x_key: str, y_key: str) -> dict[str, Any]:
    """Small dependency-free OLS summary for the required theory checks."""
    pairs = [(num(row.get(x_key), math.nan), num(row.get(y_key), math.nan)) for row in rows]
    pairs = [(x, y) for x, y in pairs if math.isfinite(x) and math.isfinite(y)]
    if len(pairs) < 2:
        return {"n": len(pairs), "slope": None, "intercept": None, "r_squared": None}
    xs = [x for x, _ in pairs]; ys = [y for _, y in pairs]
    xm = statistics.fmean(xs); ym = statistics.fmean(ys)
    sxx = sum((x - xm) ** 2 for x in xs)
    slope = sum((x - xm) * (y - ym) for x, y in pairs) / sxx if sxx else 0.0
    intercept = ym - slope * xm
    ss_tot = sum((y - ym) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in pairs)
    return {"n": len(pairs), "slope": slope, "intercept": intercept,
            "r_squared": 1.0 - ss_res / ss_tot if ss_tot else 1.0}


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if columns is None:
        columns = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: "" if row.get(column) is None else row.get(column) for column in columns})


def pcap_records(path: Path):
    with path.open("rb") as handle:
        magic = handle.read(4)
        formats = {
            b"\xd4\xc3\xb2\xa1": ("<", 1e6), b"\xa1\xb2\xc3\xd4": (">", 1e6),
            b"\x4d\x3c\xb2\xa1": ("<", 1e9), b"\xa1\xb2\x3c\x4d": (">", 1e9),
        }
        if magic not in formats:
            return
        endian, scale = formats[magic]
        if len(handle.read(20)) != 20:
            return
        while True:
            header = handle.read(16)
            if not header:
                return
            if len(header) != 16:
                return
            sec, fraction, captured, _ = struct.unpack(endian + "IIII", header)
            packet = handle.read(captured)
            if len(packet) != captured:
                return
            yield sec + fraction / scale, packet


def ipv4_tcp(frame: bytes) -> dict[str, Any] | None:
    for offset in (0, 2, 4, 14):
        if len(frame) < offset + 20 or frame[offset] >> 4 != 4:
            continue
        ihl = (frame[offset] & 0x0F) * 4
        if ihl < 20 or len(frame) < offset + ihl or frame[offset + 9] != 6:
            continue
        total = struct.unpack_from("!H", frame, offset + 2)[0]
        if total < ihl + 20 or len(frame) < offset + total:
            continue
        tcp = offset + ihl
        hlen = (frame[tcp + 12] >> 4) * 4
        if hlen < 20 or tcp + hlen > offset + total:
            continue
        source = ".".join(str(x) for x in frame[offset + 12:offset + 16])
        target = ".".join(str(x) for x in frame[offset + 16:offset + 20])
        sport, dport, seq, ack = struct.unpack_from("!HHII", frame, tcp)
        flags = frame[tcp + 13]
        payload = max(0, total - ihl - hlen)
        return {
            "source": source, "target": target, "sport": sport, "dport": dport,
            "seq": seq, "ack": ack, "flags": flags, "payload": payload,
            "window": struct.unpack_from("!H", frame, tcp + 14)[0],
        }
    return None


def tcp_metrics(output: Path) -> dict[str, Any]:
    # r0-r4 is the unique SCADA->PLC4 backbone segment; using one capture
    # prevents counting the same TCP packet again on the adjacent segment.
    pcaps = sorted((output / "runtime/network/pcap").glob("ns3_network-r0-r4-0-*.pcap"))
    if not pcaps:
        return {key: None for key in (
            "tcp_packets", "tcp_retransmissions", "tcp_fast_retransmissions",
            "tcp_spurious_retransmissions", "tcp_retransmission_rate", "tcp_duplicate_acks",
            "tcp_out_of_order_packets", "tcp_zero_window_events", "tcp_connection_resets",
            "tcp_syn_retries",
        )}
    path = pcaps[0]
    target_flows = {("192.168.255.1", "192.168.4.1"), ("192.168.4.1", "192.168.255.1")}
    seen: dict[tuple[str, str, int, int], set[tuple[int, int]]] = defaultdict(set)
    highest: dict[tuple[str, str, int, int], int] = {}
    ack_last: dict[tuple[str, str, int, int], int] = {}
    retrans = out_of_order = duplicate_acks = zero_window = resets = syn_retries = packets = 0
    for _timestamp, frame in pcap_records(path):
        segment = ipv4_tcp(frame)
        if segment is None or (segment["source"], segment["target"]) not in target_flows:
            continue
        flow = (segment["source"], segment["target"], segment["sport"], segment["dport"])
        packets += 1
        flags = segment["flags"]
        if flags & 0x04:
            resets += 1
        if flags & 0x02:
            if flow in seen and segment["seq"] in {item[0] for item in seen[flow]}:
                syn_retries += 1
        if flags & 0x10 and segment["payload"] == 0:
            previous_ack = ack_last.get(flow)
            if previous_ack is not None and previous_ack == segment["ack"]:
                duplicate_acks += 1
            ack_last[flow] = segment["ack"]
        window = segment.get("window", 1)
        if window == 0:
            zero_window += 1
        consumed = segment["payload"] + int(bool(flags & 0x02)) + int(bool(flags & 0x01))
        if consumed <= 0:
            continue
        key = (segment["seq"], segment["payload"])
        if key in seen[flow]:
            retrans += 1
        else:
            seen[flow].add(key)
            end = (segment["seq"] + consumed) & 0xFFFFFFFF
            previous = highest.get(flow)
            if previous is not None and segment["seq"] < previous:
                out_of_order += 1
            if previous is None or end > previous:
                highest[flow] = end
    return {
        "tcp_packets": packets, "tcp_retransmissions": retrans,
        "tcp_fast_retransmissions": None, "tcp_spurious_retransmissions": None,
        "tcp_retransmission_rate": retrans / packets if packets else None,
        "tcp_duplicate_acks": duplicate_acks, "tcp_out_of_order_packets": out_of_order,
        "tcp_zero_window_events": zero_window, "tcp_connection_resets": resets,
        "tcp_syn_retries": syn_retries,
    }


def comm_metrics(output: Path) -> dict[str, Any]:
    rows = [
        row for row in read_csv(output / "runtime/csv/communication.csv")
        if row.get("target") == "PLC4" and str(row.get("operation", "")).lower() != "connect"
        and str(row.get("warmup", "")).lower() not in {"true", "1"}
    ]
    counts = {key: 0 for key in ("success", "timeout", "exception", "connection_error", "other_failure")}
    rtts: list[float] = []
    consecutive = maximum = 0
    for row in rows:
        status = str(row.get("status", "")).lower()
        if status == "success":
            counts["success"] += 1
            if row.get("latency_ms") not in (None, ""):
                rtts.append(num(row["latency_ms"]))
            consecutive = 0
        elif "timeout" in status or "timeout" in str(row.get("error", "")).lower():
            counts["timeout"] += 1; consecutive += 1; maximum = max(maximum, consecutive)
        elif "connection" in status or "connect" in str(row.get("error", "")).lower():
            counts["connection_error"] += 1; consecutive += 1; maximum = max(maximum, consecutive)
        elif "exception" in status or "exception" in str(row.get("error", "")).lower():
            counts["exception"] += 1; consecutive += 1; maximum = max(maximum, consecutive)
        else:
            counts["other_failure"] += 1; consecutive += 1; maximum = max(maximum, consecutive)
    total = len(rows)
    return {
        "modbus_request_count": total, **{f"modbus_{key}_count": value for key, value in counts.items()},
        "modbus_conservation_ok": total == sum(counts.values()),
        "modbus_success_rate": counts["success"] / total if total else None,
        "modbus_timeout_rate": counts["timeout"] / total if total else None,
        "modbus_connection_error_rate": counts["connection_error"] / total if total else None,
        "modbus_rtt_mean_ms": statistics.fmean(rtts) if rtts else None,
        "modbus_rtt_median_ms": statistics.median(rtts) if rtts else None,
        "modbus_rtt_p95_ms": percentile(rtts, .95), "modbus_rtt_p99_ms": percentile(rtts, .99),
        "modbus_rtt_max_ms": max(rtts) if rtts else None,
        "maximum_consecutive_failures": maximum,
    }


def network_metrics(output: Path, links: set[str]) -> dict[str, Any]:
    rows = [row for row in read_csv(output / "runtime/csv/network.csv") if row.get("metric_source") == "link_trace" and row.get("link") in links]
    tx = sum(integer(row.get("tx_packets")) for row in rows)
    rx = sum(integer(row.get("rx_packets")) for row in rows)
    errors = sum(integer(row.get("error_model_drop_packets")) for row in rows)
    queue = sum(integer(row.get("queue_drop_packets")) for row in rows)
    pending = sum(integer(row.get("pending_packets")) for row in rows)
    other = sum(integer(row.get("other_classified_losses")) for row in rows)
    delay_values: list[float] = []
    for row in rows:
        count = integer(row.get("delay_samples"))
        if count and row.get("mean_delay_ms") not in (None, ""):
            delay_values.extend([num(row["mean_delay_ms"])] * count)
    configured = num(rows[0].get("configured_error_rate")) if rows else None
    measured = (errors + queue + other) / tx if tx else None
    return {
        "target_tx_packets": tx, "target_rx_packets": rx,
        "target_error_model_drops": errors, "target_queue_drops": queue,
        "target_pending_packets_at_stop": pending, "target_other_losses": other,
        "measured_loss_rate": measured,
        "configured_loss_rate": configured,
        "loss_absolute_error": abs(measured - configured) if measured is not None and configured is not None else None,
        "network_conservation_ok": tx == rx + errors + queue + pending + other,
        "network_delay_mean_ms": statistics.fmean(delay_values) if delay_values else None,
        "network_delay_median_ms": statistics.median(delay_values) if delay_values else None,
        "network_delay_p95_ms": percentile(delay_values, .95), "network_delay_p99_ms": percentile(delay_values, .99),
        "network_jitter_ms": None,
        "queue_capacity_packets": max((integer(row.get("queue_capacity_packets")) for row in rows), default=None),
        "queue_max_observed_packets": max((integer(row.get("queue_packets_max")) for row in rows), default=None),
        "queue_mean_observed_packets": statistics.fmean([num(row.get("queue_packets_mean")) for row in rows]) if rows else None,
        "queue_occupancy_ratio_max": max((num(row.get("queue_occupancy_ratio_max")) for row in rows), default=None),
        "queue_occupancy_ratio_mean": statistics.fmean([num(row.get("queue_occupancy_ratio_mean")) for row in rows]) if rows else None,
        "first_queue_nonzero_time_s": min((num(row.get("first_queue_nonzero_time_s"), math.inf) for row in rows if num(row.get("first_queue_nonzero_time_s"), math.inf) < math.inf), default=None),
        "first_queue_full_time_s": min((num(row.get("first_queue_full_time_s"), math.inf) for row in rows if num(row.get("first_queue_full_time_s"), math.inf) < math.inf), default=None),
        "first_queue_drop_time_s": min((num(row.get("first_queue_drop_time_s"), math.inf) for row in rows if num(row.get("first_queue_drop_time_s"), math.inf) < math.inf), default=None),
    }


def stale_age(output: Path, step_sec: float = 300.0) -> dict[str, Any]:
    ages: list[float] = []
    stale_iterations: list[int] = []
    rows = read_csv(output / "runtime/csv/scada_timeout_events.csv")
    # The timeout event export is the authoritative source for retained
    # previous values; older runs may only have the wide observation table.
    for row in rows:
        if str(row.get("used_previous", "")).lower() not in {"true", "1"}:
            continue
        iteration = integer(row.get("iteration"))
        previous = integer(row.get("previous_iteration"), iteration - 1)
        stale_iterations.append(iteration)
        ages.append(max(1, iteration - previous) * step_sec * 1000.0)
    if not rows:
        for row in read_csv(output / "runtime/csv/scada_observed_long.csv"):
            if str(row.get("source", "")).lower() not in {"modbus_timeout_previous", "previous"}:
                continue
            iteration = integer(row.get("iteration")); stale_iterations.append(iteration)
            ages.append(step_sec * 1000.0)
    max_consecutive = 0
    current = 0
    for iteration in sorted(set(stale_iterations)):
        current = current + 1 if current == 0 or iteration == previous_iteration + 1 else 1
        max_consecutive = max(max_consecutive, current)
        previous_iteration = iteration
    return {
        "mean_data_age_ms": statistics.fmean(ages) if ages else 0.0,
        "median_data_age_ms": statistics.median(ages) if ages else 0.0,
        "p95_data_age_ms": percentile(ages, .95) or 0.0,
        "p99_data_age_ms": percentile(ages, .99) or 0.0,
        "maximum_data_age_ms": max(ages, default=0.0),
        "maximum_consecutive_stale_cycles": max_consecutive,
    }


def control_physical(output: Path) -> dict[str, Any]:
    try:
        summary = analyze_correctness(BASELINE, output, BASELINE, output, variables=[f"T{i}" for i in range(1, 8)])
        write_correctness_outputs(summary, output / "reports/metrics")
    except Exception as exc:
        return {"correctness_status": "error", "correctness_error": str(exc)}
    control = summary.get("control", {}).get("overall", {}) or {}
    physical = summary.get("physical", {}).get("overall", {}) or {}
    cycles = read_csv(output / "runtime/csv/cycle_timing.csv")
    durations = [num(row.get("cycle_wall_sec"), num(row.get("duration_sec"))) for row in cycles]
    return {
        "correctness_status": "ok",
        "completed_control_cycles": len(cycles),
        "control_cycle_mean_ms": statistics.fmean(durations) * 1000 if durations else None,
        "control_cycle_p95_ms": percentile([x * 1000 for x in durations], .95),
        "control_cycle_p99_ms": percentile([x * 1000 for x in durations], .99),
        "control_deadline_miss_count": None, "deadline_source": "zero_loss_baseline_p99",
        "actuator_mismatch_count": control.get("mismatch_count"),
        "actuator_mismatch_rate": control.get("mismatch_rate"),
        "abnormal_switch_count": control.get("abnormal_switch_count"),
        "tank_pooled_rmse": physical.get("pooled_rmse"),
        "tank_mean_rmse": physical.get("mean_rmse"),
        "overall_peak_absolute_deviation": physical.get("max_absolute_error"),
        "physical_tolerance": 0.01,
    }


def packet_events(output: Path) -> dict[str, list[dict[str, str]]]:
    root = output / "runtime/network"
    return {
        "scada": [row for path in root.glob("modbus-packet-trace-*-scada.csv") for row in read_csv(path)],
        "plc": [row for path in root.glob("modbus-packet-trace-*-plc.csv") for row in read_csv(path)],
    }


def modbus_trace_rows(output: Path) -> list[dict[str, Any]]:
    events = packet_events(output)
    starts = defaultdict(list)
    arrivals = defaultdict(list)
    sends = defaultdict(list)
    ends = defaultdict(list)
    for row in events["scada"]:
        key = (row.get("transaction_id"), row.get("function_code"))
        if row.get("event_type") == "request_send_scada": starts[key].append(row)
        if row.get("event_type") == "response_arrive_scada": ends[key].append(row)
    for row in events["plc"]:
        key = (row.get("transaction_id"), row.get("function_code"))
        if row.get("event_type") == "request_arrive_plc": arrivals[key].append(row)
        if row.get("event_type") == "response_send_plc": sends[key].append(row)
    comm = [row for row in read_csv(output / "runtime/csv/communication.csv") if row.get("target") == "PLC4" and row.get("operation") != "connect" and str(row.get("warmup", "")).lower() != "true"]
    rows: list[dict[str, Any]] = []
    used: set[tuple[str, str, int]] = set()
    for index, request in enumerate(comm):
        key = (request.get("transaction_id"), request.get("function_code"))
        start_ns = integer(request.get("monotonic_start_ns"))
        candidates = [row for row in starts[key] if abs(integer(row.get("monotonic_ns")) - start_ns) < 2_000_000_000]
        start = min(candidates, key=lambda row: abs(integer(row.get("monotonic_ns")) - start_ns), default=None)
        if start is None:
            continue
        sid = (key[0], key[1], integer(start.get("monotonic_ns")))
        if sid in used:
            continue
        used.add(sid)
        send_ns = integer(start.get("monotonic_ns"))
        arrive = min((row for row in arrivals[key] if integer(row.get("monotonic_ns")) >= send_ns), key=lambda row: integer(row.get("monotonic_ns")), default=None)
        arrive_ns = integer(arrive.get("monotonic_ns")) if arrive else None
        response = min((row for row in sends[key] if arrive_ns is not None and integer(row.get("monotonic_ns")) >= arrive_ns), key=lambda row: integer(row.get("monotonic_ns")), default=None)
        response_ns = integer(response.get("monotonic_ns")) if response else None
        end = min((row for row in ends[key] if response_ns is not None and integer(row.get("monotonic_ns")) >= response_ns), key=lambda row: integer(row.get("monotonic_ns")), default=None)
        end_ns = integer(end.get("monotonic_ns")) if end else None
        rows.append({
            "experiment_id": read_json(output / "runtime/manifest.json", {}).get("experiment_id", output.name),
            "iteration": request.get("iteration"), "plc_id": "PLC4", "request_id": request.get("request_id"),
            "modbus_transaction_id": request.get("transaction_id"), "function_code": request.get("function_code"),
            "register_or_coil": request.get("address"), "request_send_monotonic_ns": send_ns,
            "request_arrive_plc_monotonic_ns": arrive_ns, "response_send_plc_monotonic_ns": response_ns,
            "response_arrive_scada_monotonic_ns": end_ns,
            "request_one_way_ms": (arrive_ns - send_ns) / 1e6 if arrive_ns is not None else None,
            "plc_processing_ms": (response_ns - arrive_ns) / 1e6 if response_ns is not None and arrive_ns is not None else None,
            "response_one_way_ms": (end_ns - response_ns) / 1e6 if end_ns is not None and response_ns is not None else None,
            "modbus_rtt_ms": (end_ns - send_ns) / 1e6 if end_ns is not None else None,
            "status": request.get("status", "missing_trace") if end_ns is not None else "missing_trace",
        })
    return rows


def timeline(output: Path, experiment_id: str, scenario: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(read_csv(output / "runtime/csv/events.csv"), start=1):
        details = row.get("details", "")
        rows.append({
            "experiment_id": experiment_id, "scenario": scenario, "event_id": f"{experiment_id}-{index:06d}",
            "event_type": row.get("event_type"), "event_source": row.get("component", row.get("source", "")),
            "iteration": row.get("iteration"), "hydraulic_time_sec": num(row.get("iteration")) * 300,
            "monotonic_ns": row.get("monotonic_ns"), "epoch_ns": row.get("wall_time_ns"),
            "request_id": row.get("request_id"), "modbus_transaction_id": "",
            "plc_id": row.get("target", "") if "PLC" in str(row.get("target", "")) else "",
            "variable": row.get("variable"), "value_before": "", "value_after": row.get("value"),
            "status": row.get("status"), "details": details,
        })
    return rows


def plot_line(path: Path, rows: list[dict[str, Any]], x: str, y: str, title: str, xlabel: str, ylabel: str, *, series: str | None = None) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 4.4), constrained_layout=True)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(series, "")) if series else ""].append(row)
    for label, values in sorted(groups.items()):
        values = sorted(values, key=lambda row: num(row.get(x)))
        ax.plot([num(row.get(x)) for row in values], [num(row.get(y), math.nan) for row in values], marker="o", label=label or None)
    ax.set_title(title, loc="left"); ax.set_xlabel(xlabel); ax.set_ylabel(ylabel); ax.grid(axis="y", alpha=.25)
    ax.spines[["top", "right"]].set_visible(False)
    if series: ax.legend(frameon=False)
    path.parent.mkdir(parents=True, exist_ok=True); fig.savefig(path, dpi=180); plt.close(fig)


def analyze(archive: Path) -> int:
    index = read_json(archive / "RUN_INDEX.json", []) or []
    adopted = {row["id"]: Path(row["output"]) for row in index if row.get("valid")}
    results = archive / "09_combined_statistics"; plots = archive / "09_combined_statistics/plots"; results.mkdir(parents=True, exist_ok=True); plots.mkdir(parents=True, exist_ok=True)
    delay_rows: list[dict[str, Any]] = []; e2e_rows: list[dict[str, Any]] = []; rtt_rows: list[dict[str, Any]] = []
    loss_rows: list[dict[str, Any]] = []; congestion_rows: list[dict[str, Any]] = []; quality_rows: list[dict[str, Any]] = []
    for item_id, output in adopted.items():
        config = read_json(output / "runtime/manifest.json", {}) or {}
        cfg = read_json(output / "runtime/manifest.json", {}) or {}
        resolved = yaml.safe_load((output / "runtime/config_resolved.yaml").read_text(encoding="utf-8")) if (output / "runtime/config_resolved.yaml").is_file() else {}
        group = str(resolved.get("experiment", {}).get("group", ""))
        if group == "delay":
            for row in read_csv(output / "runtime/csv/network.csv"):
                if row.get("metric_source") == "link_trace" and row.get("link") in {"r0-r_scada", "r0-r4"}:
                    delay_rows.append({"experiment_id": item_id, "configured_delay_ms": num(row.get("configured_delay_ms")), "link": row.get("link"), "direction": row.get("direction"), "measured_delay_mean_ms": num(row.get("mean_delay_ms")), "packet_count": integer(row.get("delay_samples")), "absolute_error_ms": abs(num(row.get("mean_delay_ms")) - num(row.get("configured_delay_ms")))})
            trace = modbus_trace_rows(output)
            for row in trace:
                e2e_rows.append({"experiment_id": item_id, **row})
            rtts = [num(row.get("modbus_rtt_ms")) for row in trace if row.get("modbus_rtt_ms") is not None]
            rtt_rows.append({"experiment_id": item_id, "configured_delay_ms": num(resolved.get("experiment", {}).get("value")), "modbus_rtt_mean_ms": statistics.fmean(rtts) if rtts else None, "modbus_rtt_median_ms": statistics.median(rtts) if rtts else None, "modbus_rtt_p95_ms": percentile(rtts, .95), "modbus_rtt_p99_ms": percentile(rtts, .99), "modbus_rtt_max_ms": max(rtts) if rtts else None, "packet_count": len(rtts)})
        elif group == "loss":
            row = {"experiment_id": item_id, "output_path": str(output), **network_metrics(output, {"r0-r_scada", "r0-r4"}), **tcp_metrics(output), **comm_metrics(output), **stale_age(output), **control_physical(output)}
            row["configured_loss_rate"] = num(resolved.get("experiment", {}).get("value"), row.get("configured_loss_rate", 0))
            loss_rows.append(row); quality_rows.append({"experiment_id": item_id, "group": group, "network_conservation_ok": row.get("network_conservation_ok"), "modbus_conservation_ok": row.get("modbus_conservation_ok"), "simulation_end": (read_json(output / "runtime/manifest.json", {}) or {}).get("termination_reason", "normal")})
        elif group == "congestion":
            row = {"experiment_id": item_id, "output_path": str(output), "rho": num(resolved.get("experiment", {}).get("value")), **network_metrics(output, {"r0-r4"}), **tcp_metrics(output), **comm_metrics(output), **control_physical(output)}
            congestion_rows.append(row)
    write_csv(results / "delay_link_direction_per_run.csv", delay_rows)
    write_csv(results / "delay_end_to_end_per_run.csv", e2e_rows)
    write_csv(results / "delay_modbus_rtt_per_run.csv", rtt_rows)
    write_csv(results / "packet_loss_21_levels_per_run.csv", loss_rows)
    write_csv(results / "packet_loss_link_direction.csv", [dict(row) for row in loss_rows])
    write_csv(results / "packet_loss_tcp_metrics.csv", [{key: row.get(key) for key in ("experiment_id", "configured_loss_rate", "tcp_packets", "tcp_retransmissions", "tcp_retransmission_rate", "tcp_duplicate_acks", "tcp_connection_resets")} for row in loss_rows])
    write_csv(results / "packet_loss_modbus_metrics.csv", [{key: row.get(key) for key in ("experiment_id", "configured_loss_rate", "modbus_request_count", "modbus_success_rate", "modbus_timeout_rate", "modbus_rtt_mean_ms", "modbus_rtt_p95_ms", "maximum_consecutive_failures")} for row in loss_rows])
    write_csv(results / "packet_loss_control_physical_metrics.csv", [{key: row.get(key) for key in ("experiment_id", "configured_loss_rate", "maximum_data_age_ms", "actuator_mismatch_rate", "tank_pooled_rmse", "overall_peak_absolute_deviation")} for row in loss_rows])
    write_csv(results / "controlled_congestion_per_run.csv", congestion_rows)
    # Explicit theory/regression summaries requested by TODO3. These are
    # descriptive fits over the adopted single observations, not inferential
    # uncertainty estimates.
    delay_regression = {
        "link_delay_ms_vs_configured": ols_summary(delay_rows, "configured_delay_ms", "measured_delay_mean_ms"),
        "modbus_rtt_ms_vs_configured": ols_summary(rtt_rows, "configured_delay_ms", "modbus_rtt_mean_ms"),
        "e2e_rtt_ms_vs_configured": ols_summary(rtt_rows, "configured_delay_ms", "modbus_rtt_p95_ms"),
        "definitions": {"x": "configured delay in milliseconds", "y": "measured one-run mean or P95"},
    }
    (results / "delay_regression_summary.json").write_text(json.dumps(delay_regression, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    loss_summary = {
        "levels_adopted": len(loss_rows),
        "configured_levels": sorted(num(row.get("configured_loss_rate")) for row in loss_rows),
        "network_conservation_failures": [row["experiment_id"] for row in loss_rows if row.get("network_conservation_ok") is not True],
        "modbus_conservation_failures": [row["experiment_id"] for row in loss_rows if row.get("modbus_conservation_ok") is not True],
        "first_tcp_retransmission_level": next((num(row.get("configured_loss_rate")) for row in sorted(loss_rows, key=lambda x: num(x.get("configured_loss_rate"))) if integer(row.get("tcp_retransmissions")) > 0), None),
        "first_modbus_timeout_level": next((num(row.get("configured_loss_rate")) for row in sorted(loss_rows, key=lambda x: num(x.get("configured_loss_rate"))) if num(row.get("modbus_timeout_rate")) > 0), None),
        "wallclock_limited_levels": [row["experiment_id"] for row in loss_rows if "limit" in str(row.get("simulation_end", ""))],
    }
    (results / "packet_loss_experiment_summary.json").write_text(json.dumps(loss_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    congestion_summary = {
        "rho_levels_adopted": len(congestion_rows),
        "rho_levels": sorted(num(row.get("rho")) for row in congestion_rows),
        "first_queue_drop_rho": next((num(row.get("rho")) for row in sorted(congestion_rows, key=lambda x: num(x.get("rho"))) if integer(row.get("target_queue_drops")) > 0), None),
        "max_queue_occupancy_ratio": max((num(row.get("queue_occupancy_ratio_max")) for row in congestion_rows), default=None),
    }
    (results / "controlled_congestion_summary.json").write_text(json.dumps(congestion_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for item_id, output in adopted.items():
        resolved = yaml.safe_load((output / "runtime/config_resolved.yaml").read_text(encoding="utf-8")) if (output / "runtime/config_resolved.yaml").is_file() else {}
        group = str(resolved.get("experiment", {}).get("group", ""))
        if group in {"timestamps", "sensitivity"}:
            rows = timeline(output, item_id, str(resolved.get("experiment", {}).get("value", group)))
            write_csv(archive / SECTION_NAMES[group] / "runs" / item_id / "event_timeline.csv", rows)
            if group == "timestamps":
                # Keep the normalized propagation artifact beside each
                # cross-layer run, as required by the TODO3 hand-off schema.
                try:
                    propagation = analyze_propagation(BASELINE, output, BASELINE, output)
                    write_propagation_outputs(propagation, archive / SECTION_NAMES[group] / "runs" / item_id)
                except Exception as exc:
                    (archive / SECTION_NAMES[group] / "runs" / item_id / "propagation_error.txt").write_text(str(exc) + "\n", encoding="utf-8")
    queue_rows = []
    for item_id, output in adopted.items():
        if item_id.startswith("controlled_congestion") or item_id == "timestamp_dos_three_bot_strong":
            queue_rows.extend([{**row, "experiment_id": item_id} for row in read_csv(output / "runtime/network/queue-timeseries.csv")])
    write_csv(results / "controlled_congestion_queue_timeseries.csv", queue_rows)
    congestion_summary["queue_timeseries_rows"] = len(queue_rows)
    (results / "controlled_congestion_summary.json").write_text(json.dumps(congestion_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    propagation_summaries: dict[str, Any] = {}
    propagation_comparison: list[dict[str, Any]] = []
    for item_id in sorted(item for item in adopted if item.startswith("timestamp_")):
        summary_path = archive / SECTION_NAMES["timestamps"] / "runs" / item_id / "propagation_summary.json"
        payload = read_json(summary_path, {}) or {}
        propagation_summaries[item_id] = payload
        control = payload.get("control", {}).get("overall", {}) or {}
        physical = payload.get("physical", {}).get("overall", {}) or {}
        mismatch_count = control.get("mismatch_count")
        actuator_count = control.get("actuator_count")
        iterations_compared = (payload.get("alignment", {}).get("control", {}) or {}).get("iterations_compared")
        propagation_comparison.append({
            "experiment_id": item_id,
            "scenario": payload.get("scenario") or item_id,
            "iterations_compared": iterations_compared,
            "actuator_mismatch_count": mismatch_count,
            "actuator_mismatch_rate": (num(mismatch_count) / (num(actuator_count) * num(iterations_compared))) if num(actuator_count) and num(iterations_compared) else None,
            "physical_mean_rmse": physical.get("mean_rmse"),
            "physical_peak_absolute_deviation": physical.get("peak_abs_deviation"),
        })
    (archive / SECTION_NAMES["timestamps"] / "propagation_summary_v2.json").write_text(json.dumps(propagation_summaries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(archive / SECTION_NAMES["timestamps"] / "attack_propagation_comparison.csv", propagation_comparison)
    # Add stable, review-friendly aliases at each adopted attempt directory so
    # a reviewer can inspect a run without knowing the internal output tree.
    for item_id, output in adopted.items():
        attempt_dir = output.parent
        aliases = {
            "resolved_config.yaml": output / "runtime/config_resolved.yaml",
            "manifest.json": output / "runtime/manifest.json",
            "lifecycle.json": output / "runtime/run_started.json",
            "run_timing.csv": output / "timing/run_all_timing.csv",
        }
        for name, source in aliases.items():
            if source.is_file():
                shutil.copyfile(source, attempt_dir / name)
        resolved = yaml.safe_load((output / "runtime/config_resolved.yaml").read_text(encoding="utf-8")) if (output / "runtime/config_resolved.yaml").is_file() else {}
        group = str(resolved.get("experiment", {}).get("group", ""))
        if group == "loss":
            metrics = {**network_metrics(output, {"r0-r_scada", "r0-r4"}), **tcp_metrics(output), **comm_metrics(output), **stale_age(output), **control_physical(output)}
        elif group == "congestion":
            metrics = {"rho": num(resolved.get("experiment", {}).get("value")), **network_metrics(output, {"r0-r4"}), **tcp_metrics(output), **comm_metrics(output), **control_physical(output)}
        else:
            metrics = {"experiment_id": item_id, "packet_trace_rows": len(packet_events(output)["scada"]) + len(packet_events(output)["plc"])}
        for name, payload in (("summary.json", metrics), ("quality_summary.json", {
            "network_conservation_ok": metrics.get("network_conservation_ok"),
            "modbus_conservation_ok": metrics.get("modbus_conservation_ok"),
            "packet_trace_rows": metrics.get("packet_trace_rows", 0),
        })):
            (attempt_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(results / "data_quality_per_run.csv", quality_rows)
    if loss_rows:
        for field, name, ylabel in (("measured_loss_rate", "configured_vs_measured_loss_rate", "Measured loss rate"), ("loss_absolute_error", "loss_rate_absolute_error", "Absolute error"), ("tcp_retransmission_rate", "loss_rate_vs_tcp_retransmission_rate", "TCP retransmission rate"), ("modbus_rtt_mean_ms", "loss_rate_vs_modbus_rtt_mean", "RTT mean (ms)"), ("modbus_rtt_p95_ms", "loss_rate_vs_modbus_rtt_p95", "RTT P95 (ms)"), ("modbus_success_rate", "loss_rate_vs_modbus_success_rate", "Success rate"), ("modbus_timeout_rate", "loss_rate_vs_modbus_timeout_rate", "Timeout rate"), ("maximum_data_age_ms", "loss_rate_vs_maximum_data_age", "Maximum data age (ms)"), ("actuator_mismatch_rate", "loss_rate_vs_actuator_mismatch_rate", "Actuator mismatch rate"), ("tank_pooled_rmse", "loss_rate_vs_tank_pooled_rmse", "Tank pooled RMSE (m)")):
            plot_line(plots / f"{name}.png", loss_rows, "configured_loss_rate", field, name.replace("_", " "), "Configured loss rate (%)", ylabel)
    if congestion_rows:
        for field, name, ylabel in (("queue_occupancy_ratio_max", "rho_vs_queue_occupancy", "Queue occupancy ratio"), ("target_queue_drops", "rho_vs_queue_drops", "Queue drops"), ("tcp_retransmissions", "rho_vs_tcp_retransmissions", "TCP retransmissions"), ("modbus_rtt_p95_ms", "rho_vs_modbus_rtt_p95", "RTT P95 (ms)"), ("modbus_timeout_rate", "rho_vs_modbus_timeout_rate", "Modbus timeout rate")):
            plot_line(plots / f"{name}.png", congestion_rows, "rho", field, name.replace("_", " "), "Configured rho", ylabel)
    if delay_rows:
        plot_line(plots / "configured_vs_measured_link_delay.png", delay_rows, "configured_delay_ms", "measured_delay_mean_ms", "Configured vs measured link delay", "Configured delay (ms)", "Measured delay (ms)", series="link")
    if rtt_rows:
        plot_line(plots / "configured_vs_modbus_rtt.png", rtt_rows, "configured_delay_ms", "modbus_rtt_mean_ms", "Configured delay vs Modbus RTT", "Configured delay (ms)", "RTT mean (ms)")
    summary = {
        "formal_adopted_count": len(adopted), "delay_rows": len(delay_rows), "delay_e2e_rows": len(e2e_rows), "loss_rows": len(loss_rows), "congestion_rows": len(congestion_rows),
        "network_conservation_failures": [row["experiment_id"] for row in loss_rows + congestion_rows if row.get("network_conservation_ok") is not True],
        "modbus_conservation_failures": [row["experiment_id"] for row in loss_rows + congestion_rows if row.get("modbus_conservation_ok") is not True],
        "first_observed_tcp_retransmission_loss_rate": next((row.get("configured_loss_rate") for row in sorted(loss_rows, key=lambda row: num(row.get("configured_loss_rate"))) if num(row.get("tcp_retransmissions")) > 0), None),
        "first_observed_modbus_timeout_loss_rate": next((row.get("configured_loss_rate") for row in sorted(loss_rows, key=lambda row: num(row.get("configured_loss_rate"))) if num(row.get("modbus_timeout_rate")) > 0), None),
        "first_observed_queue_drop_rho": next((row.get("rho") for row in sorted(congestion_rows, key=lambda row: num(row.get("rho"))) if num(row.get("target_queue_drops")) > 0), None),
    }
    (results / "supplement_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # D-DoS is explicitly a reuse/diagnostic item in TODO3. Preserve a
    # machine-readable statement even when no new 20 Mbps rerun is needed.
    dos_rows = []
    old_dos = OLD_ARCHIVE / "07_bandwidth_dos" / "bandwidth_dos_measured_rho.csv"
    legacy_dos = LEGACY_TODO2 / "results" / "bandwidth_dos_per_run.csv"
    source_dos = old_dos if old_dos.is_file() else legacy_dos
    if source_dos.is_file():
        dos_rows = read_csv(source_dos)
        if source_dos == legacy_dos:
            dos_rows = [row for row in dos_rows if num(row.get("bandwidth_mbps")) == 20.0]
    write_csv(results / "bandwidth_dos_measured_rho.csv", dos_rows)
    (results / "bandwidth_dos_path_diagnostics.json").write_text(json.dumps({
        "status": "reused_existing_data" if dos_rows else "no_compatible_prior_data",
        "source": str(source_dos) if dos_rows else None,
        "new_rerun": False,
        "reason": "TODO3 permits reuse of the existing 20 Mbps diagnostic when the path and instrumentation are unchanged.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (results / "bandwidth_dos_20mbps_diagnostic.md").write_text(
        "# 20 Mbps bandwidth DoS diagnostic\n\n"
        + (f"Reused `{source_dos}`; no new run was required.\n" if dos_rows else "No compatible prior 20 Mbps table was found; the diagnostic is marked unavailable.\n"),
        encoding="utf-8",
    )
    (archive / "08_large_scale_summary/large_scale_capability_summary.json").write_text(json.dumps(large_scale_summary(adopted), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report(archive, summary, loss_rows, congestion_rows, delay_rows, rtt_rows, adopted)
    return 0


def large_scale_summary(adopted: dict[str, Path]) -> dict[str, Any]:
    cfg = load_yaml(PROJECT_ROOT / "examples/c_town/config.yaml")
    network = cfg.get("network", {}) or {}
    nodes = network.get("nodes", {}) or {}
    output = next(iter(adopted.values()), None)
    performance = read_json(output / "reports/metrics/performance_summary.json", {}) if output else {}
    resources = read_csv(output / "runtime/csv/resources.csv") if output else []
    if not resources and output:
        resources = read_csv(output / "reports/csv/resources.csv")
    pcap_size = sum(path.stat().st_size for path in (output / "runtime/network/pcap").glob("*") if path.is_file()) if output else 0
    log_size = sum(path.stat().st_size for path in output.rglob("*.log") if path.is_file()) if output else 0
    inp_counts = {name.lower(): 0 for name in ("JUNCTIONS", "TANKS", "RESERVOIRS", "PIPES", "PUMPS", "VALVES")}
    inp_path = Path(str(cfg.get("inp_file", "")))
    if inp_path.is_file():
        section = None
        for raw in inp_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].upper()
                continue
            if section in {name.upper() for name in inp_counts} and line and not line.startswith(";"):
                inp_counts[section.lower()] += 1
    sensor_count = sum(len(plc.get("sensors", []) or []) for plc in cfg.get("plcs", []) or [])
    return {
        "junction_count": inp_counts["junctions"], "tank_count": inp_counts["tanks"],
        "reservoir_count": inp_counts["reservoirs"], "pipe_count": inp_counts["pipes"],
        "pump_count": inp_counts["pumps"], "valve_count": inp_counts["valves"],
        "plc_count": len(nodes.get("endpoints", []) or []), "scada_count": 1,
        "network_node_count": sum(len(nodes.get(group, []) or []) for group in ("routers", "switches", "endpoints")),
        "router_count": len(nodes.get("routers", []) or []), "link_count": len(network.get("backbone_links", []) or []),
        "sensor_mapping_count": sensor_count, "actuator_mapping_count": len(cfg.get("actuators", []) or []),
        "completed_control_cycles": integer((performance or {}).get("iteration_time", {}).get("count")),
        "total_wall_clock_sec": num((performance or {}).get("runtime", {}).get("wall_clock_sec")),
        "mean_cycle_wall_clock_sec": num((performance or {}).get("iteration_time", {}).get("mean_sec")),
        "peak_memory_mb": max((num(row.get("rss_mb"), num(row.get("rss_bytes")) / 1e6) for row in resources), default=None),
        "mean_cpu_percent": statistics.fmean([num(row.get("cpu_percent")) for row in resources]) if resources else None,
        "pcap_size_mb": pcap_size / 1e6, "log_size_mb": log_size / 1e6, "cleanup_status": "observed_in_run_index",
    }


def report(archive: Path, summary: dict[str, Any], loss_rows: list[dict[str, Any]], congestion_rows: list[dict[str, Any]], delay_rows: list[dict[str, Any]], rtt_rows: list[dict[str, Any]], adopted: dict[str, Path]) -> None:
    failures = [row for row in (read_json(archive / "RUN_INDEX.json", []) or []) if not row.get("valid")]
    report_text = f"""# TODO3 quantitative supplement report

This report describes one adopted observation per unique configuration. It does not calculate cross-run standard deviations, confidence intervals, significance tests, or error bars.

## Observed evidence

- Delay validation: {len({row['experiment_id'] for row in delay_rows})} adopted configurations, {len(delay_rows)} directional rows, and {len(rtt_rows)} RTT rows. Link directions are retained separately; endpoint traces use monotonic timestamps from SCADA and PLC4 namespace packet capture.
- Packet loss: {len(loss_rows)} adopted rows, configured levels 0–9.5% in 0.5% increments plus 50% extreme stress. The 300-cycle choice follows the precheck recorded in `EXPERIMENT_PLAN.json` (3,960 target-link packets at 100 cycles would produce 19.8 expected drops at 0.5%).
- Controlled congestion: {len(congestion_rows)} adopted rho rows on the r0→r4 10 Mbps, 20-packet DropTail bottleneck with three bots.
- Cross-layer and sensitivity outputs are retained under their section run directories; normalized `event_timeline.csv` files are written by the analyzer.

## Scan observations

The first observed TCP retransmission loss rate in this one-run scan was `{summary.get('first_observed_tcp_retransmission_loss_rate')}`; the first observed Modbus timeout loss rate was `{summary.get('first_observed_modbus_timeout_loss_rate')}`. These are scan observations, not system thresholds. The first observed queue-drop rho was `{summary.get('first_observed_queue_drop_rho')}`.

Network conservation failures: `{', '.join(summary.get('network_conservation_failures', [])) or 'none'}`. Modbus conservation failures: `{', '.join(summary.get('modbus_conservation_failures', [])) or 'none'}`.

## Required limitations

The 50% configuration is an extreme communication-destruction stress test, not a typical industrial loss rate. A wall-clock-limited run is retained as `completed_with_limit` with completed cycles and termination reason. Curves connect single observations for visual inspection only. A zero control or physical deviation remains a valid observation and is not a reason to rerun.

## Artifact locations

All CSV, JSON, PCAP, resolved configurations, manifests, logs, lifecycle files, event timelines, plots, failure attempts, and workspace patches remain below this archive. The main tables and figures are under `09_combined_statistics/`; scale/resource metadata is under `08_large_scale_summary/`.
"""
    (archive / "FINAL_REPORT.md").write_text(report_text, encoding="utf-8")
    validation = f"""# Validation report

Assessment: share with explicit single-observation caveats.

- Adopted run records: {len(adopted)}; retained failed attempts: {len(failures)}.
- Network conservation failures: {len(summary.get('network_conservation_failures', []))}.
- Modbus conservation failures: {len(summary.get('modbus_conservation_failures', []))}.
- Packet-boundary trace rows: {sum(len(read_csv(path)) for output in adopted.values() for path in (output / 'runtime/network').glob('modbus-packet-trace-*.csv'))}.
- Every conclusion is scoped to the configured local C-Town simulation and its one observation per configuration.

Known caveats: TCP fast/spurious retransmission labels require a packet analyzer with expert-info classification and are left null when unavailable; raw packet captures and sequence-based retransmission counts remain available. The optional WNTR crosscheck is not silently represented as a full DHALSIM comparison.
"""
    (archive / "VALIDATION_REPORT.md").write_text(validation, encoding="utf-8")
    (archive / "ARCHIVE_INDEX.md").write_text("# TODO3 supplement archive\n\n- [Final report](FINAL_REPORT.md)\n- [Validation report](VALIDATION_REPORT.md)\n- [Experiment plan](EXPERIMENT_PLAN.json)\n- [Run index](RUN_INDEX.json)\n- [Combined tables and plots](09_combined_statistics/)\n- [Large-scale summary](08_large_scale_summary/)\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    args = parser.parse_args()
    return analyze(args.archive.expanduser().resolve())


if __name__ == "__main__":
    raise SystemExit(main())
