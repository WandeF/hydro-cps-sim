"""Select adapters through configuration; never silently fall back to mock data."""
from __future__ import annotations

from pathlib import Path
from importlib import import_module

from src.core.config import RuntimeConfig
from src.physics.base import PhysicsAdapter
from src.physics.config import BACKEND_SPECS, physics_backend


def create_physics_adapter(cfg: RuntimeConfig, mode: str | None = None,
                           work_dir: Path | None = None) -> PhysicsAdapter:
    backend = physics_backend(cfg.raw, mode)
    module_name, class_name, _, _ = BACKEND_SPECS[backend]
    adapter_type = getattr(import_module(module_name), class_name)
    return adapter_type(cfg, work_dir)


def check_physics_dependencies(backend: str) -> None:
    """Import only the selected solver, before the runner changes the system."""
    _, _, dependency, _ = BACKEND_SPECS[backend]
    try:
        import_module(dependency)
    except ImportError as exc:
        raise ImportError(f"Missing solver for {backend}: {dependency}. Install the selected backend dependencies.") from exc
