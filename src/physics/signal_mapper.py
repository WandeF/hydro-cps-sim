"""Logical signal ↔ native port mapping with strict types, ranges and units.

For numeric mappings: logical = native * scale + offset. A unit change requires
an explicit transform. Input conversion uses the inverse transform.
"""
from __future__ import annotations
import math
from src.physics.types import SignalSpec


class SignalMapper:
    def __init__(self, mapping, ports):
        self.ports = ports
        self.specs, self.bindings, self.transforms = {}, {}, {}
        targets = set()
        for group, direction in (('observations','output'), ('commands','input')):
            for name, item in mapping.get(group, {}).items():
                port = item['port']
                if port not in ports or ports[port].direction != direction:
                    raise ValueError(f'{name}: unknown/wrong-direction port {port}')
                native = ports[port]
                spec = SignalSpec(name, direction, item.get('dtype', native.dtype), item.get('unit', native.unit),
                                  item.get('minimum'), item.get('maximum'))
                if spec.dtype != native.dtype:
                    raise ValueError(f'{name}: dtype differs from native port {port}')
                if name in self.specs: raise ValueError(f'Duplicate logical signal: {name}')
                if direction == 'input' and port in targets: raise ValueError(f'Duplicate command port: {port}')
                if direction == 'input': targets.add(port)
                scale, offset = item.get('scale',1), item.get('offset',0)
                if any(type(v) not in (int,float) or not math.isfinite(v) for v in (scale,offset)) or scale == 0:
                    raise ValueError(f'{name}: invalid affine transform')
                if spec.dtype != 'float' and (scale != 1 or offset != 0):
                    raise ValueError(f'{name}: affine transforms require float signals')
                if spec.unit != native.unit and not ('scale' in item or 'offset' in item):
                    raise ValueError(f'{name}: unit change {native.unit} → {spec.unit} requires explicit scale/offset')
                from src.physics.units import validate_transform
                self.transforms[name] = validate_transform(native.unit,spec.unit,scale,offset,
                    context=item.get('unit_context'),calibration=item.get('calibration_reason'))
                self.specs[name] = spec
                invert = item.get('invert',False)
                if type(invert) is not bool or (invert and spec.dtype != 'bool'):
                    raise ValueError(f'{name}: invert requires a boolean signal')
                self.bindings[name] = (port,scale,offset,invert)
        if targets != {name for name,spec in ports.items() if spec.direction == 'input'}:
            raise ValueError('Every native input port needs exactly one command mapping')

    def inputs(self, values):
        expected = {name for name,spec in self.specs.items() if spec.direction == 'input'}
        if set(values) != expected: raise ValueError('Commands must supply every declared input exactly once')
        result = {}
        for name,value in values.items():
            value = self.specs[name].validate(value)
            port,scale,offset,invert = self.bindings[name]
            if invert: value = not value
            if self.specs[name].dtype == 'float': value = (value-offset)/scale
            result[port] = self.ports[port].validate(value)
        return result

    def outputs(self, result):
        values, quality, timestamps = {}, {}, {}
        for name,spec in self.specs.items():
            if spec.direction != 'output': continue
            port,scale,offset,invert = self.bindings[name]
            value = result.outputs[port]
            if invert: value = not value
            if spec.dtype == 'float': value = value*scale+offset
            values[name] = spec.validate(value)
            quality[name] = result.quality[port]
            timestamps[name] = result.timestamps[port]
        return values, quality, timestamps
