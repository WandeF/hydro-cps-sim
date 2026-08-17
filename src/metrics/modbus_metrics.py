"""Application-layer Modbus request metrics and unified timeline events."""
from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

from src.metrics.event_logger import (
    EventLogger,
    MetricEvent,
    RequestIdFactory,
    _append_csv_rows,
    safe_log_many,
)
from src.sync.filesystem import atomic_write_json


COMMUNICATION_FIELDS = (
    "request_id",
    "iteration",
    "phase",
    "warmup",
    "operation",
    "source",
    "target",
    "host",
    "port",
    "unit_id",
    "address",
    "count",
    "transaction_id",
    "function_code",
    "wall_start_ns",
    "wall_end_ns",
    "monotonic_start_ns",
    "monotonic_end_ns",
    "latency_ms",
    "status",
    "error_type",
    "error",
)

_TIMEOUT_MARKERS = (
    "timed out",
    "timeout",
    "no response",
    "cannot connect",
    "connection refused",
    "connection reset",
    "broken pipe",
)


def _request_kind(operation: str) -> str:
    normalized = str(operation).lower()
    if normalized == "connect":
        return "connection"
    return "read" if normalized.startswith("read") else "write"


def _status(payload: dict[str, Any], *, warmup: bool = False) -> str:
    if payload.get("status") == "success":
        return "success"
    message = str(payload.get("error", "")).lower()
    if any(marker in message for marker in _TIMEOUT_MARKERS):
        return "warmup_timeout" if warmup else "timeout"
    return "error"


