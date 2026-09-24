from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.attack import openplc_logic
from src.core.config import CoilVar, MdVar, PlcRuntime


def plc4_runtime() -> PlcRuntime:
    return PlcRuntime(
        name="PLC4",
        namespace="ns-plc4",
        ip="192.168.4.1",
        st_path=Path("plc4.st"),
        md_vars={
            "PLC_Ready": MdVar("PLC_Ready", 0, None, "PLC_Ready"),
            "PLC9_T7": MdVar("PLC9_T7", 2, "PLC9", "T7"),
        },
        coil_vars={
            "PLC4_PU10": CoilVar("PLC4_PU10", 1, "PLC4", "PU10"),
            "PLC4_PU11": CoilVar("PLC4_PU11", 2, "PLC4", "PU11"),
        },
    )


class OpenPlcLogicStateHandoffTests(unittest.TestCase):
    def test_snapshot_captures_registers_and_hysteresis_outputs(self) -> None:
        endpoint = mock.MagicMock()
        endpoint.__enter__.return_value = endpoint
        endpoint.read_real_mds.return_value = {0: 1.0, 2: 1.514478}
        endpoint.read_coils.return_value = {1: True, 2: False}

        with mock.patch.object(openplc_logic, "ModbusEndpoint", return_value=endpoint) as endpoint_cls:
            snapshot = openplc_logic._snapshot_runtime_memory(plc4_runtime())

        endpoint_cls.assert_called_once_with("127.0.0.1", port=502, timeout=2.0)
        self.assertEqual(snapshot["md"], {0: 1.0, 2: 1.514478})
        self.assertEqual(snapshot["coils"], {1: True, 2: False})
        self.assertEqual(set(endpoint.read_real_mds.call_args.args[0]), {0, 2})
        self.assertEqual(set(endpoint.read_coils.call_args.args[0]), {1, 2})

    def test_restore_writes_coils_around_inputs_to_erase_zero_scan(self) -> None:
        endpoint = mock.MagicMock()
        endpoint.__enter__.return_value = endpoint
        snapshot = {
            "md": {0: 1.0, 2: 1.514478},
            "coils": {1: True, 2: False},
        }

        with mock.patch.object(openplc_logic, "ModbusEndpoint", return_value=endpoint):
            openplc_logic._restore_runtime_memory(plc4_runtime(), snapshot)

        self.assertEqual(
            endpoint.method_calls,
            [
                mock.call.write_coils_values({1: True, 2: False}),
                mock.call.write_real_mds({0: 1.0, 2: 1.514478}),
                mock.call.write_coils_values({1: True, 2: False}),
            ],
        )

    def test_restart_hands_memory_to_new_runtime_after_modbus_is_ready(self) -> None:
        plc = plc4_runtime()
        runtime = SimpleNamespace(output_dir=Path("output"), plcs={"PLC4": plc})
        proc = mock.MagicMock(pid=321)
        proc.poll.return_value = None
        snapshot = {"md": {2: 1.5}, "coils": {1: True, 2: False}}

        with (
            mock.patch.object(openplc_logic, "load_runtime_config", return_value=runtime),
            mock.patch.object(openplc_logic, "_cleanup_old_plc"),
            mock.patch.object(openplc_logic, "_launch_plc", return_value=proc),
            mock.patch.object(Path, "write_text"),
            mock.patch.object(openplc_logic.time, "sleep"),
            mock.patch.object(openplc_logic, "_wait_for_port", return_value=True),
            mock.patch.object(openplc_logic, "_start_modbus_with_retry") as start_modbus,
            mock.patch.object(openplc_logic, "_restore_runtime_memory") as restore_memory,
        ):
            pid = openplc_logic._restart_runtime(
                Path("config.yaml"),
                Path("runtime"),
                "PLC4",
                "ns-plc4",
                memory_snapshot=snapshot,
            )

        self.assertEqual(pid, 321)
        start_modbus.assert_called_once_with("ns-plc4", 502, timeout=30.0)
        restore_memory.assert_called_once_with(plc, snapshot)


if __name__ == "__main__":
    unittest.main()
