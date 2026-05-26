#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

MD_RE = re.compile(r"\b([A-Za-z_]\w*)\s+AT\s+%MD(\d+)\s*:\s*REAL\s*;", re.IGNORECASE)
QX_RE = re.compile(r"\b([A-Za-z_]\w*)\s+AT\s+%QX(\d+)\.(\d+)\s*:\s*BOOL\s*;", re.IGNORECASE)
PLC_RE = re.compile(r"^PLC(\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class MdVar:
    name: str
    md_index: int
    source_prefix: Optional[str]
    tag: str


@dataclass(frozen=True)
class CoilVar:
    name: str
    coil_index: int
    source_prefix: Optional[str]
    tag: str


@dataclass
class PlcRuntime:
    name: str
    namespace: str
    ip: str
    st_path: Path
    md_vars: dict[str, MdVar] = field(default_factory=dict)
    coil_vars: dict[str, CoilVar] = field(default_factory=dict)

    @property
    def lower_name(self) -> str:
        return self.name.lower()


@dataclass
class RuntimeConfig:
    config_path: Path
    raw: dict[str, Any]
    output_dir: Path
    st_dir: Path
    plcs: dict[str, PlcRuntime]
    initial_state: dict[str, float]
    actuator_initial_state: dict[str, bool]
    hydraulic_timestep: int
    iterations: int

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "config": str(self.config_path),
            "output_dir": str(self.output_dir),
            "st_dir": str(self.st_dir),
            "iterations": self.iterations,
            "hydraulic_timestep": self.hydraulic_timestep,
            "plcs": {
                name: {
                    "namespace": plc.namespace,
                    "ip": plc.ip,
                    "st_path": str(plc.st_path),
                    "md_vars": {k: v.md_index for k, v in plc.md_vars.items()},
                    "coil_vars": {k: v.coil_index for k, v in plc.coil_vars.items()},
                }
                for name, plc in self.plcs.items()
            },
        }


def load_yaml(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {p}")
    return data


def split_var_name(var_name: str) -> tuple[Optional[str], str]:
    # PLC2_T1 -> (PLC2, T1); PLC_Ready stays special.
    if "_" not in var_name:
        return None, var_name
    prefix, tag = var_name.split("_", 1)
    if PLC_RE.match(prefix):
        return prefix.upper(), tag
    return None, var_name


def parse_st_file(st_path: Path) -> tuple[dict[str, MdVar], dict[str, CoilVar]]:
    text = st_path.read_text(encoding="utf-8", errors="ignore")
    md_vars: dict[str, MdVar] = {}
    coil_vars: dict[str, CoilVar] = {}

    for name, md_index in MD_RE.findall(text):
        prefix, tag = split_var_name(name)
        md_vars[name] = MdVar(name=name, md_index=int(md_index), source_prefix=prefix, tag=tag)

    for name, byte_idx, bit_idx in QX_RE.findall(text):
        prefix, tag = split_var_name(name)
        coil_index = int(byte_idx) * 8 + int(bit_idx)
        coil_vars[name] = CoilVar(name=name, coil_index=coil_index, source_prefix=prefix, tag=tag)

    return md_vars, coil_vars


def _resolve_output_dir(config_path: Path, cfg: dict[str, Any]) -> Path:
    raw = cfg.get("output_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("config.yaml missing valid top-level output_path")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        return (config_path.parent / p).resolve()

    # The example config often contains the author's absolute path. Keep that
    # path when it exists on the current machine, but make copied projects usable
    # by falling back to examples/<case>/output beside the config file.
    if p.exists():
        return p.resolve()
    local_output = (config_path.parent / "output").resolve()
    if local_output.exists():
        return local_output
    return p.resolve()


def _endpoint_table(cfg: dict[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    endpoints = cfg.get("network", {}).get("nodes", {}).get("endpoints", [])
    if isinstance(endpoints, list):
        for ep in endpoints:
            if not isinstance(ep, dict):
                continue
            name = ep.get("name")
            if isinstance(name, str):
                result[name.upper() if name.upper().startswith("PLC") else name] = {
                    "name": name,
                    "role": str(ep.get("role", "")),
                    "namespace": str(ep.get("namespace", "")),
                    "tap": str(ep.get("tap", ep.get("tap_name", ""))),
                }
    return result


def _endpoint_ips(cfg: dict[str, Any]) -> dict[str, str]:
    ips: dict[str, str] = {}
    for lan in cfg.get("network", {}).get("lans", []) or []:
        if not isinstance(lan, dict):
            continue
        interfaces = lan.get("interfaces", {})
        if not isinstance(interfaces, dict):
            continue
        for name, item in interfaces.items():
            if not isinstance(item, dict) or "ip" not in item:
                continue
            key = str(name).upper() if str(name).upper().startswith("PLC") else str(name)
            try:
                ips[key] = str(ipaddress.ip_interface(str(item["ip"])).ip)
            except Exception:
                ips[key] = str(item["ip"]).split("/", 1)[0]
    return ips


def _initial_physical_state(cfg: dict[str, Any]) -> dict[str, float]:
    state: dict[str, float] = {}
    for k, v in (cfg.get("initial_tank_values") or {}).items():
        try:
            state[str(k)] = float(v)
        except Exception:
            pass

    # Provide defaults for all declared sensors so the first iteration is total.
    for plc in cfg.get("plcs", []) or []:
        if not isinstance(plc, dict):
            continue
        for s in plc.get("sensors", []) or []:
            name = str(s)
            state.setdefault(name, 0.0)
    return state


def _initial_actuators(cfg: dict[str, Any]) -> dict[str, bool]:
    states: dict[str, bool] = {}
    for item in cfg.get("actuators", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        if not name:
            continue
        raw = str(item.get("initial_state", "closed")).lower()
        states[name] = raw in {"open", "true", "on", "1", "opened"}
    return states


def _hydraulic_timestep(cfg: dict[str, Any]) -> int:
    for item in cfg.get("time", []) or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("hydraulic_timestep")
        if isinstance(raw, list) and len(raw) >= 2:
            try:
                return int(raw[1])
            except Exception:
                pass
    return 300


def load_runtime_config(config_path: Path | str) -> RuntimeConfig:
    config_path = Path(config_path).resolve()
    cfg = load_yaml(config_path)
    output_dir = _resolve_output_dir(config_path, cfg)
    st_dir = output_dir / "st"
    endpoint_info = _endpoint_table(cfg)
    endpoint_ips = _endpoint_ips(cfg)

    plcs: dict[str, PlcRuntime] = {}
    for name, ep in endpoint_info.items():
        if ep.get("role") != "plc":
            continue
        st_path = st_dir / f"{name.lower()}.st"
        if not st_path.exists():
            continue
        md_vars, coil_vars = parse_st_file(st_path)
        plc = PlcRuntime(
            name=name,
            namespace=ep.get("namespace", ""),
            ip=endpoint_ips.get(name, ""),
            st_path=st_path,
            md_vars=md_vars,
            coil_vars=coil_vars,
        )
        plcs[name] = plc

    if not plcs:
        raise ValueError(f"No PLC runtime can be built from config={config_path}, st_dir={st_dir}")

    return RuntimeConfig(
        config_path=config_path,
        raw=cfg,
        output_dir=output_dir,
        st_dir=st_dir,
        plcs=plcs,
        initial_state=_initial_physical_state(cfg),
        actuator_initial_state=_initial_actuators(cfg),
        hydraulic_timestep=_hydraulic_timestep(cfg),
        iterations=int(cfg.get("iterations", 1) or 1),
    )


def read_json(path: Path | str) -> Any:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path | str, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
