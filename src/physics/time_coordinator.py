"""Actual single-backend time grants with bounded floating-point tolerance."""
from __future__ import annotations
import math
import sys


class TimeCoordinator:
    def __init__(self, step_seconds, mode='target', *, absolute_s=None, relative=8*sys.float_info.epsilon):
        if type(step_seconds) not in (int,float) or not math.isfinite(step_seconds) or step_seconds<=0:
            raise ValueError('step_seconds must be finite and positive')
        if absolute_s is None: absolute_s=min(1e-9,step_seconds/8)
        for name,value in (('step_seconds',step_seconds),('absolute_s',absolute_s),('relative',relative)):
            if type(value) not in (int,float) or not math.isfinite(value) or value < 0 or (name=='step_seconds' and value==0):
                raise ValueError(f'{name} must be finite and nonnegative (step must be positive)')
        if mode not in {'target','event_step'}: raise ValueError('Unknown time advancement capability')
        if absolute_s > step_seconds/4 or relative > 1e-6:
            raise ValueError('Time tolerance must not mask a physical step')
        self.step_seconds, self.mode = float(step_seconds), mode
        self.absolute_s, self.relative = absolute_s, relative
        self.time_s = None

    def initialize(self, result):
        if self.time_s is not None: raise RuntimeError('Time coordinator is already initialized')
        if type(result.time_s) not in (int,float) or not math.isfinite(result.time_s) or result.time_s < 0:
            raise ValueError('Invalid initial physical clock')
        self.time_s = result.time_s

    def target(self):
        if self.time_s is None: raise RuntimeError('Time coordinator is not initialized')
        target = self.time_s+self.step_seconds
        if not math.isfinite(target) or target <= self.time_s:
            raise RuntimeError('Physical step is below clock resolution or overflows')
        return target

    def accept(self, result, requested):
        if self.time_s is None or type(requested) not in (int,float) or not math.isfinite(requested) or requested<=self.time_s:
            raise ValueError('Requested time must be finite and increase')
        actual = result.time_s
        scale=max(abs(self.time_s),abs(requested),self.step_seconds)
        tolerance=min(max(self.absolute_s,self.relative*scale,2*math.ulp(float(requested))),self.step_seconds/4)
        if type(actual) not in (int,float) or not math.isfinite(actual) or actual <= self.time_s or actual > requested+tolerance:
            raise RuntimeError(f'Invalid simulator time grant {actual}; current={self.time_s}, requested={requested}')
        if self.mode=='target' and abs(actual-requested)>tolerance:
            raise RuntimeError(f'Simulator stopped before requested target {requested}: {actual}')
        start=self.time_s
        self.time_s=actual
        return {'input_effective_time_s':start,'requested_time_s':requested,'granted_time_s':actual}
