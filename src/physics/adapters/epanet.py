"""Compatibility adapter: leave the established EPANET engine unchanged."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from src.core.config import RuntimeConfig
from src.physics.base import PhysicsAdapter, Snapshot
from src.physics.engine import PhysicsEngine


class EpanetAdapter(PhysicsAdapter):
    default_init_style = "dhalsim"

    def __init__(self, cfg: RuntimeConfig, work_dir: Path | None = None) -> None:
        self.engine = PhysicsEngine(cfg, mode="dhalsim_epynet", work_dir=work_dir)
        self.available = self.engine.available
        self.warning = self.engine.warning

    def initialize(self) -> Snapshot:
        return self.current_snapshot()

    def get_state(self) -> dict[str, float]:
        return dict(self.engine.current_snapshot()["values"])

    def current_snapshot(self, iteration: int | None = None) -> Snapshot:
        return self.engine.current_snapshot(iteration)

    def step(self, actuator_state: Mapping[str, bool] | None = None,
             iteration: int | None = None, *, dt: float | None = None) -> Snapshot:
        if dt is not None and dt != self.engine.cfg.hydraulic_timestep:
            raise ValueError("EPANET dt must equal the configured timestep")
        return self.engine.step(dict(actuator_state) if actuator_state is not None else None, iteration)

    def dhalsim_zero_snapshot(self, iteration: int = 0) -> Snapshot:
        return self.engine.dhalsim_zero_snapshot(iteration)

    def dhalsim_initial_snapshot(self, iteration: int = 1) -> Snapshot:
        return self.engine.dhalsim_initial_snapshot(iteration)

    def close(self) -> None:
        self.engine.close()
