"""Select adapters through configuration; never silently fall back to mock data."""
from __future__ import annotations

from pathlib import Path
from importlib import import_module

from src.core.config import RuntimeConfig
from src.physics.base import PhysicsAdapter
from src.physics.config import physics_backend


def create_physics_adapter(cfg: RuntimeConfig, mode: str | None = None,
                           work_dir: Path | None = None) -> PhysicsAdapter:
    backend = physics_backend(cfg.raw, mode)
    from src.physics.registry import registration, resolve_factory
    source = registration(backend).legacy_factory
    if source is None:
        raise ValueError('This plugin uses UPSI PhysicsBackend; create a PhysicsSession instead')
    return resolve_factory(source)(cfg, work_dir)


def check_physics_dependencies(backend: str) -> None:
    """Import only the selected solver, before the runner changes the system."""
    from src.physics.registry import registration, resolve_factory
    spec = registration(backend)
    dependency = spec.dependency
    try:
        if dependency:
            import_module(dependency)
        else:
            resolve_factory(spec.factory)
    except ImportError as exc:
        raise ImportError(f"Missing solver for {backend}: {dependency}. Install the selected backend dependencies.") from exc


def backend_configuration(cfg, work_dir=None):
    from src.physics.normalization import normalize_physics
    from src.physics.config import physics_init_style
    from src.physics.types import BackendConfig
    options = normalize_physics(cfg.raw, cfg.config_path)
    options['init_style'] = physics_init_style(cfg.raw)
    return BackendConfig(cfg.config_path, work_dir or cfg.output_dir/'runtime', options['model'], options,
                         cfg.timestep_seconds, {},
                         {'raw':cfg.raw, 'initial_state':dict(cfg.initial_state)})


def create_backend(cfg, work_dir=None):
    from src.physics.registry import create_backend as registered_backend
    config = backend_configuration(cfg,work_dir)
    return registered_backend(config.options['backend'],config), config
