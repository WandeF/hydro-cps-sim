#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Batch launcher for OpenPLC runtimes in Linux network namespaces.

This version performs a cleanup step before launching each PLC runtime so stale
OpenPLC processes do not keep the interactive server port 43628 occupied.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import yaml

if os.geteuid() != 0:
    os.execvp("sudo", ["sudo", sys.executable] + sys.argv)


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def parse_plc_endpoints(cfg: dict) -> list[tuple[str, str, str]]:
    """
    Return [(endpoint_name, namespace, plc_binary_name), ...] for role=plc.
    Compatible with endpoint list or mapping forms.
    """
    endpoints = cfg.get("network", {}).get("nodes", {}).get("endpoints", [])
    result: list[tuple[str, str, str]] = []

    if isinstance(endpoints, list):
        for item in endpoints:
            if not isinstance(item, dict):
                continue
            if item.get("role") != "plc":
                continue
            name = item.get("name")
            namespace = item.get("namespace")
            if isinstance(name, str) and name and isinstance(namespace, str) and namespace:
                result.append((name, namespace, name.lower()))
    elif isinstance(endpoints, dict):
        for ep_name, ep_cfg in endpoints.items():
            if not isinstance(ep_cfg, dict):
                continue
            if ep_cfg.get("role") != "plc":
                continue
            namespace = ep_cfg.get("namespace")
            if isinstance(namespace, str) and namespace:
                result.append((str(ep_name), namespace, str(ep_name).lower()))
    else:
        raise ValueError("config.yaml: network.nodes.endpoints must be list or dict")

    return result


def run_cmd(cmd: list[str], check: bool = True, capture: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=text)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_pid_file(pid_path: Path) -> int | None:
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def ensure_executable(path: Path) -> None:
    """Make copied/exported OpenPLC runtime binaries executable.

    This is intentionally done at launch time as well as compile time because
    executable bits can be lost when the project is packed/unpacked or copied
    between filesystems. Without this, `ip netns exec ... plcN` exits
    immediately with code 1 and the real error only appears in plcN.log.
    """
    mode = path.stat().st_mode
    if not (mode & 0o111):
        path.chmod(mode | 0o755)
        print(f"[FIX] chmod +x {path}")


def tail_file(path: Path, n: int = 30) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"<cannot read {path}: {exc}>"
    if not lines:
        return "<empty log>"
    return "\n".join(lines[-n:])


def process_args(pid: int) -> str:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True)
    return (result.stdout or "").strip()


def netns_pids(namespace: str) -> list[int]:
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


def kill_pids(pids: set[int], *, grace: float = 0.8) -> None:
    live = [pid for pid in pids if pid_alive(pid)]
    if not live:
        return
    for pid in live:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + grace
    while time.time() < deadline:
        if not any(pid_alive(pid) for pid in live):
            return
        time.sleep(0.05)
    for pid in live:
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def cleanup_old_plc(namespace: str, binary_path: Path, pid_path: Path) -> None:
    """
    Stop stale runtime processes for one PLC.

    The OpenPLC executable name is plc1/plc2/... after compilation. We combine
    the saved pidfile and `ip netns pids` so this also handles processes left
    behind by previous runs where the pidfile was stale or overwritten.
    """
    binary_name = binary_path.name
    binary_abs = str(binary_path)
    candidates: set[int] = set()

    old_pid = read_pid_file(pid_path)
    if old_pid is not None:
        candidates.add(old_pid)

    for pid in netns_pids(namespace):
        args = process_args(pid)
        # Match only this PLC runtime, not arbitrary helper processes in netns.
        tokens = args.split()
        if binary_abs in args or any(Path(t).name == binary_name for t in tokens):
            candidates.add(pid)

    candidates = {pid for pid in candidates if pid_alive(pid)}
    if candidates:
        print(f"[CLEAN] {binary_name} in {namespace}: kill stale pid(s) {sorted(candidates)}")
        kill_pids(candidates)
    if pid_path.exists():
        try:
            pid_path.unlink()
        except OSError:
            pass


