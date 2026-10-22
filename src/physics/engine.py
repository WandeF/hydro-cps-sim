"""Compatibility import for the historical water engine.

New integrations implement PhysicsBackend; water-specific behavior lives under
physics.legacy and is never selected by the shared scheduler.
"""
from src.physics.legacy.epanet_engine import PhysicsEngine

__all__ = ["PhysicsEngine"]
