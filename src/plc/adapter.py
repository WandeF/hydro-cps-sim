#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Local PLC adapter.

One-shot mode keeps backward compatibility with the original runner.
Daemon mode is the preferred runtime path: start one long-lived adapter process
inside each PLC namespace and synchronize cycles with filesystem markers.
"""
from __future__ import annotations

import argparse
import signal
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.io.csv import append_row, csv_dir, json_dir
from src.comm.modbus import ModbusEndpoint
from src.core.config import RuntimeConfig, PlcRuntime, load_runtime_config, read_json, write_json
from src.sync.filesystem import DEFAULT_POLL_INTERVAL, marker_path, stop_requested, touch_marker, wait_for_marker
from src.sync.helics_sync import HelicsSync, coordinator_endpoint, plc_endpoint, scada_endpoint

LOCAL_HOST = "127.0.0.1"
_STOP = False


def _install_signal_handlers() -> None:
    def _handler(signum, frame):  # type: ignore[no-untyped-def]
        global _STOP
        _STOP = True

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _resolve_plc(rt: RuntimeConfig, plc_arg: str) -> PlcRuntime:
    plc_name = plc_arg.upper()
    if plc_name not in rt.plcs:
        raise ValueError(f"Unknown PLC: {plc_arg}")
    return rt.plcs[plc_name]


def _state_values(state_path: Path) -> dict[str, Any]:
    snapshot = read_json(state_path)
    values = snapshot.get("values", snapshot)
    if not isinstance(values, dict):
        raise ValueError("state JSON must contain object or {'values': object}")
    return values


def _write_sensors_with_client(plc: PlcRuntime, mb: ModbusEndpoint, values: dict[str, Any], scope: str) -> dict[str, Any]:
    written: dict[str, float] = {}
    skipped: dict[str, str] = {}
    writes_by_md: dict[int, float] = {}

    for var in plc.md_vars.values():
        if var.name == "PLC_Ready":
            continue
        if scope == "local" and var.source_prefix not in {None, plc.name}:
            skipped[var.name] = f"non-local source prefix {var.source_prefix}"
            continue

        raw = None
        for key in (var.name, var.tag):
            if key in values:
                raw = values[key]
                break
        if raw is None:
            skipped[var.name] = f"no value for {var.name}/{var.tag}"
            continue

        value = float(raw)
        writes_by_md[var.md_index] = value
        written[var.name] = value

    # Batch contiguous %MD writes to reduce Modbus round trips.
    if writes_by_md:
        mb.write_real_mds(writes_by_md)

    return {"plc": plc.name, "written": written, "skipped": skipped}


def _read_actuators_with_client(plc: PlcRuntime, mb: ModbusEndpoint) -> dict[str, Any]:
    values: dict[str, bool] = {}
    tags: dict[str, bool] = {}
    if not plc.coil_vars:
        return {"plc": plc.name, "actuators": values, "tags": tags}

    coil_values = mb.read_coils(var.coil_index for var in plc.coil_vars.values())
    for var in plc.coil_vars.values():
        value = bool(coil_values[var.coil_index])
        values[var.name] = value
        tags[var.tag] = value
    return {"plc": plc.name, "actuators": values, "tags": tags}


def _bool_from_any(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in {"1", "true", "open", "opened", "on", "yes"}


def _write_actuators_with_client(plc: PlcRuntime, mb: ModbusEndpoint, actuator_state: dict[str, Any]) -> dict[str, Any]:
    """Write PLC output coils from an actuator state map.

    The state map may use either full ST variable names, e.g. PLC4_PU10, or
    actuator tags, e.g. PU10. This is used during bootstrap so hysteresis
    outputs preserve the WNTR/config initial actuator state before normal
    control cycles start.
    """
    written: dict[str, bool] = {}
    skipped: dict[str, str] = {}
    writes_by_coil: dict[int, bool] = {}

    for var in plc.coil_vars.values():
        raw = None
        for key in (var.name, var.tag):
            if key in actuator_state:
                raw = actuator_state[key]
                break
        if raw is None:
            skipped[var.name] = f"no value for {var.name}/{var.tag}"
            continue
        value = _bool_from_any(raw)
        writes_by_coil[var.coil_index] = value
        written[var.name] = value

    if writes_by_coil:
        mb.write_coils_values(writes_by_coil)

    return {"plc": plc.name, "written": written, "skipped": skipped}


def _write_plc_adapter_csv(
    runtime_dir: Path,
    iteration: int,
    plc: PlcRuntime,
    local_payload: dict[str, Any],
    actuator_payload: dict[str, Any],
) -> None:
    written = local_payload.get("written", {}) or {}
    skipped = local_payload.get("skipped", {}) or {}
    actuators = actuator_payload.get("actuators", {}) or {}
    tags = actuator_payload.get("tags", {}) or {}

    row: dict[str, Any] = {
        "iteration": iteration,
        "plc": plc.name,
        "namespace": plc.namespace,
        "ip": plc.ip,
        "sensor_written_count": len(written),
        "sensor_skipped_count": len(skipped),
        "actuator_count": len(actuators),
    }
    if skipped:
        row["sensor_skipped"] = " | ".join(f"{k}:{v}" for k, v in skipped.items())
    for name, value in written.items():
        row[f"sensor.{name}"] = value
        if name in plc.md_vars:
            row[f"sensor.{name}.md_index"] = plc.md_vars[name].md_index
    for name, value in actuators.items():
        row[f"actuator.{name}"] = value
        if name in plc.coil_vars:
            row[f"actuator.{name}.coil_index"] = plc.coil_vars[name].coil_index
    for tag, value in tags.items():
        row[f"tag.{tag}"] = value

    append_row(
        csv_dir(runtime_dir) / "plc_adapter.csv",
        row,
        fixed_columns=[
            "iteration",
            "plc",
            "namespace",
            "ip",
            "sensor_written_count",
            "sensor_skipped_count",
            "actuator_count",
            "sensor_skipped",
        ],
    )


def _write_plc_adapter_timing_csv(runtime_dir: Path, timing: dict[str, Any]) -> None:
    append_row(
        csv_dir(runtime_dir) / "plc_adapter_timing.csv",
        timing,
        fixed_columns=[
            "iteration",
            "plc",
            "namespace",
            "ip",
            "wait_physics_marker_sec",
            "load_state_sec",
            "write_sensors_sec",
            "write_local_output_sec",
            "wait_read_actuators_marker_sec",
            "read_actuators_sec",
            "write_actuator_output_sec",
            "cycle_total_sec",
            "sensor_written_count",
            "sensor_skipped_count",
            "actuator_count",
        ],
    )


def write_sensors(args: argparse.Namespace) -> int:
    rt = load_runtime_config(args.config)
    plc = _resolve_plc(rt, args.plc)
    values = _state_values(args.state)

    with ModbusEndpoint(LOCAL_HOST, port=args.port, unit_id=args.unit_id, timeout=args.timeout) as mb:
        payload = _write_sensors_with_client(plc, mb, values, args.scope)

    if args.out:
        write_json(args.out, payload)

    print(f"[PLC-ADAPTER] {plc.name} write-sensors scope={args.scope} written={len(payload['written'])} skipped={len(payload['skipped'])}", flush=True)
    for name, val in payload["written"].items():
        print(f"  WRITE {name:16s} %MD{plc.md_vars[name].md_index:<3d} = {val}", flush=True)
    return 0


def read_actuators(args: argparse.Namespace) -> int:
    rt = load_runtime_config(args.config)
    plc = _resolve_plc(rt, args.plc)

    with ModbusEndpoint(LOCAL_HOST, port=args.port, unit_id=args.unit_id, timeout=args.timeout) as mb:
        payload = _read_actuators_with_client(plc, mb)

    if args.out:
        write_json(args.out, payload)

    print(f"[PLC-ADAPTER] {plc.name} read-actuators count={len(payload['actuators'])}", flush=True)
    for name, val in payload["actuators"].items():
        print(f"  READ  {name:16s} coil={plc.coil_vars[name].coil_index:<3d} = {val}", flush=True)
    return 0


def write_initial_actuators(args: argparse.Namespace) -> int:
    rt = load_runtime_config(args.config)
    plc = _resolve_plc(rt, args.plc)
    actuator_state = dict(rt.actuator_initial_state)
    if args.state:
        raw = read_json(args.state)
        if isinstance(raw, dict) and "actuators" in raw and isinstance(raw["actuators"], dict):
            actuator_state.update(raw["actuators"])
        elif isinstance(raw, dict) and "tags" in raw and isinstance(raw["tags"], dict):
            actuator_state.update(raw["tags"])
        elif isinstance(raw, dict):
            actuator_state.update(raw)
        else:
            raise ValueError("actuator state JSON must be an object")

    with ModbusEndpoint(LOCAL_HOST, port=args.port, unit_id=args.unit_id, timeout=args.timeout) as mb:
        payload = _write_actuators_with_client(plc, mb, actuator_state)

    if args.out:
        write_json(args.out, payload)

    print(f"[PLC-ADAPTER] {plc.name} write-initial-actuators written={len(payload['written'])} skipped={len(payload['skipped'])}", flush=True)
    for name, val in payload["written"].items():
        print(f"  WRITE {name:16s} coil={plc.coil_vars[name].coil_index:<3d} = {val}", flush=True)
    for name, reason in payload["skipped"].items():
        print(f"  SKIP  {name:16s} {reason}", flush=True)
    return 0


def _connect_local(args: argparse.Namespace) -> ModbusEndpoint:
    mb = ModbusEndpoint(LOCAL_HOST, port=args.port, unit_id=args.unit_id, timeout=args.timeout)
    mb.connect(retries=max(1, args.connect_retries), delay=args.connect_retry_delay)
    return mb


def daemon(args: argparse.Namespace) -> int:
    _install_signal_handlers()
    rt = load_runtime_config(args.config)
    plc = _resolve_plc(rt, args.plc)
    runtime_dir = args.runtime_dir or (rt.output_dir / "runtime")
    sync_dir = args.sync_dir or (runtime_dir / "sync")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    out_json_dir = json_dir(runtime_dir)
    sync_dir.mkdir(parents=True, exist_ok=True)

    lower = plc.lower_name
    max_iterations = args.max_iterations
    end_iteration = None if max_iterations is None else args.start_iteration + max_iterations

    print(f"[PLC-ADAPTER-DAEMON] start plc={plc.name} ns={plc.namespace} runtime={runtime_dir} sync={sync_dir}", flush=True)

    sync: HelicsSync | None = None
    if args.sync_backend == "helics":
        sync = HelicsSync.from_args(
            f"hydro_adapter_{plc.lower_name}",
            plc_endpoint(plc.lower_name, args.helics_prefix),
            args,
            timeout=args.sync_timeout,
        ).start()
        print(f"[PLC-ADAPTER-DAEMON] HELICS endpoint={sync.endpoint}", flush=True)

    mb: ModbusEndpoint | None = None
    iteration = args.start_iteration
    while not _STOP and not stop_requested(sync_dir):
        if end_iteration is not None and iteration >= end_iteration:
            break

        try:
            cycle_t0 = time.monotonic()
            timing: dict[str, Any] = {
                "iteration": iteration,
                "plc": plc.name,
                "namespace": plc.namespace,
                "ip": plc.ip,
            }

            physics_marker = marker_path(sync_dir, "physics", iteration)
            wait_t0 = time.monotonic()
            wait_for_marker(physics_marker, timeout=args.sync_timeout, poll_interval=args.poll_interval, stop_dir=sync_dir)
            timing["wait_physics_marker_sec"] = time.monotonic() - wait_t0

            load_t0 = time.monotonic()
            state_path = out_json_dir / f"physics_{iteration:04d}.json"
            values = _state_values(state_path)
            timing["load_state_sec"] = time.monotonic() - load_t0

            write_sensor_t0 = time.monotonic()
            if mb is None:
                mb = _connect_local(args)
            try:
                local_payload = _write_sensors_with_client(plc, mb, values, args.scope)
            except Exception:
                if mb is not None:
                    mb.close()
                mb = _connect_local(args)
                local_payload = _write_sensors_with_client(plc, mb, values, args.scope)
            timing["write_sensors_sec"] = time.monotonic() - write_sensor_t0

            local_output_t0 = time.monotonic()
            local_path = out_json_dir / f"local_write_{iteration:04d}_{lower}.json"
            write_json(local_path, local_payload)
            touch_marker(marker_path(sync_dir, "local_write", iteration, lower), {
                "plc": plc.name,
                "iteration": iteration,
                "output": str(local_path),
                "written": len(local_payload.get("written", {})),
                "skipped": len(local_payload.get("skipped", {})),
            })
            timing["write_local_output_sec"] = time.monotonic() - local_output_t0
            print(f"[PLC-ADAPTER-DAEMON] {plc.name} cycle={iteration} sensors written={len(local_payload['written'])}", flush=True)

            wait_read_t0 = time.monotonic()
            if args.sync_backend == "helics":
                if sync is None:
                    raise RuntimeError("HELICS adapter sync is not initialized")
                sync.wait_for("read_actuators", iteration=iteration, count=1, timeout=args.sync_timeout)
            else:
                read_marker = marker_path(sync_dir, "read_actuators", iteration)
                wait_for_marker(read_marker, timeout=args.sync_timeout, poll_interval=args.poll_interval, stop_dir=sync_dir)
            timing["wait_read_actuators_marker_sec"] = time.monotonic() - wait_read_t0

            read_actuator_t0 = time.monotonic()
            if mb is None:
                mb = _connect_local(args)
            try:
                actuator_payload = _read_actuators_with_client(plc, mb)
            except Exception:
                if mb is not None:
                    mb.close()
                mb = _connect_local(args)
                actuator_payload = _read_actuators_with_client(plc, mb)
            timing["read_actuators_sec"] = time.monotonic() - read_actuator_t0

            actuator_output_t0 = time.monotonic()
            actuator_path = out_json_dir / f"actuators_{iteration:04d}_{lower}.json"
            write_json(actuator_path, actuator_payload)
            _write_plc_adapter_csv(runtime_dir, iteration, plc, local_payload, actuator_payload)
            actuator_signal = {
                "plc": plc.name,
                "iteration": iteration,
                "output": str(actuator_path),
                "actuators": len(actuator_payload.get("actuators", {})),
                "tags": actuator_payload.get("tags", {}),
            }
            if args.sync_backend == "helics":
                if sync is None:
                    raise RuntimeError("HELICS adapter sync is not initialized")
                sync.send(coordinator_endpoint(sync.prefix), "actuators", iteration, actuator_signal)
                sync.flush_time()
            else:
                touch_marker(marker_path(sync_dir, "actuators", iteration, lower), actuator_signal)
            timing["write_actuator_output_sec"] = time.monotonic() - actuator_output_t0
            timing["sensor_written_count"] = len(local_payload.get("written", {}) or {})
            timing["sensor_skipped_count"] = len(local_payload.get("skipped", {}) or {})
            timing["actuator_count"] = len(actuator_payload.get("actuators", {}) or {})
            timing["cycle_total_sec"] = time.monotonic() - cycle_t0
            _write_plc_adapter_timing_csv(runtime_dir, timing)
            print(
                f"[PLC-ADAPTER-DAEMON] {plc.name} cycle={iteration} actuators read={len(actuator_payload['actuators'])} "
                f"timing wait_physics={timing['wait_physics_marker_sec']:.4f}s "
                f"write_sensors={timing['write_sensors_sec']:.4f}s "
                f"wait_read={timing['wait_read_actuators_marker_sec']:.4f}s "
                f"read_actuators={timing['read_actuators_sec']:.4f}s "
                f"total={timing['cycle_total_sec']:.4f}s",
                flush=True,
            )
            iteration += 1
        except Exception as exc:
            err_path = out_json_dir / f"error_{iteration:04d}_{lower}.json"
            write_json(err_path, {"plc": plc.name, "iteration": iteration, "error": str(exc), "traceback": traceback.format_exc()})
            error_signal = {"plc": plc.name, "iteration": iteration, "error": str(exc), "output": str(err_path)}
            if args.sync_backend == "helics" and sync is not None:
                try:
                    sync.send(coordinator_endpoint(sync.prefix), "error", iteration, error_signal)
                    sync.send(scada_endpoint(sync.prefix), "error", iteration, error_signal)
                    sync.flush_time()
                except Exception:
                    pass
            else:
                touch_marker(marker_path(sync_dir, "error", iteration, lower), error_signal)
            print(f"[PLC-ADAPTER-DAEMON][ERR] {plc.name} cycle={iteration}: {exc}", flush=True)
            if args.keep_running_on_error:
                iteration += 1
                continue
            if mb is not None:
                mb.close()
            return 1

    if mb is not None:
        mb.close()
    if sync is not None:
        sync.close()
    print(f"[PLC-ADAPTER-DAEMON] stop plc={plc.name} last_iteration={iteration}", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Local PLC adapter for Hydro-CPS-Sim")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", required=True, type=Path)
    common.add_argument("--plc", required=True)
    common.add_argument("--port", type=int, default=502)
    common.add_argument("--unit-id", type=int, default=1)
    common.add_argument("--timeout", type=float, default=2.0)

    p_write = sub.add_parser("write-sensors", parents=[common])
    p_write.add_argument("--state", required=True, type=Path)
    p_write.add_argument("--scope", choices=["local", "all"], default="local")
    p_write.add_argument("--out", type=Path)
    p_write.set_defaults(func=write_sensors)

    p_read = sub.add_parser("read-actuators", parents=[common])
    p_read.add_argument("--out", type=Path)
    p_read.set_defaults(func=read_actuators)

    p_init = sub.add_parser("write-initial-actuators", parents=[common])
    p_init.add_argument("--state", type=Path, help="Optional actuator state JSON; defaults to config actuators.initial_state")
    p_init.add_argument("--out", type=Path)
    p_init.set_defaults(func=write_initial_actuators)

    p_daemon = sub.add_parser("daemon", parents=[common])
    p_daemon.add_argument("--sync-dir", type=Path)
    p_daemon.add_argument("--runtime-dir", type=Path)
    p_daemon.add_argument("--start-iteration", type=int, default=0)
    p_daemon.add_argument("--max-iterations", type=int)
    p_daemon.add_argument("--scope", choices=["local", "all"], default="local")
    p_daemon.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help="Filesystem marker polling interval in seconds")
    p_daemon.add_argument("--sync-timeout", type=float, default=30.0)
    p_daemon.add_argument("--sync-backend", choices=["filesystem", "helics"], default="filesystem")
    p_daemon.add_argument("--helics-core-type", default="ipc")
    p_daemon.add_argument("--helics-core-init", default="")
    p_daemon.add_argument("--helics-broker-address", default="")
    p_daemon.add_argument("--helics-time-delta", type=float, default=0.001)
    p_daemon.add_argument("--helics-prefix", default="hydro")
    p_daemon.add_argument("--helics-log-level", type=int, default=1)
    p_daemon.add_argument("--connect-retries", type=int, default=60)
    p_daemon.add_argument("--connect-retry-delay", type=float, default=0.25)
    p_daemon.add_argument("--keep-running-on-error", action="store_true")
    p_daemon.set_defaults(func=daemon)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
