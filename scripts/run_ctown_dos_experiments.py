#!/usr/bin/env python3
"""Run the prepared C-Town PLC7 DoS matrix, or validate it without running."""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import yaml

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from src.control.st_generation import generate_and_validate
from src.core.config import load_runtime_config
from src.network.network_sh_generation import generate_network_sh
from src.network.ns3_generation import generate_cc
from src.experiment.runner import experiment_completed
from src.check.delivered_inputs import verify_delivered_inputs


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def rows(path):
    with Path(path).open(newline='') as handle:
        return list(csv.DictReader(handle))


def launch_logged_process(command, *, cwd, env, log):
    # Keep the controlling terminal/session used by the parent's sudo -v.
    # A separate process group still permits timeout cleanup via killpg.
    # This runner is single-threaded; setpgrp supports its Python 3.10 runtime.
    return subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                            stderr=subprocess.STDOUT, preexec_fn=os.setpgrp)


def validate(archive, plan):
    source = yaml.safe_load((archive / 'source/config_C_Town.yaml').read_text())
    assert digest(archive / 'source/config_C_Town.yaml') == plan['source_config_sha256']
    assert digest(archive / 'source/ctown_map.inp') == plan['inp_sha256']
    for spec in plan['experiments']:
        config = Path(spec['config'])
        assert digest(config) == spec['config_sha256'], f'Configuration changed: {config}'
        cfg = yaml.safe_load(config.read_text())
        assert cfg['plcs'] == source['plcs']
        for key in ('actuators', 'initial_tank_values', 'physics', 'time', 'noise_scale'):
            assert cfg[key] == source[key], key
        assert Path(cfg['inp_file']) == archive / 'source/ctown_map.inp'
        assert Path(cfg['output_path']) == Path(spec['output'])
        assert cfg['iterations'] == 80
        plc = {p['name']: p for p in cfg['plcs']}
        assert set(plc) == {f'PLC{i}' for i in range(1, 9)}
        assert 'T4' in plc['PLC7']['sensors']
        assert plc['PLC5']['actuators'] == ['PU6', 'PU7']
        assert {c['dependant'] for c in plc['PLC5']['controls']} == {'T4'}
        net = cfg['network']
        for link in net['backbone_links']:
            target = link['name'] == 'r0-r7'
            assert link['data_rate'] == ('10Mbps' if target else '100Mbps')
            assert link['queue']['max_packets'] == (20 if target else 100)
            assert link['delay'] == '2ms' and link.get('error_model') is None
        assert all(lan['data_rate'] == '100Mbps' for lan in net['lans'])
        lan = next(lan for lan in net['lans'] if lan['name'] == 'plc7_lan')
        assert set(lan['members']) == {'r7', 's7', 'PLC7'}
        assert lan['interfaces']['PLC7']['ip'] == '192.168.7.1/24'
        rho = spec['value']
        attacks = cfg['attacks']
        assert attacks['enabled'] == (rho > 0)
        assert len(attacks['scenarios']) == 3
        total = 0.0
        for scenario in attacks['scenarios']:
            assert scenario['enabled'] == (rho > 0)
            assert scenario['type'] == 'udp_dos'
            assert scenario['target'] == dict(endpoint='PLC7', namespace='ns-plc7',
                                              ip='192.168.7.1', protocol='udp', port=502)
            assert scenario['trigger'] == dict(type='iteration_window', start_iteration=15, end_iteration=55)
            assert scenario['traffic']['packet_size'] == 1400
            if rho:
                total += float(scenario['traffic']['rate'].removesuffix('Mbps'))
        assert abs(total - 10 * rho) < 1e-5
        # Generate in a temporary directory: dry-run never creates runtime results.
        with tempfile.TemporaryDirectory(prefix='ctown-dos-validate-') as directory:
            temp = Path(directory)
            cfg['output_path'] = str(temp / 'output')
            temporary_config = temp / 'config.yaml'
            temporary_config.write_text(yaml.safe_dump(cfg, sort_keys=False))
            st = generate_and_validate(temporary_config)
            assert st['ok'], st['errors']
            runtime = load_runtime_config(temporary_config)
            assert 'PLC7_T4' in runtime.plcs['PLC5'].md_vars
            assert {'PLC5_PU6', 'PLC5_PU7'} <= runtime.plcs['PLC5'].coil_vars.keys()
            network_sh = temp / 'network.sh'
            network_sh.write_text(generate_network_sh(cfg))
            subprocess.run(['bash', '-n', str(network_sh)], check=True)
            cc = generate_cc(cfg, temp / 'output')
            assert 'r0-r7' in cc and '192.168.7.1' in network_sh.read_text()
        print(f"[VALIDATE] {spec['id']} rho={rho:g} PLC7/T4 -> SCADA -> PLC5/PU6,PU7", flush=True)


