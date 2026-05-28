#!/usr/bin/env python3
"""Compare C-Town MITM attack results against a baseline physics CSV."""
from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path
from statistics import mean
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config import load_yaml


DEFAULT_COLUMNS = ["PU10", "PU11", "T7"]
DEFAULT_SCADA_COLUMNS = ["PLC9.PLC9_T7"]


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _scenario_list(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    attacks = cfg.get("attacks", {}) or {}
    if isinstance(attacks, dict) and not bool(attacks.get("enabled", False)):
        return []
    if isinstance(attacks, list):
        return [x for x in attacks if isinstance(x, dict) and bool(x.get("enabled", True))]
    scenarios = attacks.get("scenarios", []) if isinstance(attacks, dict) else []
    return [x for x in scenarios if isinstance(x, dict) and bool(x.get("enabled", True))]


def _rule_physics_variable(variable: str) -> str:
    parts = str(variable).split("_")
    return parts[-1] if parts else str(variable)


def _infer_columns_from_config(config_path: Path) -> tuple[list[str], list[str], int | None, int | None]:
    cfg = load_yaml(config_path)
    scenarios = _scenario_list(cfg)
    attacked_physics: list[str] = []
    scada_columns: list[str] = []
    starts: list[int] = []
    ends: list[int] = []

    for scenario in scenarios:
        trigger = scenario.get("trigger", {}) or {}
        if isinstance(trigger, dict):
            if trigger.get("start_iteration") is not None:
                starts.append(int(trigger["start_iteration"]))
            if trigger.get("end_iteration") is not None:
                ends.append(int(trigger["end_iteration"]))
        for rule in scenario.get("rules", []) or []:
            if not isinstance(rule, dict):
                continue
            target = str(rule.get("target", "")).strip().upper()
            variable = str(rule.get("variable", "")).strip()
            if not variable:
                continue
            physical = _rule_physics_variable(variable)
            attacked_physics.append(physical)
            owner, _, _name = variable.partition("_")
            if target and owner == target:
                scada_columns.append(f"{target}.{variable}")

    actuator_columns: list[str] = []
    attacked_set = set(attacked_physics)
    for plc in cfg.get("plcs", []) or []:
        if not isinstance(plc, dict):
            continue
        for control in plc.get("controls", []) or []:
            if not isinstance(control, dict):
                continue
            dependant = str(control.get("dependant", "")).strip()
            actuator = str(control.get("actuator", "")).strip()
            if dependant in attacked_set and actuator:
                actuator_columns.append(actuator)

    physics_columns = _dedupe(actuator_columns + attacked_physics)
    return physics_columns, _dedupe(scada_columns), (min(starts) if starts else None), (max(ends) if ends else None)


def _resolve_physics_csv(path: Path) -> Path:
    candidates = [
        path,
        path / "physics.csv",
        path / "csv" / "physics.csv",
        path / "reports" / "csv" / "physics.csv",
        path / "runtime" / "csv" / "physics.csv",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"physics.csv not found from {path}")


def _resolve_scada_csv(path: Path) -> Path:
    candidates = [
        path,
        path / "scada_observed_key.csv",
        path / "scada_observed_wide.csv",
        path / "scada.csv",
        path / "csv" / "scada_observed_key.csv",
        path / "csv" / "scada_observed_wide.csv",
        path / "csv" / "scada.csv",
        path / "reports" / "csv" / "scada_observed_key.csv",
        path / "reports" / "csv" / "scada_observed_wide.csv",
        path / "runtime" / "csv" / "scada_observed_key.csv",
        path / "runtime" / "csv" / "scada_observed_wide.csv",
        path / "runtime" / "csv" / "scada.csv",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"SCADA CSV not found from {path}")


def _read_rows(path: Path) -> dict[int, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows: dict[int, dict[str, str]] = {}
        for row in reader:
            raw = row.get("iteration", "")
            if raw == "":
                continue
            rows[int(float(raw))] = row
        return rows


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result):
        return None
    return result


def _window_name(iteration: int, start: int, end: int) -> str:
    if iteration < start:
        return "pre"
    if iteration <= end:
        return "attack"
    return "post"


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.6f}"


