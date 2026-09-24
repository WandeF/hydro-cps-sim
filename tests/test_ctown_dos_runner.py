"""Execution validity must not accept incomplete or wrongly targeted DoS runs."""
import csv
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from scripts.run_ctown_dos_experiments import check_result, launch_logged_process, accepted_execution, PROJECT


class DosProcessLaunch(unittest.TestCase):
    def test_preserves_session_and_isolates_process_group(self):
        code = ('import json, os; print(json.dumps(dict('
                'pid=os.getpid(), sid=os.getsid(0), pgid=os.getpgrp())))')
        with tempfile.TemporaryFile(mode='w+') as log:
            proc = launch_logged_process([sys.executable, '-c', code], cwd=PROJECT,
                                         env=os.environ.copy(), log=log)
            self.assertEqual(proc.wait(timeout=10), 0)
            log.seek(0)
            state = json.load(log)
        self.assertEqual(state['sid'], os.getsid(0))
        self.assertEqual(state['pgid'], state['pid'])
        self.assertNotEqual(state['pgid'], os.getpgrp())

    def test_sudo_authentication_failure_stops_stage(self):
        script = (PROJECT / 'scripts/run_all.sh').read_text()
        function = 'ensure_sudo() {' + script.split('ensure_sudo() {', 1)[1].split('\n}\n', 1)[0] + '\n}\n'
        # Mock sudo in a shell with errexit disabled, just as time_stage does.
        command = 'sudo() { return 1; }\n' + function + '\nset +e\nensure_sudo\nexit $?\n'
        result = subprocess.run(['bash', '-c', command], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)


class DosExecutionValidation(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.spec = {'output': str(self.output), 'value': 1.5, 'config': 'fixture.yaml'}
        audit = patch('scripts.run_ctown_dos_experiments.verify_delivered_inputs', return_value={'ok': True})
        self.audit = audit.start()
        self.addCleanup(audit.stop)
        (self.output / 'check').mkdir()
        (self.output / 'check/check_summary.json').write_text(json.dumps({'ok': True, 'actuator': {'ok': True}, 'open_loop': {'ok': True}}))
        self.write_csv('runtime/csv/events.csv', ['event_type', 'status'], [['simulation_end', 'success']])
        self.write_csv('reports/csv/physics.csv', ['iteration', 'T4'], [[i, 2.5] for i in range(81)])
        self.attack_rows = [[event, f'ns_bot{i}', 'PLC7'] for i in range(1, 4)
                            for event in ['dos_start', 'attack_packet_sent']]
        self.write_attack(self.attack_rows)

    def write_csv(self, relative, columns, data):
        path = self.output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(data)

    def write_attack(self, data):
        self.write_csv('reports/csv/attack_events.csv', ['event', 'source', 'target'], data)

    def test_complete_targeted_attack_passes(self):
        self.assertTrue(all(check_result(self.spec).values()))

    def test_missing_bot_packet_rejected(self):
        self.write_attack(self.attack_rows[:-1])
        self.assertFalse(check_result(self.spec)['attack_sources_ok'])

    def test_old_target_rejected(self):
        self.write_attack([[event, source, 'PLC4'] for event, source, _ in self.attack_rows])
        self.assertFalse(check_result(self.spec)['attack_sources_ok'])

    def test_missing_iteration_rejected_even_with_final_iteration(self):
        self.write_csv('reports/csv/physics.csv', ['iteration'], [[i] for i in range(81) if i != 30])
        self.assertFalse(check_result(self.spec)['complete_physics'])

    def test_failed_lifecycle_rejected(self):
        self.write_csv('runtime/csv/events.csv', ['event_type', 'status'], [['simulation_end', 'failed']])
        self.assertFalse(check_result(self.spec)['simulation_end'])

    def test_baseline_has_no_attack_to_any_plc(self):
        self.spec['value'] = 0
        self.write_attack([])
        self.assertTrue(all(check_result(self.spec).values()))
        self.write_attack([['dos_start', 'ns_bot1', 'PLC4']])
        self.assertFalse(check_result(self.spec)['attack_sources_ok'])

    def test_missing_check_report_rejected(self):
        (self.output / 'check/check_summary.json').unlink()
        self.assertFalse(all(check_result(self.spec).values()))

    def test_explained_nominal_deviation_is_retained(self):
        (self.output / 'check/check_summary.json').write_text(json.dumps({'ok': False, 'actuator': {'ok': False}, 'open_loop': {'ok': True}}))
        checks = check_result(self.spec)
        self.assertTrue(accepted_execution(self.spec, {'returncode': 2}, checks))
        self.assertFalse(accepted_execution(self.spec, {'returncode': 1}, checks))
        self.assertFalse(accepted_execution(self.spec, {'returncode': 2, 'timed_out': True}, checks))
        self.audit.return_value = {'ok': False}
        self.assertFalse(accepted_execution(self.spec, {'returncode': 2}, check_result(self.spec)))

    def test_hydraulic_failure_is_not_accepted(self):
        (self.output / 'check/check_summary.json').write_text(json.dumps({'ok': False, 'actuator': {'ok': False}, 'open_loop': {'ok': False}}))
        self.assertFalse(accepted_execution(self.spec, {'returncode': 2}, check_result(self.spec)))


if __name__ == '__main__':
    unittest.main()
