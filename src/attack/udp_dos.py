#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safe UDP CBR traffic generator for simulated DoS experiments.

This module intentionally uses only normal UDP sockets. It does not use raw
sockets, packet crafting libraries, or external traffic tools.
"""
from __future__ import annotations

import argparse
import re
import signal
import socket
import time
from pathlib import Path
from typing import Any

from src.io.csv import append_jsonl, append_row, raw_dir


_STOP = False
_RATE_RE = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[kmgt]?bps|b/s|bit/s|bits/s)?\s*$", re.IGNORECASE)


def _handle_stop(_signum, _frame) -> None:  # type: ignore[no-untyped-def]
    global _STOP
    _STOP = True


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)


def parse_rate_bps(raw: str) -> float:
    match = _RATE_RE.match(str(raw))
    if not match:
        raise ValueError(f"unsupported DoS rate format: {raw!r}")
    value = float(match.group("value"))
    unit = (match.group("unit") or "bps").lower()
    multiplier = 1.0
    if unit.startswith("k"):
        multiplier = 1_000.0
    elif unit.startswith("m"):
        multiplier = 1_000_000.0
    elif unit.startswith("g"):
        multiplier = 1_000_000_000.0
    elif unit.startswith("t"):
        multiplier = 1_000_000_000_000.0
    bps = value * multiplier
    if bps <= 0:
        raise ValueError(f"DoS rate must be positive: {raw!r}")
    return bps


class DosEventWriter:
    def __init__(self, runtime_dir: Path):
        self.runtime_dir = runtime_dir
        self.csv_path = runtime_dir / "csv" / "attack_events.csv"
        self.raw_path = raw_dir(runtime_dir) / "attack_events.jsonl"
        self.columns = [
            "timestamp_epoch",
            "attack",
            "event",
            "source",
            "target",
            "target_ip",
            "target_port",
            "protocol",
            "rate",
            "packet_size",
            "packets",
            "bytes",
            "message",
        ]

    def write(self, row: dict[str, Any]) -> None:
        payload = {**{col: "" for col in self.columns}, **row}
        append_row(self.csv_path, payload, fixed_columns=self.columns)
        append_jsonl(self.raw_path, payload)


def _write_event(
    events: DosEventWriter,
    args: argparse.Namespace,
    event: str,
    *,
    packets: int,
    bytes_sent: int,
    message: str,
) -> None:
    events.write(
        {
            "timestamp_epoch": f"{time.time():.6f}",
            "attack": args.attack,
            "event": event,
            "source": args.source,
            "target": args.target,
            "target_ip": args.target_host,
            "target_port": args.target_port,
            "protocol": "udp",
            "rate": args.rate,
            "packet_size": args.packet_size,
            "packets": packets,
            "bytes": bytes_sent,
            "message": message,
        }
    )


def run(args: argparse.Namespace) -> int:
    _install_signal_handlers()
    rate_bps = parse_rate_bps(args.rate)
    packet_size = int(args.packet_size)
    if packet_size <= 0:
        raise ValueError("--packet-size must be positive")
    if packet_size > 65_507:
        raise ValueError("--packet-size exceeds maximum UDP payload size")

    events = DosEventWriter(args.runtime_dir)
    payload = bytes((i % 251 for i in range(packet_size)))
    interval = max(packet_size * 8.0 / rate_bps, 0.000001)

    if args.start_after_sec > 0:
        deadline = time.monotonic() + float(args.start_after_sec)
        while not _STOP and time.monotonic() < deadline:
            time.sleep(min(0.1, deadline - time.monotonic()))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    packets = 0
    bytes_sent = 0
    tick_packets = 0
    tick_bytes = 0
    next_send = time.monotonic()
    next_tick = next_send + 1.0

    _write_event(events, args, "dos_start", packets=0, bytes_sent=0, message="UDP CBR started")
    print(
        f"[UDP-DOS] start attack={args.attack} source={args.source} "
        f"target={args.target_host}:{args.target_port} rate={args.rate} packet_size={packet_size}",
        flush=True,
    )

    try:
        while not _STOP:
            now = time.monotonic()
            if now < next_send:
                time.sleep(min(next_send - now, 0.05))
                continue

            try:
                sent = sock.sendto(payload, (args.target_host, int(args.target_port)))
            except OSError as exc:
                _write_event(
                    events,
                    args,
                    "dos_stop",
                    packets=packets,
                    bytes_sent=bytes_sent,
                    message=f"socket error: {exc}",
                )
                raise

            packets += 1
            bytes_sent += sent
            tick_packets += 1
            tick_bytes += sent
            next_send += interval

            now = time.monotonic()
            if now >= next_tick:
                _write_event(
                    events,
                    args,
                    "dos_tick",
                    packets=tick_packets,
                    bytes_sent=tick_bytes,
                    message=f"total_packets={packets} total_bytes={bytes_sent}",
                )
                print(
                    f"[UDP-DOS] tick attack={args.attack} packets={tick_packets} "
                    f"bytes={tick_bytes} total_packets={packets} total_bytes={bytes_sent}",
                    flush=True,
                )
                tick_packets = 0
                tick_bytes = 0
                next_tick = now + 1.0
    finally:
        sock.close()
        _write_event(
            events,
            args,
            "dos_stop",
            packets=packets,
            bytes_sent=bytes_sent,
            message="UDP CBR stopped",
        )
        print(f"[UDP-DOS] stop attack={args.attack} packets={packets} bytes={bytes_sent}", flush=True)

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run safe UDP CBR traffic for simulated DoS experiments")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--attack", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--target-host", required=True)
    p.add_argument("--target-port", type=int, required=True)
    p.add_argument("--rate", required=True)
    p.add_argument("--packet-size", type=int, required=True)
    p.add_argument("--runtime-dir", required=True, type=Path)
    p.add_argument("--start-after-sec", type=float, default=0.0)
    return p


def main() -> int:
    args = build_parser().parse_args()
    args.config = args.config.resolve()
    args.runtime_dir = args.runtime_dir.resolve()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
