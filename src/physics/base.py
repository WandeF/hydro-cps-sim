"""UPSI lifecycle and the backwards-compatible step/snapshot API.

New integrations implement PhysicsBackend and exchange typed StepResult values.
PhysicsAdapter preserves the earlier Python API for existing callers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping
from src.physics.types import Scalar, StepResult

Snapshot = dict[str, Any]


class PhysicsAdapter(ABC):
    """Deprecated step/snapshot API retained for existing Python callers."""
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


class PhysicsBackend(ABC):
    """UPSI v2. Ports and scalar values have no PLC/Modbus representation.

    Construction must not acquire solver resources. describe() is available
    before initialize(). advance() reports actual simulator time, never wall time.
    """

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        """Return signals: dict[str, SignalSpec] and timing: target|event_step."""

    @abstractmethod
    def initialize(self) -> StepResult:
        """Return an idempotent StepResult at the initial physical time."""

    @abstractmethod
    def advance(self, until_s: float, inputs: Mapping[str, Scalar]) -> StepResult:
        """Apply inputs at the current time and advance toward until_s."""

    @abstractmethod
    def close(self) -> None:
        """Release resources. Safe before initialization and after failure."""
