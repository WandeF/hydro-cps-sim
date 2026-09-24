from __future__ import annotations

import csv
import math
import tempfile
import unittest
from pathlib import Path

from src.metrics.propagation import analyze_propagation_roots, write_propagation_outputs


def write_csv(path: Path, columns: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_run_series(
    root: Path,
    physics_values: dict[int, float],
    actuator_values: dict[int, bool],
) -> None:
    write_csv(
        root / "reports" / "csv" / "physics.csv",
        ["iteration", "T1", "T2"],
        [
            {"iteration": iteration, "T1": value, "T2": 2.0}
            for iteration, value in sorted(physics_values.items())
        ],
    )
    write_csv(
        root / "reports" / "csv" / "actuator_state.csv",
        ["iteration", "P1"],
        [
            {"iteration": iteration, "P1": value}
            for iteration, value in sorted(actuator_values.items())
        ],
    )


class PropagationMetricsTests(unittest.TestCase):
    def test_full_propagation_chain_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline"
            attack = root / "attack"
            baseline_physics = {iteration: (0.0 if iteration == 0 else 1.0) for iteration in range(9)}
            attack_physics = dict(baseline_physics)
            attack_physics.update({0: 999.0, 4: 1.2, 5: 1.4, 6: 1.005})
            baseline_actuators = {iteration: False for iteration in range(1, 9)}
            attack_actuators = dict(baseline_actuators)
            attack_actuators.update({3: True, 4: True, 5: True})
            write_run_series(baseline, baseline_physics, baseline_actuators)
            write_run_series(attack, attack_physics, attack_actuators)

            report_csv = attack / "reports" / "csv"
            write_csv(
                report_csv / "attack_schedule.csv",
                ["timestamp_epoch", "iteration", "scenario", "event"],
                [
                    {"timestamp_epoch": 101.0, "iteration": 2, "scenario": "alpha", "event": "attack_on"},
                    {"timestamp_epoch": 105.0, "iteration": 5, "scenario": "alpha", "event": "attack_off"},
                ],
            )
            write_csv(
                report_csv / "attack_events.csv",
                [
                    "timestamp_epoch",
                    "iteration",
                    "scenario",
                    "direction",
                    "old_value",
                    "new_value",
                ],
                [
                    {
                        "timestamp_epoch": 101.5,
                        "iteration": 2,
                        "scenario": "alpha",
                        "direction": "response",
                        "old_value": 1.0,
                        "new_value": 5.0,
                    }
                ],
            )
            write_csv(
                report_csv / "scada_timeout_events.csv",
                ["timestamp_epoch", "iteration", "phase", "scenario"],
                [
                    {"timestamp_epoch": 102.0, "iteration": 3, "phase": "poll", "scenario": "alpha"}
                ],
            )

            summary = analyze_propagation_roots(
                baseline,
                attack,
                variables=["T1", "T2"],
                actuators=["P1"],
                scenario="alpha",
                recovery_consecutive_iterations=2,
            )
            timeline = summary["timeline"]
            self.assertEqual(timeline["tA_attack"]["iteration"], 2)
            self.assertEqual(timeline["tC_communication"]["iteration"], 2)
            self.assertEqual(timeline["tU_control"]["iteration"], 3)
            self.assertEqual(timeline["tP_physical"]["iteration"], 4)
            self.assertAlmostEqual(
                summary["delays"]["attack_to_communication"]["wall_clock_sec"],
                0.0,
            )
            self.assertEqual(
                timeline["tA_attack"]["epoch_source"],
                "attack_events:modification",
            )
            self.assertEqual(summary["delays"]["communication_to_control"]["iterations"], 1)
            self.assertEqual(summary["delays"]["control_to_physical"]["hydraulic_time_sec"], 300)

            physical = {row["variable"]: row for row in summary["physical"]["variables"]}
            self.assertAlmostEqual(physical["T1"]["rmse"], math.sqrt(0.2 / 4))
            self.assertAlmostEqual(physical["T1"]["peak_abs_deviation"], 0.4)
            self.assertAlmostEqual(physical["T1"]["auc_abs_deviation"], 180.0)
            self.assertEqual(summary["recovery"]["status"], "recovered")
            self.assertFalse(summary["recovery"]["not_recovered"])
            self.assertEqual(summary["recovery"]["recovery_iteration"], 6)
            self.assertEqual(summary["recovery"]["recovery_iterations"], 1)

            outputs = write_propagation_outputs(summary, root / "metrics")
            self.assertTrue(all(path.is_file() for path in outputs.values()))

    def test_missing_communication_and_no_recovery_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline"
            attack = root / "attack"
            baseline_physics = {iteration: (0.0 if iteration == 0 else 1.0) for iteration in range(7)}
            attack_physics = dict(baseline_physics)
            attack_physics.update({3: 1.2, 4: 1.2, 5: 1.2, 6: 1.2})
            actuator_values = {iteration: False for iteration in range(1, 7)}
            write_run_series(baseline, baseline_physics, actuator_values)
            write_run_series(attack, attack_physics, actuator_values)
            report_csv = attack / "reports" / "csv"
            write_csv(
                report_csv / "attack_schedule.csv",
                ["timestamp_epoch", "iteration", "scenario", "event"],
                [
                    {"timestamp_epoch": 10.0, "iteration": 2, "scenario": "logic", "event": "openplc_logic_start"},
                    {"timestamp_epoch": 12.0, "iteration": 4, "scenario": "logic", "event": "openplc_logic_restore"},
                ],
            )
            write_csv(
                report_csv / "attack_events.csv",
                ["timestamp_epoch", "scenario", "event"],
                [],
            )
            write_csv(
                report_csv / "scada_timeout_events.csv",
                ["timestamp_epoch", "iteration", "phase"],
                [],
            )

            summary = analyze_propagation_roots(
                baseline,
                attack,
                variables=["T1"],
                actuators=["P1"],
                scenario="logic",
                recovery_consecutive_iterations=2,
            )
            self.assertIsNone(summary["timeline"]["tC_communication"]["iteration"])
            self.assertIsNone(summary["timeline"]["tC_communication"]["epoch"])
            self.assertIsNone(summary["timeline"]["tU_control"]["iteration"])
            self.assertEqual(summary["timeline"]["tP_physical"]["iteration"], 3)
            self.assertEqual(summary["recovery"]["status"], "not_recovered")
            self.assertTrue(summary["recovery"]["not_recovered"])
            self.assertIsNone(summary["recovery"]["recovery_iteration"])


if __name__ == "__main__":
    unittest.main()
