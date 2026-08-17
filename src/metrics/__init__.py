"""Metrics collection primitives for Hydro-CPS-Sim.

The package deliberately has no dependency on the runtime business modules so
that instrumentation can be enabled incrementally.  ``psutil`` is optional;
when it is not installed, :class:`RuntimeMonitor` remains importable and simply
does not start a sampling thread.
"""

from .event_logger import EventLogger, MetricEvent, RequestIdFactory, make_event, safe_log, safe_log_many
from .runtime_monitor import RuntimeMonitor

__all__ = [
    "EventLogger",
    "MetricEvent",
    "RequestIdFactory",
    "RuntimeMonitor",
    "make_event",
    "safe_log",
    "safe_log_many",
]
