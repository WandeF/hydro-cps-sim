from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.runtime import persistent_closed_loop


class PersistentResourceLifecycleTests(unittest.TestCase):
    def test_metric_quality_failure_maps_to_cleanup_error_end_status(self) -> None:
        self.assertEqual(
            "cleanup_error",
            persistent_closed_loop._runtime_end_status(
                None,
                ["metric_writer_quality:modbus-1.json:write_errors=1"],
            ),
        )
        self.assertEqual("success", persistent_closed_loop._runtime_end_status(None, []))
        self.assertEqual(
            "error",
            persistent_closed_loop._runtime_end_status(RuntimeError("run failed"), []),
        )

    def test_registry_discovers_only_transient_launcher_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_dir = root / "runtime"
            runtime_dir.mkdir()
            rt = SimpleNamespace(plcs={}, output_dir=root / "output")
            parent = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import subprocess,sys,time; "
                        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(2)']); "
                        "print(p.pid, flush=True); time.sleep(2)"
                    ),
                ],
                stdout=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
            try:
                assert parent.stdout is not None
                child_pid = int(parent.stdout.readline().strip())
                registry = persistent_closed_loop._MetricProcessRegistry(rt, runtime_dir)
                registry.add_transient_root("attack-scheduler:sync", parent.pid)

                deadline = time.monotonic() + 1.0
                resolved: dict[str, int] = {}
                while time.monotonic() < deadline:
                    resolved = registry.resolve()
                    if child_pid in resolved.values():
                        break
                    time.sleep(0.01)

                self.assertIn(parent.pid, resolved.values())
                self.assertIn(child_pid, resolved.values())
                self.assertNotIn(unrelated.pid, resolved.values())
                self.assertEqual(
                    resolved["attack-scheduler:sync"],
                    parent.pid,
                )
            finally:
                if parent.poll() is None:
                    os.killpg(parent.pid, signal.SIGTERM)
                parent.wait(timeout=3)
                if parent.stdout is not None:
                    parent.stdout.close()
                if unrelated.poll() is None:
                    unrelated.terminate()
                unrelated.wait(timeout=3)

    def test_scheduler_nonzero_propagates_and_unregisters_transient_root(self) -> None:
        process = mock.Mock(pid=4321)
        process.wait.return_value = 7
        registry = mock.Mock()
        with mock.patch.object(persistent_closed_loop.subprocess, "Popen", return_value=process):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                persistent_closed_loop._run_attack_scheduler(
                    ["attack-launcher"],
                    project_root=Path("/tmp"),
                    process_registry=registry,
                    component="attack-scheduler:stop",
                )

        self.assertEqual(raised.exception.returncode, 7)
        registry.add_transient_root.assert_called_once_with("attack-scheduler:stop", 4321)
        registry.remove_transient_root.assert_called_once_with("attack-scheduler:stop", 4321)


if __name__ == "__main__":
    unittest.main()
