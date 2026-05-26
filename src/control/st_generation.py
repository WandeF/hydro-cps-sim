#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate and validate OpenPLC Structured Text programs from config.yaml.

The generator intentionally keeps the ST simple: each PLC receives one PROGRAM
with REAL sensor/dependency inputs (%MD*) and BOOL actuator outputs (%QX*).
The runtime parser reads the generated addresses, so these files are the single
source of truth for Modbus register/coil layout.
"""
from __future__ import annotations

import argparse
import re
import stat
from pathlib import Path
from typing import Any

from src.core.config import MD_RE, QX_RE, load_yaml

PLC_RE = re.compile(r"^PLC(\d+)$", re.IGNORECASE)


def _resolve_output_dir(config_path: Path, cfg: dict[str, Any]) -> Path:
    raw = cfg.get("output_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("config.yaml missing valid top-level output_path")
    p = Path(raw).expanduser()
    if p.is_absolute():
        if p.exists():
            return p.resolve()
        local_output = (config_path.parent / "output").resolve()
        if local_output.exists():
            return local_output
        return p.resolve()
    return (config_path.parent / p).resolve()


def _plc_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    name = str(item.get("name", ""))
    m = PLC_RE.match(name)
    if m:
        return (int(m.group(1)), name)
    return (10**9, name)


def _plcs(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    plcs = [p for p in (cfg.get("plcs") or []) if isinstance(p, dict) and p.get("name")]
    return sorted(plcs, key=_plc_sort_key)


def _sensor_owner_map(cfg: dict[str, Any]) -> dict[str, str]:
    """Return physical tag -> PLC name for tags listed under each PLC's sensors."""
    owners: dict[str, str] = {}
    for plc in _plcs(cfg):
        name = str(plc["name"]).upper()
        for sensor in plc.get("sensors", []) or []:
            tag = str(sensor)
            owners.setdefault(tag, name)
    return owners


