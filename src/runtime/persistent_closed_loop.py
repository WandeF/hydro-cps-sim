#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent closed-loop coordinator for Hydro-CPS-Sim.

Runtime order, DHALSIM-compatible mode:
  0. Persist physics_0000 as an all-zero dummy CSV/JSON row, but do not release
     it to PLC/SCADA.
  1. Publish physics_0001 as configured initial tank/actuator state. Hydraulic
     pressures/flows are intentionally left as zero placeholders.
  2. For control cycle k >= 1:
     a) PLC adapters write physics_k into local PLC sensor registers.
     b) SCADA polls PLCs and downlinks cross-PLC dependency values.
     c) PLC coils are read as actuator_state_k.
     d) actuator_state_k is applied to the EPANET Toolkit step that produces
        physics_{k+1}.

This mirrors DHALSIM/epynet's exported initialization semantics while preserving
the discrete closed-loop order x_k -> controller -> u_k -> plant -> x_{k+1}.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from src.io.csv import append_jsonl, append_row, csv_dir, json_dir, raw_dir
from src.io.dhalsim import write_physics_row
from src.physics.engine import PhysicsEngine
from src.core.config import load_runtime_config, read_json, write_json
from src.experiment.manifest import write_manifest
from src.metrics import EventLogger, RuntimeMonitor, make_event, safe_log, safe_log_many
from src.metrics.writer_quality import analyze_metric_writer_stats, required_metric_writers
from src.sync.filesystem import DEFAULT_POLL_INTERVAL, clear_ready_files, marker_path, touch_marker
from src.sync.helics_sync import HelicsSync, coordinator_endpoint, plc_endpoint, scada_endpoint


def ensure_root() -> None:
    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", sys.executable] + sys.argv)


def ns_cmd(namespace: str, python_bin: str, module: str, args: list[str]) -> list[str]:
    return ["ip", "netns", "exec", namespace, python_bin, "-m", module] + args


def scada_namespace(rt) -> str:  # type: ignore[no-untyped-def]
    scada_ns = str(rt.raw.get("scada", {}).get("namespace", "ns-scada"))
    for ep in rt.raw.get("network", {}).get("nodes", {}).get("endpoints", []) or []:
        if isinstance(ep, dict) and ep.get("role") == "scada" and ep.get("namespace"):
            scada_ns = str(ep["namespace"])
            break
    return scada_ns


def merge_actuator_files(paths: list[Path]) -> dict[str, bool]:
    merged: dict[str, bool] = {}
    for p in paths:
        if not p.exists():
            continue
        data = read_json(p)
        for k, v in (data.get("tags", {}) or {}).items():
            merged[str(k)] = bool(v)
    return merged


def _open_log(path: Path):  # type: ignore[no-untyped-def]
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a", encoding="utf-8")


def _check_processes(processes: dict[str, subprocess.Popen]) -> None:
    dead = []
    for name, proc in processes.items():
        rc = proc.poll()
        if rc is not None and rc != 0:
            dead.append(f"{name}(rc={rc})")
    if dead:
        raise RuntimeError("persistent runtime process exited unexpectedly: " + ", ".join(dead))


def wait_for_markers_checked(paths: list[Path], processes: dict[str, subprocess.Popen], *, timeout: float, poll_interval: float, stop_dir: Path) -> None:
    start = time.monotonic()
    while True:
        _check_processes(processes)
        pending = [p for p in paths if not p.exists()]
        if not pending:
            return
        if time.monotonic() - start > timeout:
            raise TimeoutError("timeout waiting for markers: " + ", ".join(str(p) for p in pending))
        time.sleep(max(0.001, poll_interval))


def wait_for_marker_checked(path: Path, processes: dict[str, subprocess.Popen], *, timeout: float, poll_interval: float, stop_dir: Path) -> None:
    wait_for_markers_checked([path], processes, timeout=timeout, poll_interval=poll_interval, stop_dir=stop_dir)


def launch_daemons(
    args: argparse.Namespace,
    rt,
    project_root: Path,
    runtime_dir: Path,
    sync_dir: Path,
    *,
    start_iteration: int,
    max_iterations: int,
) -> tuple[dict[str, subprocess.Popen], list[Any]]:  # type: ignore[no-untyped-def]
    processes: dict[str, subprocess.Popen] = {}
    log_handles: list[Any] = []
    logs_dir = runtime_dir / "logs"

    common_runtime_args = [
        "--config", str(args.config.resolve()),
        "--port", str(args.modbus_port),
        "--unit-id", str(args.unit_id),
        "--timeout", str(args.timeout),
        "--sync-dir", str(sync_dir),
        "--runtime-dir", str(runtime_dir),
        "--start-iteration", str(start_iteration),
        "--max-iterations", str(max_iterations),
        "--poll-interval", str(args.poll_interval),
        "--sync-timeout", str(args.sync_timeout),
        "--sync-backend", str(args.sync_backend),
        "--helics-core-type", str(args.helics_core_type),
        "--helics-core-init", str(args.helics_core_init),
        "--helics-broker-address", str(args.helics_broker_address),
        "--helics-time-delta", str(args.helics_time_delta),
        "--helics-prefix", str(args.helics_prefix),
        "--helics-log-level", str(args.helics_log_level),
    ]

    for plc in rt.plcs.values():
        log = _open_log(logs_dir / f"plc_adapter_{plc.lower_name}.log")
        log_handles.append(log)
        cmd = ns_cmd(
            plc.namespace,
            args.python_bin,
            "src.plc.adapter",
            [
                "daemon",
                *common_runtime_args,
                "--plc", plc.name,
                "--scope", "local",
                "--connect-retries", str(args.connect_retries),
                "--connect-retry-delay", str(args.connect_retry_delay),
            ],
        )
        print("[LAUNCH]", " ".join(cmd))
        processes[f"adapter:{plc.name}"] = subprocess.Popen(cmd, cwd=str(project_root), stdout=log, stderr=subprocess.STDOUT, text=True)

    scada_log = _open_log(logs_dir / "scada_daemon.log")
    log_handles.append(scada_log)
    scada_runtime_args = [
        *common_runtime_args,
        "--modbus-workers", str(args.scada_modbus_workers),
        "--connect-retries", str(args.connect_retries),
        "--connect-retry-delay", str(args.connect_retry_delay),
        "--timeout-grace-iterations", str(args.scada_timeout_grace_iterations),
    ]
    if args.no_batch_modbus:
        scada_runtime_args.append("--no-batch-modbus")
    if args.no_persistent_scada_connections:
        scada_runtime_args.append("--no-persistent-scada-connections")

    scada_cmd = ns_cmd(
        scada_namespace(rt),
        args.python_bin,
        "src.scada.client",
        ["daemon", *scada_runtime_args],
    )
    print("[LAUNCH]", " ".join(scada_cmd))
    processes["scada"] = subprocess.Popen(scada_cmd, cwd=str(project_root), stdout=scada_log, stderr=subprocess.STDOUT, text=True)

    time.sleep(args.daemon_start_wait)
    _check_processes(processes)
    return processes, log_handles