class ModbusMetricsRecorder:
    """Convert low-level :class:`ModbusEndpoint` observations into metrics."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        event_logger: EventLogger | None = None,
        emit_events: bool = True,
        async_mode: bool = False,
        queue_capacity: int = 4096,
        source: str = "scada",
        request_ids: RequestIdFactory | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.communication_path = self.runtime_dir / "csv" / "communication.csv"
        self.event_logger = (
            event_logger
            if event_logger is not None
            else (EventLogger(self.runtime_dir / "csv" / "events.csv") if emit_events else None)
        )
        self.source = str(source)
        self.request_ids = request_ids or RequestIdFactory()
        self.async_mode = bool(async_mode)
        if int(queue_capacity) <= 0:
            raise ValueError("queue_capacity must be positive")
        self.queue_capacity = int(queue_capacity)
        self._queue: queue.Queue[tuple[int, str, str, bool, dict[str, Any]]] = queue.Queue(
            maxsize=self.queue_capacity
        )
        self._stop = threading.Event()
        self._queue_full_notice = threading.Event()
        self._state_lock = threading.Lock()
        self._accepting = self.async_mode
        self._closed = False
        self._stats = {
            "accepted": 0,
            "processed": 0,
            "written": 0,
            "write_errors": 0,
            "dropped_queue_full": 0,
            "dropped_after_close": 0,
            "unflushed_on_close": 0,
        }
        self._warned = False
        self._thread: threading.Thread | None = None
        if self.async_mode:
            self._thread = threading.Thread(
                target=self._run,
                name="modbus-metrics-writer",
                daemon=True,
            )
            self._thread.start()

    def observer(
        self,
        *,
        iteration: int,
        target: str,
        phase: str,
        warmup: bool = False,
    ) -> Callable[[dict[str, Any]], None]:
        def record(payload: dict[str, Any]) -> None:
            if self.async_mode:
                self._enqueue((iteration, target, phase, warmup, dict(payload)))
            else:
                self.record(
                    iteration=iteration,
                    target=target,
                    phase=phase,
                    warmup=warmup,
                    payload=payload,
                )

        return record

    def _enqueue(self, item: tuple[int, str, str, bool, dict[str, Any]]) -> bool:
        """Enqueue without ever blocking the observed Modbus request path."""

        with self._state_lock:
            if not self._accepting:
                self._stats["dropped_after_close"] += 1
                return False
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._stats["dropped_queue_full"] += 1
                self._queue_full_notice.set()
                return False
            self._stats["accepted"] += 1
            return True

    def _run(self) -> None:
        while True:
            if self._queue_full_notice.is_set():
                self._queue_full_notice.clear()
                if not self._warned:
                    self._warned = True
                    print(
                        "[METRICS][WARN] Modbus metric queue is full; "
                        "telemetry was dropped without blocking SCADA",
                        flush=True,
                    )
            if self._stop.is_set() and self._queue.empty():
                return
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            iteration, target, phase, warmup, payload = item
            try:
                self.record(
                    iteration=iteration,
                    target=target,
                    phase=phase,
                    warmup=warmup,
                    payload=payload,
                )
            except Exception as exc:
                # Metrics are observational.  A failed sink must not stop the
                # SCADA worker or alter request timing.
                if not self._warned:
                    self._warned = True
                    print(f"[METRICS][WARN] Modbus metric writer failed: {exc}", flush=True)
                with self._state_lock:
                    self._stats["write_errors"] += 1
            else:
                with self._state_lock:
                    self._stats["written"] += 1
            finally:
                with self._state_lock:
                    self._stats["processed"] += 1
                self._queue.task_done()

    def close(self, timeout: float = 3.0) -> None:
        if self._thread is None or self._closed:
            return
        with self._state_lock:
            self._accepting = False
            self._closed = True
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=max(0.0, float(timeout)))
        with self._state_lock:
            outstanding = max(0, self._stats["accepted"] - self._stats["processed"])
            self._stats["unflushed_on_close"] = outstanding
        if outstanding:
            print(
                f"[METRICS][WARN] Modbus metric writer closed with "
                f"{outstanding} accepted record(s) not flushed",
                flush=True,
            )
        self._write_stats()
        if not self._thread.is_alive():
            self._thread = None

    @property
    def stats(self) -> dict[str, int | bool]:
        with self._state_lock:
            snapshot: dict[str, int | bool] = dict(self._stats)
        snapshot["queue_capacity"] = self.queue_capacity
        snapshot["pending"] = self._queue.qsize()
        snapshot["thread_alive"] = bool(self._thread and self._thread.is_alive())
        return snapshot

    def _write_stats(self) -> None:
        path = (
            self.runtime_dir
            / "raw"
            / "metric_writer_stats"
            / f"modbus-{os.getpid()}.json"
        )
        try:
            atomic_write_json(path, {
                "writer": "modbus",
                "timestamp_epoch": time.time(),
                **self.stats,
            })
        except Exception as exc:
            if not self._warned:
                self._warned = True
                print(f"[METRICS][WARN] Modbus metric stats write failed: {exc}", flush=True)

    def record(
        self,
        *,
        iteration: int,
        target: str,
        phase: str,
        warmup: bool = False,
        payload: dict[str, Any],
    ) -> str:
        operation = str(payload.get("operation", "modbus"))
        kind = _request_kind(operation)
        status = _status(payload, warmup=warmup)
        # ``warmup`` means timeout-grace is active at observation time.  Only
        # an actual timeout is a warmup sample to exclude; successful requests
        # remain valid RTT/throughput observations.
        warmup_timeout = status == "warmup_timeout"
        request_id = self.request_ids.next_id(iteration, target, operation)
        details = {
            "phase": phase,
            "warmup": warmup_timeout,
            "timeout_grace": bool(warmup),
            "operation": operation,
            "host": payload.get("host", ""),
            "port": payload.get("port", ""),
            "unit_id": payload.get("unit_id", ""),
            "address": payload.get("address", ""),
            "count": payload.get("count", ""),
            "transaction_id": payload.get("transaction_id", ""),
            "function_code": payload.get("function_code", ""),
        }
        variable = f"{operation}:{payload.get('address', '')}+{payload.get('count', '')}"
        safe_log_many(
            self.event_logger,
            [
                MetricEvent(
                    wall_time_ns=int(payload.get("wall_start_ns", 0) or 0),
                    monotonic_ns=int(payload.get("monotonic_start_ns", 0) or 0),
                    iteration=int(iteration),
                    layer="communication",
                    component="scada",
                    event_type=f"modbus_{kind}_start",
                    source=self.source,
                    target=target,
                    variable=variable,
                    status="started",
                    request_id=request_id,
                    details=details,
                ),
                MetricEvent(
                    wall_time_ns=int(payload.get("wall_end_ns", 0) or 0),
                    monotonic_ns=int(payload.get("monotonic_end_ns", 0) or 0),
                    iteration=int(iteration),
                    layer="communication",
                    component="scada",
                    event_type=f"modbus_{kind}_{status}",
                    source=target,
                    target=self.source,
                    variable=variable,
                    value=payload.get("count", ""),
                    status=status,
                    request_id=request_id,
                    details={
                        **details,
                        "latency_ms": payload.get("latency_ms", ""),
                        "error_type": payload.get("error_type", ""),
                        "error": payload.get("error", ""),
                    },
                ),
            ]
        )
        row = {
            "request_id": request_id,
            "iteration": iteration,
            "phase": phase,
            "warmup": warmup_timeout,
            "operation": operation,
            "source": self.source,
            "target": target,
            **{
                field: payload.get(field, "")
                for field in COMMUNICATION_FIELDS
                if field not in {"request_id", "iteration", "phase", "warmup", "operation", "source", "target"}
            },
            "status": status,
        }
        _append_csv_rows(self.communication_path, COMMUNICATION_FIELDS, [row])
        return request_id
