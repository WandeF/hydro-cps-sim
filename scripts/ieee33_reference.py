#!/usr/bin/env python3
"""Rootless, unmodified case33bw reference, with complete input provenance."""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
import platform
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.physics.adapters.power_models import load_builtin, power_metrics
from src.core.config import load_yaml


def write_csv(path, rows):
    with path.open('w', newline='') as f:
        writer=csv.DictWriter(f, fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)


def reference(output):
    import pandapower as pp
    import numpy, scipy, pandas
    model=load_yaml(Path(__file__).resolve().parents[1]/'examples/ieee33_bw/model.yaml')
    net,names,provenance=load_builtin(model)
    pp.runpp(net, numba=False, algorithm='nr', max_iteration=20, tolerance_mva=1e-10)
    if not net.converged:raise ValueError('Native solve did not converge')
    if abs(float(net.load.p_mw.sum())-3.715)>=1e-12:raise ValueError('Unexpected active demand')
    if abs(float(net.load.q_mvar.sum())-2.3)>=1e-12:raise ValueError('Unexpected reactive demand')
    metrics=power_metrics(net)
    minimum=float(net.res_bus.vm_pu.min())
    if abs(minimum-.9131)>=5e-5:raise ValueError('Native voltage outside comparison tolerance')
    if abs(metrics['p_loss_mw']-.20266)>=5e-5:raise ValueError('Native loss outside comparison tolerance')
    v02=float(net.res_bus.at[names['bus']['BUS_02'],'vm_pu'])
    if not (.95<v02<1.03 and v02-.10<.95):raise ValueError('Control threshold precheck failed')
    output.mkdir(parents=True,exist_ok=True)
    buses=[{'iteration':0,'sim_time_s':0.,'bus_number':m['paper_bus_number'],
            'vm_pu':float(net.res_bus.at[m['index'],'vm_pu']),'quality':'good'}
           for m in provenance['bus_id_map'].values()]
    inverse={m['index']:m['paper_bus_number'] for m in provenance['bus_id_map'].values()}
    lines=[{'line_index':int(i),'from_bus':inverse[int(r.from_bus)],'to_bus':inverse[int(r.to_bus)],
            'in_service':bool(r.in_service),**{c:float(net.res_line.at[i,c])
                for c in ('p_from_mw','q_from_mvar','p_to_mw','q_to_mvar','pl_mw','ql_mvar','i_ka')}}
           for i,r in net.line.iterrows()]
    result={**provenance,'status':'passed','execution':'native AC power flow; no PLC/network',
            'python':platform.python_version(), 'numpy':numpy.__version__,'scipy':scipy.__version__,
            'pandas':pandas.__version__,'solver':{'algorithm':'nr','numba':False,'max_iteration':20,'tolerance_mva':1e-10},
            'counts':{t:len(net[t]) for t in ('bus','line','load','ext_grid','switch')},
            'active_lines':int(net.line.in_service.sum()),'total_load_mw':float(net.load.p_mw.sum()),
            'total_load_mvar':float(net.load.q_mvar.sum()),'minimum_vm_pu':minimum,
            'minimum_bus_number':inverse[int(net.res_bus.vm_pu.idxmin())],
            'metrics':metrics,'solver_converged':bool(net.converged),'bus_voltage':buses,'line_results':lines,
            'threshold_precheck':{'V_BUS02':v02,'bias_pu':-.10,'biased_voltage':v02-.10,'trip_below':.95,'restore_above':1.03},
            'literature_comparison':{'vm_pu':.9131,'loss_mw':.20266,'voltage_abs_tolerance':5e-5,'loss_abs_tolerance_mw':5e-5,
                                     'loss_difference_kw':1000*(metrics['p_loss_mw']-.20266)}}
    (output/'benchmark_reference.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    (output/'bus_id_map.json').write_text(json.dumps(provenance['bus_id_map'],indent=2)+'\n')
    write_csv(output/'bus_voltage.csv',buses);write_csv(output/'line_results.csv',lines)
    print(json.dumps({k:result[k] for k in ('status','counts','minimum_vm_pu','metrics','threshold_precheck')},indent=2))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('output/ieee33_bw/native'))
    reference(parser.parse_args().output)
