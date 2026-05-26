#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Start/stop/synchronize configured attack runtime components.

MITM attacks are controlled by the closed-loop coordinator through an iteration
state switch.  The attacker endpoint is generated as a normal namespace/TAP/ns-3
node from config.yaml.  The Modbus proxy and DNAT route stay online for the
whole experiment so SCADA can keep persistent TCP connections; only the proxy's
modification state changes across configured attack windows.
"""
from __future__ import annotations

import argparse
import csv
import json
import ipaddress
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from src.core.config import load_runtime_config, load_yaml


SUPPORTED_MITM_TYPES = {"mitm", "modbus_mitm"}


def _scenario_list(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    attacks = cfg.get("attacks", {}) or {}
    if isinstance(attacks, dict) and not bool(attacks.get("enabled", False)):
        return []
    if isinstance(attacks, list):
        return [x for x in attacks if isinstance(x, dict) and bool(x.get("enabled", True))]
    scenarios = attacks.get("scenarios", []) if isinstance(attacks, dict) else []
    return [x for x in scenarios if isinstance(x, dict) and bool(x.get("enabled", True))]


def _scada_namespace(cfg: dict[str, Any]) -> str:
    for ep in cfg.get("network", {}).get("nodes", {}).get("endpoints", []) or []:
        if isinstance(ep, dict) and ep.get("role") == "scada" and ep.get("namespace"):
            return str(ep["namespace"])
    return str(cfg.get("scada", {}).get("namespace", "ns-scada"))


def _endpoint_namespace(cfg: dict[str, Any], endpoint_name: str) -> str:
    for ep in cfg.get("network", {}).get("nodes", {}).get("endpoints", []) or []:
        if isinstance(ep, dict) and str(ep.get("name", "")) == endpoint_name and ep.get("namespace"):
            return str(ep["namespace"])
    raise ValueError(f"endpoint has no namespace: {endpoint_name}")


def _endpoint_ip(cfg: dict[str, Any], endpoint_name: str) -> str:
    for lan in cfg.get("network", {}).get("lans", []) or []:
        if not isinstance(lan, dict):
            continue
        interfaces = lan.get("interfaces", {}) or {}
        if endpoint_name not in interfaces:
            continue
        raw = interfaces[endpoint_name].get("ip")
        if raw:
            return str(ipaddress.ip_interface(str(raw)).ip)
    raise ValueError(f"endpoint has no LAN IP: {endpoint_name}")


def _attack_runtime_dir(runtime_dir: Path) -> Path:
    path = runtime_dir / "attacks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_key(attack_name: str, target: str) -> str:
    return f"{attack_name}_{target}".replace("/", "_").replace(" ", "_")


def _pid_file(runtime_dir: Path, attack_name: str, target: str) -> Path:
    return _attack_runtime_dir(runtime_dir) / f"{_safe_key(attack_name, target)}.pid"


def _state_file(runtime_dir: Path, attack_name: str, target: str) -> Path:
    return _attack_runtime_dir(runtime_dir) / f"{_safe_key(attack_name, target)}.state.json"


def _schedule_csv(runtime_dir: Path) -> Path:
    return runtime_dir / "csv" / "attack_schedule.csv"


def _write_schedule_event(runtime_dir: Path, row: dict[str, Any]) -> None:
    path = _schedule_csv(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "timestamp_epoch",
        "iteration",
        "action",
        "attack",
        "target",
        "active_window",
        "proxy_pid",
        "message",
    ]
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow({**{c: "" for c in cols}, **row})


def _write_control_state(
    runtime_dir: Path,
    scenario: dict[str, Any],
    target_key: str,
    *,
    iteration: int | None,
    active: bool,
    reason: str,
) -> Path:
    """Write the per-target MITM control file consumed by the proxy.

    The proxy is kept alive for the whole experiment so existing SCADA TCP
    connections remain valid.  Only this state file changes across iterations.
    """
    name = str(scenario.get("name"))
    path = _state_file(runtime_dir, name, target_key)
    payload = {
        "attack": name,
        "target": target_key,
        "iteration": iteration,
        "active": bool(active),
        "active_window": _active_window_label(scenario),
        "updated_epoch": time.time(),
        "reason": reason,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _iptables_rule(scada_ns: str, target_ip: str, target_port: int, attacker_ip: str, listen_port: int) -> list[str]:
    return [
        "ip", "netns", "exec", scada_ns,
        "iptables", "-t", "nat", "-A", "OUTPUT",
        "-p", "tcp", "-d", target_ip, "--dport", str(target_port),
        "-j", "DNAT", "--to-destination", f"{attacker_ip}:{listen_port}",
    ]


def _iptables_check_rule(scada_ns: str, target_ip: str, target_port: int, attacker_ip: str, listen_port: int) -> list[str]:
    cmd = _iptables_rule(scada_ns, target_ip, target_port, attacker_ip, listen_port)
    check = cmd.copy()
    check[check.index("-A")] = "-C"
    return check


def _iptables_delete_rule(scada_ns: str, target_ip: str, target_port: int, attacker_ip: str, listen_port: int) -> list[str]:
    cmd = _iptables_rule(scada_ns, target_ip, target_port, attacker_ip, listen_port)
    delete = cmd.copy()
    delete[delete.index("-A")] = "-D"
    return delete


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print("[ATTACK]", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check, text=True)


def _scenario_targets(scenario: dict[str, Any]) -> list[str]:
    intercept = scenario.get("intercept", {}) or {}
    targets = intercept.get("targets", scenario.get("targets", []))
    if isinstance(targets, str):
        return [targets]
    return [str(x) for x in targets]


def _listen_port(scenario: dict[str, Any], index: int, target: str) -> int:
    intercept = scenario.get("intercept", {}) or {}
    per_target = intercept.get("listen_ports", {}) or {}
    if isinstance(per_target, dict) and target in per_target:
        return int(per_target[target])
    base = int(intercept.get("listen_port_base", scenario.get("listen_port_base", 15020)))
    return base + index


def _target_port(scenario: dict[str, Any]) -> int:
    intercept = scenario.get("intercept", {}) or {}
    return int(intercept.get("port", scenario.get("target_port", 502)))


def _attacker_endpoint(scenario: dict[str, Any]) -> str:
    attacker = scenario.get("attacker", {}) or {}
    endpoint = attacker.get("endpoint", scenario.get("attacker_endpoint", ""))
    if not endpoint:
        raise ValueError(f"attack {scenario.get('name')} missing attacker.endpoint")
    return str(endpoint)


def _trigger(scenario: dict[str, Any]) -> dict[str, Any]:
    raw = scenario.get("trigger", scenario.get("schedule", {})) or {}
    if not isinstance(raw, dict):
        raw = {}
    # Backward compatibility for older second-based windows inside rule.window.
    return raw


def _iteration_window(scenario: dict[str, Any]) -> tuple[int, int | None] | None:
    trig = _trigger(scenario)
    if not trig:
        return None
    trig_type = str(trig.get("type", "iteration_window")).lower()
    if trig_type not in {"iteration", "iteration_window", "round", "round_window"}:
        return None
    start_raw = trig.get("start_iteration", trig.get("start_round", trig.get("start", 0)))
    end_raw = trig.get("end_iteration", trig.get("end_round", trig.get("end", None)))
    start = int(start_raw)
    end = None if end_raw is None else int(end_raw)
    return start, end


def _active_window_label(scenario: dict[str, Any]) -> str:
    window = _iteration_window(scenario)
    if window is None:
        return "always"
    start, end = window
    return f"{start}..{end if end is not None else 'end'}"


def _active_at_iteration(scenario: dict[str, Any], iteration: int | None, *, default_active: bool) -> bool:
    window = _iteration_window(scenario)
    if window is None:
        return default_active
    if iteration is None:
        return False
    start, end = window
    if iteration < start:
        return False
    if end is not None and iteration > end:
        return False
    return True


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _running_pid(runtime_dir: Path, attack_name: str, target_key: str) -> int | None:
    pf = _pid_file(runtime_dir, attack_name, target_key)
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except Exception:
        try:
            pf.unlink()
        except Exception:
            pass
        return None
    if _is_pid_alive(pid):
        return pid
    try:
        pf.unlink()
    except Exception:
        pass
    return None


def _iptables_add_once(scada_ns: str, target_ip: str, target_port: int, attacker_ip: str, listen_port: int) -> None:
    check_cmd = _iptables_check_rule(scada_ns, target_ip, target_port, attacker_ip, listen_port)
    try:
        if subprocess.run(check_cmd, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return
    except FileNotFoundError:
        pass
    _run(_iptables_rule(scada_ns, target_ip, target_port, attacker_ip, listen_port))


def _iptables_delete_all(scada_ns: str, target_ip: str, target_port: int, attacker_ip: str, listen_port: int) -> None:
    # Try several times because duplicate rules may exist after aborted debug runs.
    for _ in range(8):
        try:
            rc = subprocess.run(
                _iptables_delete_rule(scada_ns, target_ip, target_port, attacker_ip, listen_port),
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
        except FileNotFoundError:
            rc = 127
        if rc != 0:
            break


def _stop_pid(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        os.kill(pid, signal.SIGTERM)
    time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _stop_target(
    *,
    runtime_dir: Path,
    scada_ns: str,
    scenario: dict[str, Any],
    target_key: str,
    target_ip: str,
    target_port: int,
    attacker_ip: str,
    listen_port: int,
    iteration: int | None = None,
    reason: str = "stop",
) -> bool:
    name = str(scenario.get("name"))
    _iptables_delete_all(scada_ns, target_ip, target_port, attacker_ip, listen_port)
    pid = _running_pid(runtime_dir, name, target_key)
    pf = _pid_file(runtime_dir, name, target_key)
    stopped = False
    if pid is not None:
        _stop_pid(pid)
        stopped = True
    try:
        pf.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    try:
        _state_file(runtime_dir, name, target_key).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    if stopped:
        _write_schedule_event(runtime_dir, {
            "timestamp_epoch": f"{time.time():.6f}",
            "iteration": "" if iteration is None else iteration,
            "action": "stop",
            "attack": name,
            "target": target_key,
            "active_window": _active_window_label(scenario),
            "proxy_pid": pid,
            "message": reason,
        })
    return stopped


def _start_target(
    *,
    args: argparse.Namespace,
    cfg: dict[str, Any],
    rt,
    runtime_dir: Path,
    scada_ns: str,
    scenario: dict[str, Any],
    target_key: str,
    idx: int,
    iteration: int | None = None,
    active: bool = False,
    reason: str = "transparent",
) -> bool:
    name = str(scenario.get("name"))
    running = _running_pid(runtime_dir, name, target_key)
    endpoint = _attacker_endpoint(scenario)
    attacker_ns = _endpoint_namespace(cfg, endpoint)
    attacker_ip = _endpoint_ip(cfg, endpoint)
    target_ip = rt.plcs[target_key].ip
    target_port = _target_port(scenario)
    listen_port = _listen_port(scenario, idx, target_key)
    state_path = _write_control_state(runtime_dir, scenario, target_key, iteration=iteration, active=active, reason=reason)

    if running is not None:
        _iptables_add_once(scada_ns, target_ip, target_port, attacker_ip, listen_port)
        return False

    project_root = Path(__file__).resolve().parents[2]
    logs_dir = runtime_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"attack_{name}_{target_key}.log"
    log = log_path.open("a", encoding="utf-8")
    cmd = [
        "ip", "netns", "exec", attacker_ns,
        args.python_bin, "-m", "src.attack.modbus_mitm",
        "--config", str(args.config.resolve()),
        "--attack", name,
        "--target", target_key,
        "--listen-host", attacker_ip,
        "--listen-port", str(listen_port),
        "--target-host", target_ip,
        "--target-port", str(target_port),
        "--runtime-dir", str(runtime_dir),
        "--state-file", str(state_path),
    ]
    print("[ATTACK] launch", " ".join(cmd), flush=True)
    proc = subprocess.Popen(cmd, cwd=str(project_root), stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    _pid_file(runtime_dir, name, target_key).write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.2)
    if proc.poll() is not None:
        raise RuntimeError(f"attack proxy exited early name={name} target={target_key}; see {log_path}")
    _iptables_add_once(scada_ns, target_ip, target_port, attacker_ip, listen_port)
    print(
        f"[ATTACK] MITM proxy ready name={name} target={target_key} "
        f"mode={'attack' if active else 'transparent'} scada_ns={scada_ns} "
        f"{target_ip}:{target_port} -> {attacker_ip}:{listen_port}",
        flush=True,
    )
    _write_schedule_event(runtime_dir, {
        "timestamp_epoch": f"{time.time():.6f}",
        "iteration": "" if iteration is None else iteration,
        "action": "proxy_start",
        "attack": name,
        "target": target_key,
        "active_window": _active_window_label(scenario),
        "proxy_pid": proc.pid,
        "message": f"mode={'attack' if active else 'transparent'} {target_ip}:{target_port}->{attacker_ip}:{listen_port}",
    })
    return True


def _iter_mitm_targets(cfg: dict[str, Any], rt, runtime_dir: Path):  # type: ignore[no-untyped-def]
    scada_ns = _scada_namespace(cfg)
    for scenario in _scenario_list(cfg):
        attack_type = str(scenario.get("type", "")).lower()
        if attack_type not in SUPPORTED_MITM_TYPES:
            print(f"[ATTACK] skip unsupported attack type={attack_type} name={scenario.get('name')}", flush=True)
            continue
        name = str(scenario.get("name"))
        endpoint = _attacker_endpoint(scenario)
        attacker_ip = _endpoint_ip(cfg, endpoint)
        targets = _scenario_targets(scenario)
        if not targets:
            raise ValueError(f"attack {name} has no intercept.targets")
        for idx, target in enumerate(targets):
            target_key = target.upper()
            if target_key not in rt.plcs:
                raise ValueError(f"attack {name} target is not a PLC in runtime config: {target}")
            target_ip = rt.plcs[target_key].ip
            target_port = _target_port(scenario)
            listen_port = _listen_port(scenario, idx, target_key)
            yield scenario, idx, target_key, scada_ns, attacker_ip, target_ip, target_port, listen_port


def stop(args: argparse.Namespace) -> int:
    cfg = load_yaml(args.config)
    rt = load_runtime_config(args.config)
    runtime_dir = args.runtime_dir or (rt.output_dir / "runtime")

    count = 0
    for scenario, _idx, target_key, scada_ns, attacker_ip, target_ip, target_port, listen_port in _iter_mitm_targets(cfg, rt, runtime_dir):
        if _stop_target(
            runtime_dir=runtime_dir,
            scada_ns=scada_ns,
            scenario=scenario,
            target_key=target_key,
            target_ip=target_ip,
            target_port=target_port,
            attacker_ip=attacker_ip,
            listen_port=listen_port,
            reason="stop all",
        ):
            count += 1
    print(f"[ATTACK] stopped configured attack components count={count}", flush=True)
    return 0


def start(args: argparse.Namespace) -> int:
    cfg = load_yaml(args.config)
    rt = load_runtime_config(args.config)
    runtime_dir = args.runtime_dir or (rt.output_dir / "runtime")
    scenarios = _scenario_list(cfg)
    if not scenarios:
        print("[ATTACK] no enabled attacks in config; skip", flush=True)
        return 0

    # Manual start keeps the original behavior: clear stale state, then start every
    # enabled scenario regardless of iteration trigger.
    stop(args)

    scada_ns = _scada_namespace(cfg)
    launched = 0
    for scenario in scenarios:
        attack_type = str(scenario.get("type", "")).lower()
        if attack_type not in SUPPORTED_MITM_TYPES:
            print(f"[ATTACK] skip unsupported attack type={attack_type} name={scenario.get('name')}", flush=True)
            continue
        for idx, target in enumerate(_scenario_targets(scenario)):
            target_key = target.upper()
            if target_key not in rt.plcs:
                raise ValueError(f"attack {scenario.get('name')} target is not a PLC in runtime config: {target}")
            if _start_target(
                args=args,
                cfg=cfg,
                rt=rt,
                runtime_dir=runtime_dir,
                scada_ns=scada_ns,
                scenario=scenario,
                target_key=target_key,
                idx=idx,
                iteration=args.iteration,
                active=True,
                reason="manual start",
            ):
                launched += 1
    print(f"[ATTACK] launched proxies={launched}", flush=True)
    return 0


def sync(args: argparse.Namespace) -> int:
    if args.iteration is None:
        raise ValueError("--action sync requires --iteration")

    cfg = load_yaml(args.config)
    rt = load_runtime_config(args.config)
    runtime_dir = args.runtime_dir or (rt.output_dir / "runtime")
    scenarios = _scenario_list(cfg)
    if not scenarios:
        print(f"[ATTACK] no enabled attacks at iteration={args.iteration}; skip", flush=True)
        return 0

    scada_ns = _scada_namespace(cfg)
    started = 0
    stopped = 0
    checked = 0

    for scenario in scenarios:
        attack_type = str(scenario.get("type", "")).lower()
        if attack_type not in SUPPORTED_MITM_TYPES:
            continue
        name = str(scenario.get("name"))
        endpoint = _attacker_endpoint(scenario)
        attacker_ip = _endpoint_ip(cfg, endpoint)
        for idx, target in enumerate(_scenario_targets(scenario)):
            target_key = target.upper()
            if target_key not in rt.plcs:
                raise ValueError(f"attack {name} target is not a PLC in runtime config: {target}")
            target_ip = rt.plcs[target_key].ip
            target_port = _target_port(scenario)
            listen_port = _listen_port(scenario, idx, target_key)
            checked += 1
            active = _active_at_iteration(scenario, args.iteration, default_active=True)
            if _start_target(
                args=args,
                cfg=cfg,
                rt=rt,
                runtime_dir=runtime_dir,
                scada_ns=scada_ns,
                scenario=scenario,
                target_key=target_key,
                idx=idx,
                iteration=args.iteration,
                active=active,
                reason="inside iteration window" if active else "outside iteration window",
            ):
                started += 1
            _write_schedule_event(runtime_dir, {
                "timestamp_epoch": f"{time.time():.6f}",
                "iteration": args.iteration,
                "action": "attack_on" if active else "attack_off",
                "attack": name,
                "target": target_key,
                "active_window": _active_window_label(scenario),
                "proxy_pid": _running_pid(runtime_dir, name, target_key) or "",
                "message": "proxy remains online; only modification state changed",
            })
    print(
        f"[ATTACK] sync iteration={args.iteration} checked={checked} proxy_started={started} proxy_stopped={stopped}",
        flush=True,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Start/stop/sync Hydro-CPS-Sim attack runtime")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--action", choices=["start", "stop", "sync"], required=True)
    p.add_argument("--iteration", type=int, default=None, help="Closed-loop iteration used by --action sync")
    p.add_argument("--runtime-dir", type=Path)
    p.add_argument("--python", dest="python_bin", default="python3")
    return p


def main() -> int:
    args = build_parser().parse_args()
    args.config = args.config.resolve()
    if args.action == "start":
        return start(args)
    if args.action == "sync":
        return sync(args)
    return stop(args)


if __name__ == "__main__":
    raise SystemExit(main())