def _actuator_initial_state(cfg: dict[str, Any]) -> dict[str, bool]:
    states: dict[str, bool] = {}
    for item in cfg.get("actuators", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        if not name:
            continue
        raw = str(item.get("initial_state", "closed")).strip().lower()
        states[name] = raw in {"open", "opened", "true", "on", "1", "yes"}
    return states


def _action_to_bool(action: str) -> str:
    action = action.strip().lower()
    if action in {"open", "opened", "true", "on", "1"}:
        return "TRUE"
    if action in {"closed", "close", "false", "off", "0"}:
        return "FALSE"
    raise ValueError(f"Unsupported actuator action: {action!r}")


def _st_real_literal(value: Any) -> str:
    """Render a YAML numeric value as an IEC 61131-3 REAL literal.

    OpenPLC's ST compiler is strict about comparing REAL variables with INT
    literals.  For example, ``PLC2_T1 < 4`` may fail, while
    ``PLC2_T1 < 4.0`` compiles correctly.  YAML loads ``4.0`` as the Python
    float ``4.0``, and the old ``:g`` formatting collapsed it to ``4``.
    This helper therefore always emits a decimal point for finite numbers.
    """
    threshold = float(value)
    if threshold != threshold or threshold in {float("inf"), float("-inf")}:
        raise ValueError(f"Invalid REAL threshold: {value!r}")

    text = f"{threshold:.12g}"
    if "e" in text.lower():
        text = f"{threshold:.12f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text


def _condition_expr(var_name: str, typ: str, value: Any) -> str:
    typ = str(typ).strip().lower()
    threshold = _st_real_literal(value)
    if typ == "below":
        return f"{var_name} < {threshold}"
    if typ == "above":
        return f"{var_name} > {threshold}"
    raise ValueError(f"Unsupported control type: {typ!r}; expected 'below' or 'above'")


def _dependency_variables(plc: dict[str, Any], sensor_owner: dict[str, str]) -> list[tuple[str, str]]:
    """Return [(variable_name, physical_tag), ...] for non-local control inputs."""
    plc_name = str(plc["name"]).upper()
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for ctrl in plc.get("controls", []) or []:
        if not isinstance(ctrl, dict):
            continue
        tag = str(ctrl.get("dependant", ""))
        if not tag:
            continue
        owner = sensor_owner.get(tag, plc_name)
        var = f"{owner}_{tag}"
        if var not in seen:
            seen.add(var)
            result.append((var, tag))
    return result


def _local_sensor_variables(plc: dict[str, Any], dependency_vars: list[tuple[str, str]]) -> list[tuple[str, str]]:
    plc_name = str(plc["name"]).upper()
    dep_names = {name for name, _ in dependency_vars}
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for sensor in plc.get("sensors", []) or []:
        tag = str(sensor)
        var = f"{plc_name}_{tag}"
        if var in dep_names or var in seen:
            continue
        seen.add(var)
        result.append((var, tag))
    return result


def _actuator_variables(plc: dict[str, Any]) -> list[tuple[str, str]]:
    plc_name = str(plc["name"]).upper()
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for actuator in plc.get("actuators", []) or []:
        tag = str(actuator)
        var = f"{plc_name}_{tag}"
        if var not in seen:
            seen.add(var)
            result.append((var, tag))
    return result


def _coil_addr(index: int) -> str:
    return f"%QX{index // 8}.{index % 8}"


def generate_plc_st(cfg: dict[str, Any], plc: dict[str, Any]) -> str:
    plc_name = str(plc["name"]).upper()
    sensor_owner = _sensor_owner_map(cfg)
    dependencies = _dependency_variables(plc, sensor_owner)
    local_sensors = _local_sensor_variables(plc, dependencies)
    actuators = _actuator_variables(plc)

    lines: list[str] = []
    lines.append("PROGRAM SYSTEM_LOGIC")
    lines.append("  VAR")
    lines.append("    PLC_Ready AT %MD0 : REAL;")

    md_index = 1
    lines.append("    (* Dependent physical inputs used by this PLC's control logic *)")
    for var, _tag in dependencies:
        lines.append(f"    {var} AT %MD{md_index} : REAL;")
        md_index += 1

    lines.append("    (* Local physical sensors exposed by this PLC *)")
    for var, _tag in local_sensors:
        lines.append(f"    {var} AT %MD{md_index} : REAL;")
        md_index += 1

    lines.append("    (* Actuators *)")
    for idx, (var, _tag) in enumerate(actuators):
        lines.append(f"    {var} AT {_coil_addr(idx)} : BOOL;")
    lines.append("  END_VAR")
    lines.append("")
    lines.append("  PLC_Ready := 1.0;")

    controls = [c for c in plc.get("controls", []) or [] if isinstance(c, dict)]
    if controls:
        lines.append("")
        lines.append("  (* Virtual control logic *)")
    dep_var_by_tag = {tag: var for var, tag in dependencies}
    for ctrl in controls:
        tag = str(ctrl.get("dependant", ""))
        actuator = str(ctrl.get("actuator", ""))
        action = str(ctrl.get("action", ""))
        if not tag or not actuator or not action:
            raise ValueError(f"Bad control entry in {plc_name}: {ctrl!r}")
        dep_var = dep_var_by_tag.get(tag)
        if dep_var is None:
            owner = sensor_owner.get(tag, plc_name)
            dep_var = f"{owner}_{tag}"
        act_var = f"{plc_name}_{actuator}"
        condition = _condition_expr(dep_var, str(ctrl.get("type", "")), ctrl.get("value"))
        target = _action_to_bool(action)
        lines.append(f"  IF {condition} THEN")
        lines.append(f"    {act_var} := {target};")
        lines.append("  END_IF;")
        lines.append("")

    lines.append("END_PROGRAM")
    lines.append("")
    lines.append("")
    lines.append("CONFIGURATION Config0")
    lines.append("  RESOURCE Res0 ON PLC ")
    lines.append("    TASK task0(INTERVAL := T#100ms,PRIORITY := 0);")
    lines.append("    PROGRAM instance0 WITH task0 : SYSTEM_LOGIC;")
    lines.append("  END_RESOURCE")
    lines.append("END_CONFIGURATION")
    return "\n".join(lines) + "\n"


def generate_combined_st(cfg: dict[str, Any]) -> str:
    """Generate a single aggregate ST file for inspection/backward compatibility.

    The per-PLC files under output/st are the files compiled by
    plc_precompile.py.  This aggregate file is still valid ST and is useful for
    reviewing the complete virtual control policy in one place.
    """
    sensor_owner = _sensor_owner_map(cfg)
    md_vars: list[str] = []
    md_seen: set[str] = set()

    def add_md(var: str) -> None:
        if var not in md_seen:
            md_seen.add(var)
            md_vars.append(var)

    for plc in _plcs(cfg):
        plc_name = str(plc["name"]).upper()
        for sensor in plc.get("sensors", []) or []:
            add_md(f"{plc_name}_{str(sensor)}")
        for ctrl in plc.get("controls", []) or []:
            if not isinstance(ctrl, dict):
                continue
            tag = str(ctrl.get("dependant", ""))
            if not tag:
                continue
            owner = sensor_owner.get(tag, plc_name)
            add_md(f"{owner}_{tag}")

    actuator_vars: list[tuple[str, str]] = []
    actuator_seen: set[str] = set()
    for plc in _plcs(cfg):
        for var, tag in _actuator_variables(plc):
            if var not in actuator_seen:
                actuator_seen.add(var)
                actuator_vars.append((var, tag))

    lines: list[str] = []
    lines.append("PROGRAM SYSTEM_LOGIC")
    lines.append("  VAR")
    lines.append("    PLC_Ready AT %MD0 : REAL;")
    lines.append("    (* Physical inputs *)")
    for idx, var in enumerate(md_vars, start=1):
        lines.append(f"    {var} AT %MD{idx} : REAL;")
    lines.append("")
    lines.append("    (* Actuators *)")
    for idx, (var, _tag) in enumerate(actuator_vars):
        lines.append(f"    {var} AT {_coil_addr(idx)} : BOOL;")
    lines.append("  END_VAR")
    lines.append("")
    lines.append("  PLC_Ready := 1.0;")

    for plc in _plcs(cfg):
        plc_name = str(plc["name"]).upper()
        controls = [c for c in plc.get("controls", []) or [] if isinstance(c, dict)]
        if not controls:
            continue
        lines.append("")
        lines.append(f"  (* Virtual {plc_name} control logic *)")
        for ctrl in controls:
            tag = str(ctrl.get("dependant", ""))
            owner = sensor_owner.get(tag, plc_name)
            dep_var = f"{owner}_{tag}"
            act_var = f"{plc_name}_{str(ctrl.get('actuator', ''))}"
            condition = _condition_expr(dep_var, str(ctrl.get("type", "")), ctrl.get("value"))
            target = _action_to_bool(str(ctrl.get("action", "")))
            lines.append(f"  IF {condition} THEN")
            lines.append(f"    {act_var} := {target};")
            lines.append("  END_IF;")
            lines.append("")

    lines.append("END_PROGRAM")
    lines.append("")
    lines.append("")
    lines.append("CONFIGURATION Config0")
    lines.append("  RESOURCE Res0 ON PLC ")
    lines.append("    TASK task0(INTERVAL := T#100ms,PRIORITY := 0);")
    lines.append("    PROGRAM instance0 WITH task0 : SYSTEM_LOGIC;")
    lines.append("  END_RESOURCE")
    lines.append("END_CONFIGURATION")
    return "\n".join(lines) + "\n"


def validate_st_file(path: Path, plc_cfg: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    if "PROGRAM SYSTEM_LOGIC" not in text:
        errors.append("missing PROGRAM SYSTEM_LOGIC")
    if "CONFIGURATION Config0" not in text:
        errors.append("missing CONFIGURATION Config0")
    if "PLC_Ready AT %MD0 : REAL;" not in text:
        errors.append("missing PLC_Ready at %MD0")

    md_matches = MD_RE.findall(text)
    qx_matches = QX_RE.findall(text)
    md_addrs = [int(idx) for _name, idx in md_matches]
    qx_addrs = [int(byte) * 8 + int(bit) for _name, byte, bit in qx_matches]
    if len(md_addrs) != len(set(md_addrs)):
        errors.append("duplicate %MD address")
    if len(qx_addrs) != len(set(qx_addrs)):
        errors.append("duplicate %QX address")

    plc_name = str(plc_cfg.get("name", "")).upper()
    expected_actuators = {f"{plc_name}_{str(a)}" for a in plc_cfg.get("actuators", []) or []}
    actual_actuators = {name for name, _byte, _bit in qx_matches}
    missing = sorted(expected_actuators - actual_actuators)
    extra = sorted(actual_actuators - expected_actuators)
    if missing:
        errors.append("missing actuator declarations: " + ", ".join(missing))
    if extra:
        errors.append("unexpected actuator declarations: " + ", ".join(extra))

    real_int_cmp = re.compile(r"\bIF\s+[A-Za-z_][A-Za-z0-9_]*\s*[<>]\s*[-+]?\d+\s+THEN\b")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if real_int_cmp.search(line):
            errors.append(
                f"line {lineno}: REAL comparison uses an integer literal; use a REAL literal such as 4.0"
            )

    declared_names = {name for name, _idx in md_matches} | actual_actuators | {"PLC_Ready"}
    for ctrl in plc_cfg.get("controls", []) or []:
        if not isinstance(ctrl, dict):
            continue
        act_var = f"{plc_name}_{str(ctrl.get('actuator', ''))}"
        if act_var not in declared_names:
            errors.append(f"control references undeclared actuator {act_var}")
    return errors


def generate_and_validate(config_path: Path) -> dict[str, Any]:
    cfg = load_yaml(config_path)
    output_dir = _resolve_output_dir(config_path, cfg)
    st_dir = output_dir / "st"
    st_dir.mkdir(parents=True, exist_ok=True)

    plcs = _plcs(cfg)
    if not plcs:
        raise ValueError("config.yaml contains no PLC entries under top-level 'plcs'")

    generated: list[str] = []
    errors: dict[str, list[str]] = {}
    for plc in plcs:
        name = str(plc["name"]).lower()
        path = st_dir / f"{name}.st"
        path.write_text(generate_plc_st(cfg, plc), encoding="utf-8")
        generated.append(str(path))
        file_errors = validate_st_file(path, plc)
        if file_errors:
            errors[path.name] = file_errors

    combined_path = output_dir / "plc.st"
    combined_path.write_text(generate_combined_st(cfg), encoding="utf-8")
    generated.append(str(combined_path))

    for path_str in generated:
        path = Path(path_str)
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

    return {
        "output_dir": str(output_dir),
        "st_dir": str(st_dir),
        "generated": generated,
        "errors": errors,
        "ok": not errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate OpenPLC ST files from Hydro-CPS config.yaml")
    parser.add_argument("--config", required=True, type=Path, help="Path to config.yaml")
    args = parser.parse_args()

    config_path = args.config.resolve()
    summary = generate_and_validate(config_path)
    print(f"[ST] output_dir={summary['output_dir']}")
    print(f"[ST] st_dir    ={summary['st_dir']}")
    for item in summary["generated"]:
        print(f"[ST] generated {item}")
    if not summary["ok"]:
        for file_name, errs in summary["errors"].items():
            for err in errs:
                print(f"[ST][ERROR] {file_name}: {err}")
        return 1
    print(f"[ST] validation ok=True files={len(summary['generated'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
