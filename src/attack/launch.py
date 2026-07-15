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
from src.io.csv import append_jsonl, raw_dir


SUPPORTED_MITM_TYPES = {"mitm", "modbus_mitm"}
SUPPORTED_DOS_TYPES = {"udp_dos", "dos_udp", "udp_cbr_flood"}
SUPPORTED_OPENPLC_TYPES = {"openplc_logic", "openplc_logic_injection", "plc_logic_injection"}


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


def _endpoint_entry(cfg: dict[str, Any], endpoint_name: str) -> dict[str, Any]:
    for ep in cfg.get("network", {}).get("nodes", {}).get("endpoints", []) or []:
        if isinstance(ep, dict) and str(ep.get("name", "")) == endpoint_name:
            return ep
    raise ValueError(f"endpoint not found in network.nodes.endpoints: {endpoint_name}")


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


def _internal_networks(cfg: dict[str, Any]) -> list[ipaddress._BaseNetwork]:
    networks: list[ipaddress._BaseNetwork] = [
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("10.0.0.0/8"),
    ]
    network_cfg = cfg.get("network", {}) or {}
    for group in ("lans", "backbone_links"):
        for item in network_cfg.get(group, []) or []:
            if not isinstance(item, dict) or not item.get("subnet"):
                continue
            try:
                networks.append(ipaddress.ip_network(str(item["subnet"]), strict=False))
            except ValueError:
                pass
    return networks


def _require_internal_ip(cfg: dict[str, Any], ip_raw: str) -> str:
    try:
        ip = ipaddress.ip_address(str(ip_raw))
    except ValueError as exc:
        raise ValueError(f"invalid DoS target IP: {ip_raw}") from exc
    if not any(ip in network for network in _internal_networks(cfg)):
        raise ValueError(f"DoS target {ip} is not an internal simulated endpoint")
    if ip.is_global:
        raise ValueError(f"DoS target {ip} is not an internal simulated endpoint")
    return str(ip)


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
    action = str(row.get("action", ""))
    append_jsonl(raw_dir(runtime_dir) / "attack_schedule.jsonl", {
        "timestamp_epoch": row.get("timestamp_epoch"),
        "iteration": row.get("iteration"),
        "scenario": row.get("attack"),
        "target": row.get("target"),
        "event": action,
        "active": action in {"attack_on", "dos_start"},
        "active_window": row.get("active_window"),
        "proxy_pid": row.get("proxy_pid"),
        "message": row.get("message"),
    })


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


def _dos_target_port(scenario: dict[str, Any]) -> int:
    target = scenario.get("target", {}) or {}
    return int(target.get("port", scenario.get("target_port", 502)))


def _attacker_endpoint(scenario: dict[str, Any]) -> str:
    attacker = scenario.get("attacker", {}) or {}
    endpoint = attacker.get("endpoint", scenario.get("attacker_endpoint", ""))
    if not endpoint:
        raise ValueError(f"attack {scenario.get('name')} missing attacker.endpoint")
    return str(endpoint)


def _dos_target_endpoint(scenario: dict[str, Any]) -> str:
    target = scenario.get("target", {}) or {}
    endpoint = target.get("endpoint", scenario.get("target_endpoint", ""))
    if not endpoint:
        raise ValueError(f"attack {scenario.get('name')} missing target.endpoint")
    return str(endpoint)


def _openplc_target_endpoint(scenario: dict[str, Any]) -> str:
    target = scenario.get("target", {}) or {}
    endpoint = target.get("endpoint", scenario.get("target_endpoint", ""))
    if not endpoint:
        raise ValueError(f"OpenPLC logic attack {scenario.get('name')} missing target.endpoint")
    return str(endpoint)


def _dos_pid_key(source: str, target: str) -> str:
    return f"{source}_{target}"


def _dos_traffic(scenario: dict[str, Any]) -> dict[str, Any]:
    traffic = scenario.get("traffic", {}) or {}
    if not isinstance(traffic, dict):
        raise ValueError(f"attack {scenario.get('name')} traffic must be a mapping")
    mode = str(traffic.get("mode", "cbr")).lower()
    if mode != "cbr":
        raise ValueError(f"attack {scenario.get('name')} only supports traffic.mode=cbr")
    return traffic


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
        if attack_type in SUPPORTED_DOS_TYPES or attack_type in SUPPORTED_OPENPLC_TYPES:
            continue
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


