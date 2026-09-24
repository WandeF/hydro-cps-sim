from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import yaml

from scripts.export_results import export_metric_artifacts
from src.attack.modbus_mitm import EventWriter


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MetricIntegrationTests(unittest.TestCase):
    def test_mitm_event_writer_flushes_asynchronously(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            path = runtime / "csv" / "attack_events.csv"
            writer = EventWriter(path)
            writer.write({
                "timestamp_epoch": "100.5",
                "attack": "mitm",
                "rule": "r1",
                "target": "PLC4",
                "iteration": 20,
                "direction": "response",
                "function_code": 3,
                "transaction_id": 9,
                "variable": "T7",
                "original_value": 1.0,
                "modified_value": 2.0,
            })
            writer.close()

            with path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(1, len(rows))
            self.assertEqual("mitm", rows[0]["attack"])
            self.assertTrue((runtime / "csv" / "events.csv").is_file())

    def test_mitm_writer_bounds_queue_and_exposes_dropped_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            writer = EventWriter(
                runtime / "csv" / "attack_events.csv",
                queue_capacity=1,
            )
            started = threading.Event()
            release = threading.Event()
            original_write = writer._write_sync

            def blocked_write(row):
                started.set()
                release.wait(timeout=2.0)
                original_write(row)

            writer._write_sync = blocked_write  # type: ignore[method-assign]
            row = {
                "timestamp_epoch": "100.5",
                "attack": "mitm",
                "rule": "r1",
                "target": "PLC4",
                "iteration": 20,
                "direction": "response",
                "function_code": 3,
                "transaction_id": 9,
                "variable": "T7",
                "original_value": 1.0,
                "modified_value": 2.0,
            }

            self.assertTrue(writer.write(row))
            self.assertTrue(started.wait(timeout=1.0))
            self.assertTrue(writer.write(row))
            self.assertFalse(writer.write(row))
            release.set()
            writer.close()

            self.assertEqual(2, writer.stats["written"])
            self.assertEqual(1, writer.stats["dropped_queue_full"])
            self.assertEqual(0, writer.stats["unflushed_on_close"])
            saved = json.loads(writer.stats_path.read_text(encoding="utf-8"))
            self.assertEqual(1, saved["dropped_queue_full"])

    def test_mitm_sink_failure_does_not_reach_packet_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            writer = EventWriter(runtime / "csv" / "attack_events.csv")

            def failed_write(row):
                raise OSError("metric disk unavailable")

            writer._write_sync = failed_write  # type: ignore[method-assign]
            self.assertTrue(writer.write({"attack": "mitm", "iteration": 1}))
            writer.close()

            self.assertEqual(1, writer.stats["accepted"])
            self.assertEqual(1, writer.stats["processed"])
            self.assertEqual(1, writer.stats["write_errors"])
            self.assertEqual(0, writer.stats["unflushed_on_close"])

    def test_export_removes_stale_network_targets_when_sources_are_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            reports = root / "reports"
            runtime.mkdir()
            stale_csv = reports / "csv" / "network.csv"
            stale_json = reports / "network" / "network-aggregate.json"
            stale_writer_stats = reports / "metric_writer_stats"
            stale_csv.parent.mkdir(parents=True)
            stale_json.parent.mkdir(parents=True)
            stale_writer_stats.mkdir(parents=True)
            stale_csv.write_text("stale\n", encoding="utf-8")
            stale_json.write_text("{}\n", encoding="utf-8")
            (stale_writer_stats / "modbus-old.json").write_text("{}\n", encoding="utf-8")

            export_metric_artifacts(runtime, reports)

            self.assertFalse(stale_csv.exists())
            self.assertFalse(stale_json.exists())
            self.assertFalse(stale_writer_stats.exists())

    def test_export_copies_metric_writer_quality_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            reports = root / "reports"
            stats = runtime / "raw" / "metric_writer_stats"
            stats.mkdir(parents=True)
            payload = {"writer": "modbus", "accepted": 2, "processed": 2, "written": 2}
            (stats / "modbus-123.json").write_text(json.dumps(payload), encoding="utf-8")

            outputs = export_metric_artifacts(runtime, reports)

            copied = reports / "metric_writer_stats" / "modbus-123.json"
            self.assertEqual(reports / "metric_writer_stats", outputs["metric_writer_stats"])
            self.assertEqual(payload, json.loads(copied.read_text(encoding="utf-8")))

    def test_network_cli_uses_current_config_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            config = root / "config.yaml"
            config.write_text(yaml.safe_dump({
                "output_path": str(output),
                "experiment": {
                    "id": "current-run",
                    "group": "network_delay",
                    "parameter": "delay_ms",
                    "value": 20,
                    "repetition": 3,
                },
                "network": {},
            }), encoding="utf-8")
            network = output / "runtime" / "network"
            network.mkdir(parents=True)
            (network / "link-metrics.csv").write_text(
                "simulation_time_s,link,direction,source,target,configured_delay,configured_data_rate,"
                "configured_error_rate,configured_error_unit,tx_packets,rx_packets,tx_bytes,rx_bytes,"
                "drop_packets,delay_samples,mean_delay_ms,max_delay_ms,pending_packets\n"
                "2,r0-r1,a-to-b,r0,r1,20ms,10Mbps,0,packet,10,10,1000,1000,0,10,20.1,20.2,0\n",
                encoding="utf-8",
            )
            # A manifest for another config must not override this config's ID.
            (output / "runtime" / "manifest.json").write_text(json.dumps({
                "experiment_id": "stale-run",
                "config_file": str(config),
                "config_sha256": hashlib.sha256(b"different").hexdigest(),
            }), encoding="utf-8")

            subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "analyze_network.py"), "--config", str(config)],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            summary = json.loads((network / "network-aggregate.json").read_text(encoding="utf-8"))

            self.assertEqual("network", summary["metric_type"])
            self.assertEqual("current-run", summary["experiment_id"])
            self.assertEqual(3, summary["repetition"])
            self.assertAlmostEqual(
                0.1,
                summary["by_source"]["link_trace"]["mean_abs_delay_error_ms"],
            )


if __name__ == "__main__":
    unittest.main()
