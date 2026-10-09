import ast
import csv
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from src.io.export_results import export_scada_observed
from src.runtime.persistent_closed_loop import build_parser, release_physics_snapshot
from src.sync.filesystem import marker_path, read_marker

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'scripts/run_all.sh'


def shell_function(name):
    text = RUNNER.read_text()
    start = text.index(name + '() {')
    end = text.index('\n}\n', start) + 3
    return text[start:end]


class RuntimeEntrypointTests(unittest.TestCase):
    def run_shell(self, text):
        return subprocess.run(['bash', '-c', text], capture_output=True, text=True, timeout=10)

    def test_stage_stops_at_first_failed_command(self):
        script = 'set -euo pipefail\n' + shell_function('stage') + '''
        timing_record() { :; }
        failing_stage() { false; echo SHOULD_NOT_RUN; }
        stage failure failing_stage
        echo SHOULD_NOT_CONTINUE
        '''
        result = self.run_shell(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('SHOULD_NOT', result.stdout)

    def test_failed_authentication_does_not_start_keepalive(self):
        script = shell_function('ensure_sudo') + '''
        SUDO_READY=0
        sudo() { return 7; }
        ensure_sudo
        rc=$?
        echo "$SUDO_READY:${SUDO_KEEPALIVE_PID:-unset}"
        exit "$rc"
        '''
        result = self.run_shell(script)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.strip(), '0:unset')

    def test_failed_export_is_not_marked_complete(self):
        script = shell_function('export_results') + '''
        EXPORTED=0; RUNTIME_STARTED=1; PYTHON_BIN=python; CONFIG=config; OUTPUT_DIR=/tmp/example
        sudo() { return 9; }
        export_results
        rc=$?
        echo "$EXPORTED"
        exit "$rc"
        '''
        result = self.run_shell(script)
        self.assertEqual(result.returncode, 9)
        self.assertEqual(result.stdout.strip(), '0')

    def test_no_export_before_runtime_started(self):
        script = shell_function('export_results') + '''
        EXPORTED=0; RUNTIME_STARTED=0
        sudo() { echo SHOULD_NOT_RUN; return 9; }
        export_results
        '''
        result = self.run_shell(script)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_first_run_has_no_attacks_to_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = shell_function('stop_attacks') + '''
            OUTPUT_DIR="$1"
            sudo() { echo SHOULD_NOT_RUN; return 9; }
            stop_attacks
            '''
            result = subprocess.run(['bash', '-c', script, '_', tmp], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')

    def test_invalid_runner_arguments_fail_before_sudo(self):
        for args in (['--logic-wait'], ['--logic-wait', '-1'], ['--logic-wait', 'nan'],
                     ['--poll-interval', '0'], ['--iterations', '0'],
                     ['--skip-prep'], ['--check'], ['--compare'], ['--sync-backend', 'helics']):
            with self.subTest(args=args):
                result = subprocess.run(['bash', str(RUNNER), *args], cwd=ROOT,
                    env=dict(os.environ, PYTHON_BIN=sys.executable), capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('[RUN-ALL] Authenticate sudo', result.stdout)

    def test_logic_wait_accepts_zero_and_nondefault(self):
        parser = build_parser()
        for value in ('0', '0.4'):
            self.assertEqual(parser.parse_args(['--config', 'config.yaml', '--logic-wait', value]).logic_wait, float(value))
        for value in ('-1', 'nan', 'inf'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                parser.parse_args(['--config', 'config.yaml', '--logic-wait', value])

    def test_physics_marker_is_released_with_iteration_and_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync = Path(tmp)
            release_physics_snapshot(sync, 3, sync/'physics_0003.json', {'backend': 'dhalsim_epynet', 'advanced': True})
            value = read_marker(marker_path(sync, 'physics', 3))
            self.assertEqual(value['iteration'], 3)
            self.assertEqual(value['output'], str(sync/'physics_0003.json'))
            self.assertTrue(value['advanced'])

    def test_export_uses_observed_tank_owners_and_numeric_plc_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)/'runtime'
            (runtime/'csv').mkdir(parents=True)
            with (runtime/'csv/scada_observed.csv').open('w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=['iteration', 'plc', 'variable', 'value'])
                writer.writeheader()
                writer.writerows([
                    dict(iteration=1, plc='PLC10', variable='PLC10_T2', value=2),
                    dict(iteration=1, plc='PLC6', variable='PLC6_T5', value=1),
                    dict(iteration=1, plc='PLC4', variable='PLC8_T7', value=4),
                    dict(iteration=1, plc='PLC8', variable='PLC8_T7', value=4),
                ])
            paths = export_scada_observed(runtime, Path(tmp)/'reports')
            with paths[-1].open() as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, ['iteration','PLC6.PLC6_T5','PLC8.PLC8_T7','PLC10.PLC10_T2'])
                row = next(reader)
                self.assertTrue(all(row.values()))


if __name__ == '__main__':
    unittest.main()
