from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.attack import launch


class AttackCleanupTests(unittest.TestCase):
    def test_stop_pid_waits_for_graceful_writer_shutdown_without_sleeping_in_test(self) -> None:
        with (
            mock.patch.object(launch.os, "killpg") as killpg,
            mock.patch.object(launch, "_is_pid_alive", side_effect=[True, False, False]),
            mock.patch.object(launch.time, "monotonic", side_effect=[10.0, 10.0]),
            mock.patch.object(launch.time, "sleep") as sleep,
        ):
            launch._stop_pid(123, grace=3.0, poll_interval=0.05)

        killpg.assert_called_once_with(123, launch.signal.SIGTERM)
        sleep.assert_called_once_with(0.05)

    def test_stop_pid_escalates_only_after_grace_deadline(self) -> None:
        with (
            mock.patch.object(launch.os, "killpg") as killpg,
            mock.patch.object(launch, "_is_pid_alive", return_value=True),
            mock.patch.object(launch.time, "monotonic", side_effect=[10.0, 13.1]),
            mock.patch.object(launch.time, "sleep") as sleep,
        ):
            launch._stop_pid(123, grace=3.0)

        self.assertEqual(
            [
                mock.call(123, launch.signal.SIGTERM),
                mock.call(123, launch.signal.SIGKILL),
            ],
            killpg.call_args_list,
        )
        sleep.assert_not_called()

    def test_missing_iptables_rule_is_idempotent_success(self) -> None:
        result = subprocess.CompletedProcess(
            ["iptables"],
            1,
            stdout="",
            stderr="Bad rule (does a matching rule exist in that chain?).",
        )
        with mock.patch.object(launch.subprocess, "run", return_value=result):
            launch._iptables_delete_all("ns", "10.0.0.2", 502, "10.0.0.3", 15020)

    def test_real_iptables_delete_failure_propagates(self) -> None:
        result = subprocess.CompletedProcess(
            ["iptables"],
            4,
            stdout="",
            stderr="Permission denied",
        )
        with mock.patch.object(launch.subprocess, "run", return_value=result):
            with self.assertRaises(subprocess.CalledProcessError):
                launch._iptables_delete_all("ns", "10.0.0.2", 502, "10.0.0.3", 15020)

    def test_mitm_cleanup_continues_after_rule_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            pid_file = launch._pid_file(runtime_dir, "mitm", "PLC4")
            state_file = launch._state_file(runtime_dir, "mitm", "PLC4")
            pid_file.write_text("123", encoding="utf-8")
            state_file.write_text("{}", encoding="utf-8")

            with (
                mock.patch.object(launch, "_iptables_delete_all", side_effect=PermissionError("denied")),
                mock.patch.object(launch, "_running_pid", return_value=123),
                mock.patch.object(launch, "_stop_pid") as stop_pid,
                mock.patch.object(launch, "_write_schedule_event"),
            ):
                with self.assertRaisesRegex(RuntimeError, "MITM attack"):
                    launch._stop_target(
                        runtime_dir=runtime_dir,
                        scada_ns="ns-scada",
                        scenario={"name": "mitm"},
                        target_key="PLC4",
                        target_ip="10.0.0.2",
                        target_port=502,
                        attacker_ip="10.0.0.3",
                        listen_port=15020,
                    )

            stop_pid.assert_called_once_with(123)
            self.assertFalse(pid_file.exists())
            self.assertFalse(state_file.exists())

    def test_stop_is_best_effort_and_returns_nonzero_after_target_failure(self) -> None:
        args = argparse.Namespace(config=Path("config.yaml"), runtime_dir=Path("runtime"))
        targets = [
            ({"name": "first"}, 0, "PLC1", "ns", "10.0.0.3", "10.0.0.1", 502, 15020),
            ({"name": "second"}, 1, "PLC2", "ns", "10.0.0.3", "10.0.0.2", 502, 15021),
        ]
        with (
            mock.patch.object(launch, "load_yaml", return_value={}),
            mock.patch.object(launch, "load_runtime_config", return_value=SimpleNamespace(output_dir=Path("output"))),
            mock.patch.object(launch, "_iter_mitm_targets", return_value=targets),
            mock.patch.object(launch, "_iter_dos_targets", return_value=[]),
            mock.patch.object(launch, "_iter_openplc_targets", return_value=[]),
            mock.patch.object(launch, "_stop_target", side_effect=[RuntimeError("failed"), True]) as stop_target,
        ):
            returncode = launch.stop(args)

        self.assertEqual(returncode, 1)
        self.assertEqual(stop_target.call_count, 2)

    def test_openplc_restore_failure_still_kills_process_and_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            spec = {
                "name": "logic",
                "target": "PLC4",
                "pid_key": "openplc_PLC4",
                "state_file": str(runtime_dir / "logic.state.json"),
                "restore_on_stop": True,
            }
            pid_file = launch._pid_file(runtime_dir, "logic", "openplc_PLC4")
            pid_file.write_text("123", encoding="utf-8")
            with (
                mock.patch.object(launch, "_running_pid", return_value=123),
                mock.patch.object(launch, "_run_openplc_restore", side_effect=RuntimeError("compile failed")),
                mock.patch.object(launch.os, "killpg") as killpg,
            ):
                with self.assertRaisesRegex(RuntimeError, "OpenPLC logic attack"):
                    launch._stop_openplc_logic(
                        args=argparse.Namespace(),
                        runtime_dir=runtime_dir,
                        scenario={"name": "logic"},
                        spec=spec,
                    )

            killpg.assert_called_once_with(123, launch.signal.SIGKILL)
            self.assertFalse(pid_file.exists())


if __name__ == "__main__":
    unittest.main()
