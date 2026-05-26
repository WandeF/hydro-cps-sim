#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plot helpers for post-run checks."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from src.core.config import RuntimeConfig
from src.io.csv import append_row


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _bool_or_none(value: Any) -> int | None:
    text = str(value).strip().lower()
    if text in {"true", "1", "open", "opened", "on"}:
        return 1
    if text in {"false", "0", "closed", "close", "off"}:
        return 0
    return None


def tank_names(rt: RuntimeConfig, closed_rows: list[dict[str, str]]) -> list[str]:
    configured = [str(k) for k in (rt.raw.get("initial_tank_values") or {}).keys()]
    if configured:
        return configured
    if not closed_rows:
        return []
    columns = closed_rows[0].keys()
    return sorted(c for c in columns if c.upper().startswith("T"))


def _import_pyplot():  # type: ignore[no-untyped-def]
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def write_tank_curve_outputs(rt: RuntimeConfig, runtime_dir: Path, check_root: Path) -> dict[str, Any]:
    closed_rows = _read_csv(runtime_dir / "csv" / "physics.csv")
    open_rows = _read_csv(check_root / "open_loop_physics.csv")
    tanks = tank_names(rt, closed_rows)
    outputs: list[str] = []

    for tank in tanks:
        closed_by_iter = {int(float(r.get("iteration", 0))): _float_or_none(r.get(tank)) for r in closed_rows if r.get("iteration", "") != ""}
        open_by_iter = {int(float(r.get("iteration", 0))): _float_or_none(r.get(tank)) for r in open_rows if r.get("iteration", "") != ""}
        for iteration in sorted(set(closed_by_iter) | set(open_by_iter)):
            append_row(
                check_root / "tank_curves.csv",
                {
                    "iteration": iteration,
                    "tank": tank,
                    "closed_loop": closed_by_iter.get(iteration),
                    "open_loop": open_by_iter.get(iteration),
                    "abs_diff": None
                    if closed_by_iter.get(iteration) is None or open_by_iter.get(iteration) is None
                    else abs(float(closed_by_iter[iteration]) - float(open_by_iter[iteration])),
                },
                fixed_columns=["iteration", "tank", "closed_loop", "open_loop", "abs_diff"],
            )

    try:
        plt = _import_pyplot()
    except Exception as exc:
        return {"ok": False, "reason": f"matplotlib unavailable: {exc}", "tank_count": len(tanks), "outputs": outputs}

    for tank in tanks:
        closed_xy = [
            (int(float(r["iteration"])), _float_or_none(r.get(tank)))
            for r in closed_rows
            if r.get("iteration", "") != "" and _float_or_none(r.get(tank)) is not None
        ]
        open_xy = [
            (int(float(r["iteration"])), _float_or_none(r.get(tank)))
            for r in open_rows
            if r.get("iteration", "") != "" and _float_or_none(r.get(tank)) is not None
        ]
        if not closed_xy and not open_xy:
            continue
        fig = plt.figure(figsize=(10, 5))
        ax = fig.add_subplot(111)
        if closed_xy:
            ax.plot([x for x, _ in closed_xy], [y for _, y in closed_xy], label="closed-loop")
        if open_xy:
            ax.plot([x for x, _ in open_xy], [y for _, y in open_xy], linestyle="--", label="open-loop replay")
        ax.set_title(f"Tank {tank}: closed-loop vs open-loop")
        ax.set_xlabel("iteration")
        ax.set_ylabel("level")
        ax.grid(True, alpha=0.3)
        ax.legend()
        fig.tight_layout()
        out = check_root / f"tank_{tank}_closed_vs_open.png"
        fig.savefig(out, dpi=160)
        plt.close(fig)
        outputs.append(str(out))

    if tanks:
        fig = plt.figure(figsize=(11, 6))
        ax = fig.add_subplot(111)
        for tank in tanks:
            closed_xy = [
                (int(float(r["iteration"])), _float_or_none(r.get(tank)))
                for r in closed_rows
                if r.get("iteration", "") != "" and _float_or_none(r.get(tank)) is not None
            ]
            open_xy = [
                (int(float(r["iteration"])), _float_or_none(r.get(tank)))
                for r in open_rows
                if r.get("iteration", "") != "" and _float_or_none(r.get(tank)) is not None
            ]
            if closed_xy:
                ax.plot([x for x, _ in closed_xy], [y for _, y in closed_xy], label=f"{tank} closed")
            if open_xy:
                ax.plot([x for x, _ in open_xy], [y for _, y in open_xy], linestyle="--", label=f"{tank} open")
        ax.set_title("Tank curves: closed-loop vs open-loop replay")
        ax.set_xlabel("iteration")
        ax.set_ylabel("level")
        ax.grid(True, alpha=0.3)
        ax.legend(ncol=2, fontsize=8)
        fig.tight_layout()
        out = check_root / "tank_curves_closed_vs_open.png"
        fig.savefig(out, dpi=160)
        plt.close(fig)
        outputs.append(str(out))

    return {"ok": True, "tank_count": len(tanks), "outputs": outputs, "tank_curves_csv": str(check_root / "tank_curves.csv")}