def build_detail_rows(
    baseline: dict[int, dict[str, str]],
    attack: dict[int, dict[str, str]],
    *,
    columns: list[str],
    start: int,
    end: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for iteration in sorted(set(baseline) & set(attack)):
        row: dict[str, Any] = {
            "iteration": iteration,
            "window": _window_name(iteration, start, end),
        }
        for col in columns:
            b = _num(baseline[iteration].get(col))
            a = _num(attack[iteration].get(col))
            diff = None if b is None or a is None else a - b
            row[f"baseline_{col}"] = _fmt(b)
            row[f"attack_{col}"] = _fmt(a)
            row[f"diff_{col}"] = _fmt(diff)
        rows.append(row)
    return rows


def build_summary_rows(detail_rows: list[dict[str, Any]], columns: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for window in ("pre", "attack", "post"):
        bucket = [r for r in detail_rows if r["window"] == window]
        for col in columns:
            diffs = [_num(r.get(f"diff_{col}")) for r in bucket]
            diffs = [x for x in diffs if x is not None]
            abs_diffs = [abs(x) for x in diffs]
            rows.append({
                "window": window,
                "column": col,
                "count": len(diffs),
                "mean_diff": _fmt(mean(diffs) if diffs else None),
                "mean_abs_diff": _fmt(mean(abs_diffs) if abs_diffs else None),
                "max_abs_diff": _fmt(max(abs_diffs) if abs_diffs else None),
                "min_diff": _fmt(min(diffs) if diffs else None),
                "max_diff": _fmt(max(diffs) if diffs else None),
            })
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _scada_value(row: dict[str, str], logical_column: str) -> float | None:
    if logical_column in row:
        return _num(row.get(logical_column))
    plc, _, variable = logical_column.partition(".")
    candidates = [
        f"poll.{plc}.md.{variable}",
        f"downlink.{plc}.write.{variable}.value",
    ]
    for candidate in candidates:
        value = _num(row.get(candidate))
        if value is not None:
            return value
    return None


def _series(rows: dict[int, dict[str, str]], column: str, *, scada: bool = False) -> tuple[list[int], list[float]]:
    xs: list[int] = []
    ys: list[float] = []
    for iteration in sorted(rows):
        value = _scada_value(rows[iteration], column) if scada else _num(rows[iteration].get(column))
        if value is None:
            continue
        xs.append(iteration)
        ys.append(value)
    return xs, ys


def _plot_comparison(
    baseline: dict[int, dict[str, str]],
    attack: dict[int, dict[str, str]],
    columns: list[str],
    output_path: Path,
    *,
    title: str,
    scada: bool = False,
) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        print(f"[PLOT][INFO] matplotlib unavailable for PNG, will try SVG fallback: {exc}")
        return False

    if not columns:
        return False
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(len(columns), 1, figsize=(11, max(3, 2.6 * len(columns))), sharex=True)
    if len(columns) == 1:
        axes = [axes]
    for ax, column in zip(axes, columns):
        bx, by = _series(baseline, column, scada=scada)
        ax.plot(bx, by, label="baseline", linewidth=1.8)
        axx, ay = _series(attack, column, scada=scada)
        ax.plot(axx, ay, label="run", linewidth=1.8)
        ax.set_ylabel(column)
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best")
    axes[-1].set_xlabel("iteration")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)
    return True


def _plot_comparison_svg(
    baseline: dict[int, dict[str, str]],
    attack: dict[int, dict[str, str]],
    columns: list[str],
    output_path: Path,
    *,
    title: str,
    scada: bool = False,
) -> bool:
    width = 1100
    panel_h = 230
    margin_l = 90
    margin_r = 30
    margin_t = 55
    margin_b = 45
    height = margin_t + margin_b + panel_h * max(1, len(columns))

    all_iterations = sorted(set(baseline) | set(attack))
    if not all_iterations:
        return False
    x_min, x_max = min(all_iterations), max(all_iterations)
    if x_min == x_max:
        x_max += 1

    def sx(iteration: int) -> float:
        return margin_l + (iteration - x_min) / (x_max - x_min) * (width - margin_l - margin_r)

    def esc(text: Any) -> str:
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<style>text{font-family:Arial,sans-serif;font-size:12px}.title{font-size:18px;font-weight:bold}.label{font-size:13px;font-weight:bold}.axis{stroke:#444;stroke-width:1}.grid{stroke:#ddd;stroke-width:1}.baseline{fill:none;stroke:#1f77b4;stroke-width:2}.run{fill:none;stroke:#d62728;stroke-width:2}</style>',
        f'<text class="title" x="{width / 2:.1f}" y="28" text-anchor="middle">{esc(title)}</text>',
    ]

    for idx, column in enumerate(columns):
        y0 = margin_t + idx * panel_h
        plot_top = y0 + 20
        plot_bottom = y0 + panel_h - 35
        plot_h = plot_bottom - plot_top
        b_x, b_y = _series(baseline, column, scada=scada)
        a_x, a_y = _series(attack, column, scada=scada)
        values = b_y + a_y
        if not values:
            continue
        y_min, y_max = min(values), max(values)
        if y_min == y_max:
            y_min -= 1.0
            y_max += 1.0
        pad = (y_max - y_min) * 0.08
        y_min -= pad
        y_max += pad

        def sy(value: float) -> float:
            return plot_bottom - (value - y_min) / (y_max - y_min) * plot_h

        parts.append(f'<text class="label" x="12" y="{plot_top + 15:.1f}">{esc(column)}</text>')
        for frac in (0.0, 0.5, 1.0):
            gy = plot_bottom - frac * plot_h
            val = y_min + frac * (y_max - y_min)
            parts.append(f'<line class="grid" x1="{margin_l}" y1="{gy:.1f}" x2="{width - margin_r}" y2="{gy:.1f}"/>')
            parts.append(f'<text x="{margin_l - 8}" y="{gy + 4:.1f}" text-anchor="end">{val:.3f}</text>')
        parts.append(f'<line class="axis" x1="{margin_l}" y1="{plot_bottom}" x2="{width - margin_r}" y2="{plot_bottom}"/>')
        parts.append(f'<line class="axis" x1="{margin_l}" y1="{plot_top}" x2="{margin_l}" y2="{plot_bottom}"/>')

        def polyline(xs: list[int], ys: list[float], cls: str) -> None:
            if not xs:
                return
            pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(xs, ys))
            parts.append(f'<polyline class="{cls}" points="{pts}"/>')

        polyline(b_x, b_y, "baseline")
        polyline(a_x, a_y, "run")
        parts.append(f'<text x="{width - margin_r - 150}" y="{plot_top + 16:.1f}" fill="#1f77b4">baseline</text>')
        parts.append(f'<text x="{width - margin_r - 70}" y="{plot_top + 16:.1f}" fill="#d62728">run</text>')
        parts.append(f'<text x="{margin_l}" y="{plot_bottom + 25:.1f}">{x_min}</text>')
        parts.append(f'<text x="{width - margin_r}" y="{plot_bottom + 25:.1f}" text-anchor="end">{x_max}</text>')

    parts.append("</svg>")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return True