def _resolve_dos_target_ip(cfg: dict[str, Any], rt, scenario: dict[str, Any], target_endpoint: str) -> str:  # type: ignore[no-untyped-def]
    target_entry = _endpoint_entry(cfg, target_endpoint)
    target_key = target_endpoint.upper() if target_endpoint.upper().startswith("PLC") else target_endpoint
    if str(target_entry.get("role", "")).lower() == "plc":
        if target_key not in rt.plcs:
            raise ValueError(f"DoS target {target_endpoint} is not a PLC in runtime config")
        resolved_ip = rt.plcs[target_key].ip
    else:
        resolved_ip = _endpoint_ip(cfg, target_endpoint)
    if not resolved_ip:
        raise ValueError(f"DoS target {target_endpoint} has no resolved IP")

    target_cfg = scenario.get("target", {}) or {}
    configured_ip = target_cfg.get("ip")
    if configured_ip:
        configured = str(ipaddress.ip_interface(str(configured_ip)).ip)
        if configured != resolved_ip:
            raise ValueError(
                f"DoS target.ip mismatch for {target_endpoint}: configured {configured}, resolved {resolved_ip}"
            )
    return _require_internal_ip(cfg, resolved_ip)


def _dos_spec(cfg: dict[str, Any], rt, scenario: dict[str, Any]) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    name = str(scenario.get("name"))
    source = _attacker_endpoint(scenario)
    target = _dos_target_endpoint(scenario)
    attacker_entry = _endpoint_entry(cfg, source)
    _endpoint_entry(cfg, target)

    attacker_cfg = scenario.get("attacker", {}) or {}
    attacker_ns = str(attacker_cfg.get("namespace") or attacker_entry.get("namespace") or _endpoint_namespace(cfg, source))
    if not attacker_ns:
        raise ValueError(f"DoS attack {name} attacker endpoint has no namespace: {source}")

    attacker_ip = _endpoint_ip(cfg, source)
    configured_attacker_ip = attacker_cfg.get("ip")
    if configured_attacker_ip:
        configured = str(ipaddress.ip_interface(str(configured_attacker_ip)).ip)
        if configured != attacker_ip:
            raise ValueError(f"DoS attacker.ip mismatch for {source}: configured {configured}, resolved {attacker_ip}")

    target_cfg = scenario.get("target", {}) or {}
    protocol = str(target_cfg.get("protocol", "udp")).lower()
    if protocol != "udp":
        raise ValueError(f"DoS attack {name} only supports UDP, got protocol={protocol}")

    target_ip = _resolve_dos_target_ip(cfg, rt, scenario, target)
    traffic = _dos_traffic(scenario)
    rate = str(traffic.get("rate", "500kbps"))
    packet_size = int(traffic.get("packet_size", 512))
    if packet_size <= 0:
        raise ValueError(f"DoS attack {name} packet_size must be positive")
    if packet_size > 65_507:
        raise ValueError(f"DoS attack {name} packet_size exceeds maximum UDP payload size")

    return {
        "name": name,
        "source": source,
        "target": target,
        "pid_key": _dos_pid_key(source, target),
        "attacker_ns": attacker_ns,
        "attacker_ip": attacker_ip,
        "target_ip": target_ip,
        "target_port": _dos_target_port(scenario),
        "rate": rate,
        "packet_size": packet_size,
        "start_after_sec": float(traffic.get("start_after_sec", 0.0) or 0.0),
    }


def _iter_dos_targets(cfg: dict[str, Any], rt):  # type: ignore[no-untyped-def]
    for scenario in _scenario_list(cfg):
        attack_type = str(scenario.get("type", "")).lower()
        if attack_type not in SUPPORTED_DOS_TYPES:
            continue
        yield scenario, _dos_spec(cfg, rt, scenario)


def _openplc_pid_key(target: str) -> str:
    return target


def _openplc_backup_file(runtime_dir: Path, attack_name: str, target: str) -> Path:
    return _attack_runtime_dir(runtime_dir) / f"{_safe_key(attack_name, target)}.original.st"


