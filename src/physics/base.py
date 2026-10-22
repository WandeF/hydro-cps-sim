"""Domain-independent physics adapter contract.

Snapshots carry SI/engineering-unit values under configured, domain-neutral
sensor tags. Actuator True means enabled/connected; model mappings own any
inversion needed for a particular physical element.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

Snapshot = dict[str, Any]


class PhysicsAdapter(ABC):
    default_init_style = "current"

    @abstractmethod
    def initialize(self) -> Snapshot:
        """Load/solve the initial physical state; repeated calls are idempotent."""

    @abstractmethod
    def get_state(self) -> dict[str, float]:
        """Return a copy of currently solved sensor values."""

    @abstractmethod
    def current_snapshot(self, iteration: int | None = None) -> Snapshot:
        """Describe the current state without advancing physical time."""

    @abstractmethod
    def step(self, actuator_state: Mapping[str, bool] | None = None,
             iteration: int | None = None, *, dt: float | None = None) -> Snapshot:
        """Apply actuator commands and advance one physical step."""

    @abstractmethod
    def close(self) -> None:
        """Release resources; safe to call repeatedly."""
