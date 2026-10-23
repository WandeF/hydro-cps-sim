#!/usr/bin/env python3
"""Export reproducible paper figures from the validated AC snapshot evidence."""
import argparse
import csv
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def rows(path):
    with path.open() as f:return list(csv.DictReader(f))


def plot(root):
    ref=json.loads((root/'native/benchmark_reference.json').read_text())
    validation=json.loads((root/'validation.json').read_text())
    if validation['status']!='passed':raise ValueError('Validate experiments before plotting')
    dest=root/'figures';dest.mkdir(exist_ok=True)
    plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False})
    def save(name):
        plt.savefig(dest/f'{name}.svg',bbox_inches='tight')
        plt.savefig(dest/f'{name}.png',bbox_inches='tight',dpi=180)
        plt.close()
    fig,ax=plt.subplots(figsize=(8,3))
    ax.plot([r['bus_number'] for r in ref['bus_voltage']],[r['vm_pu'] for r in ref['bus_voltage']],'.-')
    ax.set(xlabel='Baran-Wu bus number',ylabel='Voltage (pu)',title='Original case33bw: AC quasi-steady power flow')
    ax.grid(alpha=.2);save('voltage_profile')
    # Electrical topology from the actual source model, including all five ties.
    import networkx as nx
    graph=nx.Graph();graph.add_nodes_from(range(1,34))
    graph.add_edges_from((r['from_bus'],r['to_bus']) for r in ref['line_results'] if r['in_service'])
    position=nx.spring_layout(graph,seed=33)
    fig,ax=plt.subplots(figsize=(10,5))
    nx.draw_networkx_nodes(graph,position,node_size=240,node_color=['#d8734d' if b==24 else '#c9dfeb' for b in graph],ax=ax)
    nx.draw_networkx_labels(graph,position,font_size=8,ax=ax)
    nx.draw_networkx_edges(graph,position,ax=ax)
    ties=[(r['from_bus'],r['to_bus']) for r in ref['line_results'] if not r['in_service']]
    nx.draw_networkx_edges(graph,position,edgelist=ties,style='dashed',edge_color='#999999',ax=ax)
    ax.set_title('Electrical topology: solid = 32 active branches; dashed = 5 open ties')
    ax.axis('off');save('electrical_topology')
    baseline=rows(root/'baseline/control_trace.csv');attack=rows(root/'mitm/control_trace.csv')
    bm=rows(root/'baseline/physical_metrics.csv');am=rows(root/'mitm/physical_metrics.csv')
    fig,axes=plt.subplots(4,1,sharex=True,figsize=(9,9))
    t=[int(r['iteration']) for r in attack]
    axes[0].plot(t,[float(r['raw_voltage_pu']) for r in attack],label='Raw V_BUS02')
    axes[0].plot([int(r['iteration']) for r in attack if r['plc3_register_readback_next_cycle']],
                 [float(r['plc3_register_readback_next_cycle']) for r in attack if r['plc3_register_readback_next_cycle']],
                 label='PLC3 readback (next poll; aligned to source cycle)')
    axes[0].axhline(.95,color='#888',linestyle=':',label='Experimental trip threshold')
    axes[0].set_ylabel('Voltage (pu)');axes[0].legend(fontsize=8)
    for data,name in ((baseline,'Baseline'),(attack,'MITM')):
        axes[1].step([int(r['iteration']) for r in data],[r['control_load24_enable']=='True' for r in data],where='post',label=name)
    axes[1].set_ylabel('Load24 enable');axes[1].legend(fontsize=8)
    for data,name in ((bm,'Baseline'),(am,'MITM')):
        axes[2].step([float(r['sim_time_s']) for r in data],[float(r['unserved_load_mw']) for r in data],where='post',label=name)
        axes[3].plot([float(r['sim_time_s']) for r in data],[1000*float(r['p_loss_mw']) for r in data],label=name)
    axes[2].set_ylabel('Unserved (MW)');axes[3].set_ylabel('Line loss (kW)')
    axes[3].set_xlabel('Simulation time (s) / control iteration (1 s interval)')
    for ax in axes:
        ax.axvspan(20,41,color='#e4b09f',alpha=.2);ax.grid(alpha=.2)
    axes[0].set_title('AC quasi-steady snapshots: MITM 20–40; command 20; physical effect 21')
    fig.tight_layout();save('cross_layer_timeline')
    return dest


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,default=Path('output/ieee33_bw'))
    print(plot(p.parse_args().output))
