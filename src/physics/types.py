"""UPSI values and variable metadata, independent of PLC encoding or solvers."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
import math
from pathlib import Path
from typing import Any, Mapping

Scalar = float | int | bool | str


@dataclass(frozen=True)
class SignalSpec:
    name: str
    direction: str
    dtype: str
    unit: str = "1"
    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self):
        if not self.name or self.direction not in {"input", "output"}:
            raise ValueError(f"Invalid signal identity/direction: {self.name}")
        if self.dtype not in {"float", "int", "bool", "str"}:
            raise ValueError(f"Unsupported signal dtype: {self.dtype}")
        if not isinstance(self.unit, str) or not self.unit:
            raise ValueError(f"Invalid unit for {self.name}")
        for bound in (self.minimum, self.maximum):
            if bound is not None and (isinstance(bound, bool) or not math.isfinite(bound)):
                raise ValueError(f"Invalid range for {self.name}")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(f"Inverted range for {self.name}")
        if self.dtype in {"bool", "str"} and (self.minimum is not None or self.maximum is not None):
            raise ValueError(f"Nonnumeric signal {self.name} cannot declare numeric bounds")

    def validate(self, value: Scalar) -> Scalar:
        valid = {"float": lambda: type(value) in (int, float) and math.isfinite(value),
                 "int": lambda: type(value) is int,
                 "bool": lambda: type(value) is bool,
                 "str": lambda: type(value) is str}[self.dtype]()
        if not valid:
            raise ValueError(f"{self.name} requires a finite {self.dtype}, got {value!r}")
        if self.dtype in {"float", "int"}:
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"{self.name} below minimum {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"{self.name} above maximum {self.maximum}")
        return float(value) if self.dtype == "float" else value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StepResult:
    time_s: float
    outputs: dict[str, Scalar]
    applied_inputs: dict[str, Scalar]
    status: str = "ok"
    diagnostics: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, str] = field(default_factory=dict)
    timestamps: dict[str, float] = field(default_factory=dict)

    def validate(self, signals: Mapping[str, SignalSpec]) -> None:
        if isinstance(self.time_s, bool) or not math.isfinite(self.time_s) or self.time_s < 0:
            raise ValueError("Invalid physical result time")
        if self.status not in {"ok", "initial"}:
            raise RuntimeError(f"Physical backend status {self.status}: {self.diagnostics}")
        for direction, values in (("output", self.outputs), ("input", self.applied_inputs)):
            expected = {name for name, spec in signals.items() if spec.direction == direction}
            if set(values) != expected:
                raise ValueError(f"Incomplete/unexpected {direction} ports: expected {expected}, got {set(values)}")
            for name, value in values.items():
                signals[name].validate(value)
        if set(self.quality) - self.outputs.keys() or set(self.timestamps) - self.outputs.keys():
            raise ValueError("Quality/timestamps reference unknown output ports")
        for name in self.outputs:
            quality = self.quality.get(name, "good" if self.status == "ok" else "initial")
            if quality not in ({"good", "unsupplied"} if self.status == "ok" else {"initial"}):
                raise RuntimeError(f"Invalid physical measurement {name}: {quality}")
            stamp = self.timestamps.get(name, self.time_s)
            if isinstance(stamp, bool) or not math.isfinite(stamp) or stamp != self.time_s:
                raise ValueError(f"Stale/nonfinite measurement timestamp: {name}")
            self.quality[name] = quality
            self.timestamps[name] = stamp


@dataclass
class BackendConfig:
    config_path: Path
    work_dir: Path
    model: dict[str, Any]
    options: dict[str, Any]
    step_seconds: float
    initial_inputs: dict[str, Scalar]
    # Consumed only by the historical compatibility policy, never by plugins.
    compatibility: dict[str, Any] = field(default_factory=dict)

    def model_path(self) -> Path:
        value = self.model.get("path")
        if not isinstance(value, str) or not value:
            raise ValueError("physics.model.path is required")
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (self.config_path.parent / path).resolve()
