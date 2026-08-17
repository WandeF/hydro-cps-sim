#!/usr/bin/env python3
"""Capture request-side and server-side Modbus/TCP packet timestamps.

The normal SCADA observer measures request start and completion in one process.
This optional tracer runs inside the SCADA and PLC network namespaces and
records the four packet-boundary events needed for one-way/processing/RTT
decomposition.  It is observational: malformed or fragmented packets are
ignored and never affect the protocol path.
"""
from __future__ import annotations

import argparse
import csv
import ipaddress
import os
import socket
import struct
import time
from pathlib import Path
from typing import Any, Iterable

import yaml

from src.metrics.event_logger import _append_csv_rows


TRACE_FIELDS = (
    "wall_time_ns",
    "monotonic_ns",
    "role",
    "event_type",
    "plc_id",
    "local_ip",
    "peer_ip",
    "source_ip",
    "destination_ip",
    "source_port",
    "destination_port",
    "transaction_id",
    "protocol_id",
    "unit_id",
    "function_code",
    "address",
    "tcp_sequence",
    "tcp_ack",
    "tcp_flags",
    "payload_length",
    "packet_type",
)

PACKET_OUTGOING = 4


def _ip(value: Any) -> str:
    return str(ipaddress.ip_interface(str(value)).ip)


def trace_specs(config_path: Path | str) -> list[dict[str, str]]:
    """Return SCADA/PLC capture specifications from one experiment config."""
    config_path = Path(config_path)
    with config_path.open(encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}
    metrics = cfg.get("metrics", {}) or {}
    raw = metrics.get("modbus_packet_trace", {}) if isinstance(metrics, dict) else {}
    if raw is True:
        raw = {"enabled": True, "targets": ["PLC4"]}
    if not isinstance(raw, dict) or not bool(raw.get("enabled", False)):
        return []
    targets = raw.get("targets", [raw.get("target", "PLC4")])
    if isinstance(targets, str):
        targets = [targets]
    requested = {str(item) for item in (targets or [])}

    network = cfg.get("network", {}) or {}
    endpoints = {
        str(item.get("name")): item
        for item in (network.get("nodes", {}) or {}).get("endpoints", [])
        if isinstance(item, dict) and item.get("name")
    }
    addresses: dict[str, str] = {}
    for lan in network.get("lans", []) or []:
        if not isinstance(lan, dict):
            continue
        for name, interface in (lan.get("interfaces", {}) or {}).items():
            if isinstance(interface, dict) and interface.get("ip"):
                addresses[str(name)] = _ip(interface["ip"])

    scada_names = [
        name
        for name, item in endpoints.items()
        if str(item.get("role", "")).strip().lower() == "scada"
    ]
    if len(scada_names) != 1:
        raise ValueError("modbus packet trace requires exactly one SCADA endpoint")
    scada = scada_names[0]
    if scada not in addresses:
        raise ValueError(f"SCADA endpoint {scada} has no LAN IP address")

    specs: list[dict[str, str]] = []
    for target in sorted(requested):
        if target not in endpoints:
            raise ValueError(f"packet trace target is not a network endpoint: {target}")
        if target not in addresses:
            raise ValueError(f"packet trace target {target} has no LAN IP address")
        specs.extend(
            [
                {
                    "role": "scada",
                    "namespace": str(endpoints[scada].get("namespace", "")),
                    "local_ip": addresses[scada],
                    "peer_ip": addresses[target],
                    "plc_id": target,
                },
                {
                    "role": "plc",
                    "namespace": str(endpoints[target].get("namespace", "")),
                    "local_ip": addresses[target],
                    "peer_ip": addresses[scada],
                    "plc_id": target,
                },
            ]
        )
    for spec in specs:
        if not spec["namespace"]:
            raise ValueError(f"endpoint for {spec['role']} trace has no namespace")
    return specs


def _ipv4_tcp_payload(frame: bytes) -> dict[str, Any] | None:
    """Parse one Ethernet/IPv4/TCP frame without attempting stream reassembly."""
    if len(frame) < 14:
        return None
    offset = 14
    ether_type = struct.unpack_from("!H", frame, 12)[0]
    while ether_type in {0x8100, 0x88A8}:
        if len(frame) < offset + 4:
            return None
        ether_type = struct.unpack_from("!H", frame, offset + 2)[0]
        offset += 4
    if ether_type != 0x0800 or len(frame) < offset + 20:
        return None
    version_ihl = frame[offset]
    if version_ihl >> 4 != 4:
        return None
    ip_header_len = (version_ihl & 0x0F) * 4
    if ip_header_len < 20 or len(frame) < offset + ip_header_len:
        return None
    total_len = struct.unpack_from("!H", frame, offset + 2)[0]
    flags_fragment = struct.unpack_from("!H", frame, offset + 6)[0]
    if flags_fragment & 0x1FFF:  # Non-initial fragment has no TCP header.
        return None
    if frame[offset + 9] != socket.IPPROTO_TCP:
        return None
    source_ip = socket.inet_ntoa(frame[offset + 12 : offset + 16])
    destination_ip = socket.inet_ntoa(frame[offset + 16 : offset + 20])
    tcp_offset = offset + ip_header_len
    if len(frame) < tcp_offset + 20:
        return None
    source_port, destination_port, sequence, ack = struct.unpack_from(
        "!HHII", frame, tcp_offset
    )
    tcp_header_len = (frame[tcp_offset + 12] >> 4) * 4
    if tcp_header_len < 20:
        return None
    payload_offset = tcp_offset + tcp_header_len
    ip_end = min(len(frame), offset + total_len)
    if payload_offset > ip_end:
        return None
    return {
        "source_ip": source_ip,
        "destination_ip": destination_ip,
        "source_port": source_port,
        "destination_port": destination_port,
        "tcp_sequence": sequence,
        "tcp_ack": ack,
        "tcp_flags": frame[tcp_offset + 13],
        "payload": frame[payload_offset:ip_end],
    }


