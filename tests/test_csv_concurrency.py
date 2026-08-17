from __future__ import annotations

import csv
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from src.io.csv import append_row


def _append_rows(path: str, worker: int, count: int) -> None:
    target = Path(path)
    for index in range(count):
        append_row(target, {"worker": worker, "index": index}, fixed_columns=["worker", "index"])


class CsvConcurrencyTests(unittest.TestCase):
    def test_append_row_is_safe_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shared.csv"
            workers = [multiprocessing.Process(target=_append_rows, args=(str(path), worker, 40)) for worker in range(8)]
            for process in workers:
                process.start()
            for process in workers:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 320)
        self.assertFalse(any("\x00" in value for row in rows for value in row.values()))


if __name__ == "__main__":
    unittest.main()
