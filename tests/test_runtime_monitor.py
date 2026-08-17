from __future__ import annotations

import csv
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.metrics import runtime_monitor
from src.metrics.runtime_monitor import RESOURCE_FIELDS, RuntimeMonitor


class RuntimeMonitorTests(unittest.TestCase):
    @unittest.skipUnless(runtime_monitor.psutil is not None, "psutil is not installed")
    def test_context_manager_samples_current_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resources.csv"
            monitor = RuntimeMonitor(
                path,
                {"test-runner": os.getpid()},
                interval_sec=0.02,
            )
            with monitor:
                time.sleep(0.08)

            self.assertFalse(monitor.is_running)
            self.assertIsNone(monitor.last_error)
            with path.open(newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                self.assertEqual(tuple(reader.fieldnames or ()), RESOURCE_FIELDS)
                rows = list(reader)
            self.assertGreaterEqual(len(rows), 2)
            self.assertEqual({row["component"] for row in rows}, {"test-runner"})
            self.assertEqual({int(row["pid"]) for row in rows}, {os.getpid()})
            self.assertTrue(all(int(row["rss_bytes"]) > 0 for row in rows))

    @unittest.skipUnless(runtime_monitor.psutil is not None, "psutil is not installed")
    def test_process_tree_and_exiting_process_do_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resources.csv"
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.15)"])
            try:
                monitor = RuntimeMonitor(
                    path,
                    {"suite": os.getpid()},
                    interval_sec=0.01,
                    include_process_tree=True,
                )
                initial_rows = monitor.sample()
                self.assertIn(child.pid, {int(row["pid"]) for row in initial_rows})
                overlapping_rows = monitor.sample(
                    {"suite": os.getpid(), "short-lived": child.pid},
                    include_process_tree=True,
                )
                self.assertEqual(
                    1,
                    sum(int(row["pid"]) == child.pid for row in overlapping_rows),
                )
                monitor.set_processes({"short-lived": child.pid})
                monitor.start()
                child.wait(timeout=2)
                time.sleep(0.04)
                monitor.stop(timeout=2)
                self.assertFalse(monitor.is_running)
                self.assertIsNone(monitor.last_error)
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=2)

    def test_missing_psutil_is_reported_and_keeps_valid_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resources.csv"
            with mock.patch.object(runtime_monitor, "psutil", None):
                monitor = RuntimeMonitor(path, {"self": os.getpid()}, interval_sec=0.01)
                with self.assertRaisesRegex(RuntimeError, "psutil is required"):
                    monitor.start()
                self.assertFalse(monitor.available)
                self.assertFalse(monitor.is_running)
                with self.assertRaisesRegex(RuntimeError, "psutil is required"):
                    monitor.sample()
                with self.assertRaisesRegex(RuntimeError, "resource monitoring failed"):
                    monitor.stop()

            with path.open(newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                self.assertEqual(tuple(reader.fieldnames or ()), RESOURCE_FIELDS)
                self.assertEqual(list(reader), [])

    @unittest.skipUnless(runtime_monitor.psutil is not None, "psutil is not installed")
    def test_dynamic_resolver_failure_does_not_stop_monitor_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resources.csv"
            calls = 0

            def resolver() -> dict[str, int]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("temporary discovery failure")
                return {"dynamic": os.getpid()}

            monitor = RuntimeMonitor(
                path,
                {"initial": os.getpid()},
                interval_sec=0.01,
                process_resolver=resolver,
            ).start()
            time.sleep(0.08)
            self.assertTrue(monitor.is_running)
            self.assertIsInstance(monitor.last_error, RuntimeError)
            with self.assertRaisesRegex(RuntimeError, "temporary discovery failure"):
                monitor.stop(timeout=2)
            self.assertFalse(monitor.is_running)

            with path.open(newline="", encoding="utf-8") as file_obj:
                rows = list(csv.DictReader(file_obj))
            self.assertGreaterEqual(len(rows), 1)
            self.assertEqual({row["component"] for row in rows}, {"dynamic"})

    def test_cpu_percent_first_sample_delta_and_pid_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = RuntimeMonitor(Path(tmp) / "resources.csv")
            proc = mock.Mock(pid=4321)
            proc.cpu_times.side_effect = [
                SimpleNamespace(user=0.2, system=0.1),
                SimpleNamespace(user=0.3, system=0.2),
                SimpleNamespace(user=0.05, system=0.05),
            ]
            proc.create_time.side_effect = [100.0, 100.0, 102.0]
            fake_psutil = SimpleNamespace(Error=Exception)
            with mock.patch.object(runtime_monitor, "psutil", fake_psutil):
                first = monitor._cpu_percent(
                    proc,
                    pid=4321,
                    timestamp_ns=101_000_000_000,
                    monotonic_ns=10_000_000_000,
                )
                second = monitor._cpu_percent(
                    proc,
                    pid=4321,
                    timestamp_ns=102_000_000_000,
                    monotonic_ns=11_000_000_000,
                )
                reused = monitor._cpu_percent(
                    proc,
                    pid=4321,
                    timestamp_ns=103_000_000_000,
                    monotonic_ns=12_000_000_000,
                )

            self.assertAlmostEqual(float(first), 30.0)
            self.assertAlmostEqual(float(second), 20.0)
            self.assertAlmostEqual(float(reused), 10.0)

    @unittest.skipUnless(runtime_monitor.psutil is not None, "psutil is not installed")
    def test_sample_write_error_is_rethrown_by_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = RuntimeMonitor(
                Path(tmp) / "resources.csv",
                {"self": os.getpid()},
            )
            with mock.patch.object(
                runtime_monitor,
                "_append_csv_rows",
                side_effect=OSError("metric disk unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "metric disk unavailable"):
                    monitor.sample()
            with self.assertRaisesRegex(RuntimeError, "metric disk unavailable"):
                monitor.stop()

    def test_stop_timeout_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = RuntimeMonitor(Path(tmp) / "resources.csv")
            thread = mock.Mock()
            thread.is_alive.return_value = True
            monitor._thread = thread

            with self.assertRaisesRegex(RuntimeError, "did not stop"):
                monitor.stop(timeout=0.01)
            thread.join.assert_called_once_with(timeout=0.01)


if __name__ == "__main__":
    unittest.main()
