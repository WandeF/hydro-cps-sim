"""Quality gate for bounded asynchronous metric writers.

Runtime writers persist one JSON snapshot when they close.  This module keeps
the validation rules shared by the coordinator (which fails a completed run)
and the offline performance analyzer (which marks summaries incomplete).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping


COUNTER_FIELDS = (
    "accepted",
    "processed",
    "written",
    "write_errors",
    "dropped_queue_full",
    "dropped_after_close",
    "dropped_disabled",
    "unflushed_on_close",
    "pending",
)

ANOMALY_COUNTER_FIELDS = (
    "write_errors",
    "dropped_queue_full",
    "dropped_after_close",
    "dropped_disabled",
    "unflushed_on_close",
    "pending",
)

_MITM_TYPES = {"mitm", "modbus_mitm"}


def required_metric_writers(config: Mapping[str, Any]) -> dict[str, int]:
    """Return writer counts that a successful run of ``config`` must close."""

    required: dict[str, int] = {}
    metrics = config.get("metrics", {}) or {}
    if not isinstance(metrics, Mapping) or not bool(metrics.get("enabled", False)):
        return required
    if bool(metrics.get("communication", True)):
        required["modbus"] = 1

    attacks = config.get("attacks", {}) or {}
    if isinstance(attacks, Mapping):
        scenarios = attacks.get("scenarios", []) if bool(attacks.get("enabled", False)) else []
    elif isinstance(attacks, list):
        scenarios = attacks
    else:
        scenarios = []
    mitm_count = 0
    for scenario in scenarios:
        if (
            not isinstance(scenario, Mapping)
            or not bool(scenario.get("enabled", True))
            or str(scenario.get("type", scenario.get("kind", ""))).strip().lower() not in _MITM_TYPES
        ):
            continue
        intercept = scenario.get("intercept", {}) or {}
        targets = (
            intercept.get("targets", scenario.get("targets", []))
            if isinstance(intercept, Mapping)
            else scenario.get("targets", [])
        )
        if isinstance(targets, (list, tuple)):
            mitm_count += len(targets)
        elif targets:
            mitm_count += 1
    if mitm_count:
        required["mitm"] = mitm_count
    return required


def _counter(data: Mapping[str, Any], field: str) -> int:
    value = data.get(field, 0)
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if number < 0 or str(value).strip() not in {str(number), f"{number}.0"}:
        raise ValueError(f"{field} must be a non-negative integer")
    return number


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def analyze_metric_writer_stats(
    stats_dir: Path | str | None,
    *,
    required_writers: Mapping[str, int] | Iterable[str] = (),
) -> dict[str, Any]:
    """Aggregate writer counters and return explicit data-quality failures."""

    path = None if stats_dir is None else Path(stats_dir)
    files = sorted(path.glob("*.json")) if path is not None and path.is_dir() else []
    totals = {field: 0 for field in COUNTER_FIELDS}
    writer_counts: dict[str, int] = {}
    errors: list[str] = []
    malformed_files: list[str] = []
    thread_alive_count = 0
    disabled_count = 0

    for file_path in files:
        try:
            with file_path.open(encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, Mapping):
                raise ValueError("root must be an object")
            writer = str(raw.get("writer", "")).strip().lower()
            if not writer:
                raise ValueError("writer is missing")
            counters = {field: _counter(raw, field) for field in COUNTER_FIELDS}
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            malformed_files.append(str(file_path))
            errors.append(f"{file_path.name}:malformed:{type(exc).__name__}:{exc}")
            continue

        writer_counts[writer] = writer_counts.get(writer, 0) + 1
        for field, value in counters.items():
            totals[field] += value
            if field in ANOMALY_COUNTER_FIELDS and value:
                errors.append(f"{file_path.name}:{field}={value}")

        thread_alive = _truthy(raw.get("thread_alive", False))
        if thread_alive:
            thread_alive_count += 1
            errors.append(f"{file_path.name}:thread_alive=true")
        if raw.get("enabled") is not None and not _truthy(raw.get("enabled")):
            disabled_count += 1
            errors.append(f"{file_path.name}:enabled=false")

        accepted = counters["accepted"]
        processed = counters["processed"]
        written = counters["written"]
        write_errors = counters["write_errors"]
        unflushed = counters["unflushed_on_close"]
        if processed != written + write_errors:
            errors.append(
                f"{file_path.name}:processed={processed} differs from "
                f"written+write_errors={written + write_errors}"
            )
        if accepted != processed + unflushed:
            errors.append(
                f"{file_path.name}:accepted={accepted} differs from "
                f"processed+unflushed={processed + unflushed}"
            )

    if isinstance(required_writers, Mapping):
        required = {
            str(writer).strip().lower(): max(0, int(count))
            for writer, count in required_writers.items()
            if str(writer).strip() and int(count) > 0
        }
    else:
        required = {}
        for writer in required_writers:
            name = str(writer).strip().lower()
            if name:
                required[name] = required.get(name, 0) + 1
    missing_writers = {
        writer: count - writer_counts.get(writer, 0)
        for writer, count in sorted(required.items())
        if writer_counts.get(writer, 0) < count
    }
    for writer, missing_count in missing_writers.items():
        errors.append(
            f"missing_required_writer:{writer}:expected={required[writer]}:"
            f"actual={writer_counts.get(writer, 0)}:missing={missing_count}"
        )

    dropped_total = sum(
        totals[field]
        for field in ("dropped_queue_full", "dropped_after_close", "dropped_disabled")
    )
    return {
        "stats_dir": None if path is None else str(path),
        "file_count": len(files),
        "valid_file_count": len(files) - len(malformed_files),
        "malformed_file_count": len(malformed_files),
        "malformed_files": malformed_files,
        "writer_counts": dict(sorted(writer_counts.items())),
        "required_writers": dict(sorted(required.items())),
        "missing_writers": missing_writers,
        **totals,
        "dropped_total": dropped_total,
        "thread_alive_count": thread_alive_count,
        "disabled_count": disabled_count,
        "quality_errors": errors,
        "quality_complete": not errors,
    }


__all__ = ["analyze_metric_writer_stats", "required_metric_writers"]