def _default_out_dir(attack_path: Path) -> Path:
    parts = attack_path.parts
    if "reports" in parts:
        reports_idx = parts.index("reports")
        return Path(*parts[:reports_idx + 1]) / "compare"
    if "output" in parts:
        output_idx = parts.index("output")
        return Path(*parts[:output_idx + 1]) / "reports" / "compare"

    preferred = attack_path.parent
    try:
        probe = preferred / ".compare_attack_results.write_test"
        with probe.open("w", encoding="utf-8"):
            pass
        probe.unlink()
        return preferred
    except OSError:
        pass

    if "runtime" in parts:
        runtime_idx = parts.index("runtime")
        output_dir = Path(*parts[:runtime_idx])
        return output_dir / "reports" / "compare"
    return Path("attack_compare").resolve()


def _print_window(detail_rows: list[dict[str, Any]], columns: list[str], start: int, end: int) -> None:
    window_rows = [r for r in detail_rows if start <= int(r["iteration"]) <= end]
    headers = ["iteration"]
    for col in columns:
        headers.extend([f"baseline_{col}", f"attack_{col}", f"diff_{col}"])
    print(f"\n[WINDOW] iterations {start}..{end}")
    print(",".join(headers))
    for row in window_rows:
        print(",".join(str(row.get(h, "")) for h in headers))


