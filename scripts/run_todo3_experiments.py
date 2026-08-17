#!/usr/bin/env python3
"""Prepare and execute the single-observation supplement defined by TODO3.md."""
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config import load_yaml
from src.experiment.runner import experiment_completed


LOSS_LEVELS = tuple([index / 200.0 for index in range(20)] + [0.50])
DELAY_LEVELS_MS = (0, 2, 5, 10, 20, 50, 100)
CONGESTION_RHOS = (0.0, 1.0, 1.5, 2.0)
TARGET_LINKS = ("r0-r_scada", "r0-r4")
SECTION_NAMES = {
    "delay": "01_delay_path_validation",
    "loss": "02_packet_loss_21_levels",
    "congestion": "03_controlled_queue_congestion",
    "timestamps": "05_cross_layer_timestamps",
    "sensitivity": "06_correctness_sensitivity",
}


def timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def default_archive() -> Path:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), *args], capture_output=True, text=True, check=False
        )
        return (result.stdout or "").strip()
    branch = git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    commit = (git("rev-parse", "--short", "HEAD") or "unknown")[:8]
    return Path("/home/lzh/MASTER/CODE/output") / f"quantitative_supplement_{timestamp()}_{branch}_{commit}"


def set_link(cfg: dict[str, Any], name: str, **updates: Any) -> None:
    for link in cfg["network"]["backbone_links"]:
        if link.get("name") == name:
            link.update(updates)
            return
    raise KeyError(name)


def set_lan_rate(cfg: dict[str, Any], name: str, rate: str) -> None:
    for lan in cfg["network"]["lans"]:
        if lan.get("name") == name:
            lan["data_rate"] = rate
            return
    raise KeyError(name)


def instrument(
    cfg: dict[str, Any],
    *,
    pcap_links: list[str],
    queue_timeseries: bool = False,
    modbus_trace: bool = False,
) -> None:
    metrics = cfg.setdefault("metrics", {})
    metrics.update({
        "enabled": True,
        "event_log": True,
        "communication": True,
        "resource_monitor": True,
        "sample_interval_sec": 0.5,
    })
    metrics["modbus_packet_trace"] = {
        "enabled": modbus_trace,
        "targets": ["PLC4"],
    }
    network = cfg.setdefault("network", {})
    network["pcap"] = False
    network["measurement"] = {
        "enabled": True,
        "flow_monitor": True,
        "link_metrics": True,
        "link_metrics_interval": "250ms",
        "pcap": True,
        "pcap_links": pcap_links,
        "queue_timeseries": {"enabled": queue_timeseries, "interval": "20ms"},
    }


def set_experiment(
    cfg: dict[str, Any],
    *,
    experiment_id: str,
    group: str,
    parameter: str,
    value: Any,
    seed: int,
    ns3_run: int,
    **extra: Any,
) -> None:
    cfg["experiment"] = {
        "id": experiment_id,
        "name": experiment_id,
        "group": group,
        "parameter": parameter,
        "value": value,
        "repetition": 1,
        "random_seed": seed,
        "ns3_run": ns3_run,
        "drain_period_sec": 2.0,
        "logic_wait_sec": 0.1,
        **extra,
    }


def disable_attacks(cfg: dict[str, Any]) -> None:
    cfg["attacks"] = {"enabled": False, "scenarios": []}


def configure_target_links(
    cfg: dict[str, Any],
    *,
    delay_ms: float = 2,
    data_rate: str = "100Mbps",
    queue_packets: int = 100,
    loss_rate: float | None = None,
    stream_base: int = 1,
) -> None:
    for index, link_name in enumerate(TARGET_LINKS):
        update: dict[str, Any] = {
            "delay": f"{delay_ms:g}ms",
            "data_rate": data_rate,
            "queue": {"type": "DropTailQueue", "max_packets": queue_packets},
        }
        if loss_rate is None:
            update["error_model"] = None
        else:
            update["error_model"] = {
                "type": "rate",
                "unit": "packet",
                "error_rate": loss_rate,
                "direction": "both",
                "stream": stream_base + index * 10,
            }
        set_link(cfg, link_name, **update)