def check_result(spec):
    output = Path(spec['output'])
    try:
        check = json.loads((output / 'check/check_summary.json').read_text())
        physical = rows(output / 'reports/csv/physics.csv')
        complete = {int(r['iteration']) for r in physical} == set(range(81))
        attack_rows = rows(output / 'reports/csv/attack_events.csv')
        starts = {r['source'] for r in attack_rows if r['event'] == 'dos_start' and r['target'] == 'PLC7'}
        traffic = {r['source'] for r in attack_rows if r['event'] == 'attack_packet_sent' and r['target'] == 'PLC7'}
        attack_ok = (starts == traffic == {'ns_bot1', 'ns_bot2', 'ns_bot3'}) if spec['value'] else not any(
            r['event'] in {'dos_start', 'attack_packet_sent'} for r in attack_rows)
        check_ok = check.get('ok') is True
        if spec['value']:
            delivered = verify_delivered_inputs(Path(spec['config']))
            check_ok = check.get('open_loop', {}).get('ok') is True and delivered['ok']
        return dict(simulation_end=experiment_completed(output), check_ok=check_ok,
                    complete_physics=complete, attack_sources_ok=bool(attack_ok))
    except (OSError, ValueError, KeyError, csv.Error):
        return dict(simulation_end=False, check_ok=False, complete_physics=False, attack_sources_ok=False)


def accepted_execution(spec, record, checks):
    if record.get('timed_out') or record.get('status') == 'interrupted' or not all(checks.values()):
        return False
    if record.get('returncode') == 0:
        return True
    # run_all reports exit 2 for a nominal-control mismatch. Accept that code
    # only when actual delivered-input execution AND hydraulic replay passed.
    if spec['value'] and record.get('returncode') == 2:
        summary = json.loads((Path(spec['output']) / 'check/check_summary.json').read_text())
        return (summary.get('ok') is False and summary.get('actuator', {}).get('ok') is False
                and summary.get('open_loop', {}).get('ok') is True)
    return False


def recheck_completed(archive, plan, records):
    for spec, record in zip(plan['experiments'], records):
        assert spec['id'] == record['id']
        if record.get('returncode') is None:
            continue
        checks = check_result(spec)
        valid = accepted_execution(spec, record, checks)
        record.update(validation=checks, valid=valid)
        if valid:
            record.update(status='valid', acceptance_policy='hydraulic replay plus recorded delivered inputs for DoS; strict nominal check for baseline')
        elif checks['simulation_end'] and checks['complete_physics']:
            record['status'] = 'review_required'
        print(f"[RECHECK] {spec['id']} {record['status']} {checks}", flush=True)
    update_indices(archive, records)


