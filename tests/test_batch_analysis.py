from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.analyze_batch import discover_summaries, select_complete_rows


class BatchAnalysisTests(unittest.TestCase):
    def test_failed_runs_are_excluded_by_default(self) -> None:
        rows = [
            {"experiment_id": "ok", "run_status": "success", "complete": True},
            {"experiment_id": "failed", "run_status": "error", "complete": False},
            {"experiment_id": "quality", "run_status": "success", "quality_complete": False},
            {"experiment_id": "incomplete", "run_status": "success", "complete": False},
            {"experiment_id": "legacy"},
        ]
        self.assertEqual(
            ["ok", "legacy"],
            [row["experiment_id"] for row in select_complete_rows(rows)],
        )
        self.assertEqual(5, len(select_complete_rows(rows, include_incomplete=True)))

    def test_exported_network_copy_is_not_counted_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run" / "output"
            runtime_summary = output / "runtime" / "network" / "network-aggregate.json"
            report_summary = output / "reports" / "network" / "network-aggregate.json"
            runtime_summary.parent.mkdir(parents=True)
            report_summary.parent.mkdir(parents=True)
            runtime_summary.write_text("{}\n", encoding="utf-8")
            report_summary.write_text("{}\n", encoding="utf-8")

            self.assertEqual([runtime_summary.resolve()], discover_summaries([output]))

    def test_report_only_network_summary_remains_discoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            report_summary = output / "reports" / "network" / "network-aggregate.json"
            report_summary.parent.mkdir(parents=True)
            report_summary.write_text("{}\n", encoding="utf-8")

            self.assertEqual([report_summary.resolve()], discover_summaries([output]))


if __name__ == "__main__":
    unittest.main()
