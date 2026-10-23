"""UPSI time series and compatibility dispatch for existing operator CSVs."""
from __future__ import annotations
from datetime import datetime
from src.io.csv import append_row, csv_dir
from src.io.physics import state_columns, state_row, signal_columns


def uses_legacy_format(rt):
    from src.physics.config import physics_backend
    return rt.raw.get('schema_version',1) == 1 and physics_backend(rt.raw) == 'dhalsim_epynet'


def flat_columns(rt):
    if uses_legacy_format(rt):
        from src.io.legacy_export import BASE_COLUMNS, dhalsim_tag_columns
        return BASE_COLUMNS + dhalsim_tag_columns(rt)
    return ['iteration','timestamp'] + signal_columns(rt)


def flat_row(rt, snapshot):
    if uses_legacy_format(rt):
        from src.io.legacy_export import snapshot_to_dhalsim_row
        return snapshot_to_dhalsim_row(rt,snapshot)
    values = state_row(rt,snapshot)
    return {key:(snapshot.get('timestamp') or datetime.now().isoformat(sep=' ') if key == 'timestamp'
                 else values.get(key,'')) for key in flat_columns(rt)}


def write_runtime_physics(runtime_dir, rt, snapshot, *, filename='physics.csv'):
    append_row(csv_dir(runtime_dir)/filename,flat_row(rt,snapshot),fixed_columns=flat_columns(rt))
    append_row(csv_dir(runtime_dir)/'physics_state.csv',state_row(rt,snapshot),fixed_columns=state_columns(rt))


def signal_rows(snapshot):
    metadata = snapshot.get('signal_metadata', {})
    for direction,values in (('output',snapshot.get('values',{})),('input',snapshot.get('actuators_applied',{}))):
        for name,value in values.items():
            if metadata and name not in metadata:
                continue
            spec = metadata.get(name,{})
            yield {'iteration':snapshot.get('iteration'), 'sim_time':snapshot.get('sim_time'),
                   'backend':snapshot.get('backend'), 'signal':name, 'direction':direction, 'value':value,
                   'dtype':spec.get('dtype',''), 'unit':spec.get('unit',''),
                   'freshness':snapshot.get('freshness',{}).get(name,''),
                   'valid':snapshot.get('validity',{}).get(name, snapshot.get('status') == 'ok'),
                   'quality':snapshot.get('quality',{}).get(name, 'good' if snapshot.get('status') == 'ok' else snapshot.get('status','unknown')),
                   'measurement_time_s':snapshot.get('measurement_times',{}).get(name, snapshot.get('sim_time')),
                   'input_effective_time_s':snapshot.get('time_coordination',{}).get('input_effective_time_s','')}