def actuator_names_from_check(rows: list[dict[str, str]]) -> list[str]:
    names: set[str] = set()
    for row in rows[:1]:
        for col in row:
            if col.startswith("actual."):
                names.add(col.split(".", 1)[1])
            elif col.startswith("expected."):
                names.add(col.split(".", 1)[1])
    return sorted(names)


def write_actuator_outputs(check_root: Path) -> dict[str, Any]:
    rows = _read_csv(check_root / "actuator_check.csv")
    names = actuator_names_from_check(rows)
    outputs: list[str] = []

    try:
        plt = _import_pyplot()
    except Exception as exc:
        return {"ok": False, "reason": f"matplotlib unavailable: {exc}", "actuator_count": len(names), "outputs": outputs}

    for name in names:
        xy_actual: list[tuple[int, int]] = []
        xy_expected: list[tuple[int, int]] = []
        for row in rows:
            if row.get("iteration", "") == "":
                continue
            iteration = int(float(row["iteration"]))
            actual = _bool_or_none(row.get(f"actual.{name}"))
            expected = _bool_or_none(row.get(f"expected.{name}"))
            if actual is not None:
                xy_actual.append((iteration, actual))
            if expected is not None:
                xy_expected.append((iteration, expected))
        if not xy_actual and not xy_expected:
            continue
        fig = plt.figure(figsize=(10, 3.5))
        ax = fig.add_subplot(111)
        if xy_actual:
            ax.step([x for x, _ in xy_actual], [y for _, y in xy_actual], where="post", label="actual PLC output")
        if xy_expected:
            ax.step([x for x, _ in xy_expected], [y for _, y in xy_expected], where="post", linestyle="--", label="expected by config")
        ax.set_title(f"Actuator {name}: expected vs actual")
        ax.set_xlabel("iteration")
        ax.set_ylabel("state")
        ax.set_yticks([0, 1])
        ax.set_yticklabels(["closed/off", "open/on"])
        ax.grid(True, alpha=0.3)
        ax.legend()
        fig.tight_layout()
        out = check_root / f"actuator_{name}_expected_vs_actual.png"
        fig.savefig(out, dpi=160)
        plt.close(fig)
        outputs.append(str(out))

    if names:
        fig = plt.figure(figsize=(11, 6))
        ax = fig.add_subplot(111)
        offset = 0
        y_ticks: list[int] = []
        y_labels: list[str] = []
        for name in names:
            xy_actual = []
            for row in rows:
                if row.get("iteration", "") == "":
                    continue
                state = _bool_or_none(row.get(f"actual.{name}"))
                if state is not None:
                    xy_actual.append((int(float(row["iteration"])), state + offset))
            if xy_actual:
                ax.step([x for x, _ in xy_actual], [y for _, y in xy_actual], where="post", label=name)
                y_ticks.append(offset)
                y_ticks.append(offset + 1)
                y_labels.append(f"{name}=0")
                y_labels.append(f"{name}=1")
                offset += 2
        ax.set_title("Recorded PLC actuator states")
        ax.set_xlabel("iteration")
        ax.set_ylabel("actuator state")
        ax.set_yticks(y_ticks)
        ax.set_yticklabels(y_labels, fontsize=7)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        out = check_root / "actuator_states_actual.png"
        fig.savefig(out, dpi=160)
        plt.close(fig)
        outputs.append(str(out))

    return {"ok": True, "actuator_count": len(names), "outputs": outputs}