def wait_for_interactive_server(namespace: str, host: str = "127.0.0.1", port: int = 43628,
                                timeout: float = 10.0, interval: float = 0.3) -> bool:
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
        result = subprocess.run(["ip", "netns", "exec", namespace, "python3", "-c", py],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return True
        time.sleep(interval)
    return False


def wait_for_modbus(namespace: str, port: int, timeout: float = 6.0, interval: float = 0.25) -> bool:
    py = (
        "import socket,sys\n"
        "s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
        "s.settimeout(1.0)\n"
        f"rc=s.connect_ex(('127.0.0.1', {port}))\n"
        "s.close()\n"
        "sys.exit(0 if rc==0 else 1)\n"
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(["ip", "netns", "exec", namespace, "python3", "-c", py],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return True
        time.sleep(interval)
    return False


def send_openplc_command(namespace: str, command: str, timeout: float = 3.0) -> str:
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
    result = run_cmd(["ip", "netns", "exec", namespace, "python3", "-c", py], check=False)
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        raise RuntimeError(f"Failed to send command in {namespace}: {command}. {err}")
    return (result.stdout or "").strip()


def launch_plc(namespace: str, binary_path: Path, cwd: Path, log_path: Path) -> subprocess.Popen:
    ensure_executable(binary_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = log_path.open("a", encoding="utf-8")
    cmd = ["ip", "netns", "exec", namespace, str(binary_path)]
    print(f"[RUN] {' '.join(cmd)}")
    print(f"[LOG] {log_path}")
    return subprocess.Popen(cmd, cwd=str(cwd), stdout=log_f, stderr=subprocess.STDOUT, start_new_session=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch OpenPLC runtimes in namespaces and start Modbus/TCP.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--modbus-port", type=int, default=502, help="Modbus/TCP port to start")
    parser.add_argument("--startup-timeout", type=float, default=12.0, help="Wait timeout for 43628")
    parser.add_argument("--no-cleanup", action="store_true", help="Do not kill old per-PLC runtime before launch")
    parser.add_argument("--dry-run", action="store_true", help="Only print what would be executed")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"[ERROR] config file not found: {config_path}")
        return 1

    cfg = load_yaml(config_path)
    output_path_raw = cfg.get("output_path")
    if not isinstance(output_path_raw, str) or not output_path_raw.strip():
        print("[ERROR] config.yaml missing valid 'output_path'")
        return 1

    output_dir = Path(output_path_raw)
    if not output_dir.is_absolute():
        output_dir = (config_path.parent / output_dir).resolve()

    plcs_dir = output_dir / "plcs"
    logs_dir = output_dir / "logs"
    run_dir = output_dir / "run"

    if not plcs_dir.exists():
        print(f"[ERROR] PLC binaries dir not found: {plcs_dir}")
        return 1

    plc_targets: list[tuple[str, str, Path]] = []
    for ep_name, namespace, plc_name in parse_plc_endpoints(cfg):
        binary_path = plcs_dir / plc_name
        if not binary_path.exists():
            print(f"[WARN] Skip {ep_name} ({namespace}): binary not found -> {binary_path}")
            continue
        plc_targets.append((ep_name, namespace, binary_path))

    if not plc_targets:
        print("[ERROR] No runnable PLC target found.")
        return 1

    logs_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] Config       : {config_path}")
    print(f"[INFO] Output dir   : {output_dir}")
    print(f"[INFO] PLC dir      : {plcs_dir}")
    print("[INFO] Targets:")
    for ep_name, namespace, binary_path in plc_targets:
        print(f"  - {ep_name} | {namespace} | {binary_path.name}")

    if args.dry_run:
        return 0

    if not args.no_cleanup:
        print("\n[INFO] Cleaning stale PLC runtimes...")
        for _, namespace, binary_path in plc_targets:
            cleanup_old_plc(namespace, binary_path, run_dir / f"{binary_path.name}.pid")

    launched: list[tuple[str, str, Path, subprocess.Popen]] = []
    for ep_name, namespace, binary_path in plc_targets:
        log_path = logs_dir / f"{binary_path.name}.log"
        pid_path = run_dir / f"{binary_path.name}.pid"
        proc = launch_plc(namespace=namespace, binary_path=binary_path, cwd=output_dir, log_path=log_path)
        pid_path.write_text(str(proc.pid), encoding="utf-8")
        launched.append((ep_name, namespace, binary_path, proc))

    print("\n[INFO] Waiting for interactive servers and starting Modbus...")
    failed = 0
    for ep_name, namespace, binary_path, proc in launched:
        plc_name = binary_path.name
        log_path = logs_dir / f"{plc_name}.log"
        if proc.poll() is not None:
            print(f"[ERROR] {plc_name} exited early with code {proc.returncode}")
            print(f"[TAIL] {log_path}:\n{tail_file(log_path)}")
            failed += 1
            continue

        ok = wait_for_interactive_server(namespace, timeout=args.startup_timeout)
        if not ok:
            print(f"[ERROR] {plc_name} in {namespace} did not open 43628 in time")
            print(f"[TAIL] {log_path}:\n{tail_file(log_path)}")
            failed += 1
            continue

        print(f"[OK] {plc_name} interactive server is up in {namespace}")
        try:
            resp = send_openplc_command(namespace, f"start_modbus({args.modbus_port})")
            print(f"[RESP] {plc_name}: {resp if resp else '[no response]'}")
        except Exception as e:
            print(f"[ERROR] {plc_name} start_modbus failed: {e}")
            failed += 1
            continue

        if wait_for_modbus(namespace, args.modbus_port, timeout=6.0):
            print(f"[OK] {plc_name} Modbus is listening on 127.0.0.1:{args.modbus_port}")
        else:
            print(f"[ERROR] {plc_name} Modbus did not listen on 127.0.0.1:{args.modbus_port}")
            failed += 1

    print("\n[DONE] PLC launch sequence finished.")
    print(f"Logs: {logs_dir}")
    print(f"PIDs: {run_dir}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[INTERRUPTED]")
        raise SystemExit(130)
