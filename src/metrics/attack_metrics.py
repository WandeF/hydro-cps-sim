"""Bridge existing attack telemetry into the unified metrics timeline."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from src.core.config import load_yaml
from src.metrics.event_logger import EventLogger, MetricEvent, safe_log


_CANONICAL_EVENTS = {
    "dos_start": "attack_triggered",
    "dos_stop": "attack_stopped",
    "openplc_logic_start": "attack_logic_loaded",
    "openplc_logic_restore": "attack_stopped",
    "logic_injection_start": "attack_logic_loaded",
    "logic_injection_restore": "attack_stopped",
}


def _iteration(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return -1


def _wall_time_ns(row: dict[str, Any]) -> int:
    try:
        return int(float(row.get("timestamp_epoch")) * 1_000_000_000)
    except (TypeError, ValueError):
        return time.time_ns()


class AttackMetricRecorder:
    def __init__(self, runtime_dir: Path):
        runtime_dir = Path(runtime_dir)
        resolved_config = runtime_dir / "config_resolved.yaml"
        if resolved_config.is_file():
            try:
                metrics = load_yaml(resolved_config).get("metrics", {}) or {}
                if isinstance(metrics, dict) and not bool(metrics.get("enabled", False)):
                    self.logger = None
                    return
            except Exception:
                # A malformed/missing metadata copy must not affect the attack.
                pass
        try:
            self.logger: EventLogger | None = EventLogger(runtime_dir / "csv" / "events.csv")
        except Exception:
            self.logger = None

    def record(self, row: dict[str, Any], *, default_event: str = "attack_event") -> None:
        attack = str(row.get("attack", row.get("scenario", "")))
        event = str(row.get("event", row.get("action", default_event)) or default_event)
        event_type = _CANONICAL_EVENTS.get(event.lower(), event)
        value = row.get("new_value", row.get("modified_value", row.get("packets", "")))
        event_lower = event.lower()
        if any(token in event_lower for token in ("stop", "off", "restore")):
            status = "stopped"
        elif "schedule" in event_lower:
            status = "scheduled"
        else:
            status = str(row.get("status", "active"))
        safe_log(self.logger, MetricEvent(
            wall_time_ns=_wall_time_ns(row),
            monotonic_ns=time.monotonic_ns(),
            iteration=_iteration(row.get("iteration")),
            layer="attack",
            component=attack or "attacker",
            event_type=event_type,
            source=str(row.get("source", row.get("client", "attacker"))),
            target=str(row.get("target", row.get("server", ""))),
            variable=str(row.get("variable", "")),
            value=value,
            status=status,
            attack_id=attack,
            details={**dict(row), "source_event": event},
        ))
