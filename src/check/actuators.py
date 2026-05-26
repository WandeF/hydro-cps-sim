#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Post-run actuator execution checks.

The closed-loop runtime records what each PLC actually output.  This module
replays the generated ST control semantics at the configuration level and checks
whether the recorded PLC coils match the expected actuator state.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.config import RuntimeConfig, read_json, write_json
from src.io.csv import append_row, json_dir


def _bool_from_action(action: Any) -> bool:
    text = str(action).strip().lower()
    if text in {"open", "opened", "true", "on", "1", "start"}:
        return True
    if text in {"closed", "close", "false", "off", "0", "stop"}:
        return False
    raise ValueError(f"unsupported actuator action: {action!r}")


def _rule_matches(rule: dict[str, Any], physics_values: dict[str, Any]) -> tuple[bool, float | None, str]:
    dep = str(rule.get("dependant", ""))
    if not dep or dep not in physics_values:
        return False, None, f"missing dependant {dep}"
    try:
        actual = float(physics_values[dep])
        threshold = float(rule.get("value"))
    except Exception as exc:
        return False, None, f"bad threshold/value: {exc}"

    typ = str(rule.get("type", "")).strip().lower()
    if typ == "below":
        return actual < threshold, actual, ""
    if typ == "above":
        return actual > threshold, actual, ""
    if typ in {"equal", "equals", "eq"}:
        return actual == threshold, actual, ""
    if typ in {"below_or_equal", "le", "lte"}:
        return actual <= threshold, actual, ""
    if typ in {"above_or_equal", "ge", "gte"}:
        return actual >= threshold, actual, ""
    return False, actual, f"unsupported rule type {typ!r}"


def expected_actuator_state(
    rt: RuntimeConfig,
    physics_snapshot: dict[str, Any],
    previous_state: dict[str, bool],
) -> tuple[dict[str, bool], dict[str, dict[str, Any]]]:
    """Evaluate config hysteresis rules against one physics snapshot."""
    physics_values = physics_snapshot.get("values", physics_snapshot)
    if not isinstance(physics_values, dict):
        physics_values = {}

    expected = dict(previous_state)
    details: dict[str, dict[str, Any]] = {
        name: {
            "previous": value,
            "expected": value,
            "matched_rule": "retain",
            "dependant": "",
            "value": "",
            "threshold": "",
        }
        for name, value in expected.items()
    }
    missing_inputs = 0

    for plc in rt.raw.get("plcs", []) or []:
        if not isinstance(plc, dict):
            continue
        for rule in plc.get("controls", []) or []:
            if not isinstance(rule, dict):
                continue
            actuator = str(rule.get("actuator", ""))
            if not actuator:
                continue
            matched, source_value, reason = _rule_matches(rule, physics_values)
            if reason:
                if reason.startswith("missing dependant"):
                    missing_inputs += 1
                details.setdefault(actuator, {})["skipped_rule"] = reason
                continue
            if matched:
                expected[actuator] = _bool_from_action(rule.get("action"))
                details[actuator] = {
                    "previous": previous_state.get(actuator),
                    "expected": expected[actuator],
                    "matched_rule": f"{rule.get('dependant')} {rule.get('type')} {rule.get('value')} -> {rule.get('action')}",
                    "dependant": rule.get("dependant"),
                    "value": source_value,
                    "threshold": rule.get("value"),
                    "plc": plc.get("name"),
                }

    for item in details.values():
        item.setdefault("missing_input_count", missing_inputs)
    return expected, details


def load_actuator_history(
    runtime_dir: Path,
    first_control_iteration: int,
    final_physics_iteration: int,
) -> dict[int, dict[str, bool]]:
    """Load actuator_state_XXXX.json files from output/runtime/json."""
    history: dict[int, dict[str, bool]] = {}
    src_dir = json_dir(runtime_dir)
    for i in range(first_control_iteration, final_physics_iteration):
        path = src_dir / f"actuator_state_{i:04d}.json"
        if not path.exists():
            raise FileNotFoundError(f"missing actuator state file: {path}")
        raw = read_json(path)
        history[i] = {str(k): bool(v) for k, v in raw.items()}
    return history


def verify_actuator_history(
    rt: RuntimeConfig,
    runtime_dir: Path,
    check_dir: Path,
    *,
    first_control_iteration: int,
    final_physics_iteration: int,
) -> dict[str, Any]:
    """Verify all recorded PLC actuator outputs after a run."""
    src_dir = json_dir(runtime_dir)
    check_dir.mkdir(parents=True, exist_ok=True)

    previous_state = dict(rt.actuator_initial_state)
    mismatched_iterations: list[int] = []
    rows = 0
    actuator_names = sorted(rt.actuator_initial_state)

    for i in range(first_control_iteration, final_physics_iteration):
        physics_path = src_dir / f"physics_{i:04d}.json"
        actuator_path = src_dir / f"actuator_state_{i:04d}.json"
        if not physics_path.exists():
            raise FileNotFoundError(f"missing physics file: {physics_path}")
        if not actuator_path.exists():
            raise FileNotFoundError(f"missing actuator file: {actuator_path}")

        physics_snapshot = read_json(physics_path)
        actual_state = {str(k): bool(v) for k, v in read_json(actuator_path).items()}
        expected, details = expected_actuator_state(rt, physics_snapshot, previous_state)

        mismatches: dict[str, dict[str, Any]] = {}
        row: dict[str, Any] = {
            "iteration": i,
            "ok": True,
            "checked_count": len(expected),
            "mismatch_count": 0,
            "missing_input_count": max((int(d.get("missing_input_count", 0)) for d in details.values()), default=0),
            "physics_json": str(physics_path),
            "actuator_json": str(actuator_path),
        }

        for name in sorted(set(actuator_names) | set(expected) | set(actual_state)):
            exp = bool(expected.get(name, False))
            act = bool(actual_state.get(name, False))
            ok = exp == act
            row[f"expected.{name}"] = exp
            row[f"actual.{name}"] = act
            row[f"ok.{name}"] = ok
            row[f"rule.{name}"] = details.get(name, {}).get("matched_rule", "")
            if not ok:
                mismatches[name] = {"expected": exp, "actual": act, **details.get(name, {})}

        row["ok"] = not mismatches
        row["mismatch_count"] = len(mismatches)
        append_row(
            check_dir / "actuator_check.csv",
            row,
            fixed_columns=[
                "iteration",
                "ok",
                "checked_count",
                "mismatch_count",
                "missing_input_count",
                "physics_json",
                "actuator_json",
            ],
        )
        if mismatches:
            mismatched_iterations.append(i)
        previous_state = actual_state
        rows += 1

    summary = {
        "ok": not mismatched_iterations,
        "rows": rows,
        "first_control_iteration": first_control_iteration,
        "final_physics_iteration": final_physics_iteration,
        "mismatched_iterations": mismatched_iterations,
        "actuator_check_csv": str(check_dir / "actuator_check.csv"),
    }
    write_json(check_dir / "actuator_check_summary.json", summary)
    return summary
