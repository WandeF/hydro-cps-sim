from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.metrics.writer_quality import analyze_metric_writer_stats, required_metric_writers


def write_stats(path: Path, *, writer: str, **overrides: object) -> None:
    payload: dict[str, object] = {
        "writer": writer,
        "accepted": 3,
        "processed": 3,
        "written": 3,
        "write_errors": 0,
        "dropped_queue_full": 0,
        "dropped_after_close": 0,
        "dropped_disabled": 0,
        "unflushed_on_close": 0,
        "pending": 0,
        "thread_alive": False,
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class WriterQualityTests(unittest.TestCase):
    def test_normal_writer_stats_are_complete_and_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stats = Path(tmp)
            write_stats(stats / "modbus-1.json", writer="modbus")
            write_stats(stats / "mitm-2.json", writer="mitm", accepted=2, processed=2, written=2)

            summary = analyze_metric_writer_stats(
                stats,
                required_writers={"modbus": 1, "mitm": 1},
            )

            self.assertTrue(summary["quality_complete"])
            self.assertEqual(5, summary["accepted"])
            self.assertEqual(5, summary["written"])
            self.assertEqual(0, summary["dropped_total"])
            self.assertEqual({"mitm": 1, "modbus": 1}, summary["writer_counts"])

    def test_every_loss_or_unfinished_signal_fails_quality(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stats = Path(tmp)
            write_stats(
                stats / "modbus-1.json",
                writer="modbus",
                accepted=8,
                processed=7,
                written=6,
                write_errors=1,
                dropped_queue_full=2,
                dropped_after_close=1,
                unflushed_on_close=1,
                pending=1,
                thread_alive=True,
            )

            summary = analyze_metric_writer_stats(stats, required_writers={"modbus": 1})

            self.assertFalse(summary["quality_complete"])
            self.assertEqual(3, summary["dropped_total"])
            self.assertEqual(1, summary["write_errors"])
            self.assertEqual(1, summary["unflushed_on_close"])
            self.assertEqual(1, summary["thread_alive_count"])
            self.assertTrue(any("pending=1" in error for error in summary["quality_errors"]))

    def test_required_mitm_count_detects_one_of_two_missing_proxies(self) -> None:
        config = {
            "metrics": {"enabled": True, "communication": True},
            "attacks": {
                "enabled": True,
                "scenarios": [
                    {
                        "enabled": True,
                        "type": "modbus_mitm",
                        "intercept": {"targets": ["PLC7", "PLC9"]},
                    }
                ],
            },
        }
        self.assertEqual({"modbus": 1, "mitm": 2}, required_metric_writers(config))

        with tempfile.TemporaryDirectory() as tmp:
            stats = Path(tmp)
            write_stats(stats / "modbus-1.json", writer="modbus")
            write_stats(stats / "mitm-2.json", writer="mitm")

            summary = analyze_metric_writer_stats(
                stats,
                required_writers=required_metric_writers(config),
            )

            self.assertFalse(summary["quality_complete"])
            self.assertEqual({"mitm": 1}, summary["missing_writers"])
            self.assertIn(
                "missing_required_writer:mitm:expected=2:actual=1:missing=1",
                summary["quality_errors"],
            )

    def test_required_mitm_count_supports_top_level_targets(self) -> None:
        config = {
            "metrics": {"enabled": True, "communication": False},
            "attacks": {
                "enabled": True,
                "scenarios": [{"type": "mitm", "targets": "PLC4"}],
            },
        }
        self.assertEqual({"mitm": 1}, required_metric_writers(config))

    def test_metrics_master_switch_disables_required_writers(self) -> None:
        config = {
            "metrics": {"enabled": False},
            "attacks": {
                "enabled": True,
                "scenarios": [
                    {"type": "modbus_mitm", "intercept": {"targets": ["PLC4"]}}
                ],
            },
        }
        self.assertEqual({}, required_metric_writers(config))

    def test_missing_required_modbus_snapshot_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = analyze_metric_writer_stats(
                Path(tmp) / "missing",
                required_writers={"modbus": 1},
            )

            self.assertFalse(summary["quality_complete"])
            self.assertEqual({"modbus": 1}, summary["missing_writers"])


if __name__ == "__main__":
    unittest.main()
