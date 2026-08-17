from __future__ import annotations

import unittest

from scripts.run_todo3_experiments import LOSS_LEVELS, build_specs


class Todo3ExperimentTests(unittest.TestCase):
    def test_formal_matrix_is_unique_and_matches_required_counts(self) -> None:
        specs = build_specs()
        self.assertEqual(len(specs), 36)
        self.assertEqual(len({item["id"] for item in specs}), 36)
        counts = {group: sum(item["group"] == group for item in specs) for group in {
            "delay", "loss", "congestion", "timestamps", "sensitivity"
        }}
        self.assertEqual(counts, {
            "delay": 7, "loss": 21, "congestion": 4, "timestamps": 3, "sensitivity": 1,
        })
        self.assertEqual(LOSS_LEVELS[:3], (0.0, 0.005, 0.01))
        self.assertEqual(LOSS_LEVELS[-2:], (0.095, 0.5))
        loss = [item for item in specs if item["group"] == "loss"]
        self.assertTrue(all(item["config"]["iterations"] == 300 for item in loss))
        self.assertEqual(len({item["config"]["experiment"]["ns3_run"] for item in loss}), 21)

    def test_congestion_targets_r0_to_r4_and_sensitivity_is_small(self) -> None:
        specs = build_specs()
        congestion = next(item for item in specs if item["id"] == "controlled_congestion_rho_2p0")["config"]
        link = next(item for item in congestion["network"]["backbone_links"] if item["name"] == "r0-r4")
        self.assertEqual(link["data_rate"], "10Mbps")
        self.assertEqual(link["queue"]["max_packets"], 20)
        self.assertTrue(all(scenario["target"]["endpoint"] == "PLC4" for scenario in congestion["attacks"]["scenarios"]))
        self.assertAlmostEqual(
            sum(float(scenario["traffic"]["rate"].removesuffix("Mbps")) for scenario in congestion["attacks"]["scenarios"]),
            20.0,
            places=6,
        )
        sensitivity = next(item for item in specs if item["group"] == "sensitivity")["config"]
        rule = sensitivity["attacks"]["scenarios"][0]["injection"]["rule"]
        self.assertEqual(rule["original_value"], 4.8)
        self.assertEqual(rule["injected_value"], 4.7)


if __name__ == "__main__":
    unittest.main()
