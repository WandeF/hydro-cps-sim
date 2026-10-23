#!/usr/bin/env python3
"""Recompute IEEE33 evidence and verify real network -> PLC -> physical propagation."""
from __future__ import annotations
import argparse
import csv
import json
import math
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.ieee33_reference import write_csv


def require(condition,message):
    if not condition: raise ValueError(message)


def read(path):return json.loads(path.read_text())

def rows(path):
    with path.open() as stream:return list(csv.DictReader(stream))


def near(a,b,eps=1e-8):
    return math.isfinite(float(a)) and math.isfinite(float(b)) and abs(float(a)-float(b))<=eps


def stages(root):
    timing=rows(root/'timing/run_all_timing.csv')
    for stage in ('run_all total','Start ns-3 network','Run closed loop','Export results'):
        require(any(r['stage']==stage and r['status']=='0' for r in timing),f'{root}: unsuccessful {stage}')
    require('ns3 network started.' in (root/'logs/ns3_network.log').read_text(),f'{root}: ns-3 not ready')


def check_case(root, n, reference, off_reference=None, attacked=False, events=None):
    stages(root)
    raw=root/'runtime/json'
    files=sorted(raw.glob('physics_[0-9]*.json'))
    require(len(files)==n+1,f'{root}: incomplete physical snapshots')
    snapshots=[read(p) for p in files]
    require([p['iteration'] for p in snapshots]==list(range(n+1)),f'{root}: wrong iterations')
    exported=rows(root/'reports/csv/physics_state.csv')
    require(len(exported)==len(snapshots),f'{root}: incomplete CSV export')
    actuators=rows(root/'reports/csv/actuator_state.csv')
    require([int(r['iteration']) for r in actuators]==list(range(n)),f'{root}: incomplete actuator sequence')
    event_map={int(e['iteration']):e for e in (events or [])}
    timing={int(r['iteration']):r for r in rows(root/'runtime/csv/scada_timing.csv')}
    cycle_timing={int(r['iteration']):r for r in rows(root/'runtime/csv/closed_loop_timing.csv')}
    require(set(timing)==set(range(n)) and set(cycle_timing)==set(range(n)),f'{root}: missing cycle timing')
    bus_rows=[];physical=[];control=[]
    for k,snapshot in enumerate(snapshots):
        shed=attacked and k>=21
        expected=off_reference if shed else reference
        require(snapshot['backend']=='pandapower' and snapshot['status']=='ok',f'{root}: invalid state {k}')
        require(snapshot['sim_time']==float(k),f'{root}: wrong time {k}')
        d=snapshot['diagnostics']
        require(d['solver_converged'] is True,f'{root}: failed solve {k}')
        require(d['input_tables_sha256']==reference['input_tables_sha256'],f'{root}: model provenance changed')
        require(len(d['bus_voltages'])==33,f'{root}: missing buses')
        for bus,expected_bus in zip(d['bus_voltages'],expected['bus_voltage']):
            require(bus['bus_number']==expected_bus['bus_number'] and bus['quality']=='good'
                    and near(bus['vm_pu'],expected_bus['vm_pu']),f'{root}: bus voltage mismatch {k}/{bus}')
            bus_rows.append({'iteration':k,'sim_time_s':snapshot['sim_time'],**bus})
        for metric,value in expected['metrics'].items():
            require(near(d['power_metrics'][metric],value),f'{root}: wrong {metric} at {k}')
        require(snapshot['actuators_applied']['LOAD24_ENABLE'] is (not shed),f'{root}: applied command misaligned {k}')
        require(d['controlled_loads']['LOAD_BUS24'] is (not shed),f'{root}: load control mismatch')
        by_bus={r['bus_number']:r['vm_pu'] for r in expected['bus_voltage']}
        logical_expected={f'V_BUS{b:02d}':by_bus[b] for b in (2,18,24,33)}
        logical_expected.update(P_GRID=expected['metrics']['p_grid_mw'],
                                TOTAL_P_LOSS=expected['metrics']['p_loss_mw'],
                                SERVED_LOAD_P=expected['metrics']['served_load_mw'],
                                UNSERVED_LOAD_P=expected['metrics']['unserved_load_mw'])
        for tag,value in logical_expected.items():
            require(near(snapshot['values'][tag],value),f'{root}: wrong logical measurement {tag}/{k}')
        require(near(snapshot['values']['P_LOAD24'],0. if shed else .420),f'{root}: incorrect load24')
        for tag in snapshot['values']:
            require(snapshot['quality'][tag]=='good' and snapshot['validity'][tag]
                    and snapshot['freshness'][tag]=='fresh' and snapshot['measurement_times'][tag]==float(k),f'{root}: quality {tag}/{k}')
            require(near(exported[k][tag],snapshot['values'][tag]),f'{root}: raw/CSV mismatch {tag}')
        if k:
            require(snapshot['time_coordination']['input_effective_time_s']==k-1,f'{root}: incorrect command time')
        physical.append({'iteration':k,'sim_time_s':snapshot['sim_time'],**d['power_metrics'],
                         'load24_in_service':not shed,'solver_converged':True})
        if k==n:continue
        for field in ('poll_error_total','poll_timeout_total','poll_warmup_timeout_total',
                      'downlink_error_total','downlink_timeout_total','downlink_warmup_timeout_total'):
            require(float(timing[k][field])==0.,f'{root}: communication fault {field}/{k}')
        command=read(raw/f'actuator_state_{k:04d}.json')['LOAD24_ENABLE']
        expected_command=not(attacked and k>=20)
        require(type(command) is bool and command==expected_command,f'{root}: wrong PLC control at {k}')
        require((actuators[k]['LOAD24_ENABLE']=='True')==command,f'{root}: actuator CSV differs')
        coil=read(raw/f'actuators_{k:04d}_plc3.json')
        require(coil['actuators']['PLC3_LOAD24_ENABLE']==command,f'{root}: actual PLC coil differs')
        poll=read(raw/f'scada_poll_{k:04d}.json')
        for plc in ('PLC1','PLC2','PLC3'):
            item=poll['plcs'][plc]
            require(item['status']=='ok' and not item.get('timeout') and not item.get('used_previous'),f'{root}: polling fault {plc}/{k}')
        source=poll['plcs']['PLC1']['md']['PLC1_V_BUS02'] # flat contains duplicate remote caches
        require(near(source,snapshot['values']['V_BUS02'],1e-6),f'{root}: PLC1 voltage differs')
        source_meta=poll['plcs']['PLC1']['measurement_metadata']['PLC1_V_BUS02']
        require(source_meta['source_time_s']==float(k) and source_meta['valid'],f'{root}: invalid source time')
        down=read(raw/f'scada_downlink_{k:04d}.json')['plcs']['PLC3']
        require(down['status']=='ok' and not down['errors'],f'{root}: downlink failed')
        write=down['written']['PLC1_V_BUS02']
        require(write['source']=='poll:PLC1:PLC1_V_BUS02' and near(write['value'],source),f'{root}: wrong downlink source')
        audit=next(r for r in down['quality_audit'] if r['signal']=='V_BUS02')
        require(audit['write_status']=='written' and near(audit['delivered_value'],source)
                and audit['source_time_s']==float(k),f'{root}: missing delivery audit')
        forwarded=float(event_map[k]['new_value']) if k in event_map else source
        if k in event_map:require(near(event_map[k]['old_value'],source,1e-6),f'{root}: attack source differs')
        received=None
        # Poll at k+1 reads the remote register before that cycle's downlink. It
        # corroborates reception of k's write, with a one-cycle observation lag.
        if k+1<n:
            later=read(raw/f'scada_poll_{k+1:04d}.json')['plcs']['PLC3']
            require(not later['timeout'] and later['status']=='ok',f'{root}: no actual register readback')
            received=later['md']['PLC1_V_BUS02']
            require(near(received,forwarded,2e-6),f'{root}: PLC3 readback differs at cycle {k+1}')
        control.append({'iteration':k,'source_time_s':float(k),'raw_voltage_pu':snapshot['values']['V_BUS02'],
                        'scada_owner_voltage_pu':source,'scada_written_voltage_pu':write['value'],
                        'client_confirmed_delivered_value':audit['delivered_value'],
                        'proxy_forwarded_value_pu':forwarded,'plc3_register_readback_next_cycle':received,
                        'readback_iteration':k+1 if received is not None else '',
                        'readback_source':'SCADA PLC3 md before next downlink' if received is not None else 'not observed',
                        'source_quality':audit['source_quality'],'source_freshness':audit['freshness'],
                        'network_freshness':audit['network_freshness'],'poll_used_previous':False,
                        'scada_poll_wallclock_s':float(timing[k]['poll_sec']),
                        'scada_downlink_wallclock_s':float(timing[k]['downlink_sec']),
                        'cycle_wallclock_s':float(cycle_timing[k]['cycle_total_sec']),
                        'control_load24_enable':command,'physical_effect_iteration':k+1,
                        'applied_input_effective_time_s':float(k)})
    quality=rows(root/'reports/csv/signal_quality.csv')
    require(any(r['stage']=='scada_downlink' and r['signal']=='V_BUS02' for r in quality),f'{root}: missing quality CSV')
    write_csv(root/'bus_voltage.csv',bus_rows);write_csv(root/'physical_metrics.csv',physical)
    write_csv(root/'control_trace.csv',control)
    return snapshots,actuators,control