def configure_three_bot_plc4(cfg: dict[str, Any], rho: float) -> None:
    # Keep the source LAN/uplink above the intended r0->r4 bottleneck.
    set_link(cfg, "r0-r_scada", data_rate="100Mbps", delay="2ms", queue={"type": "DropTailQueue", "max_packets": 100}, error_model=None)
    set_link(cfg, "r0-r4", data_rate="10Mbps", delay="2ms", queue={"type": "DropTailQueue", "max_packets": 20}, error_model=None)
    set_lan_rate(cfg, "scada_lan", "100Mbps")
    set_lan_rate(cfg, "plc4_lan", "100Mbps")
    attacks = cfg.setdefault("attacks", {})
    attacks["enabled"] = rho > 0
    scenarios = attacks.get("scenarios", []) or []
    per_bot_mbps = 10.0 * rho / 3.0 if rho > 0 else 0.0
    for index, scenario in enumerate(scenarios, start=1):
        scenario["enabled"] = rho > 0
        scenario["name"] = f"dos_plc4_bot{index}_rho_{str(rho).replace('.', 'p')}"
        scenario["trigger"] = {"type": "iteration_window", "start_iteration": 20, "end_iteration": 40}
        scenario["target"] = {
            "endpoint": "PLC4",
            "namespace": "ns-plc4",
            "ip": "192.168.4.1",
            "protocol": "udp",
            "port": 502,
        }
        scenario.setdefault("traffic", {})["rate"] = f"{per_bot_mbps:.9g}Mbps" if rho > 0 else "1bps"
        scenario["traffic"]["packet_size"] = 1400


def build_specs() -> list[dict[str, Any]]:
    base = load_yaml(PROJECT_ROOT / "examples/c_town/config.yaml")
    three_bots = load_yaml(PROJECT_ROOT / "examples/c_town/config_dos_plc2_three_bots.yaml")
    mitm = load_yaml(PROJECT_ROOT / "examples/c_town/config_mitm_plc4.yaml")
    plc_logic = load_yaml(PROJECT_ROOT / "examples/c_town/config_openplc_logic_plc4.yaml")
    specs: list[dict[str, Any]] = []
    seed = 2026072100
    run_number = 100

    for delay_ms in DELAY_LEVELS_MS:
        seed += 1
        run_number += 1
        cfg = deepcopy(base)
        cfg["iterations"] = 100
        configure_target_links(cfg, delay_ms=delay_ms)
        disable_attacks(cfg)
        instrument(cfg, pcap_links=list(TARGET_LINKS), modbus_trace=True)
        experiment_id = f"delay_path_{delay_ms}ms"
        set_experiment(
            cfg, experiment_id=experiment_id, group="delay", parameter="delay_ms",
            value=delay_ms, seed=seed, ns3_run=run_number, target_links=list(TARGET_LINKS),
        )
        specs.append({"id": experiment_id, "group": "delay", "config": cfg, "timeout_sec": 1200})

    # Existing 100-cycle 0% evidence contains 3,960 target-link packets, so
    # 0.5% gives 19.8 expected drops (<20). TODO3 therefore selects 300 cycles
    # uniformly for all 21 loss configurations.
    for level_index, loss_rate in enumerate(LOSS_LEVELS):
        seed += 1
        run_number += 1
        cfg = deepcopy(base)
        cfg["iterations"] = 300
        configure_target_links(cfg, loss_rate=loss_rate, stream_base=1000 + level_index * 100)
        disable_attacks(cfg)
        instrument(cfg, pcap_links=list(TARGET_LINKS))
        label = f"{loss_rate * 100:g}".replace(".", "p")
        experiment_id = f"packet_loss_{label}pct"
        extreme = loss_rate == 0.5
        set_experiment(
            cfg, experiment_id=experiment_id, group="loss", parameter="loss_rate",
            value=loss_rate, seed=seed, ns3_run=run_number, target_links=list(TARGET_LINKS),
            sample_size_precheck={"target_packets_at_100_cycles": 3960, "expected_drops_at_0p5pct": 19.8, "selected_iterations": 300},
            extreme_stress_test=extreme,
            per_request_timeout_sec=2.0 if extreme else 2.0,
            maximum_connection_retries=3 if extreme else 60,
            maximum_experiment_wall_clock_sec=1800 if extreme else 3600,
            graceful_abort=True,
        )
        specs.append({
            "id": experiment_id,
            "group": "loss",
            "config": cfg,
            "timeout_sec": 1800 if extreme else 3600,
            # At 50% packet loss, a single connection attempt is not a useful
            # observation. Give the bounded wall-clock run enough retries to
            # establish each endpoint while retaining the explicit timeout.
            "env": {"MODBUS_TIMEOUT": "2.0", "CONNECT_RETRIES": "20"} if extreme else {},
            "accept_limit": extreme,
        })

    for rho in CONGESTION_RHOS:
        seed += 1
        run_number += 1
        cfg = deepcopy(three_bots)
        cfg["iterations"] = 100
        configure_three_bot_plc4(cfg, rho)
        instrument(cfg, pcap_links=["r0-r4"], queue_timeseries=True)
        label = str(rho).replace(".", "p")
        experiment_id = f"controlled_congestion_rho_{label}"
        set_experiment(
            cfg, experiment_id=experiment_id, group="congestion", parameter="rho",
            value=rho, seed=seed, ns3_run=run_number, target_links=["r0-r4"],
            bottleneck_mbps=10, queue_packets=20, bot_count=3, attack_window=[20, 40],
        )
        specs.append({"id": experiment_id, "group": "congestion", "config": cfg, "timeout_sec": 1800})

    timestamp_sources = [
        ("timestamp_mitm_plc4_t7", deepcopy(mitm), "mitm"),
        ("timestamp_dos_three_bot_strong", deepcopy(three_bots), "dos"),
        ("timestamp_plc4_logic_injection", deepcopy(plc_logic), "plc_logic"),
    ]
    for experiment_id, cfg, scenario in timestamp_sources:
        seed += 1
        run_number += 1
        cfg["iterations"] = 100
        if scenario == "dos":
            configure_three_bot_plc4(cfg, 2.0)
            instrument(cfg, pcap_links=["r0-r4"], queue_timeseries=True, modbus_trace=True)
        else:
            configure_target_links(cfg)
            instrument(cfg, pcap_links=["r0-r4"], modbus_trace=True)
        set_experiment(
            cfg, experiment_id=experiment_id, group="timestamps", parameter="scenario",
            value=scenario, seed=seed, ns3_run=run_number, scenario=scenario,
        )
        specs.append({"id": experiment_id, "group": "timestamps", "config": cfg, "timeout_sec": 1800})

    seed += 1
    run_number += 1
    cfg = deepcopy(plc_logic)
    cfg["iterations"] = 100
    injection = cfg["attacks"]["scenarios"][0]["injection"]
    injection["rule"]["injected_value"] = 4.7
    configure_target_links(cfg)
    instrument(cfg, pcap_links=["r0-r4"])
    experiment_id = "correctness_sensitivity_plc4_4p8_to_4p7"
    set_experiment(
        cfg, experiment_id=experiment_id, group="sensitivity", parameter="plc4_threshold",
        value={"original": 4.8, "injected": 4.7}, seed=seed, ns3_run=run_number,
        diagnostic_only=True,
    )
    specs.append({"id": experiment_id, "group": "sensitivity", "config": cfg, "timeout_sec": 1800})
    return specs