def update_indices(archive, records):
    # The archive-local RUN_INDEX is authoritative and must always be updated.
    write_json(archive / 'RUN_INDEX.json', records)
    by_uid = {r['uid']: r for r in records}

    # Optional parent-level RUN_INDEX: update it only when this installation
    # actually has a global catalog next to the archive directory.
    path = archive.parent / 'RUN_INDEX.json'
    if path.exists():
        current = json.loads(path.read_text())
        write_json(path, [by_uid.get(r.get('uid'), r) for r in current])

    # The archive-local experiment plan is required. A parent-level plan is
    # optional because this DoS matrix may live in .../05_dos_intensity/DoS/.
    for path in (archive / 'EXPERIMENT_PLAN.json',
                 archive.parent / 'EXPERIMENT_PLAN.json'):
        if not path.exists():
            continue
        current = json.loads(path.read_text())
        for spec in current['experiments']:
            record = by_uid.get(spec.get('uid'))
            if record is not None:
                spec['status'] = record.get('status', spec.get('status'))
                spec['valid'] = record.get('valid', spec.get('valid', False))
                spec['has_output'] = record.get('has_output', spec.get('has_output', False))
                spec['has_log'] = record.get('has_log', spec.get('has_log', False))
                spec['attempt'] = record.get('attempt', spec.get('attempt', 0))
        write_json(path, current)

    # CONFIG_INDEX.csv belongs to the wider output catalog and is optional.
    path = archive.parent.parent / 'CONFIG_INDEX.csv'
    if not path.exists():
        return

    table = rows(path)
    if not table:
        return

    for row in table:
        record = by_uid.get(row.get('uid'))
        if record is not None:
            for key in ('status', 'valid', 'has_output', 'has_log'):
                row[key] = record.get(key, '')

    temporary = path.with_suffix('.csv.tmp')
    with temporary.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(table[0]))
        writer.writeheader()
        writer.writerows(table)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, default=Path('/home/lzh/MASTER/CODE/output/05_dos_intensity/DoS'))
    parser.add_argument('--dry-run', action='store_true', help='Validate all configurations and generated ST/network source; run no simulation')
    parser.add_argument('--recheck-only', action='store_true', help='Audit existing results and refresh statuses without sudo or simulation')
    parser.add_argument('--only', nargs='+', help='Run selected IDs; automatically include the matching no-attack baseline')
    parser.add_argument('--max-retries', type=int, default=1,
                        help='Automatically rerun a failed configuration this many times before stopping (default: 1)')
    args = parser.parse_args()
    archive = args.archive.resolve()
    plan = json.loads((archive / 'EXPERIMENT_PLAN.json').read_text())
    validate(archive, plan)
    if args.dry_run:
        print('[DRY-RUN] all configurations passed; no simulation started.')
        return 0
    selected = set(args.only or [s['id'] for s in plan['experiments']])
    selected.add('dos_rho_0')
    unknown = selected - {s['id'] for s in plan['experiments']}
    if unknown:
        parser.error(f'Unknown experiment IDs: {sorted(unknown)}')
    # Namespace names and OpenPLC build resources are shared: serialize this runner.
    lock = (PROJECT.parent / '.ctown_dos_matrix.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    records = json.loads((archive / 'RUN_INDEX.json').read_text())
    recheck_completed(archive, plan, records)
    if args.recheck_only:
        return 1 if any(r.get('status') == 'review_required' for r in records) else 0
    for name in ('epynet', 'pymodbus'):
        __import__(name)
    subprocess.run(['sudo', '-v'], check=True)
    by_id = {r['id']: r for r in records}
    env = os.environ.copy()
    env.update(plan['runtime_environment'])
    env.update(PYTHON_BIN=sys.executable, PYTHONDONTWRITEBYTECODE='1',
               PATH=f"{Path(sys.executable).parent}:{env.get('PATH', '')}")
    if args.max_retries < 0:
        parser.error('--max-retries must be >= 0')

    for spec in plan['experiments']:
        if spec['id'] not in selected:
            continue
        record = by_id[spec['id']]
        if record.get('valid') and all(check_result(spec).values()):
            print(f"[REUSE] {spec['id']}", flush=True)
            continue

        run = Path(spec['config']).parent
        retries_left = args.max_retries

        while True:
            # Archive any previous log/output before (re)running this configuration.
            if (run / 'run.log').exists() or (run / 'output').exists():
                history = run / 'past_attempts'
                history.mkdir(exist_ok=True)
                attempt = max([int(p.name.split('_')[-1]) for p in history.glob('attempt_*')], default=0) + 1
                previous = history / f'attempt_{attempt:03d}'
                previous.mkdir()
                shutil.copy2(run / 'config.yaml', previous / 'config.yaml')
                write_json(previous / 'run_record.json', record)
                for name in ('run.log', 'output'):
                    if (run / name).exists():
                        (run / name).rename(previous / name)
                print(f"[ARCHIVE] {spec['id']} -> {previous}", flush=True)

            record.update(status='running', valid=False, has_log=True,
                          attempt=record.get('attempt', 0) + 1)
            update_indices(archive, records)
            command = ['bash', str(PROJECT / 'scripts/run_all.sh'), '--config', spec['config'],
                       '--iterations', str(spec['iterations']), '--check']
            print(f"[RUN] {spec['id']} attempt={record['attempt']} log={run / 'run.log'}", flush=True)
            started = time.monotonic()
            interrupted = timed_out = False
            with (run / 'run.log').open('w') as log:
                proc = launch_logged_process(command, cwd=PROJECT, env=env, log=log)
                try:
                    rc = proc.wait(timeout=spec['timeout_sec'])
                except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                    interrupted = isinstance(error, KeyboardInterrupt)
                    timed_out = not interrupted
                    # Signal the foreground commands too, then allow run_all's cleanup trap.
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        rc = proc.wait(timeout=60)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        rc = proc.wait()

            checks = check_result(spec)
            outcome = dict(returncode=rc, timed_out=timed_out,
                           status='interrupted' if interrupted else 'completed')
            valid = accepted_execution(spec, outcome, checks)
            record.update(returncode=rc, elapsed_sec=round(time.monotonic() - started, 3),
                          timed_out=timed_out, validation=checks, valid=valid,
                          status='valid' if valid else 'interrupted' if interrupted else
                          'review_required' if checks['simulation_end'] and checks['complete_physics'] else 'failed',
                          has_output=(run / 'output').exists(), config_sha256=digest(spec['config']))
            update_indices(archive, records)
            print(f"[{'OK' if valid else 'FAILED'}] {spec['id']} {checks}", flush=True)

            if valid:
                break
            if interrupted:
                print('Interrupted by user; stopping without automatic retry.', flush=True)
                return 130
            if retries_left <= 0:
                print('Stopped after automatic retry limit was reached. See run.log for the latest attempt.',
                      flush=True)
                return 1

            retries_left -= 1
            print(f"[RETRY] {spec['id']} failed; rerunning the same configuration "
                  f"(retries remaining after this: {retries_left}).", flush=True)
    print('[DONE] selected runs completed and passed execution checks; compare physical effects against dos_rho_0.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())