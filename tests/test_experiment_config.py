from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from src.experiment.config_generator import generate_delay_configs
from src.experiment.manifest import build_manifest
from src.experiment.runner import experiment_completed


class ExperimentConfigTests(unittest.TestCase):
    def test_resume_requires_successful_simulation_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            events = output / "runtime" / "csv" / "events.csv"
            events.parent.mkdir(parents=True)
            events.write_text(
                "event_type,status\n"
                "simulation_start,started\n"
                "simulation_end,error\n",
                encoding="utf-8",
            )
            self.assertFalse(experiment_completed(output))
            events.write_text(
                "event_type,status\n"
                "simulation_start,started\n"
                "simulation_end,success\n",
                encoding="utf-8",
            )
            self.assertTrue(experiment_completed(output))

    def test_delay_matrix_has_unique_output_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.yaml"
            base.write_text(
                yaml.safe_dump(
                    {
                        "output_path": str(root / "old-output"),
                        "iterations": 10,
                        "network": {
                            "backbone_links": [
                                {"name": "r0-r2", "delay": "2ms", "data_rate": "100Mbps"}
                            ]
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            paths = generate_delay_configs(
                base,
                root / "configs",
                link_names=["r0-r2"],
                delays_ms=[2, 10],
                repetitions=2,
                results_root=root / "results",
            )
            self.assertEqual(4, len(paths))
            configs = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in paths]
            self.assertEqual(4, len({item["output_path"] for item in configs}))
            self.assertTrue(all(item["metrics"]["enabled"] for item in configs))
            self.assertEqual({"2ms", "10ms"}, {item["network"]["backbone_links"][0]["delay"] for item in configs})

    def test_manifest_records_dirty_state_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.yaml"
            config.write_text("output_path: output\niterations: 3\n", encoding="utf-8")
            manifest = build_manifest(config, project_root=Path(__file__).resolve().parents[1])
            self.assertEqual(3, manifest["iterations"])
            self.assertEqual(64, len(manifest["config_sha256"]))
            self.assertIn("commit", manifest["git"])


if __name__ == "__main__":
    unittest.main()
