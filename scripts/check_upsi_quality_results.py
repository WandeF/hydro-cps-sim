#!/usr/bin/env python3
"""Check real hold-last and expected fail-fast runs, not synthetic substitutes."""
import csv
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.check_upsi_results import completed
from scripts.check_power_results import rows


def check(root):
    held=root/'power-hold';failed=root/'power-fail'
    completed(held)
    records=[json.loads(line) for line in (held/'runtime/raw/signal_quality.jsonl').read_text().splitlines()]
    holds=[r for r in records if r['signal']=='V_LOAD' and r['stage']=='plc_local' and r['decision']=='hold_last' and r['quality']=='unsupplied']
    if not holds or any(r.get('delivered_source_time_s')!=3. or r.get('delivered_freshness')!='stale' or r['raw_value']!=0 or r['delivered_value']<.9 or r['write_status']!='written' for r in holds):
        raise ValueError('Actual PLC hold-last did not preserve a prior voltage')
    sample=json.loads((held/'runtime/json/physics_0004.json').read_text())
    if sample['values']['V_LOAD']!=0 or sample['values']['P_LOAD']!=0:
        raise ValueError('Quality policy incorrectly changed physical state')
    local=json.loads((held/'runtime/json/local_write_0004_plc1.json').read_text())
    metadata=local['measurement_metadata']['PLC1_V_LOAD']
    if metadata['source_time_s']!=3. or metadata['freshness']!='stale':
        raise ValueError('Held measurement lost its original time')
    poll=json.loads((held/'runtime/json/scada_poll_0004.json').read_text())
    if poll['plcs']['PLC1']['md']['PLC1_V_LOAD'] < .9:
        raise ValueError('SCADA did not observe the actual held PLC register value')
    stages=rows(failed/'timing/run_all_timing.csv')
    if not any(r['stage']=='run_all total' and r['status']!='0' for r in stages):
        raise ValueError('fail_fast unexpectedly succeeded')
    events=[json.loads(line) for line in (failed/'runtime/raw/signal_quality.jsonl').read_text().splitlines()]
    rejections=[r for r in events if r['signal']=='V_LOAD' and r['decision']=='fail_fast' and r['write_status']=='rejected']
    if not rejections or rejections[0]['iteration']!=4:
        raise ValueError('Missing expected quality rejection')
    if (failed/'runtime/json/physics_0005.json').exists() or (failed/'runtime/sync/physics_0005.ready').exists():
        raise ValueError('Failed control cycle advanced/published another physical state')
    return {'status':'passed','execution':'native OpenPLC/SCADA/ns-3',
            'hold_last_events':len(holds),'held_value_source_time_s':3.,
            'physical_zero_preserved':True,'fail_fast_expected_failure':True,'rejected_iteration':4,
            'no_next_physical_snapshot':True}


if __name__=='__main__':
    root=Path('output/upsi-hardening')
    try:result=check(root)
    except (OSError,ValueError,KeyError) as exc:result={'status':'failed_or_incomplete','reason':str(exc)}
    root.mkdir(parents=True,exist_ok=True)
    (root/'quality-validation.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['status']=='passed' else 1)
