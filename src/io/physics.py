"""Generic physical-state tables. No domain naming or fallback measurements."""
from __future__ import annotations
from typing import Any


def signal_columns(rt):
    columns = []
    for plc in rt.raw.get('plcs', []):
        for group in ('sensors','actuators'):
            columns.extend(str(tag) for tag in plc.get(group, []))
    p = rt.raw.get('physics', {})
    columns.extend(p.get('sensors', {}))
    for group in ('observations','commands'):
        columns.extend(p.get('signals', {}).get(group, {}))
    return list(dict.fromkeys(columns))


def state_columns(rt: Any) -> list[str]:
    metadata = ['iteration','sim_time','sim_time_start','sim_time_end','backend','solver_converged','status']
    tags = signal_columns(rt)
    if set(tags) & set(metadata): raise ValueError('Physical signal tags must not shadow snapshot metadata')
    return metadata + tags


def state_row(rt: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    values = {**snapshot.get('actuators_applied', {}), **snapshot.get('values', {})}
    row = {tag: (int(values[tag]) if isinstance(values.get(tag),bool) else values.get(tag,''))
           for tag in signal_columns(rt)}
    row.update({key:snapshot.get(key,'') for key in state_columns(rt)[:7]})
    return row
