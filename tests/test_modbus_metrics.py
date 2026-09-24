from __future__ import annotations

import csv
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

# The development test environment does not need the optional runtime pymodbus
# dependency; install a minimal import shim before loading src.comm.modbus.
if "pymodbus" not in sys.modules:
    pymodbus_module = types.ModuleType("pymodbus")
    client_module = types.ModuleType("pymodbus.client")
    sync_module = types.ModuleType("pymodbus.client.sync")
    sync_module.ModbusTcpClient = object
    client_module.ModbusTcpClient = object
    sys.modules["pymodbus"] = pymodbus_module
    sys.modules["pymodbus.client"] = client_module
    sys.modules["pymodbus.client.sync"] = sync_module

from src.comm.modbus import ModbusEndpoint, float_to_registers
from src.metrics.event_logger import EventLogger
from src.metrics.modbus_metrics import ModbusMetricsRecorder


class _Response:
    def __init__(self, registers=None):
        self.registers = registers or []
        self.transaction_id = 17
        self.function_code = 3

    def isError(self):
        return False


class _Client:
    def read_holding_registers(self, address, count, **kwargs):
        return _Response(float_to_registers(2.5))


class _TimeoutClient:
    def read_holding_registers(self, address, count, **kwargs):
        raise TimeoutError("request timed out")


class _ShortResponseClient:
    def read_holding_registers(self, address, count, **kwargs):
        return _Response([123])


class ModbusMetricsTests(unittest.TestCase):
    @staticmethod
    def _endpoint(client, observer) -> ModbusEndpoint:
        endpoint = ModbusEndpoint.__new__(ModbusEndpoint)
        endpoint.host = "192.0.2.4"
        endpoint.port = 502
        endpoint.unit_id = 1
        endpoint.timeout = 1.0
        endpoint.client = client
        endpoint._observer = observer
        return endpoint

    def test_one_request_produces_pair_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            recorder = ModbusMetricsRecorder(
                runtime,
                event_logger=EventLogger(runtime / "csv" / "events.csv"),
            )
            endpoint = self._endpoint(
                _Client(),
                recorder.observer(iteration=20, target="PLC4", phase="poll"),
            )

            self.assertAlmostEqual(2.5, endpoint.read_real_md(0))
            with (runtime / "csv" / "events.csv").open(newline="", encoding="utf-8") as handle:
                events = list(csv.DictReader(handle))
            with (runtime / "csv" / "communication.csv").open(newline="", encoding="utf-8") as handle:
                requests = list(csv.DictReader(handle))
            self.assertEqual(2, len(events))
            self.assertEqual(events[0]["request_id"], events[1]["request_id"])
            self.assertEqual("modbus_read_success", events[1]["event_type"])
            self.assertEqual(1, len(requests))
            self.assertEqual("success", requests[0]["status"])

    def test_warmup_timeout_is_not_reported_as_attack_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            recorder = ModbusMetricsRecorder(runtime, async_mode=True)
            endpoint = self._endpoint(
                _TimeoutClient(),
                recorder.observer(iteration=1, target="PLC4", phase="poll", warmup=True),
            )

            with self.assertRaises(TimeoutError):
                endpoint.read_real_md(0)
            recorder.close()

            with (runtime / "csv" / "communication.csv").open(newline="", encoding="utf-8") as handle:
                requests = list(csv.DictReader(handle))
            self.assertEqual("warmup_timeout", requests[0]["status"])
            self.assertEqual("True", requests[0]["warmup"])

    def test_success_during_timeout_grace_remains_a_performance_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            recorder = ModbusMetricsRecorder(runtime, async_mode=True)
            endpoint = self._endpoint(
                _Client(),
                recorder.observer(iteration=1, target="PLC4", phase="poll", warmup=True),
            )

            self.assertAlmostEqual(2.5, endpoint.read_real_md(0))
            recorder.close()

            with (runtime / "csv" / "communication.csv").open(newline="", encoding="utf-8") as handle:
                requests = list(csv.DictReader(handle))
            self.assertEqual("success", requests[0]["status"])
            self.assertEqual("False", requests[0]["warmup"])

    def test_async_queue_is_bounded_nonblocking_and_reports_drops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            recorder = ModbusMetricsRecorder(
                runtime,
                emit_events=False,
                async_mode=True,
                queue_capacity=1,
            )
            started = threading.Event()
            release = threading.Event()
            original_record = recorder.record

            def blocked_record(**kwargs):
                started.set()
                release.wait(timeout=2.0)
                return original_record(**kwargs)

            recorder.record = blocked_record  # type: ignore[method-assign]
            observe = recorder.observer(iteration=1, target="PLC4", phase="poll")
            payload = {
                "operation": "read_holding_registers",
                "status": "success",
                "wall_start_ns": 1,
                "wall_end_ns": 2,
                "monotonic_start_ns": 1,
                "monotonic_end_ns": 2,
                "latency_ms": 0.000001,
            }

            observe(payload)
            self.assertTrue(started.wait(timeout=1.0))
            observe(payload)
            observe(payload)  # The bounded queue is full; this call must return.
            release.set()
            recorder.close()

            self.assertEqual(2, recorder.stats["accepted"])
            self.assertEqual(2, recorder.stats["written"])
            self.assertEqual(1, recorder.stats["dropped_queue_full"])
            self.assertEqual(0, recorder.stats["unflushed_on_close"])
            stats_files = list((runtime / "raw" / "metric_writer_stats").glob("modbus-*.json"))
            self.assertEqual(1, len(stats_files))
            saved = json.loads(stats_files[0].read_text(encoding="utf-8"))
            self.assertEqual(1, saved["dropped_queue_full"])

    def test_async_sink_failure_is_observational_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recorder = ModbusMetricsRecorder(
                Path(tmp),
                emit_events=False,
                async_mode=True,
            )

            def failed_record(**kwargs):
                raise OSError("metric disk unavailable")

            recorder.record = failed_record  # type: ignore[method-assign]
            observe = recorder.observer(iteration=1, target="PLC4", phase="poll")
            observe({"operation": "read_holding_registers", "status": "success"})
            recorder.close()

            self.assertEqual(1, recorder.stats["accepted"])
            self.assertEqual(1, recorder.stats["processed"])
            self.assertEqual(1, recorder.stats["write_errors"])
            self.assertEqual(0, recorder.stats["unflushed_on_close"])

    def test_payload_validation_changes_request_status_to_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            recorder = ModbusMetricsRecorder(runtime)
            endpoint = self._endpoint(
                _ShortResponseClient(),
                recorder.observer(iteration=2, target="PLC4", phase="poll"),
            )

            with self.assertRaises(RuntimeError):
                endpoint.read_real_md(0)

            with (runtime / "csv" / "communication.csv").open(newline="", encoding="utf-8") as handle:
                requests = list(csv.DictReader(handle))
            self.assertEqual("error", requests[0]["status"])
            self.assertEqual("RuntimeError", requests[0]["error_type"])


if __name__ == "__main__":
    unittest.main()
