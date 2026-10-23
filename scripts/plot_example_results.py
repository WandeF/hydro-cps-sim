#!/usr/bin/env python3
"""Export power-feeder and thermal-box figures from existing real-loop evidence."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Rectangle
import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_power_results import check as check_power
from scripts.check_upsi_results import completed

BLUE, ORANGE, GREY = '#28648d', '#c66b37', '#646b71'
CASES = {'baseline': (BLUE, '-', 'Baseline'),
         'transparent': (GREY, ':', 'Transparent'),
         'mitm': (ORANGE, '--', 'MITM')}


class Evidence:
    def __init__(self, root):
        self.root = root.resolve()
        self.sources = set()
        self.dest = self.root/'figures'
        self.dest.mkdir(parents=True, exist_ok=True)
        self.figures = []

    def json(self, path):
        self.sources.add(path.resolve())
        return json.loads(path.read_text())

    def csv(self, path):
        self.sources.add(path.resolve())
        with path.open() as stream:
            return list(csv.DictReader(stream))

    def yaml(self, path):
        self.sources.add(path.resolve())
        return yaml.safe_load(path.read_text())

    def save(self, name):
        for suffix in ('png', 'svg', 'pdf'):
            plt.savefig(self.dest/f'{name}.{suffix}', bbox_inches='tight', dpi=180)
        self.figures.append(name)
        plt.close()

    def table(self, name, rows):
        with (self.dest/name).open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def finish(self, details):
        manifest = {'status': 'passed', 'figures': self.figures, **details,
                    'sources': [{'path': str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p),
                                 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                                for p in sorted(self.sources)]}
        (self.dest/'manifest.json').write_text(json.dumps(manifest, indent=2, allow_nan=False)+'\n')
        print(self.dest)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def close(a, b, tol=1e-6):
    return math.isfinite(float(a)) and math.isfinite(float(b)) and abs(float(a)-float(b)) <= tol


def decorate(axes, end, attack=None):
    for ax in np.atleast_1d(axes):
        ax.grid(alpha=.18)
        ax.set_xlim(0, end)
        ax.set_xticks(range(int(end)+1))
        if attack:
            ax.axvspan(*attack, color=ORANGE, alpha=.10)
    np.atleast_1d(axes)[-1].set_xlabel('Simulation time (s) / iteration (1 s interval)')


def control_trace(evidence, case, signal, actuator):
    raw = evidence.root/case/'runtime/json' if case else evidence.root/'runtime/json'
    reports = raw.parent.parent/'reports/csv'
    state = evidence.csv(reports/'physics_state.csv')
    commands = evidence.csv(reports/'actuator_state.csv')
    require(len(state) == len(commands)+1, 'Incomplete physical/command trace')
    trace = []
    for k, command in enumerate(commands):
        require(int(command['iteration']) == k, 'Missing command iteration')
        physical = evidence.json(raw/f'physics_{k:04d}.json')
        require(physical['iteration'] == k and physical['sim_time'] == k, 'Wrong physical clock')
        require(close(physical['values'][signal], state[k][signal], 1e-10), 'CSV/raw mismatch')
        poll = evidence.json(raw/f'scada_poll_{k:04d}.json')
        owner, target = poll['plcs']['PLC1'], poll['plcs']['PLC2']
        require(all(p['status'] == 'ok' and not p.get('timeout') and not p.get('used_previous')
                    for p in (owner, target)), 'Poll timeout or cached data')
        variable = f'PLC1_{signal}'
        source = owner['md'][variable]  # Do not use the flattened remote-cache dictionary.
        require(close(source, physical['values'][signal]), 'Owner register differs from physics')
        down = evidence.json(raw/f'scada_downlink_{k:04d}.json')['plcs']['PLC2']
        audit = next(r for r in down['quality_audit'] if r['signal'] == signal)
        require(audit['write_status'] == 'written' and close(audit['delivered_value'], source), 'Missing confirmed write')
        require(audit['source_time_s'] == k, 'Unexpected source time')
        received = None
        if k+1 < len(commands):
            later = evidence.json(raw/f'scada_poll_{k+1:04d}.json')['plcs']['PLC2']
            require(later['status'] == 'ok' and not later.get('timeout'), 'Missing register readback')
            received = later['md'][variable]
        coil = evidence.json(raw/f'actuators_{k:04d}_plc2.json')['tags'][actuator]
        require(type(coil) is bool and (command[actuator] == 'True') == coil, 'Coil/command mismatch')
        next_state = evidence.json(raw/f'physics_{k+1:04d}.json')
        require(next_state['actuators_applied'][actuator] == coil and next_state['sim_time'] == k+1,
                'Command/next-physical-state misalignment')
        trace.append({'iteration': k, 'source_time_s': audit['source_time_s'],
                      'raw_value': physical['values'][signal], 'scada_owner_value': source,
                      'confirmed_write_value': audit['delivered_value'],
                      'plc2_readback_next_cycle': received,
                      'readback_iteration': k+1 if received is not None else '',
                      'command': coil, 'next_physics_iteration': k+1,
                      'source_quality': audit['source_quality'], 'freshness': audit['freshness']})
    return state, commands, trace


def plot_power(root):
    validation = check_power(root)  # Re-check actual network stages, attack frames and trajectories.
    e = Evidence(root)
    for case in CASES:
        e.sources.update({(root/case/'timing/run_all_timing.csv').resolve(),
                          (root/case/'logs/ns3_network.log').resolve()})
    model = e.yaml(ROOT/'examples/power_feeder/model.yaml')
    config = e.yaml(ROOT/'examples/power_feeder/config_mitm.yaml')
    scenario = config['attacks']['scenarios'][0]
    window = (scenario['trigger']['start_iteration'], scenario['trigger']['end_iteration']+1)
    bias = scenario['rules'][0]['value']
    data, commands, traces = {}, {}, {}
    combined = []
    for case in CASES:
        data[case], commands[case], traces[case] = control_trace(e, case, 'V_CONTROL', 'LOAD_SW')
        events = [] if case == 'baseline' else e.csv(root/case/'reports/csv/attack_events.csv')
        events = {int(row['iteration']): row for row in events}
        for row in traces[case]:
            k = row['iteration']
            delta = bias if case == 'mitm' and window[0] <= k < window[1] else 0.
            if k in events:
                require(close(events[k]['old_value'], row['scada_owner_value']), 'Event/source mismatch')
                require(close(float(events[k]['new_value'])-float(events[k]['old_value']), delta), 'Event offset mismatch')
            if row['plc2_readback_next_cycle'] is not None:
                require(close(row['plc2_readback_next_cycle'], row['scada_owner_value']+delta), 'Register/forwarded-value mismatch')
            row['proxy_delta_pu'] = delta
            combined.append({'case': case, **row})
        for row in data[case]:
            snapshot = e.json(root/case/'runtime/json'/f"physics_{int(row['iteration']):04d}.json")
            if float(row['V_LOAD']) == 0:
                require(snapshot['quality']['V_LOAD'] == 'unsupplied', 'Unmarked zero voltage')
    e.table('control_trace.csv', combined)
    e.table('state_comparison.csv', [{'case': c, **r} for c in CASES for r in data[c]])

    fig, ax = plt.subplots(figsize=(10, 3.8))
    positions = {name: (i*2.6, 1.7) for i, name in enumerate(b['name'] for b in model['buses'])}
    for line in model['lines']:
        x1, y1 = positions[line['from_bus']]; x2, y2 = positions[line['to_bus']]
        ax.plot([x1, x2], [y1, y2], color=GREY, zorder=1)
        ax.text((x1+x2)/2, y1+.22, f"{line['name']}\n{line['length_km']:g} km", ha='center', fontsize=9)
    for bus in model['buses']:
        x, y = positions[bus['name']]
        ax.plot(x, y, 'o', color=BLUE, markersize=12)
        ax.text(x, y-.32, bus['name'], ha='center')
    for load in model['loads']:
        x, y = positions[load['bus']]
        ax.annotate('', xy=(x, .55), xytext=(x, y-.6), arrowprops={'arrowstyle': '->', 'color': GREY})
        ax.text(x, .35, f"{load['name']}\n{load['p_mw']:g} MW / {load['q_mvar']:g} MVAr", ha='center', va='top', fontsize=9)
    ax.text(-.15, 2.7, f"Utility {model['ext_grids'][0]['vm_pu']:g} pu; 20 kV", ha='left')
    ax.text(6.5, 2.7, 'LOAD_SW: true = connected', ha='center')
    ax.plot([6.5], [1.7], marker='s', markersize=8, color=ORANGE)
    ax.set(xlim=(-.8, 8.7), ylim=(-.5, 3.4), title='Power feeder: electrical topology and original loads')
    ax.axis('off');e.save('electrical_topology')

    fig, axes = plt.subplots(4, 1, sharex=True, figsize=(9, 9), layout='constrained')
    for case, (color, style, label) in CASES.items():
        t = [float(r['sim_time']) for r in data[case]]
        for ax, field in zip(axes, ('V_CONTROL', 'V_LOAD', 'P_LOAD', 'LINE_LOADING')):
            ax.step(t, [float(r[field]) for r in data[case]], where='post', color=color, linestyle=style, label=label)
    for ax, label in zip(axes, ('Control voltage (pu)', 'Load voltage (pu)', 'Flexible load (MW)', 'Feeder loading (%)')):
        ax.set_ylabel(label)
    axes[0].set_title('Power feeder: baseline / transparent / MITM — AC quasi-steady snapshots')
    axes[0].legend(ncols=3, fontsize=9)
    axes[1].annotate('0 pu: unsupplied bus', xy=(4, 0), xytext=(5, .35), arrowprops={'arrowstyle': '->', 'color': GREY}, fontsize=9)
    decorate(axes, 10, window);e.save('state_comparison')

    fig, axes = plt.subplots(4, 1, sharex=True, figsize=(9, 9), layout='constrained')
    tr = traces['mitm']
    axes[0].step([float(r['sim_time']) for r in data['mitm']],
                 [float(r['V_CONTROL']) for r in data['mitm']], where='post', color=BLUE, label='Raw control voltage')
    observed = [r for r in tr if r['plc2_readback_next_cycle'] is not None]
    axes[0].step([r['iteration'] for r in observed], [r['plc2_readback_next_cycle'] for r in observed],
                 '--o', where='post', color=ORANGE, markersize=3, label='PLC2 readback (next poll, aligned to source cycle)')
    trip = next(r['value'] for r in config['plcs'][1]['controls'] if r['type'] == 'below')
    axes[0].axhline(trip, color=GREY, linestyle=':', label=f'Trip below {trip:g} pu')
    axes[0].legend(fontsize=8);axes[0].set_ylabel('Voltage (pu)')
    for case in ('baseline', 'mitm'):
        color, style, label = CASES[case]
        c = commands[case]
        axes[1].step(list(range(len(c)+1)), [r['LOAD_SW']=='True' for r in c]+[c[-1]['LOAD_SW']=='True'],
                     where='post', color=color, linestyle=style, label=label)
        t = [float(r['sim_time']) for r in data[case]]
        axes[2].step(t, [float(r['P_LOAD']) for r in data[case]], where='post', color=color, linestyle=style)
        axes[3].step(t, [float(r['P_GRID']) for r in data[case]], where='post', color=color, linestyle=style)
    axes[1].set_ylabel('Breaker command');axes[1].set_yticks([0, 1], ['Open', 'Closed']);axes[1].legend(fontsize=8)
    axes[2].set_ylabel('Flexible load (MW)');axes[3].set_ylabel('Grid power (MW)')
    axes[0].set_title(f"MITM cycles {window[0]}–{window[1]-1}; command {validation['first_control_mismatch']}; physical snapshot {validation['first_physical_mismatch']}")
    decorate(axes, 10, window);e.save('cross_layer_timeline')
    e.finish({'example': 'power_feeder', 'validation': validation,
              'timing': 'Readback at k+1 before downlink is aligned to write k; final readback is unobserved. Power curves show returned snapshots.',
              'quality': 'Load-bus zero voltage is explicitly unsupplied.'})


def plot_thermal(root):
    completed(root)
    e = Evidence(root)
    e.sources.update({(root/'timing/run_all_timing.csv').resolve(),
                      (root/'logs/ns3_network.log').resolve()})
    model = e.yaml(ROOT/'examples/thermal_box/model.yaml')
    config = e.yaml(ROOT/'examples/thermal_box/config.yaml')
    state, commands, trace = control_trace(e, '', 'TEMPERATURE', 'HEATER')
    require(len(state) == 11 and len(commands) == 10, 'Expected the completed ten-cycle thermal experiment')
    ambient, loss, gain = (float(model[k]) for k in ('ambient_c', 'loss_per_s', 'heater_c_per_s'))
    heater_w = float(model['heater_w'])
    rules = config['plcs'][1]['controls']
    lower = next(r['value'] for r in rules if r['type'] == 'below')
    upper = next(r['value'] for r in rules if r['type'] == 'above')
    temperatures = [float(r['TEMPERATURE']) for r in state]
    require(close(temperatures[0], model['initial_c'], 1e-10), 'Initial temperature/model mismatch')
    dense_t, dense_temperature = [], []
    errors = [0.]
    previous = bool(config['actuators'][0]['initial_state'])
    for k, row in enumerate(trace):
        require(close(row['scada_owner_value'], row['plc2_readback_next_cycle'])
                if row['plc2_readback_next_cycle'] is not None else True, 'Thermal register readback mismatch')
        previous = True if temperatures[k] < lower else False if temperatures[k] > upper else previous
        require(row['command'] == previous, 'Actual coil does not match hysteresis rule')
        require(close(state[k+1]['HEATER_POWER'], heater_w*row['command'], 1e-10), 'Power/coil mismatch')
        start, end = float(state[k+1]['sim_time_start']), float(state[k+1]['sim_time_end'])
        require(start == k and end == k+1, 'Wrong thermal command interval')
        equilibrium = ambient+gain*row['command']/loss
        t = np.linspace(start, end, 51)
        analytic = equilibrium+(temperatures[k]-equilibrium)*np.exp(-loss*(t-start))
        dense_t.extend(t);dense_temperature.extend(analytic)
        errors.append(temperatures[k+1]-float(analytic[-1]))
    require(max(abs(x) for x in errors) < 1e-10, 'Thermal result differs from analytical ODE')
    e.table('control_trace.csv', trace)
    e.table('temperature_trace.csv', [{**r, 'analytic_step_error_c': err} for r, err in zip(state, errors)])

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.add_patch(Rectangle((3.5, 1.0), 3.0, 1.9, facecolor='#edf3f6', edgecolor=BLUE, linewidth=1.5))
    ax.text(5, 2.25, 'Thermal box', ha='center', fontsize=14)
    ax.text(5, 1.65, f"T(0) = {model['initial_c']:g} °C", ha='center')
    ax.add_patch(FancyArrowPatch((.8, 2), (3.5, 2), arrowstyle='->', mutation_scale=15, color=ORANGE))
    ax.text(2.0, 2.25, f'Heater {heater_w:g} W × u', ha='center')
    ax.add_patch(FancyArrowPatch((6.5, 2), (9, 2), arrowstyle='->', mutation_scale=15, color=GREY))
    ax.text(8, 2.25, f'Ambient {ambient:g} °C', ha='center')
    ax.text(5, .35, f'dT/dt = −{loss:g}(T − {ambient:g}) + {gain:g}u   [°C/s]', ha='center', fontsize=12)
    ax.text(5, -.2, f'PLC2: ON below {lower:g} °C; OFF above {upper:g} °C; hold in between', ha='center')
    ax.set(xlim=(0, 10), ylim=(-.6, 3.6), title='Thermal box: first-order heat balance and PLC hysteresis')
    ax.axis('off');e.save('model_diagram')

    fig, axes = plt.subplots(2, 1, sharex=True, figsize=(9, 6), layout='constrained')
    axes[0].plot(dense_t, dense_temperature, color=BLUE, label='Analytical ODE using recorded PLC commands')
    axes[0].plot(range(11), temperatures, 'o', color=ORANGE, markersize=5, label='Real-loop physical snapshots')
    for threshold, label in ((lower, 'Heating ON below'), (upper, 'Heating OFF above')):
        axes[0].axhline(threshold, color=GREY, linestyle=':', label=f'{label} {threshold:g} °C')
    axes[0].set_ylabel('Temperature (°C)');axes[0].legend(fontsize=8)
    u = [r['command'] for r in trace]
    axes[1].step(range(11), [heater_w*x for x in u]+[heater_w*u[-1]], where='post', color=ORANGE)
    axes[1].set_ylabel('Applied heater power (W)');axes[1].set_ylim(-40, heater_w*1.15)
    axes[0].set_title('Thermal box: temperature response and applied heating intervals')
    decorate(axes, 10);e.save('temperature_response')

    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(9, 8), layout='constrained')
    axes[0].plot(range(11), temperatures, color=BLUE, label='Raw temperature')
    observed = [r for r in trace if r['plc2_readback_next_cycle'] is not None]
    axes[0].plot([r['iteration'] for r in observed], [r['plc2_readback_next_cycle'] for r in observed],
                 'o', color=ORANGE, markersize=4, fillstyle='none', label='PLC2 register readback (next poll, aligned to source cycle)')
    axes[0].axhline(upper, color=GREY, linestyle=':', label=f'OFF above {upper:g} °C')
    axes[0].set_ylabel('Temperature (°C)');axes[0].legend(fontsize=8)
    axes[1].step(range(11), u+[u[-1]], where='post', color=ORANGE)
    axes[1].set_ylabel('Heater command');axes[1].set_yticks([0, 1], ['OFF', 'ON']);axes[1].set_ylim(-.15, 1.2)
    axes[2].step(range(11), [float(r['HEATER_POWER']) for r in state], 'o-', where='post', color=BLUE)
    axes[2].set_ylabel('Returned heater power (W)');axes[2].set_ylim(-40, heater_w*1.15)
    axes[2].text(.02, .90, 'Result k+1 reports power for command interval [k, k+1].', transform=axes[2].transAxes, fontsize=9)
    switch = next(k for k in range(1, len(u)) if u[k-1] and not u[k])
    axes[0].set_title(f'Thermal control: OFF command at {switch}; first cooling snapshot at {switch+1}')
    for ax in axes:
        ax.axvline(switch, color=GREY, linestyle='--', alpha=.6)
    decorate(axes, 10);e.save('control_timeline')
    e.finish({'example': 'thermal_box', 'cycles': len(commands), 'physical_snapshots': len(state),
              'max_analytic_step_error_c': max(abs(x) for x in errors),
              'first_off_command': switch, 'first_cooling_snapshot': switch+1,
              'timing': 'Applied power uses [k,k+1] command intervals; returned heater power belongs to result k+1. Readbacks aligned to prior write.'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--example', choices=('all', 'power_feeder', 'thermal_box'), default='all')
    parser.add_argument('--power-output', type=Path, default=ROOT/'examples/power_feeder/output')
    parser.add_argument('--thermal-output', type=Path, default=ROOT/'examples/thermal_box/output/baseline')
    args = parser.parse_args()
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False,
                         'svg.fonttype': 'none', 'pdf.fonttype': 42, 'axes.titlepad': 12})
    if args.example in ('all', 'power_feeder'):
        plot_power(args.power_output.resolve())
    if args.example in ('all', 'thermal_box'):
        plot_thermal(args.thermal_output.resolve())


if __name__ == '__main__':
    main()