def workspace_patch() -> str:
    status = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "status", "--short"], capture_output=True, text=True, check=False
    ).stdout
    diff = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "diff", "--binary", "HEAD"], capture_output=True, text=True, check=False
    ).stdout
    return f"# git status --short\n{status}\n# git diff --binary HEAD\n{diff}"


def attempt_paths(archive: Path, spec: dict[str, Any], attempt: int) -> tuple[Path, Path]:
    section = archive / SECTION_NAMES[spec["group"]]
    attempt_dir = section / "runs" / spec["id"] / f"attempt_{attempt:02d}"
    output_dir = attempt_dir / "output"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    cfg = deepcopy(spec["config"])
    cfg["output_path"] = str(output_dir)
    cfg["experiment"]["attempt"] = attempt
    cfg["experiment"]["requirements_file"] = str(PROJECT_ROOT / "TODO3.md")
    config_path = attempt_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (attempt_dir / "workspace.patch").write_text(workspace_patch(), encoding="utf-8")
    return config_path, output_dir


def prepare(archive: Path) -> list[dict[str, Any]]:
    archive.mkdir(parents=True, exist_ok=True)
    for name in (
        "01_delay_path_validation", "02_packet_loss_21_levels", "03_controlled_queue_congestion",
        "04_dos_intensity_diagnostics", "05_cross_layer_timestamps", "06_correctness_sensitivity",
        "07_epanet_crosscheck", "08_large_scale_summary", "09_combined_statistics",
    ):
        (archive / name).mkdir(exist_ok=True)
    specs = build_specs()
    for spec in specs:
        config_path, output_dir = attempt_paths(archive, spec, 1)
        spec["config_path"] = str(config_path)
        spec["output_dir"] = str(output_dir)
    plan = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "requirements": str(PROJECT_ROOT / "TODO3.md"),
        "formal_run_count": len(specs),
        "sample_size_precheck": {
            "source": "reused 0%/2ms/100Mbps run",
            "target_packets_at_100_cycles": 3960,
            "expected_drops_at_0p5pct": 19.8,
            "decision": "300 cycles for every loss configuration",
        },
        "experiments": [
            {"id": x["id"], "group": x["group"], "config": x["config_path"], "output": x["output_dir"]}
            for x in specs
        ],
    }
    (archive / "EXPERIMENT_PLAN.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return specs


def final_status(output: Path) -> str:
    path = output / "runtime/csv/events.csv"
    status = ""
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("event_type") == "simulation_end":
                    status = str(row.get("status", "")).strip().lower()
    except (OSError, csv.Error, UnicodeError):
        pass
    return status


def completed_cycles(output: Path) -> int:
    path = output / "runtime/csv/cycle_timing.csv"
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return sum(1 for _ in csv.DictReader(handle))
    except (OSError, csv.Error, UnicodeError):
        return 0


def execute(command: list[str], *, log_path: Path, env: dict[str, str], timeout_sec: int) -> tuple[int | None, bool, float]:
    started = time.time()
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
        )
        timed_out = False
        try:
            returncode = proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                returncode = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = proc.wait()
    return returncode, timed_out, time.time() - started


