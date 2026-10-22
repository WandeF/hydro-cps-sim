"""UPSI boundary used by the shared closed-loop scheduler."""
from __future__ import annotations
import copy
from datetime import datetime, timezone
from src.physics.factory import create_backend
from src.physics.signal_mapper import SignalMapper
from src.physics.time_coordinator import TimeCoordinator
from src.core.config import write_json
from src.io.csv import append_jsonl


class PhysicsSession:
    def __init__(self, runtime_config, mode=None, init_style=None):
        from dataclasses import replace
        raw = copy.deepcopy(runtime_config.raw)
        if mode not in (None,'auto'): raw.setdefault('physics', {})['backend'] = mode
        if init_style is not None: raw.setdefault('physics', {})['init_style'] = init_style
        self.rt = replace(runtime_config, raw=raw)
        self.backend = None
        self.available = False
        self.warning = None
        self.result = None
        self.snapshot = None
        self.bootstrap_records = []
        self.start_cycle = 0
        self.next_cycle = 0
        self.closed = False
        try:
            self.backend, self.config = create_backend(self.rt)
            description = self.backend.describe()
            from src.physics.types import SignalSpec
            if not isinstance(description,dict) or not isinstance(description.get('signals'),dict):
                raise ValueError('describe() must return a signals mapping')
            if any(not isinstance(spec,SignalSpec) or spec.name != name for name,spec in description['signals'].items()):
                raise ValueError('Native port names must match their SignalSpec names')
            self.mapper = SignalMapper(self.config.options['signals'], description['signals'])
            sensors = {str(s) for plc in raw.get('plcs', []) for s in plc.get('sensors', [])}
            if not sensors <= {n for n,s in self.mapper.specs.items() if s.direction == 'output'}:
                raise ValueError('Every PLC sensor must map to an output signal')
            self.config.initial_inputs = self.mapper.inputs(self.rt.actuator_initial_state)
            self.time = TimeCoordinator(self.config.step_seconds, description.get('timing', 'target'))
            self.ports = description['signals']
            self.description = description
            self._initialize()
        except Exception as exc:
            self._fail(exc)
            raise

    def _initialize(self):
        self.result = self.backend.initialize()
        self.result.validate(self.ports)
        if self.result.applied_inputs != self.config.initial_inputs:
            raise RuntimeError('Backend did not apply the configured initial inputs')
        self.time.initialize(self.result)
        if self.config.options['init_style'] == 'dhalsim':
            from src.physics.legacy.ctown_compat import session_bootstrap
            self.bootstrap_records, self.start_cycle = session_bootstrap(self.backend)
        self.next_cycle = self.start_cycle
        self.snapshot = self._snapshot(self.result, self.start_cycle, self.rt.actuator_initial_state)
        self.available = True
        self.schema = {'upsi_version':2, 'backend':self.config.options['backend'],
                       'model_kind':self.description.get('model_kind','unspecified'),
                       'timing':self.description.get('timing','target'),
                       'step_seconds':self.config.step_seconds,
                       'signals':{n:{**s.to_dict(),'port':self.mapper.bindings[n][0]}
                                  for n,s in self.mapper.specs.items()}}
        for record in self.bootstrap_records:
            record['signal_metadata'] = {n:s.to_dict() for n,s in self.mapper.specs.items()}
            record['measurement_times'] = {n:record['sim_time'] for n in record['values']}
            record['validity'] = {n:False for n in record['values']}
        write_json(self.config.work_dir/'json/signal_schema.json',self.schema)

    def initialize(self):
        if self.closed or not self.available: raise RuntimeError('Physics session is unavailable')
        return copy.deepcopy(self.snapshot)

    def _snapshot(self, result, iteration, commands, timing=None):
        values,quality,timestamps = self.mapper.outputs(result)
        snapshot = copy.deepcopy(result.diagnostics.get('legacy_snapshot', {}))
        snapshot.update(iteration=iteration, values=values, actuators_applied=dict(commands),
                        upsi_version=2, status=result.status, quality=quality, measurement_times=timestamps,
                        sim_time=result.time_s, sim_time_end=result.time_s,
                        sim_time_start=timing['input_effective_time_s'] if timing else result.time_s,
                        advanced=timing is not None, diagnostics={k:v for k,v in result.diagnostics.items() if k != 'legacy_snapshot'})
        snapshot.setdefault('backend',self.config.options['backend'])
        snapshot.update({k:v for k,v in result.diagnostics.items() if k != 'legacy_snapshot'})
        snapshot['validity'] = {name:q in {'good','unsupplied'} for name,q in quality.items()}
        snapshot['signal_metadata'] = {n:s.to_dict() for n,s in self.mapper.specs.items()}
        if timing: snapshot['time_coordination'] = timing
        return snapshot

    def advance(self, commands, iteration):
        if self.closed or not self.available: raise RuntimeError('Physics session is unavailable')
        requested = self.time.target()
        try:
            if type(iteration) is not int or iteration != self.next_cycle+1:
                raise ValueError('Controller cycles must increase sequentially')
            native = self.mapper.inputs(commands)
            # Historical row numbering is confined to the compatibility context.
            self.config.compatibility['next_iteration'] = iteration
            result = self.backend.advance(requested,native)
            result.validate(self.ports)
            if result.status != 'ok': raise RuntimeError('advance() must return a solved result')
            if result.applied_inputs != native: raise RuntimeError('Backend did not apply the requested inputs')
            timing = self.time.accept(result,requested)
            snapshot = self._snapshot(result,iteration,commands,timing)
            self.result, self.snapshot = result, snapshot
            self.next_cycle = iteration
            return copy.deepcopy(snapshot)
        except Exception as exc:
            self._fail(exc, requested)
            raise

    def validate_transport(self):
        """The current Modbus binding exposes REAL observations and BOOL coils."""
        for spec in self.mapper.specs.values():
            if (spec.direction == 'input' and spec.dtype != 'bool') or (spec.direction == 'output' and spec.dtype != 'float'):
                raise ValueError(f'{spec.name}: current PLC transport requires BOOL commands and REAL float observations')
        # Other UPSI scalars need an explicit PLC transport binding, not coercion.

    def _fail(self, exc, requested=None):
        self.available = False
        self.warning = str(exc)
        work_dir = getattr(getattr(self,'config',None),'work_dir',self.rt.output_dir/'runtime')
        append_jsonl(work_dir/'raw/physics_errors.jsonl',
                     {'status':'error','message':str(exc),'requested_time_s':requested,
                      'last_valid_time_s':self.snapshot.get('sim_time') if self.snapshot else None,
                      'wall_time_utc':datetime.now(timezone.utc).isoformat()})
        if self.backend is not None: self.backend.close()
        self.closed = True

    def close(self):
        if self.backend is not None and not self.closed: self.backend.close()
        self.closed, self.available = True, False
