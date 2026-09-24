from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from src.metrics.run_summary import (
    SUMMARY_COLUMNS,
    build_run_summary,
    write_run_summary,
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class RunSummaryTests(unittest.TestCase):
    def test_complete_run_merges_all_sources_into_one_stable_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            write_json(
                output / "runtime" / "manifest.json",
                {
                    "experiment_id": "delay_10_run_2",
                    "group": "network_delay",
                    "parameter": "delay_ms",
                    "parameter_value": 10,
                    "repetition": 2,
                    "timestamp": "2026-07-15T12:00:00+08:00",
                    "random_seed": 1002,
                    "iterations": 100,
                    "hydraulic_step_sec": 300,
                    "config_file": "/tmp/config.yaml",
                    "config_sha256": "abc123",
                    "git": {"commit": "deadbeef", "branch": "metric", "dirty": False},
                },
            )
            write_json(
                output / "reports" / "metrics" / "performance_summary.json",
                {
                    "experiment_id": "stale_embedded_id",
                    "group": "network_delay",
                    "parameter": "delay_ms",
                    "parameter_value": 10,
                    "repetition": 2,
                    "run_status": "success",
                    "complete": True,
                    "quality_complete": True,
                    "runtime": {
                        "wall_clock_sec": 12.5,
                        "simulation_wall_clock_sec": 10,
                        "simulated_time_sec": 30000,
                        "real_time_factor": 3000,
                    },
                    "iteration_time": {"count": 100, "mean_ms": 75, "p95_ms": 90},
                    "resources": {
                        "peak_aggregate_rss_mb": 256,
                        "mean_aggregate_cpu_percent": 42,
                        "peak_aggregate_cpu_percent": 80,
                    },
                    "communication": {
                        "request_count": 200,
                        "success_count": 198,
                        "timeout_count": 2,
                        "success_rate": 0.99,
                        "timeout_rate": 0.01,
                        "mean_rtt_ms": 23.5,
                        "p95_rtt_ms": 30,
                    },
                },
            )
            write_json(
                output / "runtime" / "network" / "network-aggregate.json",
                {
                    "status": "ok",
                    "row_count": 3,
                    "experiment_id": "delay_10_run_2",
                    "run_status": "success",
                    "complete": True,
                    "by_source": {
                        "flow_monitor": {"mean_delay_ms": 99, "packet_loss_rate": 0.5},
                        "link_trace": {
                            "tx_packets": 1000,
                            "rx_packets": 990,
                            "lost_packets": 10,
                            "drop_packets": 10,
                            "mean_delay_ms": 10.2,
                            "packet_loss_rate": 0.01,
                            "throughput_bps_sum": 2_000_000,
                            "mean_abs_delay_error_ms": 0.2,
                            "max_abs_delay_error_ms": 0.4,
                            "mean_loss_error": 0.001,
                            "max_loss_error": 0.002,
                        },
                    },
                },
            )
            write_json(
                output / "reports" / "metrics" / "correctness_summary.json",
                {
                    "alignment": {
                        "physical": {"iterations_compared": 99, "complete_alignment": True},
                        "control": {"iterations_compared": 99, "complete_alignment": True},
                    },
                    "physical": {
                        "overall": {
                            "variable_count": 7,
                            "comparable_value_count": 693,
                            "pooled_rmse": 0.02,
                            "mean_variable_rmse": 0.03,
                            "max_abs_error": 0.2,
                        }
                    },
                    "control": {
                        "overall": {
                            "actuator_count": 3,
                            "comparable_state_count": 297,
                            "mismatch_count": 1,
                            "mismatch_rate": 1 / 297,
                            "switch_match_rate": 1,
                            "switch_exact_match_rate": 0.9,
                            "mean_actuator_switch_error_iterations": 0.1,
                            "max_switch_error_iterations": 1,
                        }
                    },
                },
            )
            write_json(
                output / "reports" / "metrics" / "propagation_summary.json",
                {
                    "scenario": "mitm",
                    "timeline": {
                        "tA_attack": {"iteration": 10},
                        "tC_communication": {"iteration": 10},
                        "tU_control": {"iteration": 11},
                        "tP_physical": {"iteration": 12},
                        "tAttackEnd": {"iteration": 15},
                    },
                    "delays": {
                        "attack_to_communication": {"iterations": 0, "wall_clock_sec": 0.025},
                        "communication_to_control": {"iterations": 1},
                        "control_to_physical": {"iterations": 1},
                        "attack_to_physical": {"iterations": 2},
                    },
                    "physical": {
                        "overall": {
                            "mean_rmse": 0.5,
                            "peak_abs_deviation": 1.5,
                            "auc_abs_deviation": 120,
                        }
                    },
                    "recovery": {
                        "status": "recovered",
                        "not_recovered": False,
                        "recovery_iteration": 18,
                        "recovery_iterations": 3,
                        "hydraulic_time_sec": 900,
                    },
                },
            )

            # A runtime directory is accepted directly and still discovers
            # report-side analyzer products beside it.
            summary = build_run_summary(output / "runtime")

            self.assertEqual("delay_10_run_2", summary["experiment_id"])
            self.assertEqual(10, summary["delay_ms"])
            self.assertEqual("success", summary["run_status"])
            self.assertTrue(summary["complete"])
            self.assertTrue(summary["quality_complete"])
            self.assertEqual(12.5, summary["runtime_sec"])
            self.assertEqual(23.5, summary["modbus_rtt_ms"])
            self.assertEqual("link_trace", summary["network_metric_source"])
            self.assertEqual(10.2, summary["network_mean_delay_ms"])
            self.assertEqual(0.02, summary["physical_pooled_rmse"])
            self.assertAlmostEqual(1 / 297, summary["actuator_mismatch_rate"])
            self.assertEqual(25, summary["attack_to_comm_ms"])
            self.assertEqual(3, summary["recovery_iterations"])
            self.assertTrue(all(summary[f"{name}_available"] for name in (
                "manifest", "performance", "network", "correctness", "propagation"
            )))
            self.assertEqual(1, summary["conflict_count"])
            self.assertEqual("experiment_id", summary["conflicts"][0]["field"])

            outputs = write_run_summary(summary, output / "reports" / "metrics")
            saved = json.loads(outputs["json"].read_text(encoding="utf-8"))
            self.assertEqual(list(SUMMARY_COLUMNS), list(saved))
            with outputs["csv"].open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(1, len(rows))
            self.assertEqual(list(SUMMARY_COLUMNS), list(rows[0]))
            self.assertEqual("delay_10_run_2", rows[0]["experiment_id"])
            self.assertEqual("10.2", rows[0]["network_mean_delay_ms"])

    def test_missing_sources_remain_blank_and_are_traceable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            write_json(
                output / "manifest.json",
                {"experiment_id": "metadata_only", "group": "baseline"},
            )

            summary = build_run_summary(output)

            self.assertEqual("metadata_only", summary["experiment_id"])
            self.assertIsNone(summary["run_status"])
            self.assertIsNone(summary["complete"])
            self.assertIsNone(summary["quality_complete"])
            self.assertIsNone(summary["runtime_sec"])
            self.assertIsNone(summary["modbus_requests"])
            self.assertIsNone(summary["network_loss_rate"])
            self.assertTrue(summary["manifest_available"])
            self.assertFalse(summary["performance_available"])
            self.assertEqual(["not_found"], summary["source_errors"]["performance"])
            self.assertIn("runtime_sec", summary["unavailable_fields"])

            outputs = write_run_summary(summary, output / "metrics")
            with outputs["csv"].open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual("", row["runtime_sec"])
            self.assertEqual("", row["modbus_requests"])
            self.assertEqual("", row["complete"])

    def test_failed_lifecycle_wins_and_invalid_source_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            write_json(output / "runtime" / "manifest.json", {"experiment_id": "failed"})
            write_json(
                output / "reports" / "metrics" / "performance_summary.json",
                {
                    "run_status": "cleanup_error",
                    "complete": False,
                    "quality_complete": False,
                    "iteration_time": {"count": 4},
                },
            )
            write_json(
                output / "runtime" / "network" / "network-aggregate.json",
                {"run_status": "success", "complete": True, "status": "no_data"},
            )
            invalid = output / "reports" / "metrics" / "correctness_summary.json"
            invalid.parent.mkdir(parents=True, exist_ok=True)
            invalid.write_text("{not valid json", encoding="utf-8")

            summary = build_run_summary(output)

            self.assertEqual("cleanup_error", summary["run_status"])
            self.assertFalse(summary["complete"])
            self.assertFalse(summary["quality_complete"])
            self.assertEqual(4, summary["iteration_count"])
            self.assertFalse(summary["correctness_available"])
            self.assertTrue(summary["source_errors"]["correctness"])
            conflict_fields = {item["field"] for item in summary["conflicts"]}
            self.assertEqual({"run_status", "complete"}, conflict_fields)


if __name__ == "__main__":
    unittest.main()
