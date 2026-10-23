"""UPSI boundary used by the shared closed-loop scheduler."""
from __future__ import annotations
import copy
from datetime import datetime, timezone
from src.physics.factory import create_backend
from src.physics.signal_mapper import SignalMapper
from src.physics.time_coordinator import TimeCoordinator
from src.physics.quality import validate_policy
from src.physics.types import StepResult
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
        self.failure_records = []
        self.stage = "create"
        self.request_iteration = None
        try:
            self.backend, self.config = create_backend(self.rt)
            self.stage = "describe"
            description = self.backend.describe()
            from src.physics.types import SignalSpec
            if not isinstance(description,dict) or not isinstance(description.get('signals'),dict):
                raise ValueError('describe() must return a signals mapping')
            if any(not isinstance(spec,SignalSpec) or spec.name != name for name,spec in description['signals'].items()):
                raise ValueError('Native port names must match their SignalSpec names')
            self.stage = 'mapping'
            self.mapper = SignalMapper(self.config.options['signals'], description['signals'])
            sensors = {str(s) for plc in raw.get('plcs', []) for s in plc.get('sensors', [])}
            if not sensors <= {n for n,s in self.mapper.specs.items() if s.direction == 'output'}:
                raise ValueError('Every PLC sensor must map to an output signal')
            self.config.initial_inputs = self.mapper.inputs(self.rt.actuator_initial_state)
            self.time = TimeCoordinator(self.config.timestep_seconds, description.get('timing', 'target'),
                                        **self.config.options.get('timing',{}).get('tolerance',{}))
            self.max_age = self.config.options.get('timing',{}).get('measurement_max_age_s',0.)
            if self.max_age and description.get('asynchronous_measurements') is not True:
                raise ValueError('Backend must explicitly advertise bounded asynchronous measurements')
            self.ports = description['signals']
            self.description = description
            self._initialize()
        except Exception as exc:
            self._fail(exc)
            raise

    def _initialize(self):
        self.stage = "initialize"
        result = copy.deepcopy(self.backend.initialize())
        self.stage = "validate_initial"
        if not isinstance(result,StepResult): raise TypeError("initialize() must return StepResult")
        result.validate(self.ports, measurement_max_age_s=self.max_age)
        if result.applied_inputs != self.config.initial_inputs:
            raise RuntimeError('Backend did not apply the configured initial inputs')
        self.time.initialize(result)
        if self.config.options['init_style'] == 'dhalsim':
            from src.physics.legacy.ctown_compat import session_bootstrap
            self.bootstrap_records, self.start_cycle = session_bootstrap(self.backend)
        self.next_cycle = self.start_cycle
        snapshot = self._snapshot(result, self.start_cycle, self.rt.actuator_initial_state)
        self.schema = {'upsi_version':2, 'backend':self.config.options['backend'],
                       'model_kind':self.description.get('model_kind','unspecified'),
                       'timing':self.description.get('timing','target'),
                       'step_seconds':self.config.timestep_seconds,
                       'timestep_seconds':self.config.timestep_seconds,
                       'quality_policy':validate_policy(self.rt.raw),
                       'measurement_max_age_s':self.max_age,
                       'signals':{n:{**s.to_dict(),'port':self.mapper.bindings[n][0],
                                              'unit_transform':self.mapper.transforms[n]}
                                  for n,s in self.mapper.specs.items()}}
        for record in self.bootstrap_records:
            record['signal_metadata'] = {n:s.to_dict() for n,s in self.mapper.specs.items()}
            record['measurement_times'] = {n:record['sim_time'] for n in record['values']}
            record['validity'] = {n:False for n in record['values']}
        self.stage = 'publish_schema'
        write_json(self.config.work_dir/'json/signal_schema.json',self.schema)
        self.result, self.snapshot, self.available = result, snapshot, True
        self.stage = 'ready'

    def initialize(self):
        if self.closed or not self.available: raise RuntimeError('Physics session is unavailable')
        return copy.deepcopy(self.snapshot)

    def _snapshot(self, result, iteration, commands, timing=None):
        values,quality,timestamps = self.mapper.outputs(result)
        from src.physics.legacy.ctown_compat import snapshot_compatibility_fields
        snapshot = snapshot_compatibility_fields(self.config.options, result.diagnostics)
        snapshot.update(iteration=iteration, values=values, actuators_applied=dict(commands),
                        upsi_version=2, status=result.status, quality=quality, measurement_times=timestamps,
                        sim_time=result.time_s, sim_time_end=result.time_s,
                        sim_time_start=timing['input_effective_time_s'] if timing else result.time_s,
                        advanced=timing is not None, diagnostics=copy.deepcopy(result.diagnostics))
        snapshot.setdefault('backend',self.config.options['backend'])
        snapshot['validity'] = {name:result.validity[self.mapper.bindings[name][0]] for name in values}
        snapshot['freshness'] = {name:result.freshness[self.mapper.bindings[name][0]] for name in values}
        snapshot['signal_metadata'] = {n:s.to_dict() for n,s in self.mapper.specs.items()}
        if timing: snapshot['time_coordination'] = timing
        return snapshot

    def advance(self, commands, iteration):
        if self.closed or not self.available: raise RuntimeError('Physics session is unavailable')
        requested = None
        self.request_iteration = iteration
        self.stage = 'time_request'
        try:
            requested = self.time.target()
            self.stage = 'validate_inputs'
            if type(iteration) is not int or iteration != self.next_cycle+1:
                raise ValueError('Controller cycles must increase sequentially')
            native = self.mapper.inputs(commands)
            # Historical row numbering is confined to the compatibility context.
            self.config.compatibility['next_iteration'] = iteration
            self.stage = 'advance'
            result = copy.deepcopy(self.backend.advance(requested,native))
            self.stage = 'validate_result'
            if not isinstance(result,StepResult): raise TypeError('advance() must return StepResult')
            result.validate(self.ports, measurement_max_age_s=self.max_age)
            if result.status != 'ok': raise RuntimeError('advance() must return a solved result')
            if result.applied_inputs != native: raise RuntimeError('Backend did not apply the requested inputs')
            candidate_time = copy.deepcopy(self.time)
            timing = candidate_time.accept(result,requested)
            snapshot = self._snapshot(result,iteration,commands,timing)
            published = copy.deepcopy(snapshot)
            self.time, self.result, self.snapshot = candidate_time, result, snapshot
            self.next_cycle = iteration
            self.stage = "ready"
            return published
        except Exception as exc:
            self._fail(exc, requested)
            raise

    def validate_transport(self):
        """The current Modbus binding exposes REAL observations and BOOL coils."""
        for spec in self.mapper.specs.values():
            if (spec.direction == 'input' and spec.dtype != 'bool') or (spec.direction == 'output' and spec.dtype != 'float'):
                raise ValueError(f'{spec.name}: current PLC transport requires BOOL commands and REAL float observations')
        # Other UPSI scalars need an explicit PLC transport binding, not coercion.

    def _record_error(self, exc, stage, requested=None, primary=None):
        record = {'status':'error', 'stage':stage,
                  'backend':getattr(getattr(self,'config',None),'options',{}).get('backend',self.rt.raw.get('physics',{}).get('backend','dhalsim_epynet')),
                  'exception_type':type(exc).__name__, 'message':str(exc),
                  'requested_time_s':requested, 'iteration':self.request_iteration,
                  'last_valid_time_s':self.snapshot.get('sim_time') if self.snapshot else None,
                  'wall_time_utc':datetime.now(timezone.utc).isoformat()}
        if primary is not None: record['primary_error'] = str(primary)
        self.failure_records.append(record)
        work_dir = getattr(getattr(self,'config',None),'work_dir',self.rt.output_dir/'runtime')
        try:
            append_jsonl(work_dir/'raw/physics_errors.jsonl',record)
        except Exception as log_error:
            record['logging_error'] = str(log_error)  # Keep primary failure available even if disk failed.

    def _fail(self, exc, requested=None):
        self.available = False
        self.warning = str(exc)
        self._record_error(exc,self.stage,requested)
        self._cleanup(primary=exc,requested=requested)

    def _cleanup(self, primary=None, requested=None):
        if self.closed: return
        self.closed, self.available = True, False
        if self.backend is not None:
            try:
                self.backend.close()
            except Exception as close_error:
                self._record_error(close_error,'close',requested,primary)
                if primary is None: raise

    def abort(self, exc, *, stage='runtime', iteration=None):
        if self.closed: return
        self.stage,self.request_iteration=stage,iteration
        self._fail(exc)

    def close(self):
        self._cleanup()