def _openplc_spec(cfg: dict[str, Any], rt, scenario: dict[str, Any], runtime_dir: Path) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    name = str(scenario.get("name"))
    endpoint = _openplc_target_endpoint(scenario)
    endpoint_entry = _endpoint_entry(cfg, endpoint)
    role = str(endpoint_entry.get("role", "")).lower()
    if role != "plc":
        raise ValueError(f"OpenPLC logic attack {name} target endpoint must be role=plc, got {role or '<empty>'}")

    target_key = endpoint.upper()
    if target_key not in rt.plcs:
        raise ValueError(f"OpenPLC logic attack {name} target is not a PLC in runtime config: {endpoint}")

    target_cfg = scenario.get("target", {}) or {}
    configured_ns = str(target_cfg.get("namespace") or "")
    namespace = configured_ns or str(endpoint_entry.get("namespace") or rt.plcs[target_key].namespace)
    if not namespace:
        raise ValueError(f"OpenPLC logic attack {name} target endpoint has no namespace: {endpoint}")
    if namespace != str(endpoint_entry.get("namespace", "")):
        raise ValueError(
            f"OpenPLC logic attack {name} namespace mismatch for {endpoint}: "
            f"target.namespace={namespace} endpoint.namespace={endpoint_entry.get('namespace')}"
        )

    injection = scenario.get("injection", {}) or {}
    if not isinstance(injection, dict):
        raise ValueError(f"OpenPLC logic attack {name} injection must be a mapping")
    mode = str(injection.get("mode", "")).lower()
    if mode not in {"force_actuator", "threshold_shift", "invert_condition"}:
        raise ValueError(f"OpenPLC logic attack {name} unsupported injection.mode={mode!r}")

    restore_on_stop = bool(injection.get("restore_on_stop", True))
    pid_key = _openplc_pid_key(target_key)
    return {
        "name": name,
        "target": target_key,
        "pid_key": pid_key,
        "namespace": namespace,
        "injection": injection,
        "restore_on_stop": restore_on_stop,
        "state_file": _state_file(runtime_dir, name, pid_key),
        "backup_file": _openplc_backup_file(runtime_dir, name, pid_key),
    }


def _iter_openplc_targets(cfg: dict[str, Any], rt, runtime_dir: Path):  # type: ignore[no-untyped-def]
    for scenario in _scenario_list(cfg):
        attack_type = str(scenario.get("type", "")).lower()
        if attack_type not in SUPPORTED_OPENPLC_TYPES:
            continue
        yield scenario, _openplc_spec(cfg, rt, scenario, runtime_dir)


