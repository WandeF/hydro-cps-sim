"""Thread- and process-safe event logging for quantitative experiments."""

from __future__ import annotations

import csv
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, time as datetime_time
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, TextIO

try:  # Linux/Unix: coordinate independent runtime processes.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts.
    fcntl = None  # type: ignore[assignment]


@dataclass
class MetricEvent:
    """One event on the unified cyber/control/physical timeline.

    Field order is part of the on-disk CSV contract.  ``value`` and ``details``
    may contain structured Python values; :class:`EventLogger` converts them to
    deterministic, JSON-safe strings before writing.
    """

    wall_time_ns: int
    monotonic_ns: int
    iteration: int
    layer: str
    component: str
    event_type: str
    source: str = ""
    target: str = ""
    variable: str = ""
    value: Any = ""
    status: str = ""
    request_id: str = ""
    attack_id: str = ""
    details: Any = ""


EVENT_FIELDS = tuple(field.name for field in fields(MetricEvent))


def make_event(
    *,
    wall_time_ns: int | None = None,
    monotonic_ns: int | None = None,
    **kwargs: Any,
) -> MetricEvent:
    """Create an event, while allowing deterministic timestamps in tests/replay."""

    return MetricEvent(
        wall_time_ns=time.time_ns() if wall_time_ns is None else int(wall_time_ns),
        monotonic_ns=time.monotonic_ns() if monotonic_ns is None else int(monotonic_ns),
        **kwargs,
    )


_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.RLock:
    """Return the process-local lock shared by all writers for ``path``."""

    key = os.path.abspath(os.fspath(path))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


@contextmanager
def _exclusive_file_lock(file_obj: TextIO) -> Iterator[None]:
    """Use an advisory cross-process lock where the host supports ``fcntl``.

    The fallback still benefits from the shared in-process lock.  It cannot
    promise cross-process exclusion on platforms without ``fcntl``, but keeps
    the module usable there without adding a non-standard dependency.
    """

    if fcntl is None:  # pragma: no cover - depends on host platform.
        yield
        return
    fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)


def _json_compatible(value: Any, seen: set[int] | None = None) -> Any:
    """Convert arbitrary values into a finite, cycle-safe JSON value."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Enum):
        return _json_compatible(value.value, seen)
    if isinstance(value, (datetime, date, datetime_time)):
        return value.isoformat()
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}

    if seen is None:
        seen = set()
    object_id = id(value)
    if object_id in seen:
        return "<circular-reference>"

    if is_dataclass(value) and not isinstance(value, type):
        seen.add(object_id)
        try:
            return {
                field.name: _json_compatible(getattr(value, field.name), seen)
                for field in fields(value)
            }
        finally:
            seen.remove(object_id)

    if isinstance(value, Mapping):
        seen.add(object_id)
        try:
            return {
                str(key): _json_compatible(item, seen)
                for key, item in value.items()
            }
        finally:
            seen.remove(object_id)

    if isinstance(value, (list, tuple)):
        seen.add(object_id)
        try:
            return [_json_compatible(item, seen) for item in value]
        finally:
            seen.remove(object_id)

    if isinstance(value, (set, frozenset)):
        seen.add(object_id)
        try:
            ordered = sorted(value, key=lambda item: repr(item))
            return [_json_compatible(item, seen) for item in ordered]
        finally:
            seen.remove(object_id)

    try:
        return str(value)
    except Exception:
        return f"<{type(value).__name__}>"


def _safe_json(value: Any) -> str:
    """Serialize without allowing one unusual payload to break event logging."""

    import json

    try:
        return json.dumps(
            _json_compatible(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except Exception as exc:  # Last-resort guard for hostile ``__str__`` values.
        fallback = {
            "serialization_error": type(exc).__name__,
            "value_type": type(value).__name__,
        }
        return json.dumps(fallback, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_cell(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return _safe_json(value)


def _event_row(event: MetricEvent) -> dict[str, Any]:
    if not isinstance(event, MetricEvent):
        raise TypeError(f"event must be MetricEvent, got {type(event).__name__}")
    row = {name: getattr(event, name) for name in EVENT_FIELDS}
    row["value"] = _event_cell(row["value"])
    row["details"] = _event_cell(row["details"])
    return row


def _append_csv_rows(path: Path, fieldnames: Iterable[str], rows: Iterable[Mapping[str, Any]]) -> int:
    """Append fixed-schema rows under one thread/process critical section."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = tuple(fieldnames)
    materialized = list(rows)
    lock = _path_lock(path)
    with lock:
        with path.open("a+", encoding="utf-8", newline="") as file_obj:
            with _exclusive_file_lock(file_obj):
                file_obj.seek(0, os.SEEK_END)
                is_empty = file_obj.tell() == 0
                writer = csv.DictWriter(file_obj, fieldnames=columns, extrasaction="ignore")
                if is_empty:
                    writer.writeheader()
                for row in materialized:
                    writer.writerow({column: row.get(column, "") for column in columns})
                file_obj.flush()
    return len(materialized)


class EventLogger:
    """Append :class:`MetricEvent` records to a fixed-schema ``events.csv``."""

    fieldnames = EVENT_FIELDS

    def __init__(self, output_file: Path | str):
        self.output_file = Path(output_file)
        self.output_file.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: MetricEvent) -> None:
        _append_csv_rows(self.output_file, self.fieldnames, [_event_row(event)])

    def log_many(self, events: Iterable[MetricEvent]) -> int:
        rows = [_event_row(event) for event in events]
        if not rows:
            return 0
        return _append_csv_rows(self.output_file, self.fieldnames, rows)


def safe_log(logger: EventLogger | None, event: MetricEvent) -> bool:
    """Best-effort business-path logging.

    Metric collection must never change the simulated control or attack
    behavior.  Callers on runtime paths use this helper; offline utilities may
    still call :meth:`EventLogger.log` directly when write failures should be
    visible to the operator.
    """
    if logger is None:
        return False
    try:
        logger.log(event)
        return True
    except Exception:
        return False


def safe_log_many(logger: EventLogger | None, events: Iterable[MetricEvent]) -> int:
    if logger is None:
        return 0
    try:
        return logger.log_many(events)
    except Exception:
        return 0


_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _request_token(value: Any) -> str:
    token = _TOKEN_RE.sub("-", str(value).strip().lower()).strip("-")
    return token or "unknown"


class RequestIdFactory:
    """Generate deterministic-format, process-local request IDs safely by thread."""

    def __init__(self, *, start: int = 1, sequence_width: int = 6):
        if start < 0:
            raise ValueError("start must be non-negative")
        if sequence_width < 1:
            raise ValueError("sequence_width must be positive")
        self._next_sequence = int(start)
        self.sequence_width = int(sequence_width)
        self._lock = threading.Lock()

    def next_id(self, iteration: int, target: str, operation: str) -> str:
        with self._lock:
            sequence = self._next_sequence
            self._next_sequence += 1
        return (
            f"iter-{int(iteration):05d}-"
            f"{_request_token(target)}-"
            f"{_request_token(operation)}-"
            f"{sequence:0{self.sequence_width}d}"
        )

    def make(self, iteration: int, target: str, operation: str) -> str:
        """Alias kept for call sites that prefer an action-oriented name."""

        return self.next_id(iteration, target, operation)

    def __call__(self, iteration: int, target: str, operation: str) -> str:
        return self.next_id(iteration, target, operation)
