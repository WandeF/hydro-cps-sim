"""Independent UPSI SDK example: exact solution of a one-state thermal ODE.

No EPANET/pandapower, PLC, network, attack or runtime modules are imported.
"""
from __future__ import annotations
import copy
import math
import yaml
from src.physics.base import PhysicsBackend
from src.physics.types import SignalSpec, StepResult


class ThermalBackend(PhysicsBackend):
    def __init__(self, config):
        self.config = config
        self.result = None
        self.closed = False
        self.model = yaml.safe_load(config.model_path().read_text())
        for name in ('ambient_c','initial_c','loss_per_s','heater_c_per_s','heater_w'):
            value = self.model[name]
            if type(value) not in (int,float) or not math.isfinite(value):
                raise ValueError(f'Invalid thermal parameter: {name}')
        if self.model['loss_per_s'] <= 0 or min(self.model['heater_c_per_s'],self.model['heater_w']) < 0:
            raise ValueError('Thermal loss must be positive and heater parameters nonnegative')
        dtype = self.model.get('input_dtype','bool')
        if dtype not in ('bool','float'): raise ValueError('Thermal input_dtype must be bool or float')
        self.specs = {
            'box.temperature':SignalSpec('box.temperature','output','float','degC'),
            'box.heater_power':SignalSpec('box.heater_power','output','float','W',0),
            'heater.enabled':SignalSpec('heater.enabled','input',dtype,'1',
                                       0 if dtype == 'float' else None, 1 if dtype == 'float' else None)}

    def describe(self):
        return {'signals':dict(self.specs), 'timing':'target', 'model_kind':'first_order_thermal_ode'}

    def initialize(self):
        if self.closed: raise RuntimeError('Thermal backend is closed')
        if self.result is None:
            value = self.specs['heater.enabled'].validate(self.config.initial_inputs['heater.enabled'])
            self.result = StepResult(0.,{'box.temperature':float(self.model['initial_c']),
                                         'box.heater_power':self.model['heater_w']*float(value)},
                                     {'heater.enabled':value})
        return copy.deepcopy(self.result)

    def advance(self, until_s, inputs):
        if self.closed or self.result is None: raise RuntimeError('Thermal backend is not initialized')
        if type(until_s) not in (int,float) or not math.isfinite(until_s) or until_s <= self.result.time_s:
            raise ValueError('Target time must increase and be finite')
        if set(inputs) != {'heater.enabled'}: raise ValueError('Supply heater.enabled exactly once')
        value = self.specs['heater.enabled'].validate(inputs['heater.enabled'])
        dt = until_s-self.result.time_s
        loss, ambient = self.model['loss_per_s'],self.model['ambient_c']
        equilibrium = ambient + self.model['heater_c_per_s']*float(value)/loss
        temperature = equilibrium+(self.result.outputs['box.temperature']-equilibrium)*math.exp(-loss*dt)
        self.result = StepResult(float(until_s),{'box.temperature':temperature,
                                   'box.heater_power':self.model['heater_w']*float(value)},dict(inputs),
                                 diagnostics={'integration':'analytic','dt_s':dt})
        return copy.deepcopy(self.result)

    def close(self):
        self.closed = True