def _send_helics_stop(sync: HelicsSync | None, rt) -> None:  # type: ignore[no-untyped-def]
    if sync is None:
        return
    try:
        sync.send(scada_endpoint(sync.prefix), "stop", payload={"reason": "coordinator shutdown"})
        for plc in rt.plcs.values():
            sync.send(plc_endpoint(plc.lower_name, sync.prefix), "stop", payload={"reason": "coordinator shutdown"})
        sync.flush_time()
    except Exception as exc:
        print(f"[HELICS][WARN] failed to send stop messages: {exc}")

def stop_daemons(
    processes: dict[str, subprocess.Popen],
    log_handles: list[Any],
    sync_dir: Path,
    *,
    grace: float = 5.0,
    terminate_grace: float = 3.5,
) -> None:
    try:
        touch_marker(marker_path(sync_dir, "stop"), {"reason": "coordinator shutdown"})
    except Exception:
        pass

    deadline = time.monotonic() + grace
    for proc in processes.values():
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass

    for name, proc in processes.items():
        if proc.poll() is None:
            print(f"[STOP] terminate {name}")
            proc.terminate()
    terminate_deadline = time.monotonic() + max(0.0, terminate_grace)
    for proc in processes.values():
        if proc.poll() is not None:
            continue
        remaining = max(0.0, terminate_deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass
    for name, proc in processes.items():
        if proc.poll() is None:
            print(f"[STOP] kill {name}")
            proc.kill()

    for h in log_handles:
        try:
            h.close()
        except Exception:
            pass


def _metric_processes(
    rt,  # type: ignore[no-untyped-def]
    runtime_dir: Path,
    processes: Mapping[str, int | subprocess.Popen],
    transient_roots: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Return explicit, de-duplicated PIDs for resource sampling.

    The coordinator's child tree contains the Python PLC/SCADA daemons, so
    recursively sampling it as well as named daemons double-counts resources.
    OpenPLC and ns-3 are launched by ``run_all.sh`` and are not coordinator
    children; their PID files are therefore included explicitly.
    """

    monitored: dict[str, int] = {}
    seen: set[int] = set()

    def add(component: str, raw_pid: Any) -> None:
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            return
        if pid <= 0 or pid in seen:
            return
        seen.add(pid)
        monitored[component] = pid

    def add_pid_file(component: str, path: Path) -> int | None:
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        add(component, pid)
        return pid

    add("coordinator", os.getpid())
    for name, proc in processes.items():
        add(name, getattr(proc, "pid", proc))
    for plc in rt.plcs.values():
        add_pid_file(f"openplc:{plc.name}", rt.output_dir / "run" / f"{plc.lower_name}.pid")
    ns3_pid = add_pid_file("ns3", rt.output_dir / "logs" / "ns3_network.pid")
    if ns3_pid is not None:
        for child_pid in _process_descendants(ns3_pid):
            add(f"ns3:child:{child_pid}", child_pid)
    for pid_path in sorted((runtime_dir / "attacks").glob("*.pid")):
        add_pid_file(f"attack:{pid_path.stem}", pid_path)
    for component, root_pid in (transient_roots or {}).items():
        add(component, root_pid)
        for child_pid in _process_descendants(root_pid):
            add(f"{component}:child:{child_pid}", child_pid)
    return monitored


def _process_descendants(root_pid: int) -> list[int]:
    """Read Linux procfs to find launcher descendants without requiring psutil."""

    descendants: list[int] = []
    pending = [int(root_pid)]
    seen = {int(root_pid)}
    while pending:
        parent = pending.pop()
        children: set[int] = set()
        try:
            task_dirs = list((Path("/proc") / str(parent) / "task").iterdir())
        except OSError:
            continue
        for task_dir in task_dirs:
            try:
                raw = (task_dir / "children").read_text(encoding="utf-8")
            except OSError:
                continue
            for token in raw.split():
                try:
                    child = int(token)
                except ValueError:
                    continue
                if child > 0:
                    children.add(child)
        for child in sorted(children):
            if child in seen:
                continue
            seen.add(child)
            descendants.append(child)
            pending.append(child)
    return descendants


class _MetricProcessRegistry:
    """Thread-safe resolver for only the processes owned by this experiment."""

    def __init__(self, rt, runtime_dir: Path) -> None:  # type: ignore[no-untyped-def]
        self._rt = rt
        self._runtime_dir = runtime_dir
        self._lock = threading.RLock()
        self._runtime_processes: dict[str, int] = {}
        self._transient_roots: dict[str, int] = {}

    def set_runtime_processes(self, processes: Mapping[str, int | subprocess.Popen]) -> None:
        with self._lock:
            self._runtime_processes = {
                str(name): int(getattr(proc, "pid", proc))
                for name, proc in processes.items()
            }

    def add_transient_root(self, component: str, pid: int) -> None:
        with self._lock:
            self._transient_roots[str(component)] = int(pid)

    def remove_transient_root(self, component: str, pid: int) -> None:
        with self._lock:
            if self._transient_roots.get(str(component)) == int(pid):
                self._transient_roots.pop(str(component), None)

    def resolve(self) -> dict[str, int]:
        with self._lock:
            runtime_processes = dict(self._runtime_processes)
            transient_roots = dict(self._transient_roots)
        return _metric_processes(
            self._rt,
            self._runtime_dir,
            runtime_processes,
            transient_roots,
        )


def _write_physics_csv(runtime_dir: Path, rt, snapshot: dict[str, Any], *, filename: str = "physics.csv") -> None:  # type: ignore[no-untyped-def]
    """Write simulator state in flat DHALSIM-compatible CSV format."""
    write_physics_row(runtime_dir, rt, snapshot, filename=filename)


def _write_actuator_state_csv(
    runtime_dir: Path,
    iteration: int,
    actuator_state: dict[str, bool],
) -> None:
    row: dict[str, Any] = {"iteration": iteration}
    for name, value in sorted(actuator_state.items()):
        row[f"actuator.{name}"] = value
    append_jsonl(raw_dir(runtime_dir) / "actuator_state.jsonl", {"iteration": iteration, **dict(sorted(actuator_state.items()))})
    append_row(csv_dir(runtime_dir) / "actuator_state.csv", row, fixed_columns=["iteration"])


def _write_closed_loop_csv(runtime_dir: Path, summary: dict[str, Any]) -> None:
    append_row(
        csv_dir(runtime_dir) / "closed_loop.csv",
        summary,
        fixed_columns=[
            "iteration",
            "input_physics_iteration",
            "output_physics_iteration",
            "physics_backend",
            "physics_value_count",
            "actuator_count",
            "duration_sec",
            "input_physics_json",
            "output_physics_json",
            "scada_poll_json",
            "scada_downlink_json",
            "actuator_state_json",
        ],
    )


def _write_closed_loop_timing_csv(runtime_dir: Path, timing: dict[str, Any]) -> None:
    append_jsonl(raw_dir(runtime_dir) / "cycle_timing.jsonl", dict(timing))
    append_row(
        csv_dir(runtime_dir) / "closed_loop_timing.csv",
        timing,
        fixed_columns=[
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
        ],
    )


def _write_timing_summary_csv(runtime_dir: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    stage_columns = [
        "wait_local_write_sec",
        "wait_scada_downlink_sec",
        "logic_wait_sec",
        "signal_read_actuators_sec",
        "wait_actuator_read_sec",
        "merge_actuator_state_sec",
        "physics_step_publish_sec",
        "write_cycle_logs_sec",
        "cycle_total_sec",
    ]
    out = csv_dir(runtime_dir) / "closed_loop_timing_summary.csv"
    # overwrite summary so repeated calls during development do not append stale rows
    if out.exists():
        out.unlink()
    for stage in stage_columns:
        values = [float(row.get(stage, 0.0) or 0.0) for row in rows]
        if not values:
            continue
        append_row(
            out,
            {
                "stage": stage,
                "count": len(values),
                "total_sec": sum(values),
                "avg_sec": sum(values) / len(values),
                "min_sec": min(values),
                "max_sec": max(values),
            },
            fixed_columns=["stage", "count", "total_sec", "avg_sec", "min_sec", "max_sec"],
        )


def persist_physics_snapshot(
    runtime_dir: Path,
    rt,
    iteration: int,
    snapshot: dict[str, Any],
    *,
    event_logger: EventLogger | None = None,
) -> Path:  # type: ignore[no-untyped-def]
    """Persist one physics state without releasing the synchronization marker."""
    physics_path = json_dir(runtime_dir) / f"physics_{iteration:04d}.json"
    snapshot = dict(snapshot)
    snapshot["iteration"] = iteration
    write_json(physics_path, snapshot)
    append_jsonl(raw_dir(runtime_dir) / "physics.jsonl", snapshot)
    _write_physics_csv(runtime_dir, rt, snapshot)
    if event_logger is not None:
        values = snapshot.get("values", {}) or {}
        events = [make_event(
            iteration=iteration,
            layer="physical",
            component="epanet",
            event_type="physics_snapshot_persisted",
            source="epanet",
            target="coordinator",
            status="success",
            details={
                "backend": snapshot.get("backend", ""),
                "value_count": len(values),
                "path": str(physics_path),
            },
        )]
        for name, value in sorted(values.items()):
            events.append(make_event(
                iteration=iteration,
                layer="physical",
                component="epanet",
                event_type="physics_sensor_value",
                source="epanet",
                target="coordinator",
                variable=str(name),
                value=value,
                status="sampled",
            ))
        safe_log_many(event_logger, events)
    return physics_path


def release_physics_snapshot(
    sync_dir: Path,
    iteration: int,
    physics_path: Path,
    snapshot: dict[str, Any],
    *,
    sync_backend: str = "filesystem",
    helics_sync: HelicsSync | None = None,
    rt=None,
) -> None:
    """Release waiting PLC/SCADA daemons after a physics state is fully prepared."""
    payload = {
        "iteration": iteration,
        "output": str(physics_path),
        "backend": snapshot.get("backend"),
        "advanced": bool(snapshot.get("advanced", False)),
    }
    if sync_backend == "helics":
        if helics_sync is None or rt is None:
            raise RuntimeError("HELICS sync backend requires helics_sync and runtime config")
        for plc in rt.plcs.values():
            helics_sync.send(plc_endpoint(plc.lower_name, helics_sync.prefix), "physics", iteration, payload)
        helics_sync.flush_time()
    else:
        touch_marker(marker_path(sync_dir, "physics", iteration), payload)


def publish_physics_snapshot(
    runtime_dir: Path,
    sync_dir: Path,
    rt,
    iteration: int,
    snapshot: dict[str, Any],
    *,
    sync_backend: str = "filesystem",
    helics_sync: HelicsSync | None = None,
    event_logger: EventLogger | None = None,
) -> Path:  # type: ignore[no-untyped-def]
    """Persist one physics state and release waiting PLC/SCADA daemons."""
    physics_path = persist_physics_snapshot(runtime_dir, rt, iteration, snapshot, event_logger=event_logger)
    release_physics_snapshot(sync_dir, iteration, physics_path, snapshot, sync_backend=sync_backend, helics_sync=helics_sync, rt=rt)
    return physics_path


def _run_bootstrap_cmd(cmd: list[str], *, project_root: Path, log) -> None:  # type: ignore[no-untyped-def]
    printable = " ".join(cmd)
    print(f"[BOOTSTRAP] {printable}")
    log.write(f"$ {printable}\n")
    log.flush()
    attempts = 5
    last_exc: subprocess.CalledProcessError | None = None
    for attempt in range(1, attempts + 1):
        try:
            subprocess.run(cmd, cwd=str(project_root), stdout=log, stderr=subprocess.STDOUT, text=True, check=True)
            log.flush()
            return
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            wait_sec = 0.5 * attempt
            msg = f"[BOOTSTRAP][WARN] attempt {attempt}/{attempts} failed rc={exc.returncode}; retry in {wait_sec:.1f}s\n"
            print(msg.rstrip())
            log.write(msg)
            log.flush()
            time.sleep(wait_sec)
    log.flush()
    if last_exc is not None:
        raise last_exc


def bootstrap_preload_initial_state(
    args: argparse.Namespace,
    rt,  # type: ignore[no-untyped-def]
    project_root: Path,
    runtime_dir: Path,
    physics_path: Path,
    actuator_state: dict[str, bool],
    *,
    iteration: int = 0,
) -> None:
    """Preload PLC memory before releasing the first PLC-visible physics marker.

    OpenPLC scans continuously. Without this preload, dependency variables such
    as PLC4.PLC9_T7 can remain at the default 0.0 for a few scans, which can
    incorrectly switch hysteresis outputs such as PU10 before SCADA has written
    the real initial physics value.

    Sequence:
      1. write configured initial actuator states to all PLC coils;
      2. write local initial physics sensors in each PLC namespace;
      3. write cross-PLC dependency sensors directly from the initial state using
         the SCADA namespace, without relying on an initial poll;
      4. write the initial actuator states again to erase any scan that may have
         happened while default inputs were still being replaced;
      5. wait one scan window with correct inputs loaded.
    """
    logs_dir = runtime_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_json_dir = json_dir(runtime_dir)
    actuator_state_path = out_json_dir / "actuator_state_initial.json"
    write_json(actuator_state_path, actuator_state)

    with _open_log(logs_dir / "bootstrap_preload.log") as log:
        log.write("[BOOTSTRAP] preload initial cycle-0 PLC memory\n")
        log.write(f"physics={physics_path}\n")
        log.write(f"actuators={actuator_state_path}\n")
        log.flush()

        common_plc_args = [
            "--config", str(args.config.resolve()),
            "--port", str(args.modbus_port),
            "--unit-id", str(args.unit_id),
            "--timeout", str(args.timeout),
        ]

        # First initialize coils. This gives hysteresis logic the correct
        # memory state before normal cycle-0 control begins.
        for plc in rt.plcs.values():
            cmd = ns_cmd(
                plc.namespace,
                args.python_bin,
                "src.plc.adapter",
                [
                    "write-initial-actuators",
                    *common_plc_args,
                    "--plc", plc.name,
                    "--state", str(actuator_state_path),
                    "--out", str(out_json_dir / f"bootstrap_actuators_pre_0000_{plc.lower_name}.json"),
                ],
            )
            _run_bootstrap_cmd(cmd, project_root=project_root, log=log)

        # Then preload each PLC's local physical inputs from physics_0000.
        for plc in rt.plcs.values():
            cmd = ns_cmd(
                plc.namespace,
                args.python_bin,
                "src.plc.adapter",
                [
                    "write-sensors",
                    *common_plc_args,
                    "--plc", plc.name,
                    "--state", str(physics_path),
                    "--scope", "local",
                    "--out", str(out_json_dir / f"bootstrap_local_write_0000_{plc.lower_name}.json"),
                ],
            )
            _run_bootstrap_cmd(cmd, project_root=project_root, log=log)

        # Preload cross-PLC dependency variables directly from physics_0000.
        # This avoids the cold-start route PLC9 -> poll -> PLC4 where PLC4 may
        # scan PLC9_T7=0.0 before the first SCADA downlink.
        scada_cmd = ns_cmd(
            scada_namespace(rt),
            args.python_bin,
            "src.scada.client",
            [
                "downlink",
                "--config", str(args.config.resolve()),
                "--port", str(args.modbus_port),
                "--unit-id", str(args.unit_id),
                "--timeout", str(args.timeout),
                "--modbus-workers", str(args.scada_modbus_workers),
                *( ["--no-batch-modbus"] if args.no_batch_modbus else [] ),
                "--physics", str(physics_path),
                "--out", str(out_json_dir / "bootstrap_scada_downlink_0000.json"),
            ],
        )
        _run_bootstrap_cmd(scada_cmd, project_root=project_root, log=log)

        # Reset initial coils after preload, then let OpenPLC scan once with all
        # correct initial inputs present. Outside-threshold conditions will be
        # intentionally updated by ST during the wait; deadband variables retain
        # the configured initial state.
        for plc in rt.plcs.values():
            cmd = ns_cmd(
                plc.namespace,
                args.python_bin,
                "src.plc.adapter",
                [
                    "write-initial-actuators",
                    *common_plc_args,
                    "--plc", plc.name,
                    "--state", str(actuator_state_path),
                    "--out", str(out_json_dir / f"bootstrap_actuators_post_0000_{plc.lower_name}.json"),
                ],
            )
            _run_bootstrap_cmd(cmd, project_root=project_root, log=log)

        if args.bootstrap_wait > 0:
            print(f"[BOOTSTRAP] wait PLC scan {args.bootstrap_wait:.3f}s")
            log.write(f"[BOOTSTRAP] wait PLC scan {args.bootstrap_wait:.3f}s\n")
            log.flush()
            time.sleep(args.bootstrap_wait)

    append_row(
        csv_dir(runtime_dir) / "bootstrap.csv",
        {
            "iteration": iteration,
            "physics_json": str(physics_path),
            "actuator_state_json": str(actuator_state_path),
            "plc_count": len(rt.plcs),
            "actuator_count": len(actuator_state),
            "bootstrap_wait": args.bootstrap_wait,
        },
        fixed_columns=["iteration", "physics_json", "actuator_state_json", "plc_count", "actuator_count", "bootstrap_wait"],
    )



def prepare_runtime_csv_dir(runtime_dir: Path) -> None:
    """Start each run with fresh operator-facing CSV outputs."""
    path = csv_dir(runtime_dir)
    if path.exists():
        for old in path.glob("*.csv"):
            old.unlink()


def prepare_runtime_raw_dir(runtime_dir: Path) -> None:
    """Start each run with fresh structured raw JSONL outputs."""
    path = raw_dir(runtime_dir)
    if path.exists():
        for old in path.glob("*.jsonl"):
            old.unlink()
        # Writer snapshots are per-execution quality evidence.  Keeping a
        # previous PID's clean snapshot could hide a writer killed this run.
        writer_stats = path / "metric_writer_stats"
        if writer_stats.exists():
            shutil.rmtree(writer_stats)


def prepare_runtime_json_dir(runtime_dir: Path) -> None:
    """Remove prior per-iteration JSON so a partial rerun cannot reuse it."""
    path = json_dir(runtime_dir)
    if path.exists():
        for old in path.glob("*.json"):
            old.unlink()


def _runtime_end_status(run_error: BaseException | None, cleanup_errors: list[str]) -> str:
    if run_error is not None:
        return "error"
    return "cleanup_error" if cleanup_errors else "success"



def _enabled_attack_scenarios(raw_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    attacks = raw_cfg.get("attacks", {}) or {}
    if isinstance(attacks, dict) and not bool(attacks.get("enabled", False)):
        return []
    if isinstance(attacks, list):
        return [x for x in attacks if isinstance(x, dict) and bool(x.get("enabled", True))]
    scenarios = attacks.get("scenarios", []) if isinstance(attacks, dict) else []
    return [x for x in scenarios if isinstance(x, dict) and bool(x.get("enabled", True))]


def _has_iteration_window_attacks(raw_cfg: dict[str, Any]) -> bool:
    for scenario in _enabled_attack_scenarios(raw_cfg):
        trig = scenario.get("trigger", scenario.get("schedule", {})) or {}
        if not isinstance(trig, dict):
            continue
        trig_type = str(trig.get("type", "iteration_window")).lower()
        if trig_type in {"iteration", "iteration_window", "round", "round_window"}:
            return True
    return False


def _run_attack_scheduler(
    cmd: list[str],
    *,
    project_root: Path,
    process_registry: _MetricProcessRegistry | None,
    component: str,
) -> None:
    """Run one scheduler command while exposing its exact process tree."""

    proc = subprocess.Popen(cmd, cwd=str(project_root), text=True)
    if process_registry is not None:
        process_registry.add_transient_root(component, proc.pid)
    try:
        returncode = proc.wait()
    finally:
        if process_registry is not None:
            process_registry.remove_transient_root(component, proc.pid)
    if returncode:
        raise subprocess.CalledProcessError(returncode, cmd)


def _sync_attacks_for_iteration(
    args: argparse.Namespace,
    project_root: Path,
    runtime_dir: Path,
    iteration: int,
    *,
    process_registry: _MetricProcessRegistry | None = None,
) -> None:
    """Synchronize configured attack state before releasing physics_iteration.

    MITM proxies and DNAT rules stay online for the full experiment, so SCADA
    can keep persistent Modbus/TCP connections.  This call starts missing
    proxies/routing rules and writes the active/transparent state file.  Thus an
    attack configured for start_iteration=20 is active before physics_0020.ready
    is released.
    """
    cmd = [
        args.python_bin,
        "-m",
        "src.attack.launch",
        "--config",
        str(args.config.resolve()),
        "--action",
        "sync",
        "--iteration",
        str(iteration),
        "--runtime-dir",
        str(runtime_dir),
        "--python",
        args.python_bin,
    ]
    print(f"[ATTACK-SCHED] sync before releasing physics_{iteration:04d}: {' '.join(cmd)}")
    _run_attack_scheduler(
        cmd,
        project_root=project_root,
        process_registry=process_registry,
        component="attack-scheduler:sync",
    )


def _stop_configured_attacks(
    args: argparse.Namespace,
    project_root: Path,
    runtime_dir: Path,
    *,
    process_registry: _MetricProcessRegistry | None = None,
) -> None:
    cmd = [
        args.python_bin,
        "-m",
        "src.attack.launch",
        "--config",
        str(args.config.resolve()),
        "--action",
        "stop",
        "--runtime-dir",
        str(runtime_dir),
        "--python",
        args.python_bin,
    ]
    _run_attack_scheduler(
        cmd,
        project_root=project_root,
        process_registry=process_registry,
        component="attack-scheduler:stop",
    )

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run persistent Hydro-CPS-Sim closed-loop control")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--iterations", type=int, default=None)
    p.add_argument("--python", dest="python_bin", default=sys.executable, help="Python executable visible inside namespaces")
    p.add_argument("--physics-mode", choices=["dhalsim_epynet", "epynet"], default="dhalsim_epynet")
    p.add_argument("--modbus-port", type=int, default=502)
    p.add_argument("--unit-id", type=int, default=1)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--logic-wait", type=float, default=0.30, help="Seconds to wait after SCADA downlink before actuator read")
    p.add_argument("--cycle-wait", type=float, default=0.0)
    p.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help="Filesystem marker polling interval in seconds. Default: env HYDRO_CPS_POLL_INTERVAL or 0.005")
    p.add_argument("--sync-timeout", type=float, default=30.0)
    p.add_argument("--sync-backend", choices=["filesystem", "helics"], default="filesystem", help="Synchronization backend for runtime cycle signals")
    p.add_argument("--helics-core-type", default="ipc", help="HELICS core type, e.g. ipc, zmq, tcp")
    p.add_argument("--helics-core-init", default="", help="HELICS core init string passed to each federate")
    p.add_argument("--helics-broker-address", default="", help="Optional HELICS broker address, e.g. tcp://127.0.0.1:23405")
    p.add_argument("--helics-time-delta", type=float, default=0.001, help="HELICS time delta used while waiting for messages")
    p.add_argument("--helics-prefix", default="hydro", help="HELICS endpoint prefix")
    p.add_argument("--helics-log-level", type=int, default=1)
    p.add_argument("--daemon-start-wait", type=float, default=0.8)
    p.add_argument(
        "--init-style",
        choices=["dhalsim", "current"],
        default="dhalsim",
        help="Initialization/iteration semantics. 'dhalsim' writes row 0 all-zero and row 1 configured initial state; 'current' keeps the old physics_0000 t=0 snapshot behavior.",
    )
    p.add_argument("--no-bootstrap-preload", action="store_true", help="Disable cycle-0 PLC memory preload")
    p.add_argument("--bootstrap-wait", type=float, default=0.30, help="Seconds to wait after bootstrap preload before releasing the initial physics marker")
    p.add_argument("--connect-retries", type=int, default=60)
    p.add_argument("--connect-retry-delay", type=float, default=0.25)
    p.add_argument("--scada-modbus-workers", type=int, default=8, help="Concurrent PLC Modbus workers used by the SCADA daemon")
    p.add_argument("--scada-timeout-grace-iterations", type=int, default=1, help="Initial SCADA cycles whose Modbus timeouts are warmup-only and excluded from timeout event reports")
    p.add_argument("--no-batch-modbus", action="store_true", help="Disable batched SCADA/PLC Modbus reads/writes")
    p.add_argument("--no-persistent-scada-connections", action="store_true", help="Reconnect SCADA Modbus clients every cycle instead of reusing TCP connections")
    p.add_argument("--no-root-check", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if not args.no_root_check:
        ensure_root()

    args.config = args.config.resolve()
    project_root = Path(__file__).resolve().parents[2]
    rt = load_runtime_config(args.config)
    args.iterations = args.iterations if args.iterations is not None else rt.iterations

    runtime_dir = rt.output_dir / "runtime"
    sync_dir = runtime_dir / "sync"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    json_output_dir = json_dir(runtime_dir)
    prepare_runtime_csv_dir(runtime_dir)
    prepare_runtime_raw_dir(runtime_dir)
    prepare_runtime_json_dir(runtime_dir)
    clear_ready_files(sync_dir)
    write_json(json_dir(runtime_dir) / "runtime_map.json", rt.to_summary_dict())
    write_json(runtime_dir / "run_started.json", {
        "wall_time_ns": time.time_ns(),
        "pid": os.getpid(),
        "config": str(args.config),
        "iterations": args.iterations,
    })

    metrics_cfg = rt.raw.get("metrics", {}) or {}
    metrics_enabled = isinstance(metrics_cfg, dict) and bool(metrics_cfg.get("enabled", False))
    event_logger: EventLogger | None = None
    if metrics_enabled and bool(metrics_cfg.get("event_log", True)):
        try:
            event_logger = EventLogger(csv_dir(runtime_dir) / "events.csv")
        except Exception as exc:
            print(f"[METRICS][WARN] coordinator event metrics disabled: {exc}")
    manifest_path = runtime_dir / "manifest.json"
    resolved_config_path = runtime_dir / "config_resolved.yaml"
    try:
        manifest_path, resolved_config_path = write_manifest(
            args.config,
            runtime_dir,
            project_root=project_root,
        )
    except Exception as exc:
        print(f"[METRICS][WARN] manifest unavailable: {exc}")
    if event_logger is not None:
        safe_log(event_logger, make_event(
            iteration=-1,
            layer="runtime",
            component="coordinator",
            event_type="simulation_start",
            source="coordinator",
            status="started",
            details={
                "config": str(args.config),
                "manifest": str(manifest_path),
                "resolved_config": str(resolved_config_path),
                "iterations": args.iterations,
            },
        ))

    if args.dry_run:
        print("[DRY-RUN] Runtime map and sync directory prepared only.")
        safe_log(event_logger, make_event(
            iteration=-1,
            layer="runtime",
            component="coordinator",
            event_type="simulation_end",
            source="coordinator",
            status="dry_run",
            details={"completed_cycles": 0},
        ))
        return 0

    try:
        physics = PhysicsEngine(rt, mode=args.physics_mode)
    except BaseException as exc:
        safe_log(event_logger, make_event(
            iteration=-1,
            layer="runtime",
            component="coordinator",
            event_type="simulation_end",
            source="coordinator",
            status="error",
            details={
                "stage": "physics_initialization",
                "completed_cycles": 0,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        ))
        raise
    actuator_state = dict(rt.actuator_initial_state)

    attack_scheduling_enabled = bool(_enabled_attack_scenarios(rt.raw))

    print("=" * 80)
    print("[PERSISTENT-CLOSED-LOOP] Hydro-CPS-Sim runtime started")
    print(f"[CONFIG]       {args.config}")
    print(f"[PROJECT]      {project_root}")
    print(f"[RUNTIME]      {runtime_dir}")
    print(f"[SYNC]         {sync_dir}")
    print(f"[ITERATIONS]   {args.iterations}")
    print(f"[PYTHON]       {args.python_bin}")
    print(f"[PHYSICS]      requested={args.physics_mode}, available={physics.available}, warning={physics.warning}")
    print("[PLCS]")
    for plc in rt.plcs.values():
        print(f"  - {plc.name:5s} ns={plc.namespace:10s} ip={plc.ip:15s} md={len(plc.md_vars)} coils={len(plc.coil_vars)}")
    print("=" * 80)

    processes: dict[str, subprocess.Popen] = {}
    log_handles: list[Any] = []
    cycle_summaries: list[dict[str, Any]] = []
    timing_rows: list[dict[str, Any]] = []
    coord_sync: HelicsSync | None = None
    resource_monitor: RuntimeMonitor | None = None
    process_registry = _MetricProcessRegistry(rt, runtime_dir)
    run_error: BaseException | None = None
    cleanup_errors: list[str] = []
    metric_writer_quality: dict[str, Any] | None = None
    current_iteration = -1

    try:
        if args.init_style == "dhalsim":
            # DHALSIM exports two initialization rows before the first solved
            # hydraulic state: row 0 is all-zero bootstrap, row 1 is configured
            # tank/actuator initial state.  Row 0 is persisted for CSV alignment
            # only; it is not released to PLC/SCADA to avoid false zero inputs.
            zero_snapshot = physics.dhalsim_zero_snapshot(iteration=0)
            zero_path = persist_physics_snapshot(runtime_dir, rt, 0, zero_snapshot, event_logger=event_logger)
            print(
                f"[INIT] physics_0000 DHALSIM dummy backend={zero_snapshot.get('backend')} "
                f"values={len(zero_snapshot.get('values', {}))} -> {zero_path}"
            )

            initial_iteration = 1
            first_control_iteration = 1
            final_physics_iteration = args.iterations
            control_iterations = max(0, final_physics_iteration - first_control_iteration)
            initial_snapshot = physics.dhalsim_initial_snapshot(iteration=initial_iteration)
        else:
            initial_iteration = 0
            first_control_iteration = 0
            final_physics_iteration = args.iterations
            control_iterations = max(0, final_physics_iteration - first_control_iteration)
            initial_snapshot = physics.current_snapshot(iteration=initial_iteration)

        if metrics_enabled and bool(metrics_cfg.get("resource_monitor", True)):
            sample_interval = float(metrics_cfg.get("sample_interval_sec", 0.5) or 0.5)
            try:
                resource_monitor = RuntimeMonitor(
                    csv_dir(runtime_dir) / "resources.csv",
                    process_registry.resolve(),
                    interval_sec=sample_interval,
                    include_process_tree=False,
                    process_resolver=process_registry.resolve,
                ).start()
            except Exception as exc:
                resource_monitor = None
                print(f"[METRICS][WARN] resource monitoring disabled: {exc}")
                cleanup_errors.append(f"resources_start:{type(exc).__name__}:{exc}")

        if attack_scheduling_enabled:
            # Bring MITM proxies and DNAT rules online before the SCADA daemon starts.
            # The proxy begins in transparent mode outside the configured iteration
            # window, allowing SCADA to keep persistent Modbus/TCP connections while
            # the proxy toggles modification behavior by state file.
            _sync_attacks_for_iteration(
                args,
                project_root,
                runtime_dir,
                initial_iteration,
                process_registry=process_registry,
            )

        processes, log_handles = launch_daemons(
            args,
            rt,
            project_root,
            runtime_dir,
            sync_dir,
            start_iteration=first_control_iteration,
            max_iterations=control_iterations,
        )
        process_registry.set_runtime_processes(processes)
        if resource_monitor is not None:
            try:
                resource_monitor.refresh_processes()
            except Exception as exc:
                print(f"[METRICS][WARN] could not refresh monitored processes: {exc}")

        if args.sync_backend == "helics":
            coord_sync = HelicsSync.from_args(
                "hydro_coordinator",
                coordinator_endpoint(args.helics_prefix),
                args,
                timeout=args.sync_timeout,
            ).start()
            print(f"[HELICS] coordinator federate ready endpoint={coord_sync.endpoint}")

        # Persist the initial state used by PLC/SCADA, but do not release the
        # marker until PLC memories have been preloaded. This prevents cold-start
        # scans over default 0.0 dependency values from corrupting hysteresis
        # outputs.
        input_physics_path = persist_physics_snapshot(
            runtime_dir,
            rt,
            initial_iteration,
            initial_snapshot,
            event_logger=event_logger,
        )
        print(
            f"[INIT] physics_{initial_iteration:04d} backend={initial_snapshot.get('backend')} "
            f"values={len(initial_snapshot.get('values', {}))} -> {input_physics_path}"
        )

        if not args.no_bootstrap_preload:
            bootstrap_preload_initial_state(args, rt, project_root, runtime_dir, input_physics_path, actuator_state, iteration=initial_iteration)
            print("[INIT] bootstrap preload completed")
        else:
            print("[INIT] bootstrap preload disabled")

        release_physics_snapshot(
            sync_dir,
            initial_iteration,
            input_physics_path,
            initial_snapshot,
            sync_backend=args.sync_backend,
            helics_sync=coord_sync,
            rt=rt,
        )
        print(f"[INIT] physics_{initial_iteration:04d}.ready released")

        if control_iterations <= 0:
            print("[DONE] No control cycles requested after initialization.")

        for i in range(first_control_iteration, final_physics_iteration):
            current_iteration = i
            cycle_start = time.monotonic()
            timing: dict[str, Any] = {
                "iteration": i,
                "input_physics_iteration": i,
                "output_physics_iteration": i + 1,
                "local_plc_count": len(rt.plcs),
            }
            print("\n" + "#" * 80)
            print(f"[CYCLE {i}] control physics_{i:04d} -> actuator_state_{i:04d} -> physics_{i + 1:04d}")
            print("#" * 80)

            # 1) PLC adapters consume physics_i and write local sensor registers.
            local_wait_t0 = time.monotonic()
            if args.sync_backend == "helics":
                if coord_sync is None:
                    raise RuntimeError("HELICS coordinator sync is not initialized")
                coord_sync.wait_for("local_write", iteration=i, count=len(rt.plcs), timeout=args.sync_timeout)
            else:
                local_markers = [marker_path(sync_dir, "local_write", i, plc.lower_name) for plc in rt.plcs.values()]
                wait_for_markers_checked(local_markers, processes, timeout=args.sync_timeout, poll_interval=args.poll_interval, stop_dir=sync_dir)
            timing["wait_local_write_sec"] = time.monotonic() - local_wait_t0
            print(f"[CYCLE {i}] all PLC local sensor writes completed from physics_{i:04d}")

            # 2) SCADA polls/downlinks dependency data derived from physics_i.
            scada_wait_t0 = time.monotonic()
            if args.sync_backend == "helics":
                if coord_sync is None:
                    raise RuntimeError("HELICS coordinator sync is not initialized")
                coord_sync.wait_for("scada_downlink", iteration=i, count=1, timeout=args.sync_timeout)
            else:
                scada_marker = marker_path(sync_dir, "scada_downlink", i)
                wait_for_marker_checked(scada_marker, processes, timeout=args.sync_timeout, poll_interval=args.poll_interval, stop_dir=sync_dir)
            timing["wait_scada_downlink_sec"] = time.monotonic() - scada_wait_t0
            print(f"[CYCLE {i}] SCADA poll/downlink completed")

            # 3) Allow OpenPLC one scan window after SCADA writes before reading coils.
            logic_wait_t0 = time.monotonic()
            if args.logic_wait > 0:
                print(f"[CYCLE {i}] wait PLC logic {args.logic_wait:.3f}s")
                time.sleep(args.logic_wait)
            timing["logic_wait_sec"] = time.monotonic() - logic_wait_t0

            # 4) Read u_i from PLC coils.
            signal_t0 = time.monotonic()
            if args.sync_backend == "helics":
                if coord_sync is None:
                    raise RuntimeError("HELICS coordinator sync is not initialized")
                for plc in rt.plcs.values():
                    coord_sync.send(plc_endpoint(plc.lower_name, coord_sync.prefix), "read_actuators", i, {"iteration": i})
                coord_sync.flush_time()
            else:
                touch_marker(marker_path(sync_dir, "read_actuators", i), {"iteration": i})
            timing["signal_read_actuators_sec"] = time.monotonic() - signal_t0

            actuator_wait_t0 = time.monotonic()
            if args.sync_backend == "helics":
                if coord_sync is None:
                    raise RuntimeError("HELICS coordinator sync is not initialized")
                coord_sync.wait_for("actuators", iteration=i, count=len(rt.plcs), timeout=args.sync_timeout)
            else:
                actuator_markers = [marker_path(sync_dir, "actuators", i, plc.lower_name) for plc in rt.plcs.values()]
                wait_for_markers_checked(actuator_markers, processes, timeout=args.sync_timeout, poll_interval=args.poll_interval, stop_dir=sync_dir)
            timing["wait_actuator_read_sec"] = time.monotonic() - actuator_wait_t0

            merge_t0 = time.monotonic()
            actuator_paths = [json_output_dir / f"actuators_{i:04d}_{plc.lower_name}.json" for plc in rt.plcs.values()]
            actuator_state.update(merge_actuator_files(actuator_paths))
            actuator_path = json_output_dir / f"actuator_state_{i:04d}.json"
            write_json(actuator_path, actuator_state)
            _write_actuator_state_csv(
                runtime_dir,
                i,
                actuator_state,
            )
            timing["merge_actuator_state_sec"] = time.monotonic() - merge_t0
            print(f"[CYCLE {i}] actuator_state_{i:04d} -> {actuator_path}: {actuator_state}")

            # 5) Apply u_i to the plant interval that produces x_{i+1}.
            physics_t0 = time.monotonic()
            physics_wall_start_ns = time.time_ns()
            physics_monotonic_start_ns = time.monotonic_ns()
            safe_log(event_logger, make_event(
                wall_time_ns=physics_wall_start_ns,
                monotonic_ns=physics_monotonic_start_ns,
                iteration=i + 1,
                layer="physical",
                component="epanet",
                event_type="physics_iteration_start",
                source="coordinator",
                target="epanet",
                status="started",
                details={"actuator_iteration": i},
            ))
            try:
                next_physics_snapshot = physics.step(actuator_state, iteration=i + 1)
            except Exception as exc:
                physics_monotonic_end_ns = time.monotonic_ns()
                safe_log(event_logger, make_event(
                    iteration=i + 1,
                    layer="physical",
                    component="epanet",
                    event_type="physics_iteration_end",
                    source="epanet",
                    target="coordinator",
                    status="error",
                    details={
                        "stage": "physics_step",
                        "duration_ms": (physics_monotonic_end_ns - physics_monotonic_start_ns) / 1_000_000.0,
                        "error": str(exc),
                        "type": type(exc).__name__,
                    },
                ))
                raise
            physics_wall_end_ns = time.time_ns()
            physics_monotonic_end_ns = time.monotonic_ns()
            safe_log_many(event_logger, [
                make_event(
                    wall_time_ns=physics_wall_start_ns,
                    monotonic_ns=physics_monotonic_start_ns,
                    iteration=i + 1,
                    layer="physical",
                    component="epanet",
                    event_type="physics_actuator_input",
                    source="coordinator",
                    target="epanet",
                    variable=name,
                    value=bool(value),
                    status="applied",
                    details={"actuator_iteration": i},
                )
                for name, value in sorted(actuator_state.items())
            ])
            safe_log(event_logger, make_event(
                wall_time_ns=physics_wall_end_ns,
                monotonic_ns=physics_monotonic_end_ns,
                iteration=i + 1,
                layer="physical",
                component="epanet",
                event_type="physics_iteration_end",
                source="epanet",
                target="coordinator",
                status="success",
                details={
                    "duration_ms": (physics_monotonic_end_ns - physics_monotonic_start_ns) / 1_000_000.0,
                    "value_count": len(next_physics_snapshot.get("values", {}) or {}),
                },
            ))
            try:
                output_physics_path = persist_physics_snapshot(
                    runtime_dir,
                    rt,
                    i + 1,
                    next_physics_snapshot,
                    event_logger=event_logger,
                )
            except Exception as exc:
                safe_log(event_logger, make_event(
                    iteration=i + 1,
                    layer="physical",
                    component="persistence",
                    event_type="physics_snapshot_persist_error",
                    source="coordinator",
                    status="error",
                    details={"error": str(exc), "type": type(exc).__name__},
                ))
                raise
            if attack_scheduling_enabled:
                _sync_attacks_for_iteration(
                    args,
                    project_root,
                    runtime_dir,
                    i + 1,
                    process_registry=process_registry,
                )
            release_physics_snapshot(
                sync_dir,
                i + 1,
                output_physics_path,
                next_physics_snapshot,
                sync_backend=args.sync_backend,
                helics_sync=coord_sync,
                rt=rt,
            )
            timing["physics_step_publish_sec"] = time.monotonic() - physics_t0
            timing["physics_value_count"] = len(next_physics_snapshot.get("values", {}) or {})
            timing["actuator_count"] = len(actuator_state)
            print(
                f"[CYCLE {i}] EPANET step actuator_state_{i:04d} -> physics_{i + 1:04d} "
                f"backend={next_physics_snapshot.get('backend')} "
                f"values={len(next_physics_snapshot.get('values', {}))} -> {output_physics_path}"
            )

            log_t0 = time.monotonic()
            summary = {
                "iteration": i,
                "input_physics_iteration": i,
                "output_physics_iteration": i + 1,
                "input_physics": str(input_physics_path),
                "local_write": [str(json_output_dir / f"local_write_{i:04d}_{plc.lower_name}.json") for plc in rt.plcs.values()],
                "scada_poll": str(json_output_dir / f"scada_poll_{i:04d}.json"),
                "scada_downlink": str(json_output_dir / f"scada_downlink_{i:04d}.json"),
                "actuator_state": str(actuator_path),
                "output_physics": str(output_physics_path),
                "init_style": args.init_style,
            }
            cycle_summaries.append(summary)
            timing["cycle_total_sec"] = time.monotonic() - cycle_start
            _write_closed_loop_csv(runtime_dir, {
                "iteration": i,
                "input_physics_iteration": i,
                "output_physics_iteration": i + 1,
                "physics_backend": next_physics_snapshot.get("backend"),
                "physics_value_count": len(next_physics_snapshot.get("values", {}) or {}),
                "actuator_count": len(actuator_state),
                "duration_sec": timing["cycle_total_sec"],
                "input_physics_json": str(input_physics_path),
                "output_physics_json": str(output_physics_path),
                "scada_poll_json": str(json_output_dir / f"scada_poll_{i:04d}.json"),
                "scada_downlink_json": str(json_output_dir / f"scada_downlink_{i:04d}.json"),
                "actuator_state_json": str(actuator_path),
            })
            write_json(json_output_dir / "closed_loop_summary.json", cycle_summaries)
            timing["write_cycle_logs_sec"] = time.monotonic() - log_t0
            timing["cycle_total_sec"] = time.monotonic() - cycle_start
            _write_closed_loop_timing_csv(runtime_dir, timing)
            timing_rows.append(dict(timing))
            print(
                f"[CYCLE {i}] timing "
                f"local={timing['wait_local_write_sec']:.4f}s "
                f"scada={timing['wait_scada_downlink_sec']:.4f}s "
                f"logic={timing['logic_wait_sec']:.4f}s "
                f"actuator_wait={timing['wait_actuator_read_sec']:.4f}s "
                f"physics={timing['physics_step_publish_sec']:.4f}s "
                f"total={timing['cycle_total_sec']:.4f}s"
            )
            input_physics_path = output_physics_path

            if args.cycle_wait > 0:
                time.sleep(args.cycle_wait)

        if timing_rows:
            _write_timing_summary_csv(runtime_dir, timing_rows)


    except BaseException as exc:
        run_error = exc
        raise
    finally:
        _send_helics_stop(coord_sync, rt)
        try:
            if coord_sync is not None:
                coord_sync.close()
        except Exception as exc:
            cleanup_errors.append(f"helics:{type(exc).__name__}:{exc}")
        try:
            physics.close()
        except Exception as exc:
            cleanup_errors.append(f"physics:{type(exc).__name__}:{exc}")
        if bool(_enabled_attack_scenarios(rt.raw)):
            try:
                _stop_configured_attacks(
                    args,
                    project_root,
                    runtime_dir,
                    process_registry=process_registry,
                )
            except Exception as exc:
                cleanup_errors.append(f"attacks:{type(exc).__name__}:{exc}")
        try:
            stop_daemons(processes, log_handles, sync_dir)
        except Exception as exc:
            cleanup_errors.append(f"daemons:{type(exc).__name__}:{exc}")
        try:
            if resource_monitor is not None:
                resource_monitor.stop(timeout=2.0)
        except Exception as exc:
            cleanup_errors.append(f"resources:{type(exc).__name__}:{exc}")
        if metrics_enabled:
            try:
                required_writers = (
                    required_metric_writers(rt.raw)
                    if run_error is None
                    else set()
                )
                metric_writer_quality = analyze_metric_writer_stats(
                    runtime_dir / "raw" / "metric_writer_stats",
                    required_writers=required_writers,
                )
                cleanup_errors.extend(
                    f"metric_writer_quality:{error}"
                    for error in metric_writer_quality["quality_errors"]
                )
            except Exception as exc:
                cleanup_errors.append(f"metric_writer_quality:{type(exc).__name__}:{exc}")
        end_status = _runtime_end_status(run_error, cleanup_errors)
        end_iteration = current_iteration if run_error is not None else int(args.iterations)
        safe_log(event_logger, make_event(
            iteration=end_iteration,
            layer="runtime",
            component="coordinator",
            event_type="simulation_end",
            source="coordinator",
            status=end_status,
            details={
                "completed_cycles": len(cycle_summaries),
                "error_type": type(run_error).__name__ if run_error is not None else "",
                "error": str(run_error) if run_error is not None else "",
                "cleanup_errors": cleanup_errors,
                "metric_writer_quality": metric_writer_quality,
            },
        ))

    if cleanup_errors:
        raise RuntimeError("runtime cleanup failed: " + "; ".join(cleanup_errors))

    print("\n[DONE] Persistent closed-loop run finished.")
    print(f"[PHYSICS-CSV] {csv_dir(runtime_dir) / 'physics.csv'}")
    print(f"[SCADA-CSV]   {csv_dir(runtime_dir) / 'scada.csv'}")
    print(f"[TIMING-CSV]  {csv_dir(runtime_dir) / 'closed_loop_timing.csv'}")
    print(f"[TIMING-SUM]  {csv_dir(runtime_dir) / 'closed_loop_timing_summary.csv'}")
    print(f"[JSON]        {json_dir(runtime_dir)}")
    print(f"[CHECK]       run scripts/check.sh after runtime if actuator/open-loop verification is needed")
    print(f"[SUMMARY]     {json_dir(runtime_dir) / 'closed_loop_summary.json'}")
    print(f"[LOGS]        {runtime_dir / 'logs'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
