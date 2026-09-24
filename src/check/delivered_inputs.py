"""Audit unchanged PLC logic against recorded, successfully delivered inputs.

For DoS experiments, current physical truth is a nominal reference, not proof
of what a remote controller received. Missing/failed delivery remains an audit
failure; it is never replaced with physical truth or assumed successful.
"""
from dataclasses import replace
from pathlib import Path
import csv
import math
import struct

from src.check.actuators import expected_actuator_state
from src.core.config import load_runtime_config, read_json, write_json
from src.io.csv import json_dir


def real32(value):
    value = struct.unpack('f', struct.pack('f', float(value)))[0]
    if not math.isfinite(value):
        raise ValueError('Non-finite controller input')
    return value


def verify_delivered_inputs(config_path: Path) -> dict:
    rt = load_runtime_config(config_path)
    source = json_dir(rt.output_dir / 'runtime')
    destination = rt.output_dir / 'check'
    destination.mkdir(exist_ok=True)
    owners = {tag: p['name'] for p in rt.raw['plcs'] for tag in p['sensors']}
    previous = dict(rt.actuator_initial_state)
    comparisons, issues, nominal_deviations = [], [], []
    for iteration in range(1, rt.iterations):
        try:
            physical = read_json(source / f'physics_{iteration:04d}.json')
            actual = read_json(source / f'actuator_state_{iteration:04d}.json')
            downlink = read_json(source / f'scada_downlink_{iteration:04d}.json')['plcs']
            if set(actual) != set(previous) or not all(isinstance(v, bool) for v in actual.values()):
                raise ValueError('Missing/unexpected/non-Boolean actuator state')
        except (OSError, ValueError, KeyError, TypeError) as error:
            issues.append(dict(iteration=iteration, error=str(error)))
            continue
        expected = dict(previous)
        per_plc_inputs = {}
        for plc in rt.raw['plcs']:
            if not plc['controls']:
                continue
            name = plc['name']
            try:
                local = read_json(source / f'local_write_{iteration:04d}_{name.lower()}.json')['written']
                inputs = {}
                for tag in {r['dependant'] for r in plc['controls']}:
                    variable = f'{owners[tag]}_{tag}'
                    if owners[tag] == name:
                        inputs[tag] = real32(local[variable])
                    else:
                        delivery = downlink[name]
                        if delivery.get('status') != 'ok' or delivery.get('timeout') or delivery.get('errors'):
                            raise ValueError(f'{name}/{variable}: delivery not confirmed')
                        inputs[tag] = real32(delivery['written'][variable]['value'])
                per_plc_inputs[name] = inputs
                scoped = replace(rt, raw={**rt.raw, 'plcs': [plc]})
                states, _ = expected_actuator_state(scoped, inputs, previous)
                expected.update({tag: states[tag] for tag in plc['actuators']})
            except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
                issues.append(dict(iteration=iteration, plc=name, error=str(error)))
        nominal, _ = expected_actuator_state(rt, physical, previous)
        for actuator in sorted(actual):
            comparisons.append(dict(iteration=iteration, actuator=actuator,
                                    expected_delivered=expected[actuator], actual=actual[actuator],
                                    expected_nominal=nominal[actuator], ok=expected[actuator] == actual[actuator]))
            if nominal[actuator] != actual[actuator]:
                plc = next(p for p in rt.raw['plcs'] if actuator in p['actuators'])
                tags = {r['dependant'] for r in plc['controls'] if r['actuator'] == actuator}
                nominal_deviations.append(dict(
                    iteration=iteration, actuator=actuator, plc=plc['name'],
                    nominal=nominal[actuator], delivered_expected=expected[actuator], actual=actual[actuator],
                    physical_inputs={t: physical.get('values', physical).get(t) for t in tags},
                    delivered_inputs={t: per_plc_inputs.get(plc['name'], {}).get(t) for t in tags},
                    delivery_evidence=str(source / f'scada_downlink_{iteration:04d}.json')))
        previous = actual
    mismatches = [r for r in comparisons if not r['ok']]
    expected_count = (rt.iterations - 1) * len(rt.actuator_initial_state)
    summary = dict(ok=not issues and not mismatches and len(comparisons) == expected_count,
                   input_basis='acknowledged local writes and per-destination SCADA downlink writes (REAL32)',
                   checked_actuator_states=len(comparisons), expected_actuator_states=expected_count,
                   evidence_errors=issues, mismatches=mismatches, nominal_deviations=nominal_deviations)
    with (destination / 'delivered_input_actuator_check.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=['iteration', 'actuator', 'expected_delivered', 'actual', 'expected_nominal', 'ok'])
        writer.writeheader()
        writer.writerows(comparisons)
    write_json(destination / 'delivered_input_check_summary.json', summary)
    return summary
