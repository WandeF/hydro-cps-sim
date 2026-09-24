#!/usr/bin/env python3
"""Run the attack-intensity, propagation, and scalability experiments.

The matrix is intentionally one-factor-at-a-time.  Attack items execute the
real EPANET/OpenPLC/Modbus/ns-3 closed loop.  Scalability items intentionally
replace EPANET with a reproducible random data feed so that every configured
PLC participates in a source-PLC/SCADA/execution-PLC communication path.
Every item receives an isolated output directory.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
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
from scripts.run_todo3_experiments import (
    configure_target_links,
    configure_three_bot_plc4,
    disable_attacks,
    instrument,
    set_experiment,
)


OUTPUT_ROOT = Path("/home/lzh/MASTER/CODE/output")
ATTACK_ITERATIONS = 80
SCALE_ITERATIONS = 30
ATTACK_WINDOW = (15, 55)
MITM_BIASES = tuple(value / 10.0 for value in range(0, 36)) + (4.0, 4.5, 5.0)
DOS_RHOS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0)
PLC_THRESHOLD_SHIFTS = tuple(value / 10.0 for value in range(-34, 1))
SCALE_PLC_COUNTS = (2, 4, 6, 8, 16, 32, 64, 128)
SCALE_TIMEOUTS = {
    16: 3600,
    32: 5400,
    64: 7200,
    128: 10800,
}

SECTION_NAMES = {
    "baseline": "01_baseline",
    "mitm": "02_mitm_intensity",
    "dos": "03_dos_intensity",
    "plc_logic": "04_plc_logic_intensity",
    "scalability": "05_scalability",
}


def stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def default_archive() -> Path:
    commit = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip() or "unknown"
    return OUTPUT_ROOT / f"advisor_experiments_{stamp()}_{commit[:8]}"


def label_number(value: float) -> str:
    return f"{value:g}".replace("-", "m").replace(".", "p")


def set_window(cfg: dict[str, Any], start: int, end: int) -> None:
    for scenario in cfg.get("attacks", {}).get("scenarios", []) or []:
        scenario["trigger"] = {
            "type": "iteration_window",
            "start_iteration": start,
            "end_iteration": end,
        }


def topology_counts(cfg: dict[str, Any]) -> dict[str, int]:
    nodes = cfg.get("network", {}).get("nodes", {}) or {}
    plcs = cfg.get("plcs", []) or []
    return {
        "plc_count": len(plcs),
        "controlled_object_count": sum(len(p.get("actuators", []) or []) for p in plcs),
        "sensor_mapping_count": sum(len(p.get("sensors", []) or []) for p in plcs),
        "scada_tag_count": sum(
            len(p.get("sensors", []) or []) + len(p.get("actuators", []) or []) for p in plcs
        ),
        "network_node_count": sum(len(nodes.get(key, []) or []) for key in ("routers", "switches", "endpoints")),
        "communication_link_count": len(cfg.get("network", {}).get("backbone_links", []) or [])
        + len(cfg.get("network", {}).get("lans", []) or []),
    }


def prune_to_plcs(cfg: dict[str, Any], count: int) -> None:
    """Keep a cumulative subset of real PLCs and their star branches."""
    plcs = cfg.get("plcs", []) or []
    if count < 1 or count > len(plcs):
        raise ValueError(f"PLC count {count} outside 1..{len(plcs)}")
    selected_plcs = deepcopy(plcs[:count])
    selected = {str(item["name"]) for item in selected_plcs}
    selected_lower = {name.lower() for name in selected}
    cfg["plcs"] = selected_plcs

    network = cfg["network"]
    nodes = network["nodes"]
    edge_suffixes = {name.removeprefix("PLC").lower() for name in selected}
    keep_routers = {"r0", "r_scada", *(f"r{x}" for x in edge_suffixes)}
    keep_switches = {"s_scada", *(f"s{x}" for x in edge_suffixes)}
    nodes["routers"] = [x for x in nodes.get("routers", []) if x.get("name") in keep_routers]
    nodes["switches"] = [x for x in nodes.get("switches", []) if x.get("name") in keep_switches]
    nodes["endpoints"] = [
        x for x in nodes.get("endpoints", [])
        if x.get("name") == "scada" or x.get("name") in selected
    ]
    network["backbone_links"] = [
        x for x in network.get("backbone_links", [])
        if x.get("name") == "r0-r_scada"
        or any(x.get("name") == f"r0-r{suffix}" for suffix in edge_suffixes)
    ]
    network["lans"] = [
        x for x in network.get("lans", [])
        if x.get("name") == "scada_lan"
        or any(x.get("name") == f"plc{suffix}_lan" for suffix in edge_suffixes)
    ]
    network["taps"] = [
        x for x in network.get("taps", [])
        if x.get("endpoint") == "scada" or x.get("endpoint") in selected
    ]
    network["applications"] = [
        x for x in network.get("applications", [])
        if x.get("src") == "scada" and x.get("dst") in selected
    ]
    kept_links = {str(x.get("name")) for x in network["backbone_links"]}
    kept_paths = {"scada_path", *(f"plc{suffix}_path" for suffix in edge_suffixes)}
    attack_points = []
    for point in network.get("attack_points", []) or []:
        item = deepcopy(point)
        if item.get("name") == "core_backbone":
            item["targets"] = [target for target in item.get("targets", []) if target in kept_links]
            attack_points.append(item)
        elif item.get("name") in kept_paths:
            attack_points.append(item)
    network["attack_points"] = attack_points
    # Guard against a typo silently turning the scale factor into a partial topology.
    endpoint_names = {str(x.get("name")) for x in nodes["endpoints"]}
    if endpoint_names != selected | {"scada"}:
        raise ValueError(f"incomplete endpoint subset: wanted={selected}, got={endpoint_names}")
    if {name.lower() for name in selected} != selected_lower:
        raise AssertionError("PLC name normalization failed")


def append_scale_plc(cfg: dict[str, Any], plc_number: int) -> None:
    """Append one executable, communication-active PLC scale branch.

    Synthetic scale PLCs deliberately have no C-Town sensor, actuator, or
    control mappings.  The ST generator still emits ``PLC_Ready`` and the
    runtime launches a real OpenPLC instance that SCADA polls over Modbus TCP.
    This adds control-runtime and communication load without inventing water
    infrastructure that is absent from the physical model.
    """
    plc_name = f"PLC{plc_number}"
    suffix = str(plc_number)
    router = f"r{suffix}"
    switch = f"s{suffix}"
    namespace = f"ns-plc{suffix}"
    tap_name = f"tap-plc{suffix}"
    backbone_name = f"r0-r{suffix}"

    cfg.setdefault("plcs", []).append({
        "name": plc_name,
        "sensors": [],
        "actuators": [],
        "controls": [],
    })
    network = cfg["network"]
    nodes = network["nodes"]
    nodes.setdefault("routers", []).append({"name": router, "role": "edge_router"})
    nodes.setdefault("switches", []).append({"name": switch, "role": "bridge_switch"})
    nodes.setdefault("endpoints", []).append({
        "name": plc_name,
        "role": "plc",
        "namespace": namespace,
        "tap": tap_name,
    })
    network.setdefault("backbone_links", []).append({
        "name": backbone_name,
        "type": "point_to_point",
        "endpoints": ["r0", router],
        "data_rate": "100Mbps",
        "delay": "2ms",
        "mtu": 1500,
        "subnet": f"10.0.{suffix}.0/24",
        "interfaces": {
            "r0": {"ifname": f"r0-eth{suffix}", "ip": f"10.0.{suffix}.1/24"},
            router: {"ifname": f"{router}-uplink", "ip": f"10.0.{suffix}.2/24"},
        },
    })
    network.setdefault("lans", []).append({
        "name": f"plc{suffix}_lan",
        "type": "csma",
        "members": [router, switch, plc_name],
        "data_rate": "100Mbps",
        "delay": "100us",
        "mtu": 1500,
        "subnet": f"192.168.{suffix}.0/24",
        "interfaces": {
            router: {"ifname": f"{router}-lan0", "ip": f"192.168.{suffix}.254/24"},
            plc_name: {
                "ifname": f"{plc_name}-eth0",
                "ip": f"192.168.{suffix}.1/24",
                "gateway": f"192.168.{suffix}.254",
            },
        },
    })
    network.setdefault("taps", []).append({
        "endpoint": plc_name,
        "tap_name": tap_name,
        "mode": "use_local",
        "host_namespace": namespace,
    })
    network.setdefault("applications", []).append({
        "name": f"scada_to_plc{suffix}",
        "src": "scada",
        "dst": plc_name,
        "protocol": "tcp",
        "dst_port": 502,
    })
    attack_points = network.setdefault("attack_points", [])
    core = next((item for item in attack_points if item.get("name") == "core_backbone"), None)
    if core is not None:
        core.setdefault("targets", []).append(backbone_name)
    attack_points.append({
        "name": f"plc{suffix}_path",
        "type": "path",
        "targets": [plc_name, switch, router, "r0"],
    })


def configure_scale_plcs(cfg: dict[str, Any], count: int) -> None:
    """Configure a cumulative PLC scale point while preserving the base case."""
    plcs = cfg.get("plcs", []) or []
    if count < 1:
        raise ValueError("PLC count must be positive")
    if count <= len(plcs):
        prune_to_plcs(cfg, count)
        return
    existing_numbers = {
        int(str(item.get("name", "")).removeprefix("PLC"))
        for item in plcs
        if str(item.get("name", "")).removeprefix("PLC").isdigit()
    }
    for plc_number in range(1, count + 1):
        if plc_number not in existing_numbers:
            append_scale_plc(cfg, plc_number)
    if len(cfg.get("plcs", []) or []) != count:
        raise ValueError(f"failed to construct {count}-PLC scale configuration")


def configure_data_feed_scale_plcs(cfg: dict[str, Any], count: int) -> None:
    """Build a fully communication-active source/execution PLC pipeline.

    PLCs 1..N/2 own one random data-feed tag each. PLCs N/2+1..N receive the
    paired value through SCADA and execute one Boolean threshold rule. Every
    PLC therefore contributes real Modbus traffic and OpenPLC work; there are
    no ``PLC_Ready``-only scale placeholders and no water-network model.
    """
    if count < 2 or count % 2:
        raise ValueError("data-feed scale experiment requires a positive even PLC count")

    network = cfg["network"]
    nodes = network["nodes"]
    nodes["routers"] = [item for item in nodes.get("routers", []) if item.get("name") in {"r0", "r_scada"}]
    nodes["switches"] = [item for item in nodes.get("switches", []) if item.get("name") == "s_scada"]
    nodes["endpoints"] = [item for item in nodes.get("endpoints", []) if item.get("name") == "scada"]
    network["backbone_links"] = [
        item for item in network.get("backbone_links", []) if item.get("name") == "r0-r_scada"
    ]
    network["lans"] = [item for item in network.get("lans", []) if item.get("name") == "scada_lan"]
    network["taps"] = [item for item in network.get("taps", []) if item.get("endpoint") == "scada"]
    network["applications"] = []
    network["attack_points"] = [
        item for item in network.get("attack_points", [])
        if item.get("name") in {"core_backbone", "scada_path"}
    ]
    for point in network["attack_points"]:
        if point.get("name") == "core_backbone":
            point["targets"] = [
                target for target in point.get("targets", []) if target == "r0-r_scada"
            ]

    cfg["plcs"] = []
    for plc_number in range(1, count + 1):
        append_scale_plc(cfg, plc_number)

    half = count // 2
    plc_by_name = {str(plc["name"]): plc for plc in cfg["plcs"]}
    cfg["actuators"] = []
    cfg["initial_tank_values"] = {}
    cfg.pop("inp_file", None)
    cfg["physics"] = {
        "mode": "data_feed",
        "data_feed": {
            "minimum": 0.0,
            "maximum": 10.0,
            "precision": 6,
        },
    }

    for source_number in range(1, half + 1):
        execution_number = source_number + half
        tag = f"DATA_{source_number}"
        actuator = f"ACT_{source_number}"
        source = plc_by_name[f"PLC{source_number}"]
        source.update({
            "scale_role": "source",
            "paired_plc": f"PLC{execution_number}",
            "sensors": [tag],
            "actuators": [],
            "controls": [],
        })
        execution = plc_by_name[f"PLC{execution_number}"]
        execution.update({
            "scale_role": "execution",
            "paired_plc": f"PLC{source_number}",
            "sensors": [],
            "actuators": [actuator],
            "controls": [
                {
                    "action": "closed",
                    "actuator": actuator,
                    "dependant": tag,
                    "type": "below",
                    "value": 5.0,
                },
                {
                    "action": "open",
                    "actuator": actuator,
                    "dependant": tag,
                    "type": "above_equal",
                    "value": 5.0,
                },
            ],
        })
        cfg["actuators"].append({"name": actuator, "initial_state": "closed"})

    if len(cfg["plcs"]) != count:
        raise ValueError(f"failed to construct {count}-PLC data-feed scale configuration")
    if sum(len(plc.get("sensors", []) or []) for plc in cfg["plcs"]) != half:
        raise ValueError("data-feed source mapping is incomplete")
    if sum(len(plc.get("actuators", []) or []) for plc in cfg["plcs"]) != half:
        raise ValueError("data-feed execution mapping is incomplete")


def build_specs() -> list[dict[str, Any]]:
    base = load_yaml(PROJECT_ROOT / "examples/c_town/config.yaml")
    mitm_template = load_yaml(PROJECT_ROOT / "examples/c_town/config_mitm_plc4.yaml")
    dos_template = load_yaml(PROJECT_ROOT / "examples/c_town/config_dos_plc2_three_bots.yaml")
    logic_template = load_yaml(PROJECT_ROOT / "examples/c_town/config_openplc_logic_plc4.yaml")
    specs: list[dict[str, Any]] = []
    seed = 2026081700
    ns3_run = 500

    def add(cfg: dict[str, Any], experiment_id: str, group: str, parameter: str, value: Any,
            *, timeout: int = 1800, sync_timeout: int = 180, **metadata: Any) -> None:
        nonlocal seed, ns3_run
        seed += 1
        ns3_run += 1
        set_experiment(
            cfg, experiment_id=experiment_id, group=group, parameter=parameter,
            value=value, seed=seed, ns3_run=ns3_run, **metadata,
        )
        specs.append({
            "id": experiment_id,
            "group": group,
            "config": cfg,
            "timeout_sec": timeout,
            "sync_timeout_sec": sync_timeout,
        })

    cfg = deepcopy(base)
    cfg["iterations"] = ATTACK_ITERATIONS
    disable_attacks(cfg)
    configure_target_links(cfg)
    instrument(cfg, pcap_links=["r0-r4"], modbus_trace=True)
    add(cfg, "baseline_closed_loop", "baseline", "attack", "none",
        attack_window=list(ATTACK_WINDOW), topology=topology_counts(cfg))

    for bias in MITM_BIASES:
        cfg = deepcopy(mitm_template)
        cfg["iterations"] = ATTACK_ITERATIONS
        configure_target_links(cfg)
        set_window(cfg, *ATTACK_WINDOW)
        scenario = cfg["attacks"]["scenarios"][0]
        scenario["name"] = f"mitm_plc4_t7_bias_{label_number(bias)}"
        rule = scenario["rules"][0]
        rule.update({
            "name": f"add_t7_bias_{label_number(bias)}",
            "operation": "add",
            "value": bias,
        })
        instrument(cfg, pcap_links=["r0-r4"], modbus_trace=True)
        add(cfg, f"mitm_bias_{label_number(bias)}", "mitm", "delta_x", bias,
            attack_window=list(ATTACK_WINDOW), target="PLC4/PLC9_T7", topology=topology_counts(cfg))

    for rho in DOS_RHOS:
        cfg = deepcopy(dos_template)
        cfg["iterations"] = ATTACK_ITERATIONS
        configure_three_bot_plc4(cfg, rho)
        set_window(cfg, *ATTACK_WINDOW)
        instrument(cfg, pcap_links=["r0-r4"], queue_timeseries=True, modbus_trace=True)
        add(cfg, f"dos_rho_{label_number(rho)}", "dos", "rho", rho,
            attack_window=list(ATTACK_WINDOW), bottleneck_mbps=10.0,
            aggregate_offered_mbps=10.0 * rho, bot_count=3,
            target_link="r0-r4", topology=topology_counts(cfg), timeout=2400)

    for shift in PLC_THRESHOLD_SHIFTS:
        cfg = deepcopy(logic_template)
        cfg["iterations"] = ATTACK_ITERATIONS
        configure_target_links(cfg)
        set_window(cfg, *ATTACK_WINDOW)
        scenario = cfg["attacks"]["scenarios"][0]
        scenario["name"] = f"plc4_pu10_threshold_shift_{label_number(shift)}"
        rule = scenario["injection"]["rule"]
        # Scan only PU10's upper stop threshold.  Positive shifts are excluded:
        # baseline T7 already rises from about 1.51 m to 4.85 m during the
        # attack window, so lower thresholds expose the relevant transition.
        rule.update({
            "actuator": "PU10",
            "dependant": "T7",
            "type": "above",
            "original_value": 4.8,
        })
        original = float(rule["original_value"])
        injected = round(original + shift, 10)
        rule["injected_value"] = injected
        # A zero-strength observation is a control run through the same logic
        # experiment configuration.  The OpenPLC injector correctly rejects a
        # no-op rewrite, so keep its rule metadata for analysis while disabling
        # the scenario instead of pretending that a logic modification occurred.
        if shift == 0.0:
            cfg["attacks"]["enabled"] = False
            scenario["enabled"] = False
        instrument(cfg, pcap_links=["r0-r4"], modbus_trace=True)
        add(cfg, f"plc_threshold_shift_{label_number(shift)}", "plc_logic", "delta_h", shift,
            attack_window=list(ATTACK_WINDOW), original_threshold=original,
            injected_threshold=injected, target="PLC4/PU10", topology=topology_counts(cfg))

    for count in SCALE_PLC_COUNTS:
        cfg = deepcopy(base)
        cfg["iterations"] = SCALE_ITERATIONS
        disable_attacks(cfg)
        configure_data_feed_scale_plcs(cfg, count)
        instrument(cfg, pcap_links=[])
        cfg["network"]["measurement"]["pcap"] = False
        cfg["network"]["measurement"]["pcap_links"] = []
        cfg["network"]["pcap"] = False
        counts = topology_counts(cfg)
        add(
            cfg,
            f"scale_{count}_plcs",
            "scalability",
            "plc_count",
            count,
            topology=counts,
            measurement_window_iterations=SCALE_ITERATIONS,
            physical_process_plc_count=0,
            synthetic_scale_plc_count=0,
            data_feed_source_plc_count=count // 2,
            execution_plc_count=count // 2,
            communication_pair_count=count // 2,
            all_plcs_communication_active=True,
            scale_pipeline="data_feed -> source PLC -> SCADA -> paired execution PLC -> actuator",
            data_feed_range=[0.0, 10.0],
            execution_rule="value < 5.0 => closed; value >= 5.0 => open",
            synchronization_timeout_sec=max(180, count * 5),
            timeout=SCALE_TIMEOUTS.get(count, 1800),
            sync_timeout=max(180, count * 5),
        )
        specs[-1].update({
            "physics_mode": "data_feed",
            "init_style": "current",
            "run_check": False,
            "audit_scalability_pipeline": True,
        })

    return specs


def attempt_paths(archive: Path, spec: dict[str, Any], attempt: int) -> tuple[Path, Path, Path]:
    root = archive / SECTION_NAMES[spec["group"]] / "runs" / spec["id"] / f"attempt_{attempt:02d}"
    output = root / "output"
    root.mkdir(parents=True, exist_ok=True)
    cfg = deepcopy(spec["config"])
    cfg["output_path"] = str(output)
    cfg["experiment"]["attempt"] = attempt
    cfg_path = root / "config.yaml"
    # Preparing/resuming an archive must not rewrite the configuration of an
    # already executed attempt.  Its resolved runtime config is evidence and
    # the next execution will receive a new attempt number below.
    if not cfg_path.exists():
        cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return cfg_path, output, root / "run.log"


def prepare(archive: Path) -> list[dict[str, Any]]:
    archive.mkdir(parents=True, exist_ok=True)
    for section in SECTION_NAMES.values():
        (archive / section / "runs").mkdir(parents=True, exist_ok=True)
    (archive / "06_analysis").mkdir(exist_ok=True)
    specs = build_specs()
    experiments = []
    for spec in specs:
        config, output, _log = attempt_paths(archive, spec, 1)
        experiments.append({
            "id": spec["id"], "group": spec["group"],
            "config": str(config), "output": str(output),
            "parameter": spec["config"]["experiment"]["parameter"],
            "value": spec["config"]["experiment"]["value"],
            "timeout_sec": spec["timeout_sec"],
            "sync_timeout_sec": spec["sync_timeout_sec"],
        })
    plan = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "method": "one-factor-at-a-time; one observation per configuration",
        "attack_iterations": ATTACK_ITERATIONS,
        "scale_iterations": SCALE_ITERATIONS,
        "attack_window": list(ATTACK_WINDOW),
        "formal_run_count": len(specs),
        "experiments": experiments,
    }
    (archive / "EXPERIMENT_PLAN.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (archive / "RUN_INSTRUCTIONS.md").write_text(
        "# Resume instructions\n\n"
        "The real ns-3/TAP topology needs cached sudo credentials. In an interactive terminal run:\n\n"
        "```bash\n"
        "sudo -v\n"
        f"cd {PROJECT_ROOT}\n"
        f"python3 scripts/run_advisor_experiments.py --archive {archive}\n"
        f"python3 scripts/analyze_advisor_experiments.py {archive}\n"
        "```\n\n"
        "The runner resumes valid items and preserves failed attempts. Do not run two items concurrently, "
        "because they intentionally share Linux namespace and OpenPLC port names.\n",
        encoding="utf-8",
    )
    return specs


def lifecycle_status(output: Path) -> str:
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


def supersede_valid_records(
    archive: Path,
    specs: list[dict[str, Any]],
    *,
    reason: str,
    entire_groups: set[str] | None = None,
) -> int:
    """Retain prior observations but exclude them from reuse and analysis."""
    index_path = archive / "RUN_INDEX.json"
    try:
        records = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return 0
    selected = {str(spec["id"]) for spec in specs}
    groups = entire_groups or set()
    changed = 0
    superseded_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for record in records:
        if (
            str(record.get("id")) in selected
            or str(record.get("group")) in groups
        ) and record.get("valid") is True:
            record["valid"] = False
            record["superseded"] = True
            record["superseded_at"] = superseded_at
            record["superseded_reason"] = reason
            changed += 1
    if changed:
        index_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return changed


def run(archive: Path, specs: list[dict[str, Any]]) -> int:
    index_path = archive / "RUN_INDEX.json"
    try:
        records = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        records = []

    for position, spec in enumerate(specs, start=1):
        if any(row.get("id") == spec["id"] and row.get("valid") for row in records):
            print(f"[ADVISOR] reuse {spec['id']}", flush=True)
            continue
        previous = [int(row.get("attempt", 0)) for row in records if row.get("id") == spec["id"]]
        # A manually interrupted runner may leave a partial attempt directory
        # before it can append RUN_INDEX.json.  Never reuse that directory:
        # stale sync markers or partial runtime files would contaminate the
        # next observation.
        run_root = archive / SECTION_NAMES[spec["group"]] / "runs" / spec["id"]
        for path in run_root.glob("attempt_*"):
            if not (path / "run.log").exists():
                continue
            try:
                previous.append(int(path.name.removeprefix("attempt_")))
            except ValueError:
                continue
        succeeded = False
        # Preserve up to six infrastructure attempts.  A first-time ns-3 build
        # and a missing runtime dependency can consume early attempts without
        # producing an experimental observation.
        for attempt in range(max(previous, default=0) + 1, 7):
            config, output, log_path = attempt_paths(archive, spec, attempt)
            env = os.environ.copy()
            env.update({
                "PYTHON_BIN": sys.executable,
                "PATH": f"{Path(sys.executable).parent}:{env.get('PATH', '')}",
                "PYTHONDONTWRITEBYTECODE": "1",
                "SYNC_TIMEOUT": str(spec.get("sync_timeout_sec", 180)),
                "PHYSICS_MODE": str(spec.get("physics_mode", "dhalsim_epynet")),
                "INIT_STYLE": str(spec.get("init_style", "dhalsim")),
            })
            command = ["bash", str(PROJECT_ROOT / "scripts/run_all.sh"), "--config", str(config)]
            if spec.get("run_check", True):
                command.append("--check")
            print(
                f"[ADVISOR] {position:02d}/{len(specs)} group={spec['group']} "
                f"id={spec['id']} attempt={attempt}", flush=True,
            )
            started = time.time()
            timed_out = False
            with log_path.open("w", encoding="utf-8") as log:
                proc = subprocess.Popen(
                    command, cwd=str(PROJECT_ROOT), env=env,
                    stdout=log, stderr=subprocess.STDOUT,
                )
                try:
                    returncode = proc.wait(timeout=int(spec["timeout_sec"]))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    proc.terminate()
                    try:
                        returncode = proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        returncode = proc.wait()
            elapsed = time.time() - started
            status = lifecycle_status(output)
            audit_returncode: int | None = None
            if (
                not timed_out
                and status == "success"
                and spec.get("audit_scalability_pipeline")
            ):
                with log_path.open("a", encoding="utf-8") as log:
                    log.write("\n[ADVISOR] auditing data-feed scalability pipeline\n")
                    audit_result = subprocess.run(
                        [
                            sys.executable,
                            str(PROJECT_ROOT / "scripts/audit_scalability_pipeline.py"),
                            "--config",
                            str(config),
                        ],
                        cwd=str(PROJECT_ROOT),
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                audit_returncode = audit_result.returncode
            valid = (
                not timed_out
                and status == "success"
                and experiment_completed(output)
                and (audit_returncode in {None, 0})
            )
            record = {
                "id": spec["id"], "group": spec["group"], "attempt": attempt,
                "parameter": spec["config"]["experiment"]["parameter"],
                "value": spec["config"]["experiment"]["value"],
                "config": str(config), "output": str(output), "log": str(log_path),
                "returncode": returncode, "timed_out": timed_out,
                "audit_returncode": audit_returncode,
                "elapsed_sec": elapsed, "simulation_end": status, "valid": valid,
            }
            records.append(record)
            index_path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if valid:
                succeeded = True
                print(f"[ADVISOR] success {spec['id']} elapsed={elapsed:.1f}s", flush=True)
                break
            (config.parent / "failure.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"[ADVISOR][WARN] failed {spec['id']} rc={returncode} status={status or 'missing'}", flush=True)
        if not succeeded:
            print(f"[ADVISOR][ERROR] no valid run for {spec['id']}", flush=True)

    failed = [
        spec["id"] for spec in specs
        if not any(row.get("id") == spec["id"] and row.get("valid") for row in records)
    ]
    print(f"[ADVISOR] valid={len(specs) - len(failed)}/{len(specs)} failed={failed}", flush=True)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--group", action="append", choices=tuple(SECTION_NAMES))
    parser.add_argument("--id", action="append", dest="ids")
    parser.add_argument(
        "--rerun",
        action="store_true",
        help="preserve and supersede prior valid records for the selected --group/--id, then run new attempts",
    )
    args = parser.parse_args()
    archive = (args.archive or default_archive()).expanduser().resolve()
    specs = prepare(archive)
    if args.group:
        specs = [x for x in specs if x["group"] in set(args.group)]
    if args.ids:
        specs = [x for x in specs if x["id"] in set(args.ids)]
    if args.rerun:
        if not args.group and not args.ids:
            parser.error("--rerun requires at least one --group or --id selector")
        changed = supersede_valid_records(
            archive,
            specs,
            reason="rerun requested after experimental-design correction",
            entire_groups=set(args.group or []),
        )
        print(f"[ADVISOR] superseded_prior_valid_records={changed}", flush=True)
    print(f"[ADVISOR] archive={archive} selected={len(specs)}", flush=True)
    if args.prepare_only:
        return 0
    sudo_check = subprocess.run(
        ["sudo", "-n", "true"], capture_output=True, text=True, check=False
    )
    if sudo_check.returncode != 0:
        print(
            "[ADVISOR][BLOCKED] non-interactive sudo credentials are unavailable. "
            "Run `sudo -v` in an interactive terminal, then rerun this exact command; "
            "prepared configurations and valid prior attempts will be reused.",
            file=sys.stderr,
            flush=True,
        )
        return 3
    return run(archive, specs)


if __name__ == "__main__":
    raise SystemExit(main())
