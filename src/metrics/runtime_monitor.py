"""Optional background resource monitoring for experiment processes."""

from __future__ import annotations

import csv
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .event_logger import _append_csv_rows

try:  # Optional dependency: importing src.metrics must not require psutil.
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - depends on the test/runtime host.
    psutil = None  # type: ignore[assignment]


RESOURCE_FIELDS = (
    "timestamp_ns",
    "monotonic_ns",
    "component",
    "pid",
    "root_pid",
    "parent_pid",
    "is_child",
    "process_name",
    "status",
    "cpu_percent",
    "rss_bytes",
    "vms_bytes",
    "num_threads",
    "read_bytes",
    "write_bytes",
)


class RuntimeMonitor:
    """Sample resource usage for named PIDs into a fixed-schema CSV.

    Sampling starts immediately in a daemon thread.  A disappearing or
    inaccessible process is skipped; it never terminates the monitor thread.
    When ``include_process_tree`` is true, descendants are attributed to the
    same component and identified by ``root_pid``/``is_child``.
    """

    fieldnames = RESOURCE_FIELDS

    def __init__(
        self,
        output_file: Path | str,
        process_names: Mapping[str, int] | None = None,
        *,
        interval_sec: float = 0.5,
        include_process_tree: bool = False,
        process_resolver: Callable[[], Mapping[str, int]] | None = None,
    ) -> None:
        if interval_sec <= 0:
            raise ValueError("interval_sec must be positive")
        self.output_file = Path(output_file)
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        self.interval_sec = float(interval_sec)
        self.include_process_tree = bool(include_process_tree)
        self._process_resolver = process_resolver

        self._state_lock = threading.RLock()
        self._process_names: dict[str, int] = {}
        self._process_cache: dict[int, Any] = {}
        self._cpu_samples: dict[int, tuple[float, int, float]] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: BaseException | None = None
        self.set_processes(process_names or {})

    @property
    def available(self) -> bool:
        return psutil is not None

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def last_error(self) -> BaseException | None:
        with self._state_lock:
            return self._last_error

    @property
    def processes(self) -> dict[str, int]:
        with self._state_lock:
            return dict(self._process_names)

    @staticmethod
    def _normalize_processes(process_names: Mapping[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for component, raw_pid in process_names.items():
            if isinstance(raw_pid, bool):
                raise ValueError(f"invalid PID for {component!r}: {raw_pid!r}")
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid PID for {component!r}: {raw_pid!r}") from exc
            if pid <= 0:
                raise ValueError(f"PID must be positive for {component!r}: {pid}")
            normalized[str(component)] = pid
        return normalized

    def set_processes(self, process_names: Mapping[str, int]) -> None:
        normalized = self._normalize_processes(process_names)
        with self._state_lock:
            self._process_names = normalized
            configured_pids = set(normalized.values())
            self._process_cache = {
                pid: proc for pid, proc in self._process_cache.items() if pid in configured_pids
            }

    def add_process(self, component: str, pid: int) -> None:
        updated = self.processes
        updated[str(component)] = pid
        self.set_processes(updated)

    def remove_process(self, component: str) -> None:
        updated = self.processes
        updated.pop(str(component), None)
        self.set_processes(updated)

    def refresh_processes(self) -> dict[str, int]:
        """Resolve and atomically replace dynamic sampling targets.

        The resolver is invoked by the monitor thread immediately before every
        sample.  Resolver failures are handled by ``_run`` like sampling
        failures: they are exposed through ``last_error`` but never terminate
        the monitoring thread.
        """

        resolver = self._process_resolver
        if resolver is None:
            return self.processes
        try:
            resolved = self._normalize_processes(resolver())
            self.set_processes(resolved)
            return resolved
        except Exception as exc:
            self._record_error(exc)
            raise

    def _record_error(self, exc: BaseException) -> None:
        with self._state_lock:
            self._last_error = exc

    def _ensure_header(self) -> None:
        _append_csv_rows(self.output_file, self.fieldnames, [])

    def _get_process(self, pid: int) -> Any | None:
        if psutil is None:
            return None
        with self._state_lock:
            cached = self._process_cache.get(pid)
        if cached is not None:
            try:
                if cached.is_running():
                    return cached
            except (psutil.Error, OSError, RuntimeError):
                pass
            with self._state_lock:
                self._process_cache.pop(pid, None)
        try:
            proc = psutil.Process(pid)
        except (psutil.Error, OSError):
            return None
        with self._state_lock:
            self._process_cache[pid] = proc
        return proc

    @staticmethod
    def _safe_process_call(func: Any, default: Any = "") -> Any:
        if psutil is None:
            return default
        try:
            return func()
        except (psutil.Error, OSError, RuntimeError):
            return default

    def _process_row(
        self,
        proc: Any,
        *,
        component: str,
        root_pid: int,
        is_child: bool,
        timestamp_ns: int,
        monotonic_ns: int,
    ) -> dict[str, Any]:
        pid = int(getattr(proc, "pid", 0) or 0)
        memory = self._safe_process_call(proc.memory_info, None)
        io = self._safe_process_call(proc.io_counters, None)
        return {
            "timestamp_ns": timestamp_ns,
            "monotonic_ns": monotonic_ns,
            "component": component,
            "pid": pid,
            "root_pid": root_pid,
            "parent_pid": self._safe_process_call(proc.ppid),
            "is_child": bool(is_child),
            "process_name": self._safe_process_call(proc.name),
            "status": self._safe_process_call(proc.status),
            "cpu_percent": self._cpu_percent(
                proc,
                pid=pid,
                timestamp_ns=timestamp_ns,
                monotonic_ns=monotonic_ns,
            ),
            "rss_bytes": getattr(memory, "rss", "") if memory is not None else "",
            "vms_bytes": getattr(memory, "vms", "") if memory is not None else "",
            "num_threads": self._safe_process_call(proc.num_threads),
            "read_bytes": getattr(io, "read_bytes", "") if io is not None else "",
            "write_bytes": getattr(io, "write_bytes", "") if io is not None else "",
        }

    def _cpu_percent(
        self,
        proc: Any,
        *,
        pid: int,
        timestamp_ns: int,
        monotonic_ns: int,
    ) -> float | str:
        """Return non-blocking CPU utilization, including a measurable first sample.

        psutil's first ``cpu_percent(None)`` call is defined to return 0.0.
        Instead, the first observation uses CPU time accumulated since process
        creation; later observations use adjacent CPU/monotonic deltas.  The
        creation timestamp is also the PID-reuse identity.
        """

        cpu_times = self._safe_process_call(proc.cpu_times, None)
        create_time = self._safe_process_call(proc.create_time, None)
        if cpu_times is None or create_time in (None, ""):
            return ""
        try:
            total_cpu_sec = float(cpu_times.user) + float(cpu_times.system)
            created_epoch = float(create_time)
        except (AttributeError, TypeError, ValueError):
            return ""

        with self._state_lock:
            previous = self._cpu_samples.get(pid)
            self._cpu_samples[pid] = (created_epoch, int(monotonic_ns), total_cpu_sec)

        elapsed_sec: float
        cpu_delta_sec: float
        if previous is not None and previous[0] == created_epoch:
            elapsed_sec = (int(monotonic_ns) - previous[1]) / 1_000_000_000.0
            cpu_delta_sec = total_cpu_sec - previous[2]
        else:
            elapsed_sec = (int(timestamp_ns) / 1_000_000_000.0) - created_epoch
            cpu_delta_sec = total_cpu_sec
        if elapsed_sec <= 0:
            return ""
        return max(0.0, cpu_delta_sec) / elapsed_sec * 100.0

    def sample(
        self,
        process_names: Mapping[str, int] | None = None,
        *,
        include_process_tree: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Take one synchronous sample, append it, and return written rows."""

        if psutil is None:
            exc = RuntimeError("psutil is required for resource monitoring")
            self._record_error(exc)
            raise exc

        try:
            return self._sample(process_names, include_process_tree=include_process_tree)
        except Exception as exc:
            self._record_error(exc)
            raise

    def _sample(
        self,
        process_names: Mapping[str, int] | None = None,
        *,
        include_process_tree: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Implementation for ``sample``; callers receive recorded failures."""

        if process_names is None:
            targets = self.refresh_processes()
        else:
            targets = self._normalize_processes(process_names)
        include_tree = self.include_process_tree if include_process_tree is None else bool(include_process_tree)
        timestamp_ns = time.time_ns()
        monotonic_ns = time.monotonic_ns()
        rows: list[dict[str, Any]] = []
        sampled_pids: set[int] = set()

        for component, root_pid in targets.items():
            root = self._get_process(root_pid)
            if root is None:
                continue
            processes: list[tuple[Any, bool]] = [(root, False)]
            if include_tree:
                children = self._safe_process_call(lambda: root.children(recursive=True), [])
                for child in children or []:
                    child_pid = int(getattr(child, "pid", 0) or 0)
                    if child_pid <= 0:
                        continue
                    with self._state_lock:
                        self._process_cache[child_pid] = child
                    processes.append((child, True))

            for proc, is_child in processes:
                pid = int(getattr(proc, "pid", 0) or 0)
                if pid <= 0 or pid in sampled_pids:
                    continue
                sampled_pids.add(pid)
                rows.append(
                    self._process_row(
                        proc,
                        component=component,
                        root_pid=root_pid,
                        is_child=is_child,
                        timestamp_ns=timestamp_ns,
                        monotonic_ns=monotonic_ns,
                    )
                )

        _append_csv_rows(self.output_file, self.fieldnames, rows)
        with self._state_lock:
            self._cpu_samples = {
                pid: sample for pid, sample in self._cpu_samples.items() if pid in sampled_pids
            }
        return rows

    def _run(self) -> None:
        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            try:
                self.sample()
            except Exception as exc:  # Keep monitoring other cycles/components.
                self._record_error(exc)
            elapsed = time.monotonic() - cycle_start
            self._stop_event.wait(max(0.0, self.interval_sec - elapsed))

    def start(
        self,
        process_names: Mapping[str, int] | None = None,
        *,
        include_process_tree: bool | None = None,
    ) -> "RuntimeMonitor":
        if process_names is not None:
            self.set_processes(process_names)
        if include_process_tree is not None:
            self.include_process_tree = bool(include_process_tree)
        try:
            self._ensure_header()
        except Exception as exc:
            self._record_error(exc)
            raise
        if psutil is None:
            exc = RuntimeError("psutil is required for resource monitoring")
            self._record_error(exc)
            raise exc

        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._last_error = None
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"RuntimeMonitor-{os.getpid()}",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self, timeout: float | None = None) -> None:
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._state_lock:
            thread_alive = thread is not None and thread.is_alive()
            if self._thread is thread and not thread_alive:
                self._thread = None
            last_error = self._last_error
        failures: list[BaseException] = []
        if thread_alive:
            failures.append(TimeoutError(f"resource monitor did not stop within {timeout!r} seconds"))
        if last_error is not None:
            failures.append(last_error)
        if failures:
            detail = "; ".join(f"{type(exc).__name__}: {exc}" for exc in failures)
            raise RuntimeError(f"resource monitoring failed: {detail}") from failures[0]

    def __enter__(self) -> "RuntimeMonitor":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop()