def _wait_openplc_state_active(path: Path, proc: subprocess.Popen, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                state = {}
            if isinstance(state, dict) and bool(state.get("active", False)):
                return
        if proc.poll() is not None:
            raise RuntimeError("OpenPLC logic process exited before reporting active state")
        time.sleep(0.2)
    raise TimeoutError(f"OpenPLC logic process did not report active state within {timeout:.1f}s")


def _run_openplc_restore(
    *,
    args: argparse.Namespace,
    runtime_dir: Path,
    spec: dict[str, Any],
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    cmd = [
        "ip", "netns", "exec", spec["namespace"],
        args.python_bin, "-m", "src.attack.openplc_logic",
        "--config", str(args.config.resolve()),
        "--attack", spec["name"],
        "--target", spec["target"],
        "--namespace", spec["namespace"],
        "--runtime-dir", str(runtime_dir),
        "--state-file", str(spec["state_file"]),
        "--backup-file", str(spec["backup_file"]),
        "--injection-json", json.dumps(spec["injection"], sort_keys=True),
        "--action", "restore",
    ]
    print("[ATTACK] restore", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(project_root), check=True, text=True)


def _stop_openplc_logic(
    *,
    args: argparse.Namespace,
    runtime_dir: Path,
    scenario: dict[str, Any],
    spec: dict[str, Any],
    iteration: int | None = None,
    reason: str = "stop",
) -> bool:
    name = spec["name"]
    pid_key = spec["pid_key"]
    pid = _running_pid(runtime_dir, name, pid_key)
    restored = False
    if pid is not None:
        if spec.get("restore_on_stop", True):
            _run_openplc_restore(args=args, runtime_dir=runtime_dir, spec=spec)
            restored = True
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    elif spec.get("restore_on_stop", True) and Path(spec["state_file"]).exists():
        state = json.loads(Path(spec["state_file"]).read_text(encoding="utf-8"))
        if bool(state.get("active", False)):
            _run_openplc_restore(args=args, runtime_dir=runtime_dir, spec=spec)
            restored = True

    try:
        _pid_file(runtime_dir, name, pid_key).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass

    if restored:
        _write_schedule_event(runtime_dir, {
            "timestamp_epoch": f"{time.time():.6f}",
            "iteration": "" if iteration is None else iteration,
            "action": "openplc_logic_restore",
            "attack": name,
            "target": spec["target"],
            "active_window": _active_window_label(scenario),
            "proxy_pid": pid or "",
            "message": reason,
        })
    return restored


def _start_openplc_logic(
    *,
    args: argparse.Namespace,
    runtime_dir: Path,
    scenario: dict[str, Any],
    spec: dict[str, Any],
    iteration: int | None = None,
    reason: str = "inside iteration window",
) -> bool:
    name = spec["name"]
    pid_key = spec["pid_key"]
    running = _running_pid(runtime_dir, name, pid_key)
    if running is not None:
        return False
    state_path = Path(spec["state_file"])
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        if isinstance(state, dict) and bool(state.get("active", False)) and spec.get("restore_on_stop", True):
            _run_openplc_restore(args=args, runtime_dir=runtime_dir, spec=spec)

    project_root = Path(__file__).resolve().parents[2]
    logs_dir = runtime_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"attack_{name}_{spec['target']}.log"
    log = log_path.open("a", encoding="utf-8")
    cmd = [
        "ip", "netns", "exec", spec["namespace"],
        args.python_bin, "-m", "src.attack.openplc_logic",
        "--config", str(args.config.resolve()),
        "--attack", name,
        "--target", spec["target"],
        "--namespace", spec["namespace"],
        "--runtime-dir", str(runtime_dir),
        "--state-file", str(spec["state_file"]),
        "--backup-file", str(spec["backup_file"]),
        "--injection-json", json.dumps(spec["injection"], sort_keys=True),
    ]
    if spec.get("restore_on_stop", True):
        cmd.append("--restore-on-stop")
    else:
        cmd.append("--no-restore-on-stop")
    print("[ATTACK] launch", " ".join(cmd), flush=True)
    proc = subprocess.Popen(cmd, cwd=str(project_root), stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    _pid_file(runtime_dir, name, pid_key).write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.2)
    if proc.poll() is not None:
        raise RuntimeError(f"OpenPLC logic process exited early name={name} target={spec['target']}; see {log_path}")
    try:
        _wait_openplc_state_active(Path(spec["state_file"]), proc, timeout=90.0)
    except Exception as exc:
        raise RuntimeError(f"OpenPLC logic injection did not become active name={name} target={spec['target']}; see {log_path}") from exc
    _write_schedule_event(runtime_dir, {
        "timestamp_epoch": f"{time.time():.6f}",
        "iteration": "" if iteration is None else iteration,
        "action": "openplc_logic_start",
        "attack": name,
        "target": spec["target"],
        "active_window": _active_window_label(scenario),
        "proxy_pid": proc.pid,
        "message": f"namespace={spec['namespace']} mode={spec['injection'].get('mode')} {reason}",
    })
    return True


def _stop_dos(
    *,
    runtime_dir: Path,
    scenario: dict[str, Any],
    spec: dict[str, Any],
    iteration: int | None = None,
    reason: str = "stop",
) -> bool:
    name = spec["name"]
    pid_key = spec["pid_key"]
    pid = _running_pid(runtime_dir, name, pid_key)
    pf = _pid_file(runtime_dir, name, pid_key)
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
    if stopped:
        _write_schedule_event(runtime_dir, {
            "timestamp_epoch": f"{time.time():.6f}",
            "iteration": "" if iteration is None else iteration,
            "action": "dos_stop",
            "attack": name,
            "target": spec["target"],
            "active_window": _active_window_label(scenario),
            "proxy_pid": pid,
            "message": reason,
        })
    return stopped


def _start_dos(
    *,
    args: argparse.Namespace,
    runtime_dir: Path,
    scenario: dict[str, Any],
    spec: dict[str, Any],
    iteration: int | None = None,
    reason: str = "inside iteration window",
) -> bool:
    name = spec["name"]
    pid_key = spec["pid_key"]
    running = _running_pid(runtime_dir, name, pid_key)
    if running is not None:
        return False

    project_root = Path(__file__).resolve().parents[2]
    logs_dir = runtime_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"attack_{name}_{spec['source']}_{spec['target']}.log"
    log = log_path.open("a", encoding="utf-8")
    cmd = [
        "ip", "netns", "exec", spec["attacker_ns"],
        args.python_bin, "-m", "src.attack.udp_dos",
        "--config", str(args.config.resolve()),
        "--attack", name,
        "--source", spec["source"],
        "--target", spec["target"],
        "--target-host", spec["target_ip"],
        "--target-port", str(spec["target_port"]),
        "--rate", spec["rate"],
        "--packet-size", str(spec["packet_size"]),
        "--runtime-dir", str(runtime_dir),
        "--start-after-sec", str(spec["start_after_sec"]),
    ]
    print("[ATTACK] launch", " ".join(cmd), flush=True)
    proc = subprocess.Popen(cmd, cwd=str(project_root), stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    _pid_file(runtime_dir, name, pid_key).write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.2)
    if proc.poll() is not None:
        raise RuntimeError(f"DoS process exited early name={name} target={spec['target']}; see {log_path}")
    _write_schedule_event(runtime_dir, {
        "timestamp_epoch": f"{time.time():.6f}",
        "iteration": "" if iteration is None else iteration,
        "action": "dos_start",
        "attack": name,
        "target": spec["target"],
        "active_window": _active_window_label(scenario),
        "proxy_pid": proc.pid,
        "message": (
            f"{spec['source']} -> {spec['target_ip']}:{spec['target_port']} "
            f"udp rate={spec['rate']} packet_size={spec['packet_size']} {reason}"
        ),
    })
    return True


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
    for scenario, spec in _iter_dos_targets(cfg, rt):
        if _stop_dos(runtime_dir=runtime_dir, scenario=scenario, spec=spec, reason="stop all"):
            count += 1
    for scenario, spec in _iter_openplc_targets(cfg, rt, runtime_dir):
        if _stop_openplc_logic(args=args, runtime_dir=runtime_dir, scenario=scenario, spec=spec, reason="stop all"):
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
        if attack_type in SUPPORTED_DOS_TYPES:
            spec = _dos_spec(cfg, rt, scenario)
            if _start_dos(
                args=args,
                runtime_dir=runtime_dir,
                scenario=scenario,
                spec=spec,
                iteration=args.iteration,
                reason="manual start",
            ):
                launched += 1
            continue
        if attack_type in SUPPORTED_OPENPLC_TYPES:
            spec = _openplc_spec(cfg, rt, scenario, runtime_dir)
            if _start_openplc_logic(
                args=args,
                runtime_dir=runtime_dir,
                scenario=scenario,
                spec=spec,
                iteration=args.iteration,
                reason="manual start",
            ):
                launched += 1
            continue
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
        if attack_type in SUPPORTED_DOS_TYPES:
            spec = _dos_spec(cfg, rt, scenario)
            checked += 1
            active = _active_at_iteration(scenario, args.iteration, default_active=False)
            if active:
                if _start_dos(
                    args=args,
                    runtime_dir=runtime_dir,
                    scenario=scenario,
                    spec=spec,
                    iteration=args.iteration,
                    reason="inside iteration window",
                ):
                    started += 1
            else:
                if _stop_dos(
                    runtime_dir=runtime_dir,
                    scenario=scenario,
                    spec=spec,
                    iteration=args.iteration,
                    reason="iteration window ended",
                ):
                    stopped += 1
            continue
        if attack_type in SUPPORTED_OPENPLC_TYPES:
            spec = _openplc_spec(cfg, rt, scenario, runtime_dir)
            checked += 1
            active = _active_at_iteration(scenario, args.iteration, default_active=False)
            if active:
                if _start_openplc_logic(
                    args=args,
                    runtime_dir=runtime_dir,
                    scenario=scenario,
                    spec=spec,
                    iteration=args.iteration,
                    reason="inside iteration window",
                ):
                    started += 1
            else:
                if _stop_openplc_logic(
                    args=args,
                    runtime_dir=runtime_dir,
                    scenario=scenario,
                    spec=spec,
                    iteration=args.iteration,
                    reason="iteration window ended",
                ):
                    stopped += 1
            continue
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
        f"[ATTACK] sync iteration={args.iteration} checked={checked} started={started} stopped={stopped}",
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
