from __future__ import annotations

import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

from src.metrics.performance import analyze_performance, write_performance_outputs


def write_csv(path: Path, columns: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


class PerformanceMetricsTests(unittest.TestCase):
    def test_complete_run_metrics_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            runtime = output / "runtime"
            write_csv(
                runtime / "csv" / "cycle_timing.csv",
                ["iteration", "cycle_total_sec"],
                [
                    {"iteration": 1, "cycle_total_sec": 1},
                    {"iteration": 2, "cycle_total_sec": 2},
                    {"iteration": 3, "cycle_total_sec": 3},
                    {"iteration": 4, "cycle_total_sec": 4},
                ],
            )
            write_csv(
                runtime / "csv" / "resources.csv",
                ["timestamp_ns", "component", "pid", "cpu_percent", "rss_bytes"],
                [
                    {"timestamp_ns": 100, "component": "coordinator", "pid": 1, "cpu_percent": 10, "rss_bytes": 100},
                    {"timestamp_ns": 100, "component": "plc", "pid": 2, "cpu_percent": 20, "rss_bytes": 200},
                    # PID 2 is also visible through a monitored process tree; it
                    # must not be counted twice in the aggregate.
                    {"timestamp_ns": 100, "component": "coordinator", "pid": 2, "cpu_percent": 19, "rss_bytes": 200},
                    {"timestamp_ns": 200, "component": "coordinator", "pid": 1, "cpu_percent": 40, "rss_bytes": 150},
                    {"timestamp_ns": 200, "component": "plc", "pid": 2, "cpu_percent": 10, "rss_bytes": 250},
                ],
            )
            write_csv(
                runtime / "csv" / "communication.csv",
                ["request_id", "status", "warmup", "latency_ms", "error_type", "error"],
                [
                    {"request_id": "1", "status": "success", "latency_ms": 10},
                    {"request_id": "2", "status": "success", "latency_ms": 20},
                    {"request_id": "3", "status": "timeout", "latency_ms": 100},
                    {"request_id": "4", "status": "error", "latency_ms": 5},
                    # Older recorders marked every request in the grace cycle
                    # as warmup.  A successful request is still a valid sample.
                    {"request_id": "5", "status": "success", "warmup": True, "latency_ms": 30},
                    {
                        "request_id": "warmup",
                        "status": "warmup_timeout",
                        "warmup": True,
                        "latency_ms": 100,
                        "error": "request timed out",
                    },
                ],
            )
            write_csv(
                output / "timing" / "run_all_timing.csv",
                ["stage", "start_epoch_ns", "end_epoch_ns", "duration_sec", "status"],
                [
                    {"stage": "Setup", "start_epoch_ns": 1_000_000_000, "end_epoch_ns": 3_000_000_000, "duration_sec": 2, "status": 0},
                    {"stage": "Run persistent closed-loop control", "start_epoch_ns": 3_000_000_000, "end_epoch_ns": 13_000_000_000, "duration_sec": 10, "status": 0},
                    {"stage": "run_all total", "start_epoch_ns": 1_000_000_000, "end_epoch_ns": 13_000_000_000, "duration_sec": 12, "status": 0},
                ],
            )
            runtime.mkdir(parents=True, exist_ok=True)
            with (runtime / "manifest.json").open("w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "experiment_id": "scale_4",
                        "group": "scalability",
                        "iterations": 4,
                        "hydraulic_step_sec": 300,
                    },
                    handle,
                )
            (runtime / "config_resolved.yaml").write_text(
                "metrics:\n  enabled: true\n  communication: true\n",
                encoding="utf-8",
            )
            write_csv(
                runtime / "csv" / "events.csv",
                ["event_type", "status"],
                [{"event_type": "simulation_end", "status": "success"}],
            )
            writer_stats = runtime / "raw" / "metric_writer_stats"
            writer_stats.mkdir(parents=True)
            (writer_stats / "modbus-123.json").write_text(json.dumps({
                "writer": "modbus",
                "accepted": 6,
                "processed": 6,
                "written": 6,
                "write_errors": 0,
                "dropped_queue_full": 0,
                "dropped_after_close": 0,
                "unflushed_on_close": 0,
                "pending": 0,
                "thread_alive": False,
            }), encoding="utf-8")
            explicit_logs = output / "log-volume"
            explicit_logs.mkdir()
            (explicit_logs / "a.log").write_bytes(b"12345")
            (explicit_logs / "b.csv").write_bytes(b"1234567")

            summary = analyze_performance(output, log_roots=[explicit_logs])

            self.assertEqual("success", summary["run_status"])
            self.assertTrue(summary["complete"])
            self.assertTrue(summary["quality_complete"])
            self.assertEqual(6, summary["metric_writers"]["accepted"])
            self.assertEqual(6, summary["metric_writers"]["written"])
            self.assertEqual(0, summary["metric_writers"]["dropped_total"])

            iteration = summary["iteration_time"]
            self.assertEqual(4, iteration["count"])
            self.assertEqual(10.0, iteration["total_sec"])
            self.assertEqual(2.5, iteration["mean_sec"])
            self.assertAlmostEqual(math.sqrt(5 / 3), iteration["std_sec"])
            self.assertAlmostEqual(3.85, iteration["p95_sec"])

            self.assertEqual(12.0, summary["runtime"]["wall_clock_sec"])
            self.assertEqual(10.0, summary["runtime"]["simulation_wall_clock_sec"])
            self.assertEqual(120.0, summary["runtime"]["real_time_factor"])
            self.assertEqual(2, summary["stages"]["count"])
            self.assertEqual(
                2.0, summary["stages"]["by_name"]["Setup"]["total_sec"]
            )

            resources = summary["resources"]
            self.assertEqual(2, resources["sample_count"])
            self.assertEqual(2, resources["unique_process_count"])
            self.assertEqual(40.0, resources["mean_aggregate_cpu_percent"])
            self.assertEqual(50.0, resources["peak_aggregate_cpu_percent"])
            self.assertEqual(400.0, resources["peak_aggregate_rss_bytes"])

            communication = summary["communication"]
            self.assertEqual(6, communication["row_count"])
            self.assertEqual(5, communication["request_count"])
            self.assertEqual(1, communication["warmup_count"])
            self.assertEqual(3, communication["success_count"])
            self.assertEqual(1, communication["timeout_count"])
            self.assertEqual(0.6, communication["success_rate"])
            self.assertEqual(0.2, communication["timeout_rate"])
            self.assertEqual(20.0, communication["mean_rtt_ms"])
            self.assertAlmostEqual(29.0, communication["p95_rtt_ms"])
            self.assertEqual(12, summary["logs"]["total_bytes"])
            self.assertEqual(3.0, summary["logs"]["bytes_per_iteration"])

            outputs = write_performance_outputs(summary, output / "metrics")
            self.assertTrue(outputs["json"].is_file())
            self.assertTrue(outputs["summary_csv"].is_file())
            with outputs["summary_csv"].open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual("performance", row["metric_type"])
            self.assertEqual("success", row["run_status"])
            self.assertEqual("True", row["complete"])
            self.assertEqual("True", row["quality_complete"])
            self.assertEqual("6", row["metric_writer_accepted"])
            self.assertEqual("120.0", row["real_time_factor"])
            self.assertEqual("2.0", row["stage_setup_sec"])

    def test_runtime_directory_and_missing_files_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            runtime = output / "runtime"
            runtime.mkdir(parents=True)

            summary = analyze_performance(runtime)

            self.assertEqual("incomplete", summary["run_status"])
            self.assertFalse(summary["complete"])
            self.assertTrue(summary["quality_complete"])
            self.assertEqual(0, summary["iteration_time"]["count"])
            self.assertIsNone(summary["iteration_time"]["total_sec"])
            self.assertIsNone(summary["runtime"]["real_time_factor"])
            self.assertEqual(0, summary["resources"]["row_count"])
            self.assertIsNone(summary["resources"]["peak_aggregate_rss_bytes"])
            self.assertEqual(0, summary["communication"]["request_count"])
            self.assertIsNone(summary["communication"]["success_rate"])
            self.assertEqual(0, summary["stages"]["count"])
            self.assertEqual(0, summary["logs"]["file_count"])
            self.assertIsNone(summary["logs"]["bytes_per_iteration"])
            self.assertFalse(any(summary["availability"].values()))

    def test_writer_failure_marks_summary_incomplete_even_after_success_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            runtime = output / "runtime"
            (runtime / "raw" / "metric_writer_stats").mkdir(parents=True)
            (runtime / "config_resolved.yaml").write_text(
                "metrics:\n  enabled: true\n  communication: true\n",
                encoding="utf-8",
            )
            write_csv(
                runtime / "csv" / "events.csv",
                ["event_type", "status"],
                [{"event_type": "simulation_end", "status": "success"}],
            )
            (runtime / "raw" / "metric_writer_stats" / "modbus-1.json").write_text(
                json.dumps({
                    "writer": "modbus",
                    "accepted": 1,
                    "processed": 1,
                    "written": 0,
                    "write_errors": 1,
                    "unflushed_on_close": 0,
                    "pending": 0,
                    "thread_alive": False,
                }),
                encoding="utf-8",
            )

            summary = analyze_performance(output)

            self.assertEqual("cleanup_error", summary["run_status"])
            self.assertFalse(summary["complete"])
            self.assertFalse(summary["quality_complete"])
            self.assertEqual(1, summary["metric_writers"]["accepted"])
            self.assertEqual(0, summary["metric_writers"]["written"])
            self.assertEqual(1, summary["metric_writers"]["write_errors"])

    def test_disabled_metrics_do_not_gate_legacy_attack_writer_stats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            runtime = output / "runtime"
            stats = runtime / "raw" / "metric_writer_stats"
            stats.mkdir(parents=True)
            (runtime / "config_resolved.yaml").write_text(
                "metrics:\n  enabled: false\n",
                encoding="utf-8",
            )
            write_csv(
                runtime / "csv" / "events.csv",
                ["event_type", "status"],
                [{"event_type": "simulation_end", "status": "success"}],
            )
            (stats / "mitm-1.json").write_text(json.dumps({
                "writer": "mitm",
                "accepted": 1,
                "processed": 1,
                "written": 0,
                "write_errors": 1,
            }), encoding="utf-8")

            summary = analyze_performance(output)

            self.assertEqual("success", summary["run_status"])
            self.assertTrue(summary["complete"])
            self.assertTrue(summary["quality_complete"])
            self.assertFalse(summary["metric_writers"]["quality_enforced"])
            self.assertFalse(summary["metric_writers"]["observed_quality_complete"])


if __name__ == "__main__":
    unittest.main()
