#!/usr/bin/env python3
"""Validate full-run artifacts; never treat a solver/component test as end-to-end."""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


def rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f'Missing full-run artifact: {path}')
    with path.open() as stream:
        return list(csv.DictReader(stream))


def finite(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f'Nonfinite result: {value}')
    return result


def coil(value: str) -> float:
    if value in ('True', 'true'):
        return 1.0
    if value in ('False', 'false'):
        return 0.0
    result = finite(value)
    if result not in (0, 1):
        raise ValueError(f'Invalid coil value: {value}')
    return result


def check(output: Path) -> dict:
    traces = {}
    actuators = {}
    for case in ('baseline', 'transparent', 'mitm'):
        root = output / case
        stages = rows(root / 'timing/run_all_timing.csv')
        if not any(row['stage'] == 'run_all total' and row['status'] == '0' for row in stages):
            raise ValueError(f'{case}: no successful run_all completion')
        for stage in ('Start ns-3 network', 'Run closed loop', 'Export results'):
            if not any(row['stage'] == stage and row['status'] == '0' for row in stages):
                raise ValueError(f'{case}: stage not verified: {stage}')
        if 'ns3 network started.' not in (root/'logs/ns3_network.log').read_text():
            raise ValueError(f'{case}: ns-3 did not report readiness')
        trace = rows(root/'reports/csv/physics_state.csv')
        if [int(row['iteration']) for row in trace] != list(range(11)):
            raise ValueError(f'{case}: expected solved snapshots at iterations 0..10')
        for row in trace:
            if row['backend'] != 'pandapower' or row['solver_converged'] != 'True':
                raise ValueError(f'{case}: invalid physical backend or failed solver')
            for name in ('sim_time','V_CONTROL','V_LOAD','P_LOAD','P_GRID','LINE_LOADING'):
                finite(row[name])
            if finite(row['sim_time']) != int(row['iteration']):
                raise ValueError(f'{case}: wrong sample clock')
        traces[case] = trace
        actuators[case] = rows(root/'reports/csv/actuator_state.csv')
        if [int(row['iteration']) for row in actuators[case]] != list(range(10)):
            raise ValueError(f'{case}: missing controller cycles')
    for left, right in zip(traces['baseline'], traces['transparent']):
        for name in ('V_CONTROL','V_LOAD','P_LOAD','P_GRID','LINE_LOADING','LOAD_SW'):
            if abs(finite(left[name])-finite(right[name])) > 1e-8:
                raise ValueError(f'Transparent proxy changed baseline {name}')
    for left,right in zip(actuators['baseline'],actuators['transparent']):
        if left != right:
            raise ValueError('Transparent proxy changed actuator sequence')
    transparent_events = rows(output/'transparent/reports/csv/attack_events.csv')
    if {int(row['iteration']) for row in transparent_events} != {3, 4, 5, 6}:
        raise ValueError('Transparent proxy did not record all configured control writes')
    for row in transparent_events:
        if (row['variable'] != 'PLC1_V_CONTROL' or row['direction'] != 'request'
                or row['function_code'] != '16' or row['target'] != 'PLC2'
                or finite(row['new_value']) != finite(row['old_value'])):
            raise ValueError('Transparent proxy changed a value or forwarded an unexpected signal')
    events = rows(output/'mitm/reports/csv/attack_events.csv')
    changed = [row for row in events if abs(finite(row['new_value'])-finite(row['old_value']))>.1]
    if {int(row['iteration']) for row in changed} != {3, 4, 5, 6}:
        raise ValueError('MITM did not record modified control writes throughout its window')
    if not changed:
        raise ValueError('No actual Modbus data manipulation was recorded')
    for row in changed:
        if (row['variable'] != 'PLC1_V_CONTROL' or row['direction'] != 'request'
                or row['function_code'] != '16' or row['target'] != 'PLC2'):
            raise ValueError('Attack touched an unexpected signal, direction, function code or target')
        if not 3 <= int(row['iteration']) <= 6:
            raise ValueError('Attack occurred outside configured window')
        if abs((finite(row['new_value'])-finite(row['old_value']))+.15)>1e-5:
            raise ValueError('Wrong MITM offset')
    mismatch = [int(right['iteration']) for left,right in zip(actuators['baseline'],actuators['mitm'])
                if coil(left['LOAD_SW']) != coil(right['LOAD_SW'])]
    physical = [int(right['iteration']) for left,right in zip(traces['baseline'],traces['mitm'])
                if abs(finite(left['P_LOAD'])-finite(right['P_LOAD']))>.5]
    if not mismatch or not physical or mismatch[0]<3 or physical[0]!=mismatch[0]+1:
        raise ValueError('Expected control-to-physical propagation was not observed')
    if any(finite(row['LOAD_SW'])!=1 for row in traces['baseline']):
        raise ValueError('Baseline unexpectedly shed the flexible load')
    for row in traces['mitm']:
        if int(row['iteration']) >= physical[0] and finite(row['P_LOAD']) != 0:
            raise ValueError('Load remains served after the opening command')
    return {
        'status':'passed', 'execution':'shared OpenPLC + namespace + ns-3 closed loop',
        'backend':'pandapower', 'baseline_samples':len(traces['baseline']),
        'transparent_equals_baseline':True, 'modified_modbus_frames':len(changed),
        'first_control_mismatch':mismatch[0], 'first_physical_mismatch':physical[0],
        'baseline_flexible_load_mw':finite(traces['baseline'][0]['P_LOAD']),
        'attacked_flexible_load_mw':finite(traces['mitm'][physical[0]]['P_LOAD']),
    }


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('examples/power_feeder/output'))
    args=parser.parse_args()
    report=args.output/'validation.json'
    try:
        result=check(args.output)
    except (ValueError,KeyError,OSError) as exc:
        result={'status':'failed_or_incomplete','reason':str(exc)}
        report.parent.mkdir(parents=True,exist_ok=True)
        report.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result,indent=2))
        return 1
    report.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    return 0


if __name__=='__main__': raise SystemExit(main())
