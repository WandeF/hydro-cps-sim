from __future__ import annotations

import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

from src.metrics.correctness import analyze_correctness_roots, write_correctness_outputs


def write_csv(path: Path, columns: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


class CorrectnessMetricsTests(unittest.TestCase):
    def test_numeric_control_and_switch_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline"
            platform = root / "platform"
            write_csv(
                baseline / "csv" / "physics.csv",
                ["iteration", "T1", "T2"],
                [
                    {"iteration": 0, "T1": 0, "T2": 0},
                    {"iteration": 1, "T1": 1, "T2": 2},
                    {"iteration": 2, "T1": 2, "T2": 2},
                    {"iteration": 3, "T1": 3, "T2": 2},
                ],
            )
            write_csv(
                platform / "reports" / "csv" / "physics.csv",
                ["iteration", "T1", "T2"],
                [
                    {"iteration": 0, "T1": 999, "T2": 999},
                    {"iteration": 1, "T1": 1.1, "T2": 2},
                    {"iteration": 2, "T1": 2.2, "T2": 2},
                    {"iteration": 3, "T1": 2.7, "T2": 2},
                ],
            )
            write_csv(
                baseline / "csv" / "actuator_state.csv",
                ["iteration", "P1", "P2"],
                [
                    {"iteration": 0, "P1": False, "P2": False},
                    {"iteration": 1, "P1": False, "P2": True},
                    {"iteration": 2, "P1": True, "P2": True},
                    {"iteration": 3, "P1": True, "P2": True},
                    {"iteration": 4, "P1": False, "P2": True},
                ],
            )
            write_csv(
                platform / "reports" / "csv" / "actuator_state.csv",
                ["iteration", "P1", "P2"],
                [
                    {"iteration": 0, "P1": True, "P2": True},
                    {"iteration": 1, "P1": False, "P2": True},
                    {"iteration": 2, "P1": False, "P2": True},
                    {"iteration": 3, "P1": True, "P2": True},
                    {"iteration": 4, "P1": False, "P2": True},
                ],
            )

            summary = analyze_correctness_roots(
                baseline,
                platform,
                variables=["T1", "T2"],
                actuators=["P1", "P2"],
            )

            physical = {row["variable"]: row for row in summary["physical"]["variables"]}
            self.assertEqual(physical["T1"]["count"], 3)
            self.assertAlmostEqual(physical["T1"]["rmse"], math.sqrt(0.14 / 3))
            self.assertAlmostEqual(physical["T1"]["mae"], 0.2)
            self.assertAlmostEqual(physical["T1"]["max_abs_error"], 0.3)
            self.assertEqual(physical["T2"]["rmse"], 0.0)
            self.assertEqual(summary["alignment"]["physical"]["first_iteration"], 1)

            control = {row["actuator"]: row for row in summary["control"]["actuators"]}
            self.assertEqual(control["P1"]["mismatch_count"], 1)
            self.assertAlmostEqual(control["P1"]["mismatch_rate"], 0.25)
            self.assertEqual(control["P1"]["matched_switch_count"], 2)
            self.assertEqual(control["P1"]["switch_match_rate"], 1.0)
            self.assertEqual(control["P1"]["switch_exact_match_rate"], 0.5)
            self.assertEqual(control["P1"]["switch_mean_abs_error_iterations"], 0.5)
            self.assertEqual(control["P1"]["switch_max_abs_error_iterations"], 1)
            self.assertAlmostEqual(summary["control"]["overall"]["mismatch_rate"], 1 / 8)

            outputs = write_correctness_outputs(summary, root / "metrics")
            self.assertTrue(all(path.is_file() for path in outputs.values()))
            with outputs["json"].open("r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["metric_type"], "correctness")

    def test_misaligned_iterations_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline"
            platform = root / "platform"
            for name, rows in (
                (
                    baseline,
                    [
                        {"iteration": 0, "T1": 0},
                        {"iteration": 1, "T1": 1},
                        {"iteration": 2, "T1": 2},
                    ],
                ),
                (
                    platform,
                    [
                        {"iteration": 0, "T1": 0},
                        {"iteration": 2, "T1": 2},
                        {"iteration": 3, "T1": 3},
                    ],
                ),
            ):
                write_csv(name / "csv" / "physics.csv", ["iteration", "T1"], rows)
                write_csv(
                    name / "csv" / "actuator_state.csv",
                    ["iteration", "P1"],
                    [{"iteration": row["iteration"], "P1": False} for row in rows],
                )
            summary = analyze_correctness_roots(baseline, platform)
            alignment = summary["alignment"]["physical"]
            self.assertFalse(alignment["complete_alignment"])
            self.assertEqual(alignment["reference_only_iterations"], [1])
            self.assertEqual(alignment["candidate_only_iterations"], [3])


if __name__ == "__main__":
    unittest.main()
