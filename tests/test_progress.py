import os
from io import StringIO
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import yaml

from src.run.progress import ProgressDisplay, STAGES


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'scripts/run_all.sh'


class TTYStringIO(StringIO):
    def isatty(self):
        return True


class ProgressDisplayTests(unittest.TestCase):
    def test_initialization_stages_and_phase_transitions(self):
        display = ProgressDisplay('config.yaml', cycles=2, stream=StringIO())

        for index, (stage, label) in enumerate(STAGES):
            with self.subTest(stage=stage):
                self.assertTrue(display.consume(f'[RUN-ALL] {stage}\n'))
                self.assertEqual(display.initialized, index)
                self.assertEqual(display.status, label)

        display.consume('[RUN-ALL] Run closed loop\n')
        self.assertEqual(display.initialized, len(STAGES))
        self.assertEqual(display.phase, 'simulation')
        self.assertEqual(display.status, '初始化水力状态与 PLC 数据')

        display.consume('[RUN-ALL] Export results\n')
        self.assertEqual(display.phase, 'export')
        self.assertEqual(display.status, '导出结果')

    def test_cycle_completion_counts_dhalsim_cycles_exactly(self):
        stream = StringIO()
        display = ProgressDisplay('config.yaml', cycles=2, stream=stream)

        self.assertTrue(display.consume('[CYCLE 1] timing local=0.1s total=0.2s\n'))
        self.assertEqual(display.completed, 1)
        self.assertTrue(display.consume('[CYCLE 2] timing local=0.1s total=0.2s\n'))
        self.assertEqual(display.completed, 2)
        display.consume('[CYCLE 99] timing local=0.1s total=0.2s\n')
        self.assertEqual(display.completed, 2)
        display.consume('[CYCLE 1] timing local=0.1s total=0.2s\n')
        self.assertEqual(display.completed, 2)

    def test_tty_rendering_uses_terminal_updates(self):
        stream = TTYStringIO()
        with patch.dict(os.environ, {'TERM': 'xterm'}):
            display = ProgressDisplay('config.yaml', cycles=2, stream=stream)
            display.render()
            display.consume('[RUN-ALL] Run closed loop')
            display.render()

        output = stream.getvalue()
        self.assertIn('\x1b[2K', output)
        self.assertIn('\r', output)
        self.assertIn('\x1b[6A', output)
        self.assertIn('配置: config.yaml', output)

    def test_tty_zero_column_fallback_keeps_full_progress_and_status(self):
        stream = TTYStringIO()
        with patch.dict(os.environ, {'TERM': 'xterm'}), patch(
            'src.run.progress.shutil.get_terminal_size',
            return_value=os.terminal_size((0, 0)),
        ):
            display = ProgressDisplay('town configuration.yaml', cycles=2, stream=stream)
            display.initialized = len(STAGES)
            display.completed = 2
            display.exported = 1
            display.status = '运行仿真'
            display.render(final=True)

        output = stream.getvalue()
        self.assertIn('初始化 [========================] 100.0% 10/10', output)
        self.assertIn('仿真   [========================] 100.0% 2/2', output)
        self.assertIn('导出   [========================] 100.0% 1/1', output)
        self.assertIn('状态: 运行仿真 | 已用时', output)

    def test_non_tty_rendering_is_plain_text(self):
        stream = StringIO()
        display = ProgressDisplay('config.yaml', cycles=2, stream=stream)
        display.render()
        display.consume('[RUN-ALL] Run closed loop')
        display.render()

        output = stream.getvalue()
        self.assertNotIn('\x1b', output)
        self.assertNotIn('\r', output)
        self.assertGreaterEqual(output.count('\n'), 2)
        self.assertIn('初始化 [', output)
        self.assertIn('初始化水力状态与 PLC 数据', output)

    def test_runner_command_runs_concisely_and_preserves_success_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / 'logs'
            child_code = (
                "print('[RUN-ALL] Generate PLC programs', flush=True); "
                "print('[RUN-ALL] Run closed loop', flush=True); "
                "print('[INIT] physics_0001.ready released', flush=True); "
                "print('[CYCLE 1] timing local=0.1s total=0.2s', flush=True); "
                "print('[CYCLE 2] timing local=0.1s total=0.2s', flush=True); "
                "print('[RUN-ALL] Export results', flush=True); "
                "print('[DONE] Results: reports/csv', flush=True); "
                "print('child detail retained in file', flush=True)"
            )
            result = subprocess.run(
                [
                    sys.executable, '-m', 'src.run.progress',
                    '--log-dir', str(log_dir), '--label', 'config.yaml', '--iterations', '3',
                    '--', sys.executable, '-u', '-c', child_code,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn('已完成', result.stdout)
            self.assertNotIn('\x1b', result.stdout)
            self.assertNotIn('\r', result.stdout)
            logs = list(log_dir.glob('run_all-*.log'))
            self.assertEqual(len(logs), 1)
            contents = logs[0].read_text(encoding='utf-8')
            self.assertIn('[CYCLE 1] timing', contents)
            self.assertIn('[CYCLE 2] timing', contents)
            self.assertIn('child detail retained in file', contents)

    def test_nonzero_child_keeps_complete_log_and_reports_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / 'logs'
            child_code = (
                "import sys; "
                "[print(f'child line {i:02d}', flush=True) for i in range(35)]; "
                "print('[ERROR] simulated failure', flush=True); sys.exit(7)"
            )
            result = subprocess.run(
                [
                    sys.executable, '-m', 'src.run.progress',
                    '--log-dir', str(log_dir), '--label', 'bad.yaml', '--iterations', '3',
                    '--', sys.executable, '-u', '-c', child_code,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 7)
            logs = list(log_dir.glob('run_all-*.log'))
            self.assertEqual(len(logs), 1)
            contents = logs[0].read_text(encoding='utf-8')
            self.assertIn('child line 00', contents)
            self.assertIn('child line 34', contents)
            self.assertIn('[ERROR] simulated failure', contents)
            self.assertIn('child line 34', result.stderr)
            self.assertIn('运行失败（退出码 7）', result.stdout)

    def test_sigint_forwards_to_child_and_waits_for_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            log_dir = temp / 'logs'
            started = temp / 'started'
            cleaned = temp / 'cleaned'
            descendant_started = temp / 'descendant-started'
            descendant_cleaned = temp / 'descendant-cleaned'
            child_code = (
                'import pathlib, signal, sys, time\n'
                'import subprocess\n'
                'started, cleaned, descendant_started, descendant_cleaned = map(pathlib.Path, sys.argv[1:])\n'
                'descendant_code = "import pathlib, signal, sys, time; ready, done = map(pathlib.Path, sys.argv[1:]); " \\\n'
                '    "signal.signal(signal.SIGINT, lambda signum, frame: (done.write_text(str(signum)), print(\'descendant cleanup complete\', flush=True), sys.exit(130))); " \\\n'
                '    "ready.write_text(\'ready\'); print(\'descendant started\', flush=True); " \\\n'
                '    "time.sleep(30)"\n'
                'descendant = subprocess.Popen([sys.executable, "-u", "-c", descendant_code, str(descendant_started), str(descendant_cleaned)])\n'
                'deadline = time.monotonic() + 4\n'
                'while not descendant_started.exists() and time.monotonic() < deadline: time.sleep(0.01)\n'
                'started.write_text("ready")\n'
                'print("child started", flush=True)\n'
                'def stop(signum, frame):\n'
                '    cleaned.write_text(str(signum))\n'
                '    print("child cleanup complete", flush=True)\n'
                '    raise SystemExit(130)\n'
                'signal.signal(signal.SIGINT, stop)\n'
                'while True: time.sleep(0.05)\n'
            )
            command = [
                sys.executable, '-m', 'src.run.progress',
                '--log-dir', str(log_dir), '--label', 'interrupt.yaml', '--iterations', '3',
                '--', sys.executable, '-u', '-c', child_code,
                str(started), str(cleaned), str(descendant_started), str(descendant_cleaned),
            ]
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=dict(os.environ, PYTHONUNBUFFERED='1'),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not started.exists() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    time.sleep(0.02)
                self.assertTrue(started.exists(), 'fake child did not start before timeout')
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=2)
                self.fail('progress wrapper did not finish after SIGINT')

            self.assertEqual(process.returncode, 130, msg=stderr)
            self.assertTrue(cleaned.exists(), 'SIGINT was not forwarded to the child')
            self.assertTrue(descendant_cleaned.exists(), 'SIGINT was not forwarded to the child process group')
            self.assertIn('已中止', stdout)
            logs = list(log_dir.glob('run_all-*.log'))
            self.assertEqual(len(logs), 1)
            contents = logs[0].read_text(encoding='utf-8')
            self.assertIn('child cleanup complete', contents)
            self.assertIn('descendant cleanup complete', contents)

    def test_runner_help_and_invalid_log_level_fail_before_sudo(self):
        env = dict(os.environ, PYTHON_BIN=sys.executable)
        help_result = subprocess.run(
            ['bash', str(RUNNER), '--help'], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn('--log-level LEVEL', help_result.stdout)

        bad_result = subprocess.run(
            ['bash', str(RUNNER), '--log-level', 'invalid'], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(bad_result.returncode, 0)
        self.assertIn('Unsupported log level', bad_result.stderr)
        self.assertNotIn('[RUN-ALL] Authenticate sudo', bad_result.stdout)

    def test_config_info_exports_log_level_as_shell_safe_value(self):
        source_config = ROOT / 'examples/c_town/config.yaml'
        base_config = yaml.safe_load(source_config.read_text(encoding='utf-8'))

        for level in ('concise', '简洁'):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / 'config.yaml'
                base_config['log_level'] = level
                config.write_text(yaml.safe_dump(base_config, allow_unicode=True), encoding='utf-8')
                result = subprocess.run(
                    [sys.executable, '-m', 'src.run.config_info', '--config', str(config)],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

                self.assertEqual(result.returncode, 0, msg=result.stderr)
                self.assertIn(f'LOG_LEVEL_FROM_CONFIG=', result.stdout)
                shell_result = subprocess.run(
                    [
                        'bash', '-c',
                        'eval "$1"; printf "%s" "$LOG_LEVEL_FROM_CONFIG"',
                        '_', result.stdout,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(shell_result.returncode, 0, msg=shell_result.stderr)
                self.assertEqual(shell_result.stdout, level)


if __name__ == '__main__':
    unittest.main()
