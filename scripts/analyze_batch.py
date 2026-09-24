#!/usr/bin/env python3
"""Collect per-run metric summaries into one CSV, without pandas."""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import fmean, stdev
from typing import Any, Iterable, Mapping, Sequence


SUMMARY_FILENAMES = {
    "correctness_summary.json",
    "network-aggregate.json",
    "performance_summary.json",
    "propagation_summary.json",
}


def discover_summaries(inputs: Sequence[Path]) -> list[Path]:
    found: set[Path] = set()
    for raw in inputs:
        path = raw.expanduser().resolve()
        if path.is_file():
            found.add(path)
            continue
        if not path.is_dir():
            raise FileNotFoundError(path)
        for filename in SUMMARY_FILENAMES:
            found.update(candidate.resolve() for candidate in path.rglob(filename))
    canonical: list[Path] = []
    for path in sorted(found):
        # ``export_results.py`` mirrors the runtime network aggregate under
        # reports/network.  When a result root is scanned, count the canonical
        # runtime copy only; keep a report-only file when no runtime copy exists.
        if (
            path.name == "network-aggregate.json"
            and path.parent.name == "network"
            and path.parent.parent.name == "reports"
        ):
            runtime_copy = path.parents[2] / "runtime" / "network" / path.name
            if runtime_copy.resolve() in found:
                continue
        canonical.append(path)
    return canonical


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def flatten_summary(
    data: Mapping[str, Any],
    *,
    prefix: str = "",
    output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Flatten scalar leaves while omitting verbose per-variable/per-actuator lists."""

    if output is None:
        output = {}
    for key, value in data.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            flatten_summary(value, prefix=name, output=output)
        elif _scalar(value):
            output[name] = value
    return output


def load_summary_row(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"summary root must be an object: {path}")
    row = flatten_summary(data)
    row["summary_path"] = str(path)
    row.setdefault("metric_type", data.get("metric_type", path.stem))
    return row


def select_complete_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    include_incomplete: bool = False,
) -> list[dict[str, Any]]:
    if include_incomplete:
        return [dict(row) for row in rows]
    return [
        dict(row)
        for row in rows
        if str(row.get("run_status", "")).strip().lower() in {"", "success"}
        and row.get("complete") is not False
        and row.get("quality_complete") is not False
    ]


def write_rows(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    preferred = ["metric_type", "scenario", "summary_path"]
    columns: list[str] = [column for column in preferred if any(column in row for row in rows)]
    columns.extend(
        sorted({key for row in rows for key in row if key not in columns})
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: "" if row.get(column) is None else row.get(column) for column in columns})


def _numeric(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def aggregate_rows(
    rows: Sequence[Mapping[str, Any]],
    group_by: Sequence[str],
) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for row in rows:
        key = tuple(row.get(column) for column in group_by)
        groups.setdefault(key, []).append(row)

    results: list[dict[str, Any]] = []
    for key, members in sorted(groups.items(), key=lambda item: tuple(str(value) for value in item[0])):
        result: dict[str, Any] = {column: value for column, value in zip(group_by, key)}
        result["run_count"] = len(members)
        numeric_columns = sorted({column for row in members for column in row if column not in group_by})
        for column in numeric_columns:
            values = [number for row in members if (number := _numeric(row.get(column))) is not None]
            if not values:
                continue
            result[f"{column}.count"] = len(values)
            result[f"{column}.mean"] = fmean(values)
            result[f"{column}.std"] = stdev(values) if len(values) >= 2 else None
        results.append(result)
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="summary JSON files or directories searched recursively")
    parser.add_argument("--output", type=Path, default=Path("all_summary_metrics.csv"))
    parser.add_argument(
        "--aggregate-by",
        nargs="+",
        default=None,
        help="flattened keys used to group runs, e.g. metric_type scenario",
    )
    parser.add_argument("--aggregate-output", type=Path, default=None)
    parser.add_argument(
        "--include-incomplete",
        action="store_true",
        help="include summaries explicitly marked as failed/incomplete",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    summaries = discover_summaries(args.inputs)
    if not summaries:
        raise FileNotFoundError(
            "no correctness_summary.json, network-aggregate.json, "
            "performance_summary.json, or propagation_summary.json found"
        )
    rows = select_complete_rows(
        [load_summary_row(path) for path in summaries],
        include_incomplete=args.include_incomplete,
    )
    if not rows:
        raise RuntimeError("all discovered summaries are marked incomplete")
    output = args.output.expanduser().resolve()
    write_rows(output, rows)
    print(f"[BATCH] runs={len(rows)} -> {output}")

    if args.aggregate_by:
        aggregate = aggregate_rows(rows, args.aggregate_by)
        aggregate_output = (
            args.aggregate_output.expanduser().resolve()
            if args.aggregate_output
            else output.with_name("aggregate_summary_metrics.csv")
        )
        write_rows(aggregate_output, aggregate)
        print(f"[BATCH] groups={len(aggregate)} -> {aggregate_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
