"""Multiple PLC processes must not lose rows when telemetry headers expand."""
import csv
import multiprocessing
from pathlib import Path
import tempfile
import unittest

from src.io.csv import append_row


def write_rows(path, worker, start):
    start.wait(10)
    for iteration in range(40):
        append_row(Path(path), {'worker': worker, 'iteration': iteration,
                               f'plc_{worker}_phase_{iteration // 10}': iteration},
                   fixed_columns=['worker', 'iteration'])


class ConcurrentCsvTests(unittest.TestCase):
    def test_concurrent_header_expansion_preserves_every_row(self):
        context = multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'telemetry.csv'
            start = context.Event()
            workers = [context.Process(target=write_rows, args=(str(path), i, start)) for i in range(8)]
            for worker in workers:
                worker.start()
            start.set()
            try:
                for worker in workers:
                    worker.join(20)
                    self.assertEqual(worker.exitcode, 0)
            finally:
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                    worker.join()
            self.assertNotIn(b'\0', path.read_bytes())
            with path.open() as stream:
                reader = csv.DictReader(stream)
                rows = list(reader)
            self.assertEqual(len(rows), 320)
            self.assertEqual({(int(r['worker']), int(r['iteration'])) for r in rows},
                             {(w, i) for w in range(8) for i in range(40)})
            self.assertEqual(set(reader.fieldnames), {'worker', 'iteration'} |
                             {f'plc_{w}_phase_{phase}' for w in range(8) for phase in range(4)})
            for row in rows:
                self.assertNotIn(None, row)
                key = f"plc_{row['worker']}_phase_{int(row['iteration']) // 10}"
                self.assertEqual(row[key], row['iteration'])


if __name__ == '__main__':
    unittest.main()
