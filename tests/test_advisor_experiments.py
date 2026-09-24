from __future__ import annotations

import unittest
import json
import ipaddress
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.analyze_advisor_experiments import (
    attack_start_iteration,
    communication_summary,
    group_identical_series,
    mitm_plc4_received_t7_groups,
    read_csv,
    scada_observation_summary,
    strength_group_label,
    t7_series_groups,
    write_csv,
    write_t7_group_data,
)
from scripts.run_advisor_experiments import (
    ATTACK_WINDOW,
    build_specs,
    supersede_valid_records,
)


class AdvisorExperimentTests(unittest.TestCase):
    def test_matrix_has_expected_independent_scans(self) -> None:
        specs = build_specs()
        self.assertEqual(93, len(specs))
        self.assertEqual(93, len({item["id"] for item in specs}))
        self.assertEqual(
            Counter({"baseline": 1, "mitm": 39, "dos": 10, "plc_logic": 35, "scalability": 8}),
            Counter(item["group"] for item in specs),
        )

    def test_mitm_uses_additive_error_and_common_window(self) -> None:
        mitm = [spec for spec in build_specs() if spec["group"] == "mitm"]
        self.assertEqual(
            [value / 10.0 for value in range(0, 36)] + [4.0, 4.5, 5.0],
            [spec["config"]["experiment"]["value"] for spec in mitm],
        )
        for spec in mitm:
            scenario = spec["config"]["attacks"]["scenarios"][0]
            self.assertEqual("add", scenario["rules"][0]["operation"])
            self.assertEqual(
                list(ATTACK_WINDOW),
                [scenario["trigger"]["start_iteration"], scenario["trigger"]["end_iteration"]],
            )
        zero = next(spec["config"] for spec in mitm if spec["config"]["experiment"]["value"] == 0.0)
        self.assertTrue(zero["attacks"]["enabled"])
        self.assertTrue(zero["attacks"]["scenarios"][0]["enabled"])
        self.assertEqual(0.0, zero["attacks"]["scenarios"][0]["rules"][0]["value"])

    def test_dos_offered_load_targets_unique_plc4_bottleneck(self) -> None:
        specs = build_specs()
        self.assertEqual(
            [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0],
            [spec["config"]["experiment"]["value"] for spec in specs if spec["group"] == "dos"],
        )
        dos = next(item for item in specs if item["id"] == "dos_rho_1p5")["config"]
        rates = [float(item["traffic"]["rate"].removesuffix("Mbps")) for item in dos["attacks"]["scenarios"]]
        self.assertAlmostEqual(15.0, sum(rates), places=5)
        self.assertTrue(all(item["target"]["endpoint"] == "PLC4" for item in dos["attacks"]["scenarios"]))
        self.assertTrue(all(item["target"]["ip"] == "192.168.4.1" for item in dos["attacks"]["scenarios"]))
        backbone_rates = {
            link["name"]: link["data_rate"] for link in dos["network"]["backbone_links"]
        }
        self.assertEqual("10Mbps", backbone_rates["r0-r4"])
        self.assertTrue(all(
            rate == "100Mbps"
            for name, rate in backbone_rates.items()
            if name != "r0-r4"
        ))
        self.assertEqual(
            {"r0", "r4"},
            set(next(link for link in dos["network"]["backbone_links"] if link["name"] == "r0-r4")["endpoints"]),
        )
        self.assertTrue(all(lan["data_rate"] == "100Mbps" for lan in dos["network"]["lans"]))

    def test_logic_threshold_is_parameterized(self) -> None:
        specs = build_specs()
        logic = next(item for item in specs if item["id"] == "plc_threshold_shift_m3")["config"]
        rule = logic["attacks"]["scenarios"][0]["injection"]["rule"]
        self.assertEqual("PU10", rule["actuator"])
        self.assertEqual("T7", rule["dependant"])
        self.assertEqual("above", rule["type"])
        self.assertEqual(4.8, rule["original_value"])
        self.assertAlmostEqual(1.8, rule["injected_value"])
        logic_specs = [item for item in specs if item["group"] == "plc_logic"]
        self.assertEqual(
            [value / 10.0 for value in range(-34, 1)],
            [item["config"]["experiment"]["value"] for item in logic_specs],
        )
        self.assertEqual(
            [round(4.8 + value / 10.0, 10) for value in range(-34, 1)],
            [
                item["config"]["attacks"]["scenarios"][0]["injection"]["rule"]["injected_value"]
                for item in logic_specs
            ],
        )
        self.assertTrue(all(
            1.4 <= item["config"]["attacks"]["scenarios"][0]["injection"]["rule"]["injected_value"] <= 4.8
            for item in logic_specs
        ))
        zero = next(item["config"] for item in logic_specs if item["config"]["experiment"]["value"] == 0.0)
        self.assertFalse(zero["attacks"]["enabled"])
        self.assertFalse(zero["attacks"]["scenarios"][0]["enabled"])
        zero_rule = zero["attacks"]["scenarios"][0]["injection"]["rule"]
        self.assertEqual(zero_rule["original_value"], zero_rule["injected_value"])

    def test_scale_matrix_really_changes_topology(self) -> None:
        scales = [item["config"] for item in build_specs() if item["group"] == "scalability"]
        self.assertEqual([2, 4, 6, 8, 16, 32, 64, 128], [len(cfg["plcs"]) for cfg in scales])
        self.assertEqual(
            [10, 16, 22, 28, 52, 100, 196, 388],
            [cfg["experiment"]["topology"]["network_node_count"] for cfg in scales],
        )
        self.assertEqual(
            [6, 10, 14, 18, 34, 66, 130, 258],
            [cfg["experiment"]["topology"]["communication_link_count"] for cfg in scales],
        )

    def test_scale_128_has_unique_executable_plc_branches(self) -> None:
        spec = next(
            item for item in build_specs()
            if item["id"] == "scale_128_plcs"
        )
        cfg = spec["config"]
        network = cfg["network"]
        plc_names = {item["name"] for item in cfg["plcs"]}
        self.assertEqual({f"PLC{number}" for number in range(1, 129)}, plc_names)
        endpoints = {
            item["name"]: item for item in network["nodes"]["endpoints"]
            if item.get("role") == "plc"
        }
        self.assertEqual(plc_names, set(endpoints))
        self.assertEqual(plc_names, {item["endpoint"] for item in network["taps"] if item["endpoint"] != "scada"})
        self.assertEqual(plc_names, {item["dst"] for item in network["applications"]})
        self.assertEqual(0, cfg["experiment"]["physical_process_plc_count"])
        self.assertEqual(0, cfg["experiment"]["synthetic_scale_plc_count"])
        self.assertEqual(64, cfg["experiment"]["data_feed_source_plc_count"])
        self.assertEqual(64, cfg["experiment"]["execution_plc_count"])
        self.assertEqual(64, cfg["experiment"]["communication_pair_count"])
        self.assertTrue(cfg["experiment"]["all_plcs_communication_active"])
        self.assertEqual("data_feed", spec["physics_mode"])
        self.assertEqual("current", spec["init_style"])
        self.assertFalse(spec["run_check"])
        self.assertEqual(640, spec["sync_timeout_sec"])

        sources = [item for item in cfg["plcs"] if item["scale_role"] == "source"]
        executions = [item for item in cfg["plcs"] if item["scale_role"] == "execution"]
        self.assertEqual(64, len(sources))
        self.assertEqual(64, len(executions))
        self.assertTrue(all(len(item["sensors"]) == 1 and not item["actuators"] for item in sources))
        self.assertTrue(all(not item["controls"] for item in sources))
        self.assertTrue(all(not item["sensors"] and len(item["actuators"]) == 1 for item in executions))
        self.assertTrue(all(len(item["controls"]) == 2 for item in executions))
        self.assertEqual(
            {"below", "above_equal"},
            {control["type"] for item in executions for control in item["controls"]},
        )

        subnets = [
            ipaddress.ip_network(item["subnet"])
            for item in network["backbone_links"] + network["lans"]
        ]
        self.assertEqual(len(subnets), len(set(subnets)))
        interface_names = []
        for item in network["backbone_links"] + network["lans"]:
            interface_names.extend(interface["ifname"] for interface in item["interfaces"].values())
        self.assertTrue(all(len(name) <= 15 for name in interface_names))

    def test_analysis_csv_keeps_union_and_mitm_uses_first_real_modification(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_csv(root / "union.csv", [{"dos": 1}, {"mitm": 2}])
            self.assertEqual({"dos", "mitm"}, set(read_csv(root / "union.csv")[0]))
            events = root / "runtime/csv"
            events.mkdir(parents=True)
            write_csv(events / "attack_schedule.csv", [
                {"iteration": 1, "event": "proxy_start", "active": False},
                {"iteration": 15, "event": "attack_on", "active": True},
            ])
            write_csv(events / "attack_events.csv", [
                {"iteration": 15, "event": "", "old_value": 1.0, "new_value": 1.2},
            ])
            self.assertEqual(15, attack_start_iteration(root))

    def test_post_deviation_plc4_update_interval_includes_initial_missing_cycles(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = []
            for iteration in range(4, 9):
                rows.append({
                    "iteration": iteration,
                    "phase": "poll",
                    "warmup": False,
                    "operation": "read_holding_registers",
                    "target": "PLC1",
                    "status": "success",
                    "latency_ms": 1.0,
                })
            for iteration in (6, 8):
                rows.append({
                    "iteration": iteration,
                    "phase": "downlink",
                    "warmup": False,
                    "operation": "write_registers",
                    "target": "PLC4",
                    "status": "success",
                    "latency_ms": 2.0,
                })
            write_csv(root / "runtime/csv/communication.csv", rows)
            summary = communication_summary(
                root,
                first_control_deviation_iteration=4,
            )
            self.assertEqual(5, summary["plc4_post_deviation_observation_cycles"])
            self.assertEqual(2, summary["plc4_post_deviation_successful_control_updates"])
            self.assertAlmostEqual(
                0.4,
                summary["plc4_post_deviation_update_frequency_per_cycle"],
            )
            self.assertAlmostEqual(
                2.5,
                summary["plc4_post_deviation_average_update_interval_cycles"],
            )

    def test_scada_observation_mismatch_uses_same_run_physics_and_rounding_tolerance(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_csv(root / "runtime/csv/scada_observed_wide.csv", [
                {"iteration": 15, "PLC4.PLC4_T3": 1.000004, "PLC4.PLC4_T4": 2.0},
                {"iteration": 16, "PLC4.PLC4_T3": 1.1, "PLC4.PLC4_T4": 2.0},
            ])
            write_csv(root / "runtime/csv/physics.csv", [
                {"iteration": 15, "T3": 1.0, "T4": 2.0},
                {"iteration": 16, "T3": 1.0, "T4": 2.0},
            ])
            summary = scada_observation_summary(root, start=15)
            self.assertEqual(2, summary["scada_observation_variable_count"])
            self.assertEqual(16, summary["scada_first_observation_mismatch_iteration"])
            self.assertEqual(
                ["PLC4.PLC4_T3"],
                summary["scada_first_observation_mismatch_variables"],
            )
            self.assertAlmostEqual(
                0.1,
                summary["scada_first_observation_mismatch_peak_absolute_error"],
            )

    def test_identical_mitm_t7_series_are_merged_by_numeric_tolerance(self) -> None:
        groups = group_identical_series([
            (0.0, "baseline", [(1, 2.5), (2, 2.4)]),
            (3.5, "mitm_3p5", [(1, 2.5), (2, 2.0)]),
            (4.0, "mitm_4", [(1, 2.5), (2, 2.0 + 5e-10)]),
            (4.5, "mitm_4p5", [(1, 2.5), (2, 1.9)]),
        ])
        self.assertEqual(3, len(groups))
        self.assertEqual([3.5, 4.0], groups[1]["strengths"])

    def test_t7_group_export_and_labels_support_each_attack_parameter(self) -> None:
        groups = [{
            "strengths": [0.0, 0.5, 1.0],
            "experiment_ids": ["baseline", "dos_0p5", "dos_1"],
            "points": [(1, 2.5), (2, 2.4)],
        }]
        self.assertEqual(
            "ρ=0, 0.5, 1（轨迹重合）",
            strength_group_label(groups[0]["strengths"], parameter_symbol="ρ"),
        )
        self.assertEqual(
            "无攻击 Δh=0",
            strength_group_label([0.0], parameter_symbol="Δh", zero_description="无攻击"),
        )
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "dos_t7.csv"
            write_t7_group_data(groups, output, parameter_name="rho")
            rows = read_csv(output)
            self.assertEqual(["iteration", "rho_0_0p5_1"], list(rows[0]))
            self.assertEqual("2.5", rows[0]["rho_0_0p5_1"])

    def test_t7_scada_comparison_reads_each_run_report_csv_not_runtime_csv(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            attack = root / "mitm_1"
            for output, report_value, runtime_value in (
                (baseline, 2.5, 92.5),
                (attack, 3.0, 93.0),
            ):
                write_csv(output / "reports/csv/scada_observed_wide.csv", [{
                    "iteration": 1,
                    "PLC9.PLC9_T7": report_value,
                }])
                write_csv(output / "runtime/csv/scada_observed_wide.csv", [{
                    "iteration": 1,
                    "PLC9.PLC9_T7": runtime_value,
                }])
            groups = t7_series_groups(
                [{
                    "group": "mitm",
                    "value": 1.0,
                    "id": "mitm_1",
                    "output_path": attack,
                }],
                baseline,
                experiment_group="mitm",
                source="scada_observed",
            )
            self.assertEqual([2.5], [value for _, value in groups[0]["points"]])
            self.assertEqual([3.0], [value for _, value in groups[1]["points"]])
            self.assertTrue(all(
                "/reports/csv/scada_observed_wide.csv" in source
                for group in groups
                for source in group["source_files"]
            ))

    def test_mitm_plc4_received_t7_adds_bias_only_inside_attack_window(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            attack = root / "mitm_2"
            source_rows = [
                {"iteration": 14, "PLC9.PLC9_T7": 1.4},
                {"iteration": 15, "PLC9.PLC9_T7": 1.5},
                {"iteration": 55, "PLC9.PLC9_T7": 2.0},
                {"iteration": 56, "PLC9.PLC9_T7": 2.1},
            ]
            write_csv(baseline / "reports/csv/scada_observed_wide.csv", source_rows)
            write_csv(attack / "reports/csv/scada_observed_wide.csv", source_rows)
            groups = mitm_plc4_received_t7_groups(
                [{
                    "group": "mitm",
                    "value": 2.0,
                    "id": "mitm_2",
                    "output_path": attack,
                }],
                baseline,
            )
            attack_points = dict(groups[1]["points"])
            self.assertEqual(1.4, attack_points[14])
            self.assertEqual(3.5, attack_points[15])
            self.assertEqual(4.0, attack_points[55])
            self.assertEqual(2.1, attack_points[56])
            self.assertEqual("PLC4", groups[1]["receiver"])

    def test_mitm_zero_strength_uses_its_own_proxy_run_as_reference(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            zero = root / "mitm_0"
            rows = [{"iteration": 15, "PLC9.PLC9_T7": 1.5}]
            write_csv(baseline / "reports/csv/scada_observed_wide.csv", rows)
            write_csv(zero / "reports/csv/scada_observed_wide.csv", rows)
            groups = mitm_plc4_received_t7_groups(
                [{
                    "group": "mitm",
                    "value": 0.0,
                    "id": "mitm_0",
                    "output_path": zero,
                }],
                baseline,
            )
            self.assertEqual(1, len(groups))
            self.assertEqual(["mitm_0"], groups[0]["experiment_ids"])
            self.assertEqual([0.0], groups[0]["strengths"])

    def test_rerun_supersedes_only_selected_valid_records(self) -> None:
        with TemporaryDirectory() as tmp:
            archive = Path(tmp)
            records = [
                {"id": "dos_rho_1", "valid": True},
                {"id": "mitm_bias_1", "valid": True},
                {"id": "dos_rho_1", "valid": False, "attempt": 0},
            ]
            (archive / "RUN_INDEX.json").write_text(json.dumps(records), encoding="utf-8")
            changed = supersede_valid_records(
                archive,
                [{"id": "dos_rho_1"}],
                reason="test correction",
            )
            self.assertEqual(1, changed)
            updated = json.loads((archive / "RUN_INDEX.json").read_text(encoding="utf-8"))
            self.assertFalse(updated[0]["valid"])
            self.assertTrue(updated[0]["superseded"])
            self.assertEqual("test correction", updated[0]["superseded_reason"])
            self.assertTrue(updated[1]["valid"])

    def test_group_rerun_supersedes_obsolete_ids_from_previous_design(self) -> None:
        with TemporaryDirectory() as tmp:
            archive = Path(tmp)
            records = [
                {"id": "plc_threshold_shift_m5", "group": "plc_logic", "valid": True},
                {"id": "plc_threshold_shift_1", "group": "plc_logic", "valid": True},
                {"id": "mitm_bias_1", "group": "mitm", "valid": True},
            ]
            (archive / "RUN_INDEX.json").write_text(json.dumps(records), encoding="utf-8")
            changed = supersede_valid_records(
                archive,
                [{"id": "plc_threshold_shift_1", "group": "plc_logic"}],
                reason="new upper-threshold scan",
                entire_groups={"plc_logic"},
            )
            self.assertEqual(2, changed)
            updated = json.loads((archive / "RUN_INDEX.json").read_text(encoding="utf-8"))
            self.assertFalse(updated[0]["valid"])
            self.assertFalse(updated[1]["valid"])
            self.assertTrue(updated[2]["valid"])


if __name__ == "__main__":
    unittest.main()