def check(output):
    import pandapower as pp
    from src.physics.adapters.power_models import load_builtin,power_metrics
    from src.core.config import load_yaml
    ref=read(output/'native/benchmark_reference.json')
    net,names,provenance=load_builtin(load_yaml(Path(__file__).resolve().parents[1]/'examples/ieee33_bw/model.yaml'))
    require(ref['input_tables']==provenance['input_tables'] and ref['bus_id_map']==provenance['bus_id_map']
            and ref['source_file_sha256']==provenance['source_file_sha256'],'Native source metadata mismatch')
    require(ref['status']=='passed' and ref['input_tables_sha256']==provenance['input_tables_sha256'], 'Native reference provenance mismatch')
    pp.runpp(net,numba=False,tolerance_mva=1e-10)
    require(net.converged and near(net.res_bus.vm_pu.min(),ref['minimum_vm_pu']), 'Native reference not reproducible')
    for row in ref['bus_voltage']:
        index=provenance['bus_id_map'][f"BUS_{row['bus_number']:02d}"]['index']
        require(near(row['vm_pu'],net.res_bus.at[index,'vm_pu']),'Native bus voltage mismatch')
    for name,value in power_metrics(net).items():require(near(value,ref['metrics'][name]),'Native metrics not reproducible')
    require(read(output/'native/bus_id_map.json')==provenance['bus_id_map'],'Native map export mismatch')
    bus_csv=rows(output/'native/bus_voltage.csv')
    require(len(bus_csv)==33,'Native voltage CSV incomplete')
    for row,expected in zip(bus_csv,ref['bus_voltage']):
        require(int(row['bus_number'])==expected['bus_number'] and row['quality']=='good'
                and near(row['vm_pu'],expected['vm_pu']),'Native bus CSV mismatch')
    line_csv=rows(output/'native/line_results.csv')
    require(len(line_csv)==37 and len(ref['line_results'])==37,'Native line export incomplete')
    inverse={m['index']:m['paper_bus_number'] for m in provenance['bus_id_map'].values()}
    for index,export in zip(net.line.index,line_csv):
        saved=ref['line_results'][int(index)]
        require(int(export['line_index'])==int(index) and saved['line_index']==int(index), 'Native line index mismatch')
        for field in ('from_bus','to_bus'):
            require(int(export[field])==saved[field]==inverse[int(net.line.at[index,field])], 'Native line endpoints mismatch')
        require((export['in_service']=='True')==saved['in_service']==bool(net.line.at[index,'in_service']), 'Native line state mismatch')
        for field in ('p_from_mw','q_from_mvar','p_to_mw','q_to_mvar','pl_mw','ql_mvar','i_ka'):
            require(near(export[field],saved[field]) and near(saved[field],net.res_line.at[index,field]),'Native line flow mismatch')
    check_case(output/'quick',10,ref)
    net.load.at[names['load']['LOAD_BUS24'],'in_service']=False
    pp.runpp(net,numba=False,tolerance_mva=1e-10)
    require(net.converged,'Shed reference did not converge')
    off={'metrics':power_metrics(net),'bus_voltage':[
        {'bus_number':m['paper_bus_number'],'vm_pu':float(net.res_bus.at[m['index'],'vm_pu'])}
        for m in provenance['bus_id_map'].values()]}
    traces={};controls={};commands={}
    events_by_case={}
    for case in ('baseline','transparent','mitm'):
        events=[] if case=='baseline' else rows(output/case/'reports/csv/attack_events.csv')
        if case!='baseline':
            require(len(events)==21 and {int(e['iteration']) for e in events}==set(range(20,41)),f'{case}: incomplete attack window')
            bias=0 if case=='transparent' else -.10
            for e in events:
                require(e['target']=='PLC3' and e['variable']=='PLC1_V_BUS02' and e['function_code']=='16'
                        and e['direction']=='request' and near(float(e['new_value'])-float(e['old_value']),bias,2e-6),f'{case}: unexpected Modbus manipulation')
        events_by_case[case]=events
        traces[case],commands[case],controls[case]=check_case(output/case,100,ref,off,case=='mitm',events)
    require(commands['baseline']==commands['transparent'],'Transparent changed actuator sequence')
    for a,b in zip(traces['baseline'],traces['transparent']):
        require(a['values']==b['values'] and a['actuators_applied']==b['actuators_applied'] and a['diagnostics']==b['diagnostics'], 'Transparent changed physics')
    first_control=next(k for k,(a,b) in enumerate(zip(commands['baseline'],commands['mitm'])) if a!=b)
    first_physics=next(k for k,(a,b) in enumerate(zip(traces['baseline'],traces['mitm'])) if a['values']!=b['values'])
    require(first_control==20 and first_physics==21,'Wrong k -> u_k -> x_(k+1) propagation')
    # Each returned quasi-static result describes the command applied throughout
    # [input_effective_time_s, granted_time_s]. The terminal result covers the
    # final interval; the initialization sample covers no time interval.
    energy=sum(s['values']['UNSERVED_LOAD_P']*(s['sim_time']-s['time_coordination']['input_effective_time_s'])
               for s in traces['mitm'][1:])/3600.
    comparison=[]
    for base,attack in zip(traces['baseline'],traces['mitm']):
        comparison.append({'iteration':base['iteration'],'sim_time_s':base['sim_time'],
            **{f'delta_{v}_pu':attack['values'][v]-base['values'][v] for v in ('V_BUS02','V_BUS18','V_BUS24','V_BUS33')},
            'delta_p_loss_mw':attack['values']['TOTAL_P_LOSS']-base['values']['TOTAL_P_LOSS'],
            'unserved_load_mw':attack['values']['UNSERVED_LOAD_P']})
    write_csv(output/'comparison.csv',comparison)
    return {'status':'passed','execution':'real OpenPLC + namespace + ns-3 + Modbus + pandapower',
            'cycles':{'quick':10,'baseline':100,'transparent':100,'mitm':100},'transparent_equals_baseline':True,
            'first_network_modification_iteration':20,'first_control_deviation_iteration':first_control,
            'first_physical_deviation_iteration':first_physics,'modified_modbus_frames':21,
            'plc3_readback_evidence':'cycle 21 poll reads cycle 20 biased write before next downlink',
            'baseline_p_load24_mw':.420,'attacked_p_load24_mw':0.,'unserved_mwh_0_to_100s':energy,
            'energy_integration':'quasi-static interval power from returned solve, integrated over input_effective_time_s to granted time',
            'baseline_metrics':ref['metrics'],'shed_metrics':off['metrics'],
            'interpretation':'single deterministic experiment; no statistical probability estimate'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,default=Path('output/ieee33_bw'));args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    try:result=check(args.output)
    except FileNotFoundError as exc:
        state='failed' if any((args.output/c).exists() for c in ('quick','baseline','transparent','mitm')) else 'not_run'
        result={'status':state,'reason':str(exc)}
    except Exception as exc:result={'status':'failed','reason':str(exc)}
    (args.output/'validation.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps(result,indent=2))
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
