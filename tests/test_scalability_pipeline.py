from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from scripts.audit_scalability_pipeline import audit
from src.control.st_generation import generate_plc_st
from src.comm.modbus import _read_with_count
from src.physics.data_feed import DataFeed, DataFeedConfig, from_runtime_config


class ScalabilityPipelineTests(unittest.TestCase):
    def test_modbus_read_count_supports_keyword_only_api(self) -> None:
        calls: list[tuple[int, int]] = []

        def read(address: int, *, count: int = 1):
            calls.append((address, count))
            return "ok"

        self.assertEqual("ok", _read_with_count(read, 12, 3))
        self.assertEqual([(12, 3)], calls)

    def test_data_feed_is_reproducible_bounded_and_iteration_specific(self) -> None:
        feed = DataFeed(DataFeedConfig(tags=("DATA_1", "DATA_2"), seed=17))
        first = feed.values(3)
        self.assertEqual(first, feed.values(3))
        self.assertNotEqual(first, feed.values(4))
        self.assertTrue(all(0.0 <= value <= 10.0 for value in first.values()))

    def test_data_feed_discovers_only_source_plc_tags(self) -> None:
        feed = from_runtime_config({
            "plcs": [
                {"name": "PLC1", "scale_role": "source", "sensors": ["DATA_1"]},
                {"name": "PLC2", "scale_role": "execution", "sensors": []},
            ],
            "physics": {"data_feed": {"minimum": 0, "maximum": 10, "precision": 4}},
            "experiment": {"random_seed": 12},
        })
        self.assertEqual(("DATA_1",), feed.config.tags)
        self.assertEqual(12, feed.config.seed)

    def test_st_generation_emits_closed_below_and_open_at_or_above(self) -> None:
        cfg = {
            "plcs": [
                {"name": "PLC1", "sensors": ["DATA_1"], "actuators": [], "controls": []},
                {
                    "name": "PLC2",
                    "sensors": [],
                    "actuators": ["ACT_1"],
                    "controls": [
                        {"action": "closed", "actuator": "ACT_1", "dependant": "DATA_1", "type": "below", "value": 5.0},
                        {"action": "open", "actuator": "ACT_1", "dependant": "DATA_1", "type": "above_equal", "value": 5.0},
                    ],
                },
            ]
        }
        text = generate_plc_st(cfg, cfg["plcs"][1])
        self.assertIn("IF PLC1_DATA_1 < 5.0 THEN", text)
        self.assertIn("IF PLC1_DATA_1 >= 5.0 THEN", text)

    def test_audit_checks_source_transfer_and_execution(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            runtime = output / "runtime/json"
            runtime.mkdir(parents=True)
            cfg = {
                "output_path": str(output),
                "iterations": 1,
                "experiment": {"id": "scale_2_plcs"},
                "plcs": [
                    {
                        "name": "PLC1", "scale_role": "source", "paired_plc": "PLC2",
                        "sensors": ["DATA_1"], "actuators": [], "controls": [],
                    },
                    {
                        "name": "PLC2", "scale_role": "execution", "paired_plc": "PLC1",
                        "sensors": [], "actuators": ["ACT_1"], "controls": [],
                    },
                ],
            }
            config = root / "config.yaml"
            config.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            (runtime / "physics_0000.json").write_text(
                json.dumps({"values": {"DATA_1": 7.25}}), encoding="utf-8"
            )
            (runtime / "scada_poll_0000.json").write_text(json.dumps({
                "plcs": {"PLC1": {"md": {"PLC1_DATA_1": 7.25}}}
            }), encoding="utf-8")
            (runtime / "scada_downlink_0000.json").write_text(json.dumps({
                "plcs": {"PLC2": {"written": {"PLC1_DATA_1": {"value": 7.25}}}}
            }), encoding="utf-8")
            (runtime / "actuator_state_0000.json").write_text(
                json.dumps({"ACT_1": True}), encoding="utf-8"
            )
            rows, summary = audit(config)
            self.assertEqual(1, len(rows))
            self.assertTrue(rows[0]["end_to_end_correct"])
            self.assertTrue(summary["pass"])
            self.assertEqual(1.0, summary["end_to_end_correct_rate"])


if __name__ == "__main__":
    unittest.main()
