from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.generate_experiment_matrix import main as generate_matrix_main
from src.experiment.config_generator import (
    generate_delay_configs,
    generate_parameter_configs,
)
from src.experiment.manifest import build_manifest
from src.experiment.runner import experiment_completed


class ExperimentConfigTests(unittest.TestCase):
    @staticmethod
    def _write_base(root: Path) -> Path:
        base = root / "base.yaml"
        base.write_text(
            yaml.safe_dump(
                {
                    "output_path": str(root / "old-output"),
                    "iterations": 10,
                    "attacks": {"enabled": True, "schedule": [{"type": "dos"}]},
                    "metrics": {"enabled": False},
                    "network": {
                        "measurement": {"enabled": False},
                        "backbone_links": [
                            {
                                "name": "r0-r2",
                                "delay": "2ms",
                                "data_rate": "100Mbps",
                            },
                            {
                                "name": "r2-r3",
                                "delay": "3ms",
                                "data_rate": "50Mbps",
                            },
                        ],
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return base

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
            base = self._write_base(root)
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
            self.assertEqual(4, len({item["experiment"]["id"] for item in configs}))
            self.assertEqual(4, len({item["experiment"]["random_seed"] for item in configs}))
            self.assertTrue(all(item["metrics"]["enabled"] for item in configs))
            self.assertEqual({"2ms", "10ms"}, {item["network"]["backbone_links"][0]["delay"] for item in configs})
            self.assertTrue(all(not item["attacks"]["enabled"] for item in configs))

    def test_all_network_parameters_use_ns3_generation_schema(self) -> None:
        cases = {
            "delay_ms": (12.5, {"delay": "12.5ms"}),
            "loss_rate": (
                0.1,
                {"error_model": {"type": "rate", "unit": "packet", "error_rate": 0.1}},
            ),
            "data_rate_mbps": (25.5, {"data_rate": "25.5Mbps"}),
            "queue_packets": (
                64,
                {"queue": {"type": "DropTailQueue", "max_packets": 64}},
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = self._write_base(root)
            for parameter, (value, expected) in cases.items():
                with self.subTest(parameter=parameter):
                    paths = generate_parameter_configs(
                        base,
                        root / f"configs-{parameter}",
                        parameter=parameter,
                        values=[value],
                        link_names=["r0-r2"],
                        repetitions=1,
                        results_root=root / f"results-{parameter}",
                    )
                    config = yaml.safe_load(paths[0].read_text(encoding="utf-8"))
                    link = config["network"]["backbone_links"][0]
                    for key, expected_value in expected.items():
                        self.assertEqual(expected_value, link[key])
                    self.assertEqual(parameter, config["experiment"]["parameter"])
                    self.assertEqual(["r0-r2"], config["experiment"]["target_links"])
                    if parameter == "loss_rate":
                        self.assertIn("network_loss_0p1_run_01", paths[0].stem)

    def test_iterations_matrix_is_isolated_reproducible_and_instrumented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = generate_parameter_configs(
                self._write_base(root),
                root / "configs",
                parameter="iterations",
                values=[25, 50],
                repetitions=2,
                results_root=root / "results",
                seed_base=700,
            )
            configs = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in paths]
            self.assertEqual({25, 50}, {config["iterations"] for config in configs})
            self.assertEqual(4, len({config["experiment"]["id"] for config in configs}))
            self.assertEqual(4, len({config["output_path"] for config in configs}))
            self.assertEqual(4, len({config["experiment"]["random_seed"] for config in configs}))
            self.assertEqual({701, 702, 703, 704}, {config["experiment"]["random_seed"] for config in configs})
            for config in configs:
                self.assertEqual([], config["experiment"]["target_links"])
                self.assertFalse(config["attacks"]["enabled"])
                self.assertEqual(
                    {"enabled": True, "event_log": True, "communication": True, "resource_monitor": True},
                    config["metrics"],
                )
                measurement = config["network"]["measurement"]
                self.assertTrue(measurement["enabled"])
                self.assertTrue(measurement["flow_monitor"])
                self.assertTrue(measurement["link_metrics"])
                self.assertTrue(measurement["pcap"])

    def test_invalid_values_and_unknown_links_fail_before_writing(self) -> None:
        invalid_cases = [
            ("delay_ms", [-0.1], ["r0-r2"]),
            ("loss_rate", [1.01], ["r0-r2"]),
            ("loss_rate", [float("nan")], ["r0-r2"]),
            ("data_rate_mbps", [0], ["r0-r2"]),
            ("queue_packets", [1.5], ["r0-r2"]),
            ("iterations", [0], []),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = self._write_base(root)
            for index, (parameter, values, links) in enumerate(invalid_cases):
                output_dir = root / f"invalid-{index}"
                with self.subTest(parameter=parameter, values=values):
                    with self.assertRaises(ValueError):
                        generate_parameter_configs(
                            base,
                            output_dir,
                            parameter=parameter,
                            values=values,
                            link_names=links,
                            repetitions=1,
                            results_root=root / f"invalid-results-{index}",
                        )
                    self.assertFalse(output_dir.exists())

            with self.assertRaisesRegex(KeyError, "missing-link"):
                generate_parameter_configs(
                    base,
                    root / "unknown-link",
                    parameter="delay_ms",
                    values=[5],
                    link_names=["missing-link"],
                    repetitions=1,
                    results_root=root / "unknown-link-results",
                )
            self.assertFalse((root / "unknown-link").exists())

    def test_cli_supports_legacy_delay_and_generic_iterations_forms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = self._write_base(root)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = generate_matrix_main(
                    [
                        "--base-config",
                        str(base),
                        "--output-dir",
                        str(root / "legacy-configs"),
                        "--results-root",
                        str(root / "legacy-results"),
                        "--link",
                        "r0-r2",
                        "--delays-ms",
                        "2",
                        "5",
                        "--repetitions",
                        "1",
                    ]
                )
            self.assertEqual(0, result)
            self.assertEqual(2, len(list((root / "legacy-configs").glob("*.yaml"))))

            with contextlib.redirect_stdout(stdout):
                result = generate_matrix_main(
                    [
                        "--base-config",
                        str(base),
                        "--output-dir",
                        str(root / "iteration-configs"),
                        "--results-root",
                        str(root / "iteration-results"),
                        "--parameter",
                        "iterations",
                        "--values",
                        "20",
                        "40",
                        "--repetitions",
                        "1",
                    ]
                )
            self.assertEqual(0, result)
            self.assertEqual(2, len(list((root / "iteration-configs").glob("*.yaml"))))

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