def load_index(archive: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads((archive / "RUN_INDEX.json").read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, ValueError, TypeError):
        return []


def run(archive: Path, specs: list[dict[str, Any]]) -> int:
    results = load_index(archive)
    for position, spec in enumerate(specs, start=1):
        valid_prior = next((item for item in results if item.get("id") == spec["id"] and item.get("valid")), None)
        if valid_prior is not None:
            print(f"[TODO3] reuse id={spec['id']} status={valid_prior.get('simulation_end')}", flush=True)
            continue
        prior_attempts = [int(item.get("attempt", 0)) for item in results if item.get("id") == spec["id"]]
        success = False
        # Preserve infrastructure failures while allowing a later authenticated
        # attempt after a sudo/namespace issue is repaired.
        # Keep retry attempts bounded, but allow a fresh post-fix attempt when
        # an earlier batch exhausted its infrastructure retry budget.
        max_attempts = 9
        for attempt in range(max(prior_attempts, default=0) + 1, max_attempts + 1):
            config_path, output = attempt_paths(archive, spec, attempt)
            log_path = config_path.parent / "run.log"
            env = os.environ.copy()
            env.update(spec.get("env", {}))
            env.update({
                "PYTHON_BIN": sys.executable,
                "PATH": f"{Path(sys.executable).parent}:{env.get('PATH', '')}",
                "PYTHONDONTWRITEBYTECODE": "1",
                "SYNC_TIMEOUT": "180.0",
            })
            command = ["bash", str(PROJECT_ROOT / "scripts/run_all.sh"), "--config", str(config_path), "--check"]
            print(
                f"[TODO3] {position:02d}/{len(specs)} group={spec['group']} id={spec['id']} attempt={attempt}",
                flush=True,
            )
            returncode, timed_out, elapsed = execute(
                command, log_path=log_path, env=env, timeout_sec=int(spec["timeout_sec"])
            )
            status = final_status(output)
            cycles = completed_cycles(output)
            completed = status == "success" and experiment_completed(output)
            limited = bool(timed_out and spec.get("accept_limit") and cycles > 0)
            success = completed or limited
            record = {
                "id": spec["id"], "group": spec["group"], "attempt": attempt,
                "config": str(config_path), "output": str(output), "log": str(log_path),
                "returncode": returncode, "timed_out": timed_out, "elapsed_sec": elapsed,
                "completed_control_cycles": cycles,
                "simulation_end": "completed_with_limit" if limited else status,
                "termination_reason": "maximum_experiment_wall_clock" if limited else ("normal" if completed else "infrastructure_failure"),
                "valid": success,
            }
            results.append(record)
            (archive / "RUN_INDEX.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if success:
                print(f"[TODO3] accepted id={spec['id']} status={record['simulation_end']} cycles={cycles} elapsed={elapsed:.1f}s", flush=True)
                break
            failure = {**record, "retry_allowed": attempt < max_attempts}
            (config_path.parent / "failure.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"[TODO3][WARN] failed id={spec['id']} rc={returncode} status={status or 'missing'}", flush=True)
        if not success:
            print(f"[TODO3][ERROR] exhausted retries id={spec['id']}", flush=True)
    failed = sorted(spec["id"] for spec in specs if not any(r.get("id") == spec["id"] and r.get("valid") for r in results))
    print(f"[TODO3] complete valid={len(specs)-len(failed)}/{len(specs)} failed={failed}", flush=True)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--group", action="append", choices=tuple(SECTION_NAMES))
    parser.add_argument("--id", action="append", dest="ids")
    args = parser.parse_args()
    archive = (args.archive or default_archive()).expanduser().resolve()
    specs = prepare(archive)
    if args.group:
        specs = [item for item in specs if item["group"] in set(args.group)]
    if args.ids:
        specs = [item for item in specs if item["id"] in set(args.ids)]
    print(f"[TODO3] archive={archive} selected={len(specs)}", flush=True)
    if args.prepare_only:
        return 0
    return run(archive, specs)


if __name__ == "__main__":
    raise SystemExit(main())