def parse_modbus_frame(
    frame: bytes,
    *,
    role: str,
    local_ip: str,
    peer_ip: str,
    plc_id: str,
    packet_type: int = 0,
    port: int = 502,
) -> dict[str, Any] | None:
    parsed = _ipv4_tcp_payload(frame)
    if parsed is None:
        return None
    outgoing = parsed["source_ip"] == local_ip and parsed["destination_ip"] == peer_ip
    incoming = parsed["source_ip"] == peer_ip and parsed["destination_ip"] == local_ip
    if not (outgoing or incoming):
        return None
    if outgoing and parsed["destination_port"] == port:
        event_type = "request_send_scada" if role == "scada" else ""
    elif incoming and parsed["destination_port"] == port:
        event_type = "request_arrive_plc" if role == "plc" else ""
    elif outgoing and parsed["source_port"] == port:
        event_type = "response_send_plc" if role == "plc" else ""
    elif incoming and parsed["source_port"] == port:
        event_type = "response_arrive_scada" if role == "scada" else ""
    else:
        event_type = ""
    if not event_type:
        return None

    payload = parsed.pop("payload")
    if len(payload) < 8:
        return None
    transaction_id, protocol_id, length = struct.unpack_from("!HHH", payload, 0)
    if protocol_id != 0 or length < 2 or length + 6 > len(payload):
        return None
    unit_id = payload[6]
    function_code = payload[7]
    address = struct.unpack_from("!H", payload, 8)[0] if len(payload) >= 10 else ""
    return {
        "role": role,
        "event_type": event_type,
        "plc_id": plc_id,
        "local_ip": local_ip,
        "peer_ip": peer_ip,
        **parsed,
        "transaction_id": transaction_id,
        "protocol_id": protocol_id,
        "unit_id": unit_id,
        "function_code": function_code,
        "address": address,
        "payload_length": len(payload),
        "packet_type": packet_type,
    }


def capture(
    *,
    role: str,
    local_ip: str,
    peer_ip: str,
    plc_id: str,
    output: Path,
    stop_file: Path,
    port: int = 502,
) -> int:
    if role not in {"scada", "plc"}:
        raise ValueError("role must be scada or plc")
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
    sock.settimeout(0.2)
    captured = 0
    try:
        while not stop_file.exists():
            try:
                frame, address = sock.recvfrom(65535)
            except socket.timeout:
                continue
            monotonic_ns = time.monotonic_ns()
            wall_time_ns = time.time_ns()
            packet_type = int(address[2]) if len(address) > 2 else 0
            row = parse_modbus_frame(
                frame,
                role=role,
                local_ip=local_ip,
                peer_ip=peer_ip,
                plc_id=plc_id,
                packet_type=packet_type,
                port=port,
            )
            if row is None:
                continue
            row["monotonic_ns"] = monotonic_ns
            row["wall_time_ns"] = wall_time_ns
            _append_csv_rows(output, TRACE_FIELDS, [row])
            captured += 1
    finally:
        sock.close()
    return captured


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--list-specs", action="store_true")
    parser.add_argument("--role", choices=("scada", "plc"))
    parser.add_argument("--local-ip")
    parser.add_argument("--peer-ip")
    parser.add_argument("--plc-id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--port", type=int, default=502)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.list_specs:
        if args.config is None:
            raise SystemExit("--list-specs requires --config")
        writer = csv.DictWriter(os.sys.stdout, fieldnames=("role", "namespace", "local_ip", "peer_ip", "plc_id"), delimiter="\t", lineterminator="\n")
        for item in trace_specs(args.config):
            writer.writerow(item)
        return 0
    required = (args.role, args.local_ip, args.peer_ip, args.plc_id, args.output, args.stop_file)
    if any(value is None for value in required):
        raise SystemExit("capture requires --role, --local-ip, --peer-ip, --plc-id, --output and --stop-file")
    count = capture(
        role=args.role,
        local_ip=args.local_ip,
        peer_ip=args.peer_ip,
        plc_id=args.plc_id,
        output=args.output,
        stop_file=args.stop_file,
        port=args.port,
    )
    print(f"[MODBUS-TRACE] captured={count} output={args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
