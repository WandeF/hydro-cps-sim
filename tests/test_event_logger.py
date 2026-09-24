from __future__ import annotations

import csv
import json
import multiprocessing
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.metrics.event_logger import EVENT_FIELDS, EventLogger, RequestIdFactory, make_event, safe_log, safe_log_many


def _write_events_from_process(path: str, process_index: int, count: int) -> None:
    logger = EventLogger(path)
    events = [
        make_event(
            wall_time_ns=1_000_000 + process_index * count + item,
            monotonic_ns=2_000_000 + process_index * count + item,
            iteration=item,
            layer="communication",
            component=f"worker-{process_index}",
            event_type="modbus_read_success",
            request_id=f"p{process_index}-{item}",
            details={"process": process_index, "item": item},
        )
        for item in range(count)
    ]
    logger.log_many(events)


class EventLoggerTests(unittest.TestCase):
    def test_best_effort_helpers_swallow_sink_failures(self) -> None:
        class BrokenLogger:
            def log(self, event):
                raise OSError("disk full")

            def log_many(self, events):
                raise PermissionError("read only")

        event = make_event(
            iteration=1,
            layer="runtime",
            component="test",
            event_type="test_event",
        )
        self.assertFalse(safe_log(BrokenLogger(), event))  # type: ignore[arg-type]
        self.assertEqual(0, safe_log_many(BrokenLogger(), [event]))  # type: ignore[arg-type]

    def test_fixed_schema_explicit_timestamps_and_json_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.csv"
            logger = EventLogger(path)
            cyclic: list[object] = []
            cyclic.append(cyclic)
            event = make_event(
                wall_time_ns=123,
                monotonic_ns=456,
                iteration=7,
                layer="physical",
                component="epanet",
                event_type="physics_sensor_value",
                variable="T7",
                value={"path": Path("结果"), "items": {3, 1}},
                details={"cyclic": cyclic, "bytes": b"ok"},
            )
            logger.log(event)

            with path.open(newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                self.assertEqual(tuple(reader.fieldnames or ()), EVENT_FIELDS)
                rows = list(reader)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["wall_time_ns"], "123")
            self.assertEqual(rows[0]["monotonic_ns"], "456")
            self.assertEqual(json.loads(rows[0]["value"]), {"items": [1, 3], "path": "结果"})
            self.assertEqual(
                json.loads(rows[0]["details"]),
                {"bytes": "ok", "cyclic": ["<circular-reference>"]},
            )

    @unittest.skipUnless(hasattr(multiprocessing, "get_context"), "multiprocessing unavailable")
    def test_cross_process_appends_have_one_header_and_all_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.csv"
            methods = multiprocessing.get_all_start_methods()
            context = multiprocessing.get_context("fork" if "fork" in methods else "spawn")
            process_count = 4
            rows_per_process = 40
            workers = [
                context.Process(
                    target=_write_events_from_process,
                    args=(str(path), index, rows_per_process),
                )
                for index in range(process_count)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
                self.assertEqual(worker.exitcode, 0)

            with path.open(newline="", encoding="utf-8") as file_obj:
                rows = list(csv.DictReader(file_obj))
            self.assertEqual(len(rows), process_count * rows_per_process)
            self.assertEqual(
                len({row["request_id"] for row in rows}),
                process_count * rows_per_process,
            )
            header_text = ",".join(EVENT_FIELDS)
            self.assertEqual(path.read_text(encoding="utf-8").count(header_text), 1)

    def test_request_id_factory_is_thread_safe(self) -> None:
        factory = RequestIdFactory()

        def generate(index: int) -> str:
            return factory.next_id(20, "PLC 4", f"read md {index % 2}")

        with ThreadPoolExecutor(max_workers=12) as executor:
            request_ids = list(executor.map(generate, range(1_000)))

        self.assertEqual(len(set(request_ids)), 1_000)
        self.assertTrue(all(request_id.startswith("iter-00020-plc-4-read-md-") for request_id in request_ids))
        sequences = sorted(int(request_id.rsplit("-", 1)[1]) for request_id in request_ids)
        self.assertEqual(sequences, list(range(1, 1_001)))


if __name__ == "__main__":
    unittest.main()
