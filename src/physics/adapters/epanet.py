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


from src.physics.base import PhysicsBackend
from src.physics.types import SignalSpec, StepResult
from src.physics.legacy.ctown_compat import initialization_plan, epanet_units


class EpanetBackend(PhysicsBackend):
    """Native explicit EPANET ports; C-Town's historical policy is isolated."""
    def __init__(self, config):
        self.config = config
        self.adapter = None
        self.result = None
        self.closed = False
        self.ports = {}
        self.bindings = {}
        flow_unit, head_unit, pressure_unit = epanet_units(config.model_path())
        self.legacy = bool(config.options['legacy'])
        for group,direction in (('observations','output'),('commands','input')):
            for name,item in config.options['signals'][group].items():
                port = item['port']
                kind, element, quantity = port.split('.')
                allowed = {'node':{'level','pressure','head','demand'}, 'link':{'flow','status','setting'}}
                if kind not in allowed or quantity not in allowed[kind]: raise ValueError(f'Invalid EPANET port: {port}')
                if direction == 'input' and (kind,quantity) != ('link','status'):
                    raise ValueError('EPANET command ports are BOOL link statuses')
                unit = {'level':head_unit,'head':head_unit,'pressure':pressure_unit,
                        'flow':flow_unit,'demand':flow_unit,'status':'1','setting':'1'}[quantity]
                self.ports[port] = SignalSpec(port,direction,'bool' if direction == 'input' else 'float',unit)
                self.bindings[port] = (name,kind,element,quantity)

    def describe(self):
        return {'signals':dict(self.ports), 'timing':'event_step', 'model_kind':'hydraulic_events'}

    def _capture(self, snapshot, status='ok'):
        engine = self.adapter.engine
        outputs, commands = {}, {}
        node_codes = {'level':8,'pressure':11,'head':10,'demand':9}
        link_codes = {'flow':8,'status':11,'setting':12}
        for port,(name,kind,element,quantity) in self.bindings.items():
            if self.ports[port].direction == 'input':
                commands[port] = bool(engine.actuator_state[element])
            elif status == 'initial' and self.legacy:
                outputs[port] = float(snapshot['values'][name])
            else:
                index = engine._cache_epynet_node_index(element) if kind == 'node' else engine._cache_epynet_link_index(element)
                if not index: raise ValueError(f'Unknown EPANET element: {element}')
                if quantity == 'level' and element not in engine.epynet_tank_elevation:
                    raise ValueError(f'Level port requires a tank: {element}')
                code = node_codes[quantity] if kind == 'node' else link_codes[quantity]
                value = (engine.epynet.ENgetnodevalue(index,code) if kind == 'node'
                         else engine.epynet.ENgetlinkvalue(index,code))
                # Legacy engine derives tank level from solved head.
                if quantity == 'level':
                    value = engine.epynet.ENgetnodevalue(index,10)-engine.epynet_tank_elevation[element]
                outputs[port] = float(value)
        diagnostics = {'internal_steps':snapshot.get('internal_hydraulic_steps',0)}
        if self.legacy: diagnostics['legacy_snapshot'] = snapshot
        return StepResult(float(snapshot['sim_time']), outputs, commands, status, diagnostics)

    def initialize(self):
        if self.closed: raise RuntimeError('Physics backend is closed')
        if self.result is None:
            from src.physics.legacy.ctown_compat import engine_config
            cfg = engine_config(self.config, self.bindings)
            self.adapter = EpanetAdapter(cfg, self.config.work_dir)
            if not self.adapter.available: raise RuntimeError(self.adapter.warning or 'EPANET unavailable')
            _,initial,_ = initialization_plan(self.adapter, self.config.options['init_style'])
            status = 'initial' if self.config.options['init_style'] == 'dhalsim' else 'ok'
            self.result = self._capture(initial,status)
        import copy
        return copy.deepcopy(self.result)

    def advance(self, until_s, inputs):
        if self.closed or self.result is None: raise RuntimeError('Physics backend is not initialized')
        import math
        if type(until_s) not in (int,float) or not math.isfinite(until_s) or until_s <= self.result.time_s:
            raise ValueError('Target time must increase and be finite')
        for port,value in inputs.items(): self.ports[port].validate(value)
        commands = {self.bindings[port][2]:value for port,value in inputs.items()}
        iteration = self.config.compatibility.get('next_iteration')
        try:
            self.result = self._capture(self.adapter.step(commands,iteration=iteration))
        except Exception:
            self.close()
            raise
        import copy
        return copy.deepcopy(self.result)

    def close(self):
        if self.adapter is not None: self.adapter.close()
        self.closed = True
