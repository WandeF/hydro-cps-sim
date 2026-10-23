"""Only this policy interprets historical water names and bootstrap rows."""
from __future__ import annotations
from pathlib import Path
from typing import Any


def water_signals(raw, config_path):
    path = resolve_water_model(raw, config_path)
    tanks = set()
    flow_unit, head_unit, pressure_unit = epanet_units(path)
    if path.is_file():
        section = ''
        for line in path.read_text().splitlines():
            words = line.split(';', 1)[0].split()
            if not words: continue
            if words[0].startswith('['): section = words[0].upper()
            elif section == '[TANKS]': tanks.add(words[0])
    observations = {}
    for plc in raw.get('plcs', []):
        for tag in plc.get('sensors', []):
            if tag.endswith('F'):
                port, unit = f'link.{tag[:-1]}.flow', flow_unit
            elif tag in tanks:
                port, unit = f'node.{tag}.level', head_unit
            else:
                port, unit = f'node.{tag}.pressure', pressure_unit
            observations[tag] = {'port': port, 'dtype': 'float', 'unit': unit}
    commands = {a['name']: {'port': f"link.{a['name']}.status", 'dtype': 'bool', 'unit': '1'}
                for a in raw.get('actuators', [])}
    return {'observations': observations, 'commands': commands}


def initial_timestep(raw):
    for item in raw.get('time', []):
        value = item.get('hydraulic_timestep') if isinstance(item, dict) else None
        if isinstance(value, list) and len(value) >= 2:
            return value[1]
    return raw.get('hydraulic_timestep', 300)


def initialization_plan(adapter, style):
    if style == 'dhalsim':
        return [adapter.dhalsim_zero_snapshot(0)], adapter.dhalsim_initial_snapshot(1), 1
    return [], adapter.current_snapshot(0), 0


def engine_config(config, bindings):
    from types import SimpleNamespace
    import copy
    raw = copy.deepcopy(config.compatibility['raw'])
    raw['inp_file'] = str(config.model_path())
    if not config.options['legacy']:
        raw['plcs'] = []
    state = config.compatibility.get('initial_state', {})
    commands = {bindings[port][2]:value for port,value in config.initial_inputs.items()}
    return SimpleNamespace(config_path=config.config_path, output_dir=config.work_dir.parent,
                           raw=raw, hydraulic_timestep=int(config.step_seconds), iterations=int(raw.get("iterations",100)),
                           initial_state=state, actuator_initial_state=commands)


def session_bootstrap(backend):
    if not hasattr(backend, 'adapter') or not hasattr(backend.adapter, 'dhalsim_zero_snapshot'):
        raise ValueError('Selected backend does not support the historical initialization policy')
    snapshot = backend.adapter.dhalsim_zero_snapshot(0)
    snapshot.update(status='initial', quality={k:'initial' for k in snapshot['values']})
    return [snapshot], 1


def legacy_initial_values(cfg: dict[str, Any]) -> dict[str, float]:
    state: dict[str, float] = {}
    for k, v in {**(cfg.get("initial_tank_values") or {}), **(cfg.get("initial_state") or {})}.items():
        try:
            state[str(k)] = float(v)
        except Exception:
            pass

    # Provide defaults for all declared sensors so the first iteration is total.
    for plc in cfg.get("plcs", []) or []:
        if not isinstance(plc, dict):
            continue
        for s in plc.get("sensors", []) or []:
            name = str(s)
            state.setdefault(name, 0.0)
    return state



def legacy_initial_commands(cfg):
    states: dict[str, bool] = {}
    for item in cfg.get("actuators", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        if not name:
            continue
        raw = str(item.get("initial_state", "closed")).lower()
        states[name] = raw in {"open", "true", "on", "1", "opened"}
    return states



def epanet_units(path):
    """Read engineering units; PRESSURE EXPONENT is a separate INP option."""
    flow,pressure = 'unknown',None
    section = ''
    if not path.is_file(): return 'unknown','unknown','unknown'
    for line in path.read_text().splitlines():
        words = line.split(';',1)[0].split()
        if not words: continue
        if words[0].startswith('['): section=words[0].upper()
        elif section == '[OPTIONS]' and len(words) == 2:
            if words[0].upper() == 'UNITS': flow=words[1].upper()
            elif words[0].upper() == 'PRESSURE': pressure=words[1].upper()
    us = {'CFS','GPM','MGD','IMGD','AFD'}
    si = {'LPS','LPM','MLD','CMH','CMD','CMS'}
    if flow not in us | si:
        raise ValueError('INP must declare supported flow units for UPSI metadata')
    head = 'ft' if flow in us else 'm'
    pressure_units={'PSI':'psi','KPA':'kPa','METERS':'mH2O'}
    if pressure is not None and pressure not in pressure_units:
        raise ValueError(f'Unsupported EPANET pressure units: {pressure}')
    return flow,head,pressure_units[pressure] if pressure else ('psi' if flow in us else 'mH2O')


def resolve_water_model(raw, config_path):
    value=raw.get('inp_file')
    if not isinstance(value,str) or not value:
        raise ValueError('Legacy EPANET configuration requires inp_file')
    path=Path(value).expanduser()
    if path.is_file(): return path.resolve()
    for candidate in (config_path.parent/path, config_path.parent/path.name):
        if candidate.is_file(): return candidate.resolve()
    return path


def snapshot_compatibility_fields(options, diagnostics):
    """Explicit historical presentation fields; never a general diagnostics merge."""
    import copy
    fields = {}
    if options['backend'] == 'dhalsim_epynet' and options.get('legacy'):
        legacy = diagnostics.get('legacy_snapshot', {})
        if not isinstance(legacy,dict): legacy={}
        allowed = {'warning','hydraulic_timestep','link_status','link_flow',
                   'internal_hydraulic_steps','step_order','inp_controls',
                   'last_tstep','initialization_stage'}
        fields.update({k:copy.deepcopy(v) for k,v in legacy.items() if k in allowed})
        historic_backends = {'dhalsim_compatible_initial','dhalsim_epynet',
                             'dhalsim_epynet_initial','dhalsim_epynet_snapshot'}
        if isinstance(legacy.get('backend'),str) and legacy.get('backend') in historic_backends: fields['backend'] = legacy['backend']
        if type(diagnostics.get('internal_steps')) is int: fields['internal_steps'] = diagnostics['internal_steps']
    if options['backend'] == 'pandapower':
        for name, expected in (('solver_converged', bool), ('unsupplied_buses', list)):
            if name in diagnostics and type(diagnostics[name]) is expected:
                fields[name] = copy.deepcopy(diagnostics[name])
    return fields
