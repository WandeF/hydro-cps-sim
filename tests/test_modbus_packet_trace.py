from __future__ import annotations

import socket
import struct
import tempfile
import unittest
from pathlib import Path

import yaml

from src.metrics.modbus_packet_trace import parse_modbus_frame, trace_specs


def _frame(source_ip: str, destination_ip: str, source_port: int, destination_port: int) -> bytes:
    modbus = struct.pack("!HHHBBHH", 42, 0, 6, 1, 3, 17, 2)
    tcp = struct.pack("!HHIIBBHHH", source_port, destination_port, 100, 200, 5 << 4, 0x18, 4096, 0, 0)
    total_length = 20 + len(tcp) + len(modbus)
    ip = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        total_length,
        1,
        0,
        64,
        socket.IPPROTO_TCP,
        0,
        socket.inet_aton(source_ip),
        socket.inet_aton(destination_ip),
    )
    ethernet = b"\x00" * 12 + struct.pack("!H", 0x0800)
    return ethernet + ip + tcp + modbus


class ModbusPacketTraceTests(unittest.TestCase):
    def test_parses_scada_send_and_plc_arrival(self) -> None:
        frame = _frame("192.168.255.1", "192.168.4.1", 40000, 502)
        scada = parse_modbus_frame(
            frame,
            role="scada",
            local_ip="192.168.255.1",
            peer_ip="192.168.4.1",
            plc_id="PLC4",
        )
        plc = parse_modbus_frame(
            frame,
            role="plc",
            local_ip="192.168.4.1",
            peer_ip="192.168.255.1",
            plc_id="PLC4",
        )
        self.assertEqual(scada["event_type"], "request_send_scada")
        self.assertEqual(plc["event_type"], "request_arrive_plc")
        self.assertEqual(scada["transaction_id"], 42)
        self.assertEqual(scada["function_code"], 3)
        self.assertEqual(scada["address"], 17)

    def test_config_specs_are_disabled_or_scoped(self) -> None:
        config = {
            "metrics": {"modbus_packet_trace": {"enabled": True, "targets": ["PLC4"]}},
            "network": {
                "nodes": {"endpoints": [
                    {"name": "SCADA", "role": "scada", "namespace": "ns-scada"},
                    {"name": "PLC4", "role": "plc", "namespace": "ns-plc4"},
                ]},
                "lans": [
                    {"interfaces": {"SCADA": {"ip": "192.168.255.1/24"}}},
                    {"interfaces": {"PLC4": {"ip": "192.168.4.1/24"}}},
                ],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            specs = trace_specs(path)
        self.assertEqual([item["role"] for item in specs], ["scada", "plc"])
        self.assertEqual(specs[1]["namespace"], "ns-plc4")


if __name__ == "__main__":
    unittest.main()
