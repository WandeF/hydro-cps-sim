#!/usr/bin/env python3
"""Accept actual UPSI full runs, legacy comparison and third-party integration."""
from __future__ import annotations
import argparse
import csv
import json
import math
from pathlib import Path
import sys

# Also supports `python scripts/check_upsi_results.py` from the repository root.
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.check_power_results import check as check_power, rows, coil, finite


def completed(root):
    stages=rows(root/'timing/run_all_timing.csv')
    for name in ('Start ns-3 network','Run closed loop','Export results','run_all total'):
        if not any(r['stage']==name and r['status']=='0' for r in stages):
            raise ValueError(f'Incomplete full run: {root}, stage={name}')
    if 'ns3 network started.' not in (root/'logs/ns3_network.log').read_text():
        raise ValueError(f'Missing ns-3 readiness: {root}')


def check(root):
    comparison={}
    for case in ('reference','current'): completed(root/case)
    for file,count in (('physics.csv',101),('actuator_state.csv',99),('scada_observed_key.csv',99)):
        left=rows(root/'reference/reports/csv'/file)
        right=rows(root/'current/reports/csv'/file)
        if len(left)!=count or left!=right:
            raise ValueError(f'C-Town 100-round regression mismatch: {file}')
        comparison[file]={'rows':count,'differences':0}
    water_schema=json.loads((root/'current/reports/signal_schema.json').read_text())
    if water_schema['signals']['J129']['unit'] != 'mH2O':
        raise ValueError('C-Town pressure unit metadata mismatch')
    reference=sorted((root/'reference/runtime/json').glob('physics_*.json'))
    actual=sorted((root/'current/runtime/json').glob('physics_*.json'))
    if len(actual)!=101 or len(reference)!=101: raise ValueError('Incomplete C-Town physical snapshots')
    for a,b in zip(reference,actual):
        old,new=json.loads(a.read_text()),json.loads(b.read_text())
        if old['sim_time']!=new['sim_time'] or old['actuators_applied']!=new['actuators_applied']:
            raise ValueError(f'C-Town raw time/command mismatch: {b}')
        for name,value in new['values'].items():
            if not math.isclose(value,old['values'][name],rel_tol=0,abs_tol=1e-10):
                raise ValueError(f'C-Town raw sensor mismatch: {b}, {name}')
    power=check_power(Path('examples/power_feeder/output'))
    thermal=Path('examples/thermal_box/output/baseline'); completed(thermal)
    trace=rows(thermal/'reports/csv/physics_state.csv')
    commands=rows(thermal/'reports/csv/actuator_state.csv')
    if [int(r['iteration']) for r in trace]!=list(range(11)) or len(commands)!=10:
        raise ValueError('Incomplete thermal run')
    if {coil(r['HEATER']) for r in commands}!={0.,1.}:
        raise ValueError('Thermal PLC did not exercise heating and cooling')
    temperature=19.
    for i,row in enumerate(trace):
        if row['backend']!='thermal_box' or finite(row['sim_time'])!=i or row['status']!='ok':
            raise ValueError('Thermal backend/time/status mismatch')
        if i:
            command=coil(commands[i-1]['HEATER'])
            equilibrium=18.+8.*command
            temperature=equilibrium+(temperature-equilibrium)*math.exp(-.1)
            if finite(row['HEATER_POWER'])!=800.*command:
                raise ValueError('Thermal power did not follow actual PLC command')
        if not math.isclose(finite(row['TEMPERATURE']),temperature,abs_tol=1e-10):
            raise ValueError('Thermal trajectory does not satisfy the model ODE')
    metadata=json.loads((thermal/'reports/signal_schema.json').read_text())
    if metadata['signals']['TEMPERATURE']['unit']!='degC': raise ValueError('Thermal metadata missing')
    v2=Path('examples/power_feeder/output/schema_v2');completed(v2)
    v2_trace=rows(v2/'reports/csv/physics_state.csv')
    baseline=rows(Path('examples/power_feeder/output/baseline/reports/csv/physics_state.csv'))
    if len(v2_trace)!=11: raise ValueError('Missing schema v2 power states')
    for a,b in zip(v2_trace,baseline):
        for key in ('V_CONTROL','V_LOAD','P_LOAD','P_GRID','LINE_LOADING','LOAD_SW','sim_time'):
            if not math.isclose(finite(a[key]),finite(b[key]),abs_tol=1e-10):
                raise ValueError(f'Schema v2 power baseline mismatch: {key}')
    reference_ref=(root/'reference_commit.txt').read_text().strip() if (root/'reference_commit.txt').exists() else '4dd6b2b'
    return {'status':'passed','reference_commit':reference_ref,'ctown_100_rounds':comparison,
            'ctown_raw_time_and_values_equal':True,'power':power,'power_schema_v2_equals_baseline':True,
            'third_adapter':{'backend':'thermal_box','physical_samples':11,'native_plc_cycles':10,
                             'heating_and_cooling_exercised':True,'analytic_ode_matches':True}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('output/upsi-regression'))
    args=parser.parse_args()
    try: result=check(args.output)
    except (ValueError,KeyError,OSError,TypeError) as exc:
        result={'status':'failed_or_incomplete','reason':str(exc)}
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'validation.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