def _print_summary(summary_rows: list[dict[str, Any]]) -> None:
    headers = ["window", "column", "count", "mean_diff", "mean_abs_diff", "max_abs_diff", "min_diff", "max_diff"]
    print("\n[SUMMARY]")
    print(",".join(headers))
    for row in summary_rows:
        print(",".join(str(row.get(h, "")) for h in headers))


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare baseline and attack physics.csv files")
    parser.add_argument("--config", type=Path, default=None, help="experiment config; used to infer attack window and comparison columns")
    parser.add_argument("--baseline", type=Path, default=Path("examples/c_town/baseline"), help="baseline physics.csv, csv dir, or baseline dir")
    parser.add_argument("--attack", type=Path, default=Path("examples/c_town/output"), help="attack physics.csv, reports csv dir, runtime dir, or output dir")
    parser.add_argument("--start", type=int, default=None, help="attack window start iteration; defaults from --config, then 20")
    parser.add_argument("--end", type=int, default=None, help="attack window end iteration; defaults from --config, then 40")
    parser.add_argument("--columns", nargs="+", default=None, help="physics columns to compare; defaults from --config")
    parser.add_argument("--scada-columns", nargs="+", default=None, help="SCADA observed columns to plot, using plc.variable names; defaults from --config")
    parser.add_argument("--out-dir", type=Path, default=None, help="output CSV directory; defaults beside attack physics.csv")
    parser.add_argument("--no-plots", action="store_true", help="Do not generate line plots")
    args = parser.parse_args()

    inferred_columns: list[str] = []
    inferred_scada_columns: list[str] = []
    inferred_start: int | None = None
    inferred_end: int | None = None
    if args.config is not None:
        inferred_columns, inferred_scada_columns, inferred_start, inferred_end = _infer_columns_from_config(args.config.resolve())

    columns = args.columns or inferred_columns or DEFAULT_COLUMNS
    if args.scada_columns is not None:
        scada_columns = args.scada_columns
    elif args.config is not None:
        scada_columns = inferred_scada_columns
    else:
        scada_columns = DEFAULT_SCADA_COLUMNS
    start = args.start if args.start is not None else (inferred_start if inferred_start is not None else 20)
    end = args.end if args.end is not None else (inferred_end if inferred_end is not None else 40)

    baseline_path = _resolve_physics_csv(args.baseline)
    attack_path = _resolve_physics_csv(args.attack)
    baseline_scada_path: Path | None = None
    attack_scada_path: Path | None = None
    if scada_columns:
        baseline_scada_path = _resolve_scada_csv(args.baseline)
        attack_scada_path = _resolve_scada_csv(args.attack)
    out_dir = args.out_dir or _default_out_dir(attack_path)

    baseline = _read_rows(baseline_path)
    attack = _read_rows(attack_path)
    missing_columns = [c for c in columns if c not in next(iter(baseline.values()), {}) or c not in next(iter(attack.values()), {})]
    if missing_columns:
        raise ValueError(f"missing columns: {', '.join(missing_columns)}")

    detail_rows = build_detail_rows(baseline, attack, columns=columns, start=start, end=end)
    summary_rows = build_summary_rows(detail_rows, columns)

    detail_cols = ["iteration", "window"]
    for col in columns:
        detail_cols.extend([f"baseline_{col}", f"attack_{col}", f"diff_{col}"])
    summary_cols = ["window", "column", "count", "mean_diff", "mean_abs_diff", "max_abs_diff", "min_diff", "max_diff"]

    window_path = out_dir / "attack_vs_baseline_window.csv"
    summary_path = out_dir / "attack_vs_baseline_summary.csv"
    key_path = out_dir / "attack_vs_baseline_key.csv"
    window_rows = [r for r in detail_rows if start <= int(r["iteration"]) <= end]
    _write_csv(window_path, window_rows, detail_cols)
    _write_csv(summary_path, summary_rows, summary_cols)
    _write_csv(key_path, detail_rows, detail_cols)

    plot_paths: list[Path] = []
    if not args.no_plots:
        physics_plot = out_dir / "physics_baseline_vs_run.png"
        if _plot_comparison(baseline, attack, columns, physics_plot, title="Physics: baseline vs run"):
            plot_paths.append(physics_plot)
        else:
            physics_svg = out_dir / "physics_baseline_vs_run.svg"
            if _plot_comparison_svg(baseline, attack, columns, physics_svg, title="Physics: baseline vs run"):
                plot_paths.append(physics_svg)
        if scada_columns:
            assert baseline_scada_path is not None
            assert attack_scada_path is not None
            baseline_scada = _read_rows(baseline_scada_path)
            attack_scada = _read_rows(attack_scada_path)
            scada_plot = out_dir / "scada_baseline_vs_run.png"
            if _plot_comparison(baseline_scada, attack_scada, scada_columns, scada_plot, title="SCADA observed: baseline vs run", scada=True):
                plot_paths.append(scada_plot)
            else:
                scada_svg = out_dir / "scada_baseline_vs_run.svg"
                if _plot_comparison_svg(baseline_scada, attack_scada, scada_columns, scada_svg, title="SCADA observed: baseline vs run", scada=True):
                    plot_paths.append(scada_svg)
        else:
            print("[PLOT]     skip SCADA plot: no local SCADA columns inferred")

    if args.config is not None:
        print(f"[CONFIG]   {args.config.resolve()}")
    print(f"[COLUMNS]  physics={','.join(columns)} scada={','.join(scada_columns)} window={start}..{end}")
    print(f"[BASELINE] {baseline_path}")
    print(f"[ATTACK]   {attack_path}")
    if baseline_scada_path is not None:
        print(f"[BASELINE-SCADA] {baseline_scada_path}")
    if attack_scada_path is not None:
        print(f"[ATTACK-SCADA]   {attack_scada_path}")
    print(f"[WINDOW]   {window_path}")
    print(f"[SUMMARY]  {summary_path}")
    print(f"[KEY]      {key_path}")
    for path in plot_paths:
        print(f"[PLOT]     {path}")
    _print_window(detail_rows, columns, start, end)
    _print_summary(summary_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
