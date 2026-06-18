#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SCADA Modbus client.

One-shot poll/downlink commands are retained for compatibility. Daemon mode runs
as a long-lived SCADA scheduler inside ns-scada and is synchronized by shared
filesystem markers.
"""
from __future__ import annotations

import argparse
import signal
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from src.io.csv import append_jsonl, append_row, csv_dir, json_dir, raw_dir
from src.comm.modbus import ModbusEndpoint
from src.core.config import MdVar, RuntimeConfig, load_runtime_config, read_json, write_json
from src.sync.filesystem import DEFAULT_POLL_INTERVAL, marker_path, stop_requested, touch_marker, wait_for_markers
from src.sync.helics_sync import HelicsSync, coordinator_endpoint, scada_endpoint

_STOP = False


TIMEOUT_MARKERS = (
    "timed out",
    "timeout",
    "no response",
    "cannot connect",
    "connection refused",
    "connection reset",
    "broken pipe",
)


def _install_signal_handlers() -> None:
    def _handler(signum, frame):  # type: ignore[no-untyped-def]
        global _STOP
        _STOP = True

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _source_candidates(var: MdVar, poll: dict[str, Any], physics_values: dict[str, Any]) -> list[tuple[str, Any]]:
    candidates: list[tuple[str, Any]] = []
    if var.source_prefix:
        src_plc = var.source_prefix.upper()
        # Source PLC may have already been polled under the same full variable name.
        try:
            source_plc_data = poll.get("plcs", {}).get(src_plc, {}).get("md", {})
            if var.name in source_plc_data:
                candidates.append((f"poll:{src_plc}:{var.name}", source_plc_data[var.name]))
            source_name = f"{src_plc}_{var.tag}"
            if source_name in source_plc_data:
                candidates.append((f"poll:{src_plc}:{source_name}", source_plc_data[source_name]))
        except Exception:
            pass

    for key in (var.name, var.tag):
        if key in physics_values:
            candidates.append((f"physics:{key}", physics_values[key]))

    return candidates


def _modbus_workers(args: argparse.Namespace, plc_count: int) -> int:
    workers = int(getattr(args, "modbus_workers", 1) or 1)
    return max(1, min(workers, max(1, plc_count)))


def _batch_modbus_enabled(args: argparse.Namespace) -> bool:
    return not bool(getattr(args, "no_batch_modbus", False))


def _is_timeout_like(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in TIMEOUT_MARKERS)


def _mark_timeout(
    item: dict[str, Any],
    phase: str,
    message: str,
    previous: dict[str, Any] | None = None,
    *,
    warmup: bool = False,
) -> None:
    item["status"] = "warmup_timeout" if warmup else "timeout"
    item["timeout"] = not warmup
    item["warmup_timeout"] = bool(warmup)
    item["timeout_phase"] = phase
    label = "warmup timeout" if warmup else "timeout"
    item["errors"].append(f"{label} {phase}: {message}")
    if previous:
        item["used_previous"] = True
        item["previous_iteration"] = previous.get("iteration", "")
        item["md"].update(previous.get("md", {}) or {})
        item["coils"].update(previous.get("coils", {}) or {})


def _drop_endpoint(endpoints: dict[str, ModbusEndpoint] | None, plc_name: str) -> None:
    if endpoints is None or plc_name not in endpoints:
        return
    try:
        endpoints[plc_name].close()
    finally:
        try:
            del endpoints[plc_name]
        except KeyError:
            pass


def _run_plc_tasks(
    plcs: list[Any],
    args: argparse.Namespace,
    worker: Callable[[Any], tuple[str, dict[str, Any], dict[str, Any]]],
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    if len(plcs) <= 1 or _modbus_workers(args, len(plcs)) <= 1:
        return [worker(plc) for plc in plcs]
    with ThreadPoolExecutor(max_workers=_modbus_workers(args, len(plcs))) as executor:
        # executor.map preserves input order, which keeps CSV/log order stable.
        return list(executor.map(worker, plcs))


def _get_or_open_endpoint(plc: Any, args: argparse.Namespace, endpoints: dict[str, ModbusEndpoint] | None) -> tuple[ModbusEndpoint, bool]:
    if endpoints is not None and plc.name in endpoints:
        return endpoints[plc.name], False
    mb = ModbusEndpoint(plc.ip, port=args.port, unit_id=args.unit_id, timeout=args.timeout)
    mb.connect()
    return mb, True


def _poll_one_plc(
    plc: Any,
    args: argparse.Namespace,
    endpoints: dict[str, ModbusEndpoint] | None = None,
    previous: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    item: dict[str, Any] = {"ip": plc.ip, "md": {}, "coils": {}, "errors": [], "status": "ok", "timeout": False}
    flat: dict[str, Any] = {}
    if not plc.ip:
        item["status"] = "error"
        item["errors"].append("no IP in config")
        return plc.name, item, flat

    warmup_timeout = bool(getattr(args, "_timeout_grace_active", False))
    mb: ModbusEndpoint | None = None
    close_after = False
    try:
        mb, close_after = _get_or_open_endpoint(plc, args, endpoints)
        md_vars = [var for var in plc.md_vars.values() if not (args.skip_ready and var.name == "PLC_Ready")]
        if md_vars:
            if _batch_modbus_enabled(args):
                try:
                    md_values = mb.read_real_mds(var.md_index for var in md_vars)
                    for var in md_vars:
                        value = md_values[var.md_index]
                        item["md"][var.name] = value
                        flat[var.name] = value
                except Exception as exc:
                    if _is_timeout_like(exc):
                        _drop_endpoint(endpoints, plc.name)
                        _mark_timeout(item, "poll", f"batch read md: {exc}", previous, warmup=warmup_timeout)
                        flat.update(item["md"])
                        return plc.name, item, flat
                    item["errors"].append(f"batch read md: {exc}; fallback to single reads")
                    for var in md_vars:
                        try:
                            value = mb.read_real_md(var.md_index)
                            item["md"][var.name] = value
                            flat[var.name] = value
                        except Exception as var_exc:
                            if _is_timeout_like(var_exc):
                                _drop_endpoint(endpoints, plc.name)
                                _mark_timeout(item, "poll", f"read {var.name} %MD{var.md_index}: {var_exc}", previous, warmup=warmup_timeout)
                                flat.update(item["md"])
                                return plc.name, item, flat
                            item["errors"].append(f"read {var.name} %MD{var.md_index}: {var_exc}")
            else:
                for var in md_vars:
                    try:
                        value = mb.read_real_md(var.md_index)
                        item["md"][var.name] = value
                        flat[var.name] = value
                    except Exception as exc:
                        if _is_timeout_like(exc):
                            _drop_endpoint(endpoints, plc.name)
                            _mark_timeout(item, "poll", f"read {var.name} %MD{var.md_index}: {exc}", previous, warmup=warmup_timeout)
                            flat.update(item["md"])
                            return plc.name, item, flat
                        item["errors"].append(f"read {var.name} %MD{var.md_index}: {exc}")

        if args.read_coils and plc.coil_vars:
            if _batch_modbus_enabled(args):
                try:
                    coil_values = mb.read_coils(var.coil_index for var in plc.coil_vars.values())
                    for var in plc.coil_vars.values():
                        value = coil_values[var.coil_index]
                        item["coils"][var.name] = value
                        flat[var.name] = value
                except Exception as exc:
                    if _is_timeout_like(exc):
                        _drop_endpoint(endpoints, plc.name)
                        _mark_timeout(item, "poll", f"batch read coils: {exc}", previous, warmup=warmup_timeout)
                        flat.update(item["md"])
                        flat.update(item["coils"])
                        return plc.name, item, flat
                    item["errors"].append(f"batch read coils: {exc}; fallback to single reads")
                    for var in plc.coil_vars.values():
                        try:
                            value = mb.read_coil(var.coil_index)
                            item["coils"][var.name] = value
                            flat[var.name] = value
                        except Exception as var_exc:
                            if _is_timeout_like(var_exc):
                                _drop_endpoint(endpoints, plc.name)
                                _mark_timeout(item, "poll", f"read {var.name} coil{var.coil_index}: {var_exc}", previous, warmup=warmup_timeout)
                                flat.update(item["md"])
                                flat.update(item["coils"])
                                return plc.name, item, flat
                            item["errors"].append(f"read {var.name} coil{var.coil_index}: {var_exc}")
            else:
                for var in plc.coil_vars.values():
                    try:
                        value = mb.read_coil(var.coil_index)
                        item["coils"][var.name] = value
                        flat[var.name] = value
                    except Exception as exc:
                        if _is_timeout_like(exc):
                            _drop_endpoint(endpoints, plc.name)
                            _mark_timeout(item, "poll", f"read {var.name} coil{var.coil_index}: {exc}", previous, warmup=warmup_timeout)
                            flat.update(item["md"])
                            flat.update(item["coils"])
                            return plc.name, item, flat
                        item["errors"].append(f"read {var.name} coil{var.coil_index}: {exc}")
    except Exception as exc:
        if _is_timeout_like(exc):
            _drop_endpoint(endpoints, plc.name)
            _mark_timeout(item, "poll", f"connect/read {plc.ip}:{args.port}: {exc}", previous, warmup=warmup_timeout)
            flat.update(item["md"])
            flat.update(item["coils"])
        else:
            item["status"] = "error"
            item["errors"].append(f"connect/read {plc.ip}:{args.port}: {exc}")
    finally:
        if close_after and mb is not None:
            mb.close()

    return plc.name, item, flat


def _poll_runtime(
    rt: RuntimeConfig,
    args: argparse.Namespace,
    endpoints: dict[str, ModbusEndpoint] | None = None,
    previous_poll: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"plcs": {}, "flat": {}}
    plcs = [plc for plc in rt.plcs.values() if plc.ip]
    missing = [plc for plc in rt.plcs.values() if not plc.ip]
    for plc in missing:
        print(f"[SCADA] skip {plc.name}: no IP in config", flush=True)

    previous_poll = previous_poll or {}
    results = _run_plc_tasks(plcs, args, lambda plc: _poll_one_plc(plc, args, endpoints, previous_poll.get(plc.name)))
    for plc_name, item, flat in results:
        payload["plcs"][plc_name] = item
        payload["flat"].update(flat)
        status = item.get("status", "ok")
        prev = " previous" if item.get("used_previous") else ""
        print(f"[SCADA] poll {plc_name:5s} ip={item.get('ip', ''):15s} status={status}{prev} md={len(item['md'])} coils={len(item['coils'])} errors={len(item['errors'])}", flush=True)
        for err in item["errors"]:
            print(f"  [ERR] {err}", flush=True)

    return payload

def poll(args: argparse.Namespace) -> int:
    rt = load_runtime_config(args.config)
    payload = _poll_runtime(rt, args)
    write_json(args.out, payload)
    return 0


def _prepare_downlink_writes(
    plc: Any,
    poll_payload: dict[str, Any],
    physics_values: dict[str, Any],
) -> tuple[dict[str, Any], list[tuple[MdVar, float, str]]]:
    item: dict[str, Any] = {"ip": plc.ip, "written": {}, "skipped": {}, "errors": [], "status": "ok", "timeout": False}
    writes: list[tuple[MdVar, float, str]] = []

    for var in plc.md_vars.values():
        if var.name == "PLC_Ready":
            continue
        # Only downlink dependency variables. Local variables are written by the local adapter.
        if var.source_prefix in {None, plc.name}:
            continue

        candidates = _source_candidates(var, poll_payload, physics_values)
        if not candidates:
            item["skipped"][var.name] = f"no source for {var.name}/{var.tag}"
            continue
        source, raw = candidates[0]
        try:
            writes.append((var, float(raw), source))
        except Exception as exc:
            item["skipped"][var.name] = f"bad value from {source}: {raw!r}: {exc}"

    return item, writes


def _downlink_one_plc(
    plc: Any,
    args: argparse.Namespace,
    item: dict[str, Any],
    writes: list[tuple[MdVar, float, str]],
    endpoints: dict[str, ModbusEndpoint] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    flat: dict[str, Any] = {}
    if not writes:
        return plc.name, item, flat

    mb: ModbusEndpoint | None = None
    close_after = False
    warmup_timeout = bool(getattr(args, "_timeout_grace_active", False))
    timeout_status = "warmup_timeout" if warmup_timeout else "timeout"
    timeout_flag = not warmup_timeout
    timeout_label = "warmup timeout" if warmup_timeout else "timeout"
    try:
        mb, close_after = _get_or_open_endpoint(plc, args, endpoints)
        if _batch_modbus_enabled(args):
            try:
                mb.write_real_mds({var.md_index: value for var, value, _source in writes})
                for var, value, source in writes:
                    item["written"][var.name] = {"value": value, "source": source, "md_index": var.md_index}
                    flat[var.name] = value
            except Exception as exc:
                if _is_timeout_like(exc):
                    _drop_endpoint(endpoints, plc.name)
                    item["status"] = timeout_status
                    item["timeout"] = timeout_flag
                    item["warmup_timeout"] = warmup_timeout
                    item["timeout_phase"] = "downlink"
                    item["errors"].append(f"{timeout_label} downlink: batch write md: {exc}")
                    return plc.name, item, flat
                item["errors"].append(f"batch write md: {exc}; fallback to single writes")
                for var, value, source in writes:
                    try:
                        mb.write_real_md(var.md_index, value)
                        item["written"][var.name] = {"value": value, "source": source, "md_index": var.md_index}
                        flat[var.name] = value
                    except Exception as var_exc:
                        if _is_timeout_like(var_exc):
                            _drop_endpoint(endpoints, plc.name)
                            item["status"] = timeout_status
                            item["timeout"] = timeout_flag
                            item["warmup_timeout"] = warmup_timeout
                            item["timeout_phase"] = "downlink"
                            item["errors"].append(f"{timeout_label} downlink: write {var.name} %MD{var.md_index}: {var_exc}")
                            return plc.name, item, flat
                        item["errors"].append(f"write {var.name} %MD{var.md_index}: {var_exc}")
        else:
            for var, value, source in writes:
                try:
                    mb.write_real_md(var.md_index, value)
                    item["written"][var.name] = {"value": value, "source": source, "md_index": var.md_index}
                    flat[var.name] = value
                except Exception as exc:
                    if _is_timeout_like(exc):
                        _drop_endpoint(endpoints, plc.name)
                        item["status"] = timeout_status
                        item["timeout"] = timeout_flag
                        item["warmup_timeout"] = warmup_timeout
                        item["timeout_phase"] = "downlink"
                        item["errors"].append(f"{timeout_label} downlink: write {var.name} %MD{var.md_index}: {exc}")
                        return plc.name, item, flat
                    item["errors"].append(f"write {var.name} %MD{var.md_index}: {exc}")
    except Exception as exc:
        if _is_timeout_like(exc):
            _drop_endpoint(endpoints, plc.name)
            item["status"] = timeout_status
            item["timeout"] = timeout_flag
            item["warmup_timeout"] = warmup_timeout
            item["timeout_phase"] = "downlink"
            item["errors"].append(f"{timeout_label} downlink: connect/write {plc.ip}:{args.port}: {exc}")
        else:
            item["status"] = "error"
            item["errors"].append(f"connect/write {plc.ip}:{args.port}: {exc}")
    finally:
        if close_after and mb is not None:
            mb.close()

    return plc.name, item, flat


def _downlink_runtime(
    rt: RuntimeConfig,
    args: argparse.Namespace,
    poll_payload: dict[str, Any],
    physics_values: dict[str, Any],
    endpoints: dict[str, ModbusEndpoint] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"plcs": {}, "flat": {}}
    tasks: list[tuple[Any, dict[str, Any], list[tuple[MdVar, float, str]]]] = []

    for plc in rt.plcs.values():
        if not plc.ip:
            continue
        item, writes = _prepare_downlink_writes(plc, poll_payload, physics_values)
        tasks.append((plc, item, writes))

    if len(tasks) <= 1 or _modbus_workers(args, len(tasks)) <= 1:
        results = [_downlink_one_plc(plc, args, item, writes, endpoints) for plc, item, writes in tasks]
    else:
        with ThreadPoolExecutor(max_workers=_modbus_workers(args, len(tasks))) as executor:
            results = list(executor.map(lambda t: _downlink_one_plc(t[0], args, t[1], t[2], endpoints), tasks))

    for plc_name, item, flat in results:
        payload["plcs"][plc_name] = item
        payload["flat"].update(flat)
        status = item.get("status", "ok")
        print(f"[SCADA] downlink {plc_name:5s} ip={item.get('ip', ''):15s} status={status} written={len(item['written'])} skipped={len(item['skipped'])} errors={len(item['errors'])}", flush=True)
        for name, meta in item["written"].items():
            print(f"  WRITE {name:16s} %MD{meta['md_index']:<3d} = {meta['value']} from {meta['source']}", flush=True)
        for err in item["errors"]:
            print(f"  [ERR] {err}", flush=True)

    return payload

def _write_scada_csv(runtime_dir: Path, iteration: int, poll_payload: dict[str, Any], downlink_payload: dict[str, Any]) -> None:
    row: dict[str, Any] = {"iteration": iteration}

    total_poll_md = 0
    total_poll_coils = 0
    total_poll_errors = 0
    total_poll_timeouts = 0
    total_poll_warmup_timeouts = 0
    total_downlink_written = 0
    total_downlink_skipped = 0
    total_downlink_errors = 0
    total_downlink_timeouts = 0
    total_downlink_warmup_timeouts = 0

    for plc_name, item in sorted((poll_payload.get("plcs", {}) or {}).items()):
        prefix = f"poll.{plc_name}"
        md = item.get("md", {}) or {}
        coils = item.get("coils", {}) or {}
        errors = item.get("errors", []) or []
        row[f"{prefix}.ip"] = item.get("ip", "")
        row[f"{prefix}.status"] = item.get("status", "ok")
        row[f"{prefix}.timeout"] = bool(item.get("timeout", False))
        row[f"{prefix}.warmup_timeout"] = bool(item.get("warmup_timeout", False))
        row[f"{prefix}.used_previous"] = bool(item.get("used_previous", False))
        row[f"{prefix}.previous_iteration"] = item.get("previous_iteration", "")
        row[f"{prefix}.md_count"] = len(md)
        row[f"{prefix}.coil_count"] = len(coils)
        row[f"{prefix}.error_count"] = len(errors)
        if errors:
            row[f"{prefix}.errors"] = " | ".join(str(e) for e in errors)
        for name, value in md.items():
            row[f"{prefix}.md.{name}"] = value
        for name, value in coils.items():
            row[f"{prefix}.coil.{name}"] = value
        total_poll_md += len(md)
        total_poll_coils += len(coils)
        total_poll_errors += len(errors)
        total_poll_timeouts += 1 if item.get("timeout") else 0
        total_poll_warmup_timeouts += 1 if item.get("warmup_timeout") else 0

    for plc_name, item in sorted((downlink_payload.get("plcs", {}) or {}).items()):
        prefix = f"downlink.{plc_name}"
        written = item.get("written", {}) or {}
        skipped = item.get("skipped", {}) or {}
        errors = item.get("errors", []) or []
        row[f"{prefix}.ip"] = item.get("ip", "")
        row[f"{prefix}.status"] = item.get("status", "ok")
        row[f"{prefix}.timeout"] = bool(item.get("timeout", False))
        row[f"{prefix}.warmup_timeout"] = bool(item.get("warmup_timeout", False))
        row[f"{prefix}.written_count"] = len(written)
        row[f"{prefix}.skipped_count"] = len(skipped)
        row[f"{prefix}.error_count"] = len(errors)
        if errors:
            row[f"{prefix}.errors"] = " | ".join(str(e) for e in errors)
        for name, meta in written.items():
            row[f"{prefix}.write.{name}.value"] = meta.get("value")
            row[f"{prefix}.write.{name}.source"] = meta.get("source")
            row[f"{prefix}.write.{name}.md_index"] = meta.get("md_index")
        total_downlink_written += len(written)
        total_downlink_skipped += len(skipped)
        total_downlink_errors += len(errors)
        total_downlink_timeouts += 1 if item.get("timeout") else 0
        total_downlink_warmup_timeouts += 1 if item.get("warmup_timeout") else 0

    row.update({
        "poll_md_total": total_poll_md,
        "poll_coil_total": total_poll_coils,
        "poll_error_total": total_poll_errors,
        "poll_timeout_total": total_poll_timeouts,
        "poll_warmup_timeout_total": total_poll_warmup_timeouts,
        "downlink_written_total": total_downlink_written,
        "downlink_skipped_total": total_downlink_skipped,
        "downlink_error_total": total_downlink_errors,
        "downlink_timeout_total": total_downlink_timeouts,
        "downlink_warmup_timeout_total": total_downlink_warmup_timeouts,
    })

    append_row(
        csv_dir(runtime_dir) / "scada.csv",
        row,
        fixed_columns=[
            "iteration",
            "poll_md_total",
            "poll_coil_total",
            "poll_error_total",
            "poll_timeout_total",
            "poll_warmup_timeout_total",
            "downlink_written_total",
            "downlink_skipped_total",
            "downlink_error_total",
            "downlink_timeout_total",
            "downlink_warmup_timeout_total",
        ],
    )


def _write_scada_observed_csv(runtime_dir: Path, iteration: int, poll_payload: dict[str, Any]) -> None:
    """Append SCADA-observed Modbus poll values in long form.

    These values are the decoded values returned to SCADA by Modbus polling.
    Under MITM experiments this is the post-MITM value seen by SCADA, not the
    physical state from physics_XXXX.json.
    """
    observed_at = f"{time.time():.6f}"
    path = csv_dir(runtime_dir) / "scada_observed.csv"
    raw_path = raw_dir(runtime_dir) / "scada_observed.jsonl"
    fixed_columns = [
        "iteration",
        "plc",
        "variable",
        "value",
        "source",
        "direction",
        "timestamp_epoch",
        "kind",
    ]

    for plc_name, item in sorted((poll_payload.get("plcs", {}) or {}).items()):
        source = "modbus_timeout_previous" if item.get("timeout") and item.get("used_previous") else "modbus_poll"
        if item.get("warmup_timeout") and item.get("used_previous"):
            source = "modbus_warmup_previous"
        for name, value in sorted((item.get("md", {}) or {}).items()):
            row = {
                "iteration": iteration,
                "plc": plc_name,
                "variable": name,
                "value": value,
                "source": source,
                "direction": "response",
                "timestamp_epoch": observed_at,
                "kind": "md",
            }
            append_jsonl(raw_path, row)
            append_row(
                path,
                row,
                fixed_columns=fixed_columns,
            )
        for name, value in sorted((item.get("coils", {}) or {}).items()):
            row = {
                "iteration": iteration,
                "plc": plc_name,
                "variable": name,
                "value": value,
                "source": source,
                "direction": "response",
                "timestamp_epoch": observed_at,
                "kind": "coil",
            }
            append_jsonl(raw_path, row)
            append_row(
                path,
                row,
                fixed_columns=fixed_columns,
            )


def _write_scada_timing_csv(runtime_dir: Path, timing: dict[str, Any]) -> None:
    append_row(
        csv_dir(runtime_dir) / "scada_timing.csv",
        timing,
        fixed_columns=[
            "iteration",
            "wait_local_write_markers_sec",
            "poll_sec",
            "read_physics_json_sec",
            "downlink_sec",
            "write_scada_csv_sec",
            "write_outputs_and_marker_sec",
            "cycle_total_sec",
            "poll_md_total",
            "poll_coil_total",
            "poll_error_total",
            "poll_timeout_total",
            "poll_warmup_timeout_total",
            "downlink_written_total",
            "downlink_skipped_total",
            "downlink_error_total",
            "downlink_timeout_total",
            "downlink_warmup_timeout_total",
        ],
    )


def _write_scada_timeout_events(
    runtime_dir: Path,
    iteration: int,
    phase: str,
    payload: dict[str, Any],
) -> None:
    timestamp = f"{time.time():.6f}"
    fixed_columns = [
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
    for plc_name, item in sorted((payload.get("plcs", {}) or {}).items()):
        if not item.get("timeout"):
            continue
        row = {
            "timestamp_epoch": timestamp,
            "iteration": iteration,
            "phase": phase,
            "plc": plc_name,
            "ip": item.get("ip", ""),
            "status": "timeout",
            "warmup": False,
            "used_previous": bool(item.get("used_previous", False)),
            "previous_iteration": item.get("previous_iteration", ""),
            "message": " | ".join(str(err) for err in (item.get("errors", []) or [])),
        }
        append_jsonl(raw_dir(runtime_dir) / "scada_timeout_events.jsonl", row)
        append_row(csv_dir(runtime_dir) / "scada_timeout_events.csv", row, fixed_columns=fixed_columns)


def _count_scada_payloads(poll_payload: dict[str, Any], downlink_payload: dict[str, Any]) -> dict[str, int]:
    poll_md_total = 0
    poll_coil_total = 0
    poll_error_total = 0
    poll_timeout_total = 0
    poll_warmup_timeout_total = 0
    for item in (poll_payload.get("plcs", {}) or {}).values():
        poll_md_total += len(item.get("md", {}) or {})
        poll_coil_total += len(item.get("coils", {}) or {})
        poll_error_total += len(item.get("errors", []) or [])
        poll_timeout_total += 1 if item.get("timeout") else 0
        poll_warmup_timeout_total += 1 if item.get("warmup_timeout") else 0

    downlink_written_total = 0
    downlink_skipped_total = 0
    downlink_error_total = 0
    downlink_timeout_total = 0
    downlink_warmup_timeout_total = 0
    for item in (downlink_payload.get("plcs", {}) or {}).values():
        downlink_written_total += len(item.get("written", {}) or {})
        downlink_skipped_total += len(item.get("skipped", {}) or {})
        downlink_error_total += len(item.get("errors", []) or [])
        downlink_timeout_total += 1 if item.get("timeout") else 0
        downlink_warmup_timeout_total += 1 if item.get("warmup_timeout") else 0

    return {
        "poll_md_total": poll_md_total,
        "poll_coil_total": poll_coil_total,
        "poll_error_total": poll_error_total,
        "poll_timeout_total": poll_timeout_total,
        "poll_warmup_timeout_total": poll_warmup_timeout_total,
        "downlink_written_total": downlink_written_total,
        "downlink_skipped_total": downlink_skipped_total,
        "downlink_error_total": downlink_error_total,
        "downlink_timeout_total": downlink_timeout_total,
        "downlink_warmup_timeout_total": downlink_warmup_timeout_total,
    }


def downlink(args: argparse.Namespace) -> int:
    rt = load_runtime_config(args.config)
    poll_payload = read_json(args.poll) if args.poll else {"plcs": {}, "flat": {}}
    physics = read_json(args.physics)
    physics_values = physics.get("values", physics)
    if not isinstance(physics_values, dict):
        raise ValueError("physics JSON must contain object or {'values': object}")

    payload = _downlink_runtime(rt, args, poll_payload, physics_values)
    write_json(args.out, payload)
    return 0


def _connect_scada_endpoints(rt: RuntimeConfig, args: argparse.Namespace) -> dict[str, ModbusEndpoint]:
    endpoints: dict[str, ModbusEndpoint] = {}
    for plc in rt.plcs.values():
        if not plc.ip:
            continue
        mb = ModbusEndpoint(plc.ip, port=args.port, unit_id=args.unit_id, timeout=args.timeout)
        mb.connect(
            retries=max(1, int(getattr(args, "connect_retries", 1) or 1)),
            delay=float(getattr(args, "connect_retry_delay", 0.2) or 0.2),
        )
        endpoints[plc.name] = mb
    return endpoints


def _close_scada_endpoints(endpoints: dict[str, ModbusEndpoint] | None) -> None:
    if not endpoints:
        return
    for mb in endpoints.values():
        mb.close()


def daemon(args: argparse.Namespace) -> int:
    _install_signal_handlers()
    rt = load_runtime_config(args.config)
    runtime_dir = args.runtime_dir or (rt.output_dir / "runtime")
    sync_dir = args.sync_dir or (runtime_dir / "sync")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    out_json_dir = json_dir(runtime_dir)
    sync_dir.mkdir(parents=True, exist_ok=True)

    expected_local_markers = [plc.lower_name for plc in rt.plcs.values()]
    max_iterations = args.max_iterations
    end_iteration = None if max_iterations is None else args.start_iteration + max_iterations

    print(f"[SCADA-DAEMON] start runtime={runtime_dir} sync={sync_dir} plcs={expected_local_markers}", flush=True)

    sync: HelicsSync | None = None
    if args.sync_backend == "helics":
        sync = HelicsSync.from_args(
            "hydro_scada",
            scada_endpoint(args.helics_prefix),
            args,
            timeout=args.sync_timeout,
        ).start()
        print(f"[SCADA-DAEMON] HELICS endpoint={sync.endpoint}", flush=True)

    endpoints: dict[str, ModbusEndpoint] | None = None
    if not args.no_persistent_scada_connections:
        endpoints = _connect_scada_endpoints(rt, args)
        print(f"[SCADA-DAEMON] persistent Modbus connections={len(endpoints)}", flush=True)

    previous_poll: dict[str, dict[str, Any]] = {}
    iteration = args.start_iteration
    try:
        while not _STOP and not stop_requested(sync_dir):
            if end_iteration is not None and iteration >= end_iteration:
                break
            try:
                cycle_t0 = time.monotonic()
                timing: dict[str, Any] = {"iteration": iteration}
                grace_end_iteration = args.start_iteration + max(0, int(args.timeout_grace_iterations or 0))
                args._timeout_grace_active = iteration < grace_end_iteration

                wait_t0 = time.monotonic()
                if args.sync_backend == "helics":
                    if sync is None:
                        raise RuntimeError("HELICS SCADA sync is not initialized")
                    sync.wait_for("local_write", iteration=iteration, count=len(expected_local_markers), timeout=args.sync_timeout)
                else:
                    wait_for_markers(
                        [marker_path(sync_dir, "local_write", iteration, plc) for plc in expected_local_markers],
                        timeout=args.sync_timeout,
                        poll_interval=args.poll_interval,
                        stop_dir=sync_dir,
                    )
                timing["wait_local_write_markers_sec"] = time.monotonic() - wait_t0

                poll_path = out_json_dir / f"scada_poll_{iteration:04d}.json"
                poll_t0 = time.monotonic()
                poll_payload = _poll_runtime(rt, args, endpoints=endpoints, previous_poll=previous_poll)
                write_json(poll_path, poll_payload)
                _write_scada_observed_csv(runtime_dir, iteration, poll_payload)
                _write_scada_timeout_events(runtime_dir, iteration, "poll", poll_payload)
                for plc_name, item in (poll_payload.get("plcs", {}) or {}).items():
                    if item.get("timeout") or item.get("used_previous"):
                        continue
                    if item.get("md") or item.get("coils"):
                        previous_poll[plc_name] = {
                            "iteration": iteration,
                            "md": dict(item.get("md", {}) or {}),
                            "coils": dict(item.get("coils", {}) or {}),
                        }
                timing["poll_sec"] = time.monotonic() - poll_t0

                read_physics_t0 = time.monotonic()
                physics_path = out_json_dir / f"physics_{iteration:04d}.json"
                physics = read_json(physics_path)
                physics_values = physics.get("values", physics)
                if not isinstance(physics_values, dict):
                    raise ValueError("physics JSON must contain object or {'values': object}")
                timing["read_physics_json_sec"] = time.monotonic() - read_physics_t0

                downlink_path = out_json_dir / f"scada_downlink_{iteration:04d}.json"
                downlink_t0 = time.monotonic()
                downlink_payload = _downlink_runtime(rt, args, poll_payload, physics_values, endpoints=endpoints)
                write_json(downlink_path, downlink_payload)
                _write_scada_timeout_events(runtime_dir, iteration, "downlink", downlink_payload)
                timing["downlink_sec"] = time.monotonic() - downlink_t0

                csv_t0 = time.monotonic()
                _write_scada_csv(runtime_dir, iteration, poll_payload, downlink_payload)
                timing["write_scada_csv_sec"] = time.monotonic() - csv_t0

                marker_t0 = time.monotonic()
                scada_signal = {
                    "iteration": iteration,
                    "poll": str(poll_path),
                    "downlink": str(downlink_path),
                }
                if args.sync_backend == "helics":
                    if sync is None:
                        raise RuntimeError("HELICS SCADA sync is not initialized")
                    sync.send(coordinator_endpoint(sync.prefix), "scada_downlink", iteration, scada_signal)
                    sync.flush_time()
                else:
                    touch_marker(marker_path(sync_dir, "scada_downlink", iteration), scada_signal)
                timing.update(_count_scada_payloads(poll_payload, downlink_payload))
                timing["write_outputs_and_marker_sec"] = time.monotonic() - marker_t0
                timing["cycle_total_sec"] = time.monotonic() - cycle_t0
                _write_scada_timing_csv(runtime_dir, timing)
                print(
                    f"[SCADA-DAEMON] cycle={iteration} done "
                    f"timing wait={timing['wait_local_write_markers_sec']:.4f}s "
                    f"poll={timing['poll_sec']:.4f}s "
                    f"downlink={timing['downlink_sec']:.4f}s "
                    f"csv={timing['write_scada_csv_sec']:.4f}s "
                    f"total={timing['cycle_total_sec']:.4f}s",
                    flush=True,
                )
                iteration += 1
            except Exception as exc:
                err_path = out_json_dir / f"error_{iteration:04d}_scada.json"
                write_json(err_path, {"iteration": iteration, "error": str(exc), "traceback": traceback.format_exc()})
                error_signal = {"iteration": iteration, "error": str(exc), "output": str(err_path)}
                if args.sync_backend == "helics" and sync is not None:
                    try:
                        sync.send(coordinator_endpoint(sync.prefix), "error", iteration, error_signal)
                        sync.flush_time()
                    except Exception:
                        pass
                else:
                    touch_marker(marker_path(sync_dir, "error", iteration, "scada"), error_signal)
                print(f"[SCADA-DAEMON][ERR] cycle={iteration}: {exc}", flush=True)
                if args.keep_running_on_error:
                    iteration += 1
                    continue
                return 1
    finally:
        _close_scada_endpoints(endpoints)
        if sync is not None:
            sync.close()

    print(f"[SCADA-DAEMON] stop last_iteration={iteration}", flush=True)
    return 0

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SCADA poll/downlink client for Hydro-CPS-Sim")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", required=True, type=Path)
    common.add_argument("--port", type=int, default=502)
    common.add_argument("--unit-id", type=int, default=1)
    common.add_argument("--timeout", type=float, default=2.0)
    common.add_argument("--skip-ready", action="store_true", default=True)
    common.add_argument("--read-coils", action="store_true")
    common.add_argument("--modbus-workers", type=int, default=8, help="Concurrent PLC Modbus workers for SCADA poll/downlink")
    common.add_argument("--no-batch-modbus", action="store_true", help="Disable batched Modbus reads/writes and use per-variable requests")

    p_poll = sub.add_parser("poll", parents=[common])
    p_poll.add_argument("--out", required=True, type=Path)
    p_poll.set_defaults(func=poll)

    p_down = sub.add_parser("downlink", parents=[common])
    p_down.add_argument("--physics", required=True, type=Path)
    p_down.add_argument("--poll", type=Path)
    p_down.add_argument("--out", required=True, type=Path)
    p_down.set_defaults(func=downlink)

    p_daemon = sub.add_parser("daemon", parents=[common])
    p_daemon.add_argument("--sync-dir", type=Path)
    p_daemon.add_argument("--runtime-dir", type=Path)
    p_daemon.add_argument("--start-iteration", type=int, default=0)
    p_daemon.add_argument("--max-iterations", type=int)
    p_daemon.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help="Filesystem marker polling interval in seconds")
    p_daemon.add_argument("--sync-timeout", type=float, default=30.0)
    p_daemon.add_argument("--sync-backend", choices=["filesystem", "helics"], default="filesystem")
    p_daemon.add_argument("--helics-core-type", default="ipc")
    p_daemon.add_argument("--helics-core-init", default="")
    p_daemon.add_argument("--helics-broker-address", default="")
    p_daemon.add_argument("--helics-time-delta", type=float, default=0.001)
    p_daemon.add_argument("--helics-prefix", default="hydro")
    p_daemon.add_argument("--helics-log-level", type=int, default=1)
    p_daemon.add_argument("--connect-retries", type=int, default=10)
    p_daemon.add_argument("--connect-retry-delay", type=float, default=0.2)
    p_daemon.add_argument(
        "--timeout-grace-iterations",
        type=int,
        default=1,
        help="Initial SCADA cycles whose Modbus timeouts are treated as warmup timeouts, not attack timeout events",
    )
    p_daemon.add_argument("--no-persistent-scada-connections", action="store_true", help="Reconnect for each SCADA poll/downlink instead of reusing Modbus connections")
    p_daemon.add_argument("--keep-running-on-error", action="store_true")
    p_daemon.set_defaults(func=daemon)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
