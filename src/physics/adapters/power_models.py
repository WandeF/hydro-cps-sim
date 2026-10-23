"""Whitelisted public power models and provenance; no control/runtime policy."""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path

SOURCE_TABLES = ('bus', 'line', 'load', 'ext_grid', 'switch', 'gen', 'sgen',
                 'trafo', 'shunt', 'impedance')


def load_builtin(model):
    if model.get('case') != 'case33bw' or model.get('benchmark_mode') != 'original':
        raise ValueError('Allowed builtin: case33bw with benchmark_mode: original')
    if set(model) - {'source', 'case', 'benchmark_mode', 'source_citation', 'aliases', 'controlled_elements'}:
        raise ValueError('Original builtin model does not accept physical parameter overrides')
    import pandapower as pp
    import pandapower.networks as pn
    import pandapower.networks.power_system_test_cases as cases
    net = pn.case33bw()
    # This importer version preserves MATPOWER bus numbering as zero-based *names*.
    # Check that convention and the source anchor. DataFrame indices are independent.
    mapping = bus_map(net)
    if len(net.line) != 37 or int(net.line.in_service.sum()) != 32 or len(net.load) != 32:
        raise ValueError('Unexpected case33bw topology')
    if len(net.ext_grid) != 1 or int(net.ext_grid.iloc[0].bus) != mapping['BUS_01']['index']:
        raise ValueError('Unexpected case33bw source bus')
    if not all(net.bus.vn_kv == 12.66) or not all(net.ext_grid.vm_pu == 1.):
        raise ValueError('Unexpected case33bw voltage base')
    payload = {t:json.loads(net[t].to_json(orient='split', double_precision=15)) for t in SOURCE_TABLES}
    payload.update(sn_mva=float(net.sn_mva), f_hz=float(net.f_hz))
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    source_file = Path(cases.__file__).parent/'power_system_test_case_jsons/case33bw.json'
    provenance = {'source':'pandapower.networks.case33bw', 'pandapower_version':pp.__version__,
                  'source_citation':model.get('source_citation'),
                  'input_tables_sha256':hashlib.sha256(encoded).hexdigest(),
                  'source_file_sha256':hashlib.sha256(source_file.read_bytes()).hexdigest(),
                  'input_tables':payload, 'bus_id_map':mapping}
    names = {'bus':{a:m['index'] for a,m in mapping.items()}, 'load':{}, 'line':{}, 'ext_grid':{}}
    for alias, spec in model.get('aliases', {}).items():
        if spec.get('table') != 'bus': raise ValueError('Explicit aliases must reference bus')
        number = spec.get('paper_bus_number')
        target = mapping.get(f'BUS_{number:02d}') if type(number) is int else None
        if target is None or (alias in names['bus'] and names['bus'][alias] != target['index']):
            raise ValueError(f'Invalid/conflicting bus alias: {alias}')
        names['bus'][alias] = target['index']
    for idx, row in net.line.iterrows():
        names['line'][f'LINE_{int(idx):02d}'] = int(idx)
    names['ext_grid']['GRID'] = int(net.ext_grid.index[0])
    for alias, spec in model.get('controlled_elements', {}).items():
        if spec.get('table') != 'load' or spec.get('field') != 'in_service':
            raise ValueError('Controlled elements must reference load.in_service')
        names['load'][str(alias)] = resolve_load(net, mapping, spec.get('bus_number'))
    return net, names, provenance


def resolve_load(net, mapping, number):
    if type(number) is not int or f'BUS_{number:02d}' not in mapping:
        raise ValueError('Invalid controlled load bus number')
    candidates = net.load.index[net.load.bus == mapping[f'BUS_{number:02d}']['index']]
    if len(candidates) != 1:
        raise ValueError(f'Ambiguous/missing load on bus {number}')
    return int(candidates[0])


def bus_map(net):
    mapping = {}
    for index, row in net.bus.iterrows():
        # Validate the entire naming domain, never infer paper IDs from index+1.
        name = str(row['name'])
        try: number = int(name)+1
        except ValueError as exc: raise ValueError('Unsupported case33bw bus naming') from exc
        if str(number-1) != name or not 1 <= number <= 33:
            raise ValueError('Unsupported case33bw bus naming')
        alias = f'BUS_{number:02d}'
        if alias in mapping: raise ValueError('Duplicate case33bw bus name')
        mapping[alias] = {'paper_bus_number':number, 'index':int(index), 'source_name':name}
    if len(mapping) != 33: raise ValueError('Expected 33 unique named buses')
    return mapping


def power_metrics(net):
    # pandas sum skips NaN by default; validate source values before aggregation.
    for table, columns in ((net.load, ('p_mw', 'scaling')),
                           (net.res_load, ('p_mw',)), (net.res_ext_grid, ('p_mw',)),
                           (net.res_line, ('pl_mw',))):
        if any(not math.isfinite(float(value)) for column in columns for value in table[column]):
            raise ValueError('Nonfinite aggregate power input')
    requested = float((net.load.p_mw * net.load.scaling).sum())
    served = float(net.res_load.p_mw.sum())
    result = {'p_grid_mw':float(net.res_ext_grid.p_mw.sum()),
              'p_loss_mw':float(net.res_line.pl_mw.sum()),
              'served_load_mw':served, 'unserved_load_mw':requested-served}
    if not all(math.isfinite(x) for x in result.values()):
        raise ValueError('Nonfinite aggregate power results')
    return result
