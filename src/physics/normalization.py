"""Normalize scene schemas without importing or executing any simulator."""
from __future__ import annotations
import copy
from src.physics.config import physics_backend, physics_timestep
from src.physics.legacy.ctown_compat import water_signals, initial_timestep, resolve_water_model


def normalize_physics(raw, config_path):
    p = copy.deepcopy(raw.get('physics', {}))
    backend = physics_backend(raw)
    version = raw.get('schema_version', 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError('Supported schema_version values are 1 and 2')
    p['backend'] = backend
    p['legacy'] = version == 1
    if version == 1:
        model_path = str(resolve_water_model(raw, config_path)) if backend == 'dhalsim_epynet' else p.get('model_file')
        p.setdefault('model', {'path':model_path})
        p.setdefault('timing', {'mode': 'fixed_step', 'step_seconds': physics_timestep(raw) or initial_timestep(raw)})
        if 'signals' not in p:
            if backend == 'dhalsim_epynet':
                p['signals'] = water_signals({**raw,'inp_file':p['model']['path']}, config_path)
            else:
                units = {'vm_pu':'pu', 'p_mw':'MW', 'q_mvar':'Mvar', 'loading_percent':'%'}
                p['signals'] = {
                    'observations': {tag: {'port': '.'.join(str(s[k]) for k in ('table','element','column')),
                                          'dtype':'float', 'unit':units.get(s.get('column'), '1')}
                                     for tag,s in p.get('sensors', {}).items()},
                    'commands': {tag: {'port': '.'.join(str(s[k]) for k in ('table','element','column')),
                                       'dtype':'bool', 'unit':'1', 'invert':s.get('invert',False)}
                                 for tag,s in p.get('actuators', {}).items()}}
    else:
        if not isinstance(p.get('model'), dict):
            raise ValueError('schema_version 2 requires physics.model')
        if not isinstance(p.get('timing'), dict) or 'step_seconds' not in p['timing']:
            raise ValueError('schema_version 2 requires physics.timing.step_seconds')
        if p['timing'].get('mode', 'fixed_step') != 'fixed_step':
            raise ValueError('Only single-backend fixed_step scheduling is supported')
    if not isinstance(p.get('signals'), dict) or set(p['signals']) - {'observations','commands'}:
        raise ValueError('physics.signals requires observations and commands mappings')
    declared = raw.get('signals', {})
    if not isinstance(declared, dict):
        raise ValueError('signals must be a mapping')
    for group, direction in (('observations', 'output'), ('commands', 'input')):
        mapping = p['signals'].setdefault(group, {})
        if not isinstance(mapping, dict): raise ValueError(f'{group} must be a mapping')
        for name, item in mapping.items():
            if not isinstance(item, dict) or not isinstance(item.get('port'), str):
                raise ValueError(f'{name} needs a physical port')
            metadata = declared.get(name, {})
            if not isinstance(metadata, dict) or metadata.get('direction', direction) != direction:
                raise ValueError(f'{name} signal direction mismatch')
            for key in ('dtype','unit','minimum','maximum'):
                if key in metadata:
                    if key in item and item[key] != metadata[key]: raise ValueError(f'{name} conflicting {key}')
                    item[key] = metadata[key]
            if version == 2 and 'dtype' not in item:
                raise ValueError(f'{name} requires an explicit dtype in signals or mapping')
            if set(item) - {'port','dtype','unit','minimum','maximum','scale','offset','invert','missing'}:
                raise ValueError(f'{name} has unknown signal mapping options')
            if 'dtype' in item:
                from src.physics.types import SignalSpec
                SignalSpec(name,direction,item['dtype'],item.get('unit','1'),item.get('minimum'),item.get('maximum'))
    if version == 2 and set(declared) - set(p['signals']['observations']) - set(p['signals']['commands']):
        raise ValueError('signals metadata references unmapped variables')
    return p
