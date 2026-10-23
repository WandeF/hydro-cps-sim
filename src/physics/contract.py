"""Rootless SDK contract fixture and standalone real-backend smoke runner.

No generated PLC code, OpenPLC, Modbus endpoint, namespaces or ns-3 are used.
"""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import tempfile
from src.physics.time_coordinator import TimeCoordinator
from src.physics.types import StepResult


class OptionalBackendUnavailable(ImportError):
    """Declared optional solver dependency is not installed."""


def exercise_backend(backend, initial_inputs, step_seconds, *, steps=3, diagnostics=None):
    primary=None
    try:
        description=backend.describe();ports=description['signals']
        initial=copy.deepcopy(backend.initialize())
        if not isinstance(initial,StepResult): raise TypeError('initialize() must return StepResult')
        initial.validate(ports)
        again=copy.deepcopy(backend.initialize())
        if not isinstance(again,StepResult): raise TypeError('initialize() must return StepResult')
        again.validate(ports)
        if initial!=again: raise RuntimeError('initialize() is not idempotent')
        if initial.applied_inputs!=initial_inputs: raise RuntimeError('Initial inputs were not applied')
        clock=TimeCoordinator(step_seconds,description.get('timing','target'));clock.initialize(initial)
        samples=[initial]
        for _ in range(steps):
            result=copy.deepcopy(backend.advance(clock.target(),initial_inputs))
            if not isinstance(result,StepResult): raise TypeError('advance() must return StepResult')
            result.validate(ports)
            if result.status!='ok' or result.applied_inputs!=initial_inputs:
                raise RuntimeError('Invalid solved state/applied inputs')
            clock.accept(result,clock.target());samples.append(result)
        return samples
    except Exception as exc:
        primary=exc
        raise
    finally:
        try:backend.close()
        except Exception as close_error:
            if diagnostics is not None:diagnostics.append({'stage':'close','error':str(close_error)})
            if primary is None:raise
            primary.cleanup_error=close_error


def smoke(path, steps=3):
    from src.core.config import load_yaml, _initial_actuators, _initial_physical_state
    from src.physics.config import physics_init_style
    from src.physics.normalization import normalize_physics
    from src.physics.registry import configure_plugins, create_backend, registration
    from src.physics.types import BackendConfig
    from src.physics.signal_mapper import SignalMapper
    path=Path(path).resolve();raw=load_yaml(path);options=normalize_physics(raw,path)
    options['init_style']=physics_init_style(raw)
    with tempfile.TemporaryDirectory(prefix='upsi-contract-') as tmp:
        cfg=BackendConfig(path,Path(tmp),options['model'],options,options['timing']['step_seconds'],{},
            {'raw':raw,'initial_state':_initial_physical_state(raw)},registry=configure_plugins(raw))
        dependency=registration(options['backend'],cfg.registry).dependency
        if dependency:
            from importlib import import_module
            try:import_module(dependency)
            except ImportError as exc:raise OptionalBackendUnavailable(f'{dependency}: {exc}') from exc
        backend=create_backend(options['backend'],cfg)
        mapper=SignalMapper(options['signals'],backend.describe()['signals'])
        cfg.initial_inputs=mapper.inputs(_initial_actuators(raw))
        samples=exercise_backend(backend,cfg.initial_inputs,cfg.step_seconds,steps=steps)
        return {'status':'passed','backend':options['backend'],'execution':'standalone solver, no network/control',
                'samples':len(samples),'final_time_s':samples[-1].time_s}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--steps',type=int,default=3)
    args=parser.parse_args()
    if args.steps<1:parser.error('--steps must be positive')
    try:result=smoke(args.config,args.steps)
    except OptionalBackendUnavailable as exc:result={'status':'skipped','reason':str(exc)}
    except Exception as exc:result={'status':'failed','reason':str(exc)}
    print(json.dumps(result,indent=2))
    return 1 if result['status']=='failed' else 0


if __name__=='__main__':raise SystemExit(main())
