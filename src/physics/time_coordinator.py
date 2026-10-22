"""Single-backend time grants; wall-clock/PLC scan time never enters physics."""
from __future__ import annotations
import math


class TimeCoordinator:
    def __init__(self, step_seconds, mode='target'):
        if type(step_seconds) not in (int,float) or not math.isfinite(step_seconds) or step_seconds <= 0:
            raise ValueError('step_seconds must be finite and positive')
        if mode not in {'target','event_step'}: raise ValueError('Unknown time advancement capability')
        self.step_seconds, self.mode = float(step_seconds), mode
        self.time_s = None

    def initialize(self, result):
        if self.time_s is not None: raise RuntimeError('Time coordinator is already initialized')
        self.time_s = result.time_s

    def target(self):
        if self.time_s is None: raise RuntimeError('Time coordinator is not initialized')
        return self.time_s + self.step_seconds

    def accept(self, result, requested):
        actual = result.time_s
        if not math.isfinite(actual) or actual <= self.time_s or actual > requested + 1e-9:
            raise RuntimeError(f'Invalid simulator time grant {actual}; current={self.time_s}, requested={requested}')
        if self.mode == 'target' and not math.isclose(actual, requested, rel_tol=0, abs_tol=1e-9):
            raise RuntimeError(f'Simulator stopped before requested target {requested}: {actual}')
        start = self.time_s
        self.time_s = actual
        return {'input_effective_time_s': start, 'requested_time_s': requested, 'granted_time_s': actual}
