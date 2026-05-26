#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Small Modbus helper layer for Hydro-CPS-Sim.

OpenPLC mapping used here:
    %MDx   -> Holding Register 2048 + 2*x, 32-bit REAL, big-endian word order
    %QX0.y -> Coil y

The helper intentionally supports both pymodbus 2.x and 3.x style imports/call
signatures by retrying unit/slave/no-unit kwargs.
"""
from __future__ import annotations

import struct
import time
from typing import Iterable, Mapping, Optional

try:  # pymodbus 2.x
    from pymodbus.client.sync import ModbusTcpClient  # type: ignore
except Exception:  # pymodbus 3.x
    from pymodbus.client import ModbusTcpClient  # type: ignore

BASE_MD_REGISTER = 2048
DEFAULT_UNIT_ID = 1


def md_to_register(md_index: int) -> int:
    return BASE_MD_REGISTER + int(md_index) * 2


def float_to_registers(value: float) -> list[int]:
    raw = struct.pack(">f", float(value))
    return [int.from_bytes(raw[0:2], "big"), int.from_bytes(raw[2:4], "big")]


def registers_to_float(registers: Iterable[int]) -> float:
    regs = list(registers)
    if len(regs) < 2:
        raise ValueError(f"Need at least 2 registers for REAL, got {len(regs)}")
    raw = int(regs[0]).to_bytes(2, "big") + int(regs[1]).to_bytes(2, "big")
    return struct.unpack(">f", raw)[0]


def _candidate_unit_kwargs(unit_id: int) -> list[dict]:
    # Different pymodbus versions use different names. Try the common ones.
    return [{"unit": unit_id}, {"slave": unit_id}, {}]


def _call_with_unit(fn, *args, unit_id: int = DEFAULT_UNIT_ID, **kwargs):
    last_type_error: Optional[TypeError] = None
    for unit_kw in _candidate_unit_kwargs(unit_id):
        try:
            return fn(*args, **kwargs, **unit_kw)
        except TypeError as exc:
            last_type_error = exc
    if last_type_error is not None:
        raise last_type_error
    raise RuntimeError("unreachable pymodbus call path")


def _contiguous_ranges(indices: Iterable[int]) -> list[tuple[int, int]]:
    ordered = sorted({int(i) for i in indices})
    if not ordered:
        return []
    ranges: list[tuple[int, int]] = []
    start = prev = ordered[0]
    for idx in ordered[1:]:
        if idx == prev + 1:
            prev = idx
            continue
        ranges.append((start, prev))
        start = prev = idx
    ranges.append((start, prev))
    return ranges


class ModbusEndpoint:
    def __init__(self, host: str, port: int = 502, unit_id: int = DEFAULT_UNIT_ID, timeout: float = 2.0):
        self.host = host
        self.port = int(port)
        self.unit_id = int(unit_id)
        self.timeout = float(timeout)
        try:
            self.client = ModbusTcpClient(host, port=self.port, timeout=self.timeout)
        except TypeError:
            self.client = ModbusTcpClient(host, port=self.port)

    def __enter__(self) -> "ModbusEndpoint":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self, retries: int = 1, delay: float = 0.2) -> None:
        for attempt in range(max(1, retries)):
            if self.client.connect():
                return
            if attempt + 1 < retries:
                time.sleep(delay)
        raise ConnectionError(f"Cannot connect to Modbus endpoint {self.host}:{self.port}")

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    @staticmethod
    def _raise_if_error(resp, op: str) -> None:
        if resp is None:
            raise RuntimeError(f"{op} returned None")
        is_error = getattr(resp, "isError", None)
        if callable(is_error) and is_error():
            raise RuntimeError(f"{op} failed: {resp}")

    def write_real_md(self, md_index: int, value: float) -> None:
        addr = md_to_register(md_index)
        regs = float_to_registers(value)
        resp = _call_with_unit(self.client.write_registers, addr, regs, unit_id=self.unit_id)
        self._raise_if_error(resp, f"write %MD{md_index} addr={addr} value={value}")

    def read_real_md(self, md_index: int) -> float:
        addr = md_to_register(md_index)
        resp = _call_with_unit(self.client.read_holding_registers, addr, 2, unit_id=self.unit_id)
        self._raise_if_error(resp, f"read %MD{md_index} addr={addr}")
        return registers_to_float(resp.registers)

    def read_real_mds(self, md_indices: Iterable[int]) -> dict[int, float]:
        """Read multiple REAL %MD values using batched holding-register reads.

        Consecutive %MD indexes are grouped into one Modbus request. Non-contiguous
        indexes are split into the minimum number of contiguous requests.
        """
        result: dict[int, float] = {}
        for start, end in _contiguous_ranges(md_indices):
            addr = md_to_register(start)
            count = (end - start + 1) * 2
            resp = _call_with_unit(self.client.read_holding_registers, addr, count, unit_id=self.unit_id)
            self._raise_if_error(resp, f"batch read %MD{start}..%MD{end} addr={addr} count={count}")
            regs = list(resp.registers)
            if len(regs) < count:
                raise RuntimeError(f"batch read %MD{start}..%MD{end} returned {len(regs)} registers, expected {count}")
            for offset, md_index in enumerate(range(start, end + 1)):
                reg_offset = offset * 2
                result[md_index] = registers_to_float(regs[reg_offset : reg_offset + 2])
        return result

    def write_real_mds(self, values: Mapping[int, float]) -> None:
        """Write multiple REAL %MD values using batched holding-register writes."""
        if not values:
            return
        value_map = {int(k): float(v) for k, v in values.items()}
        for start, end in _contiguous_ranges(value_map.keys()):
            regs: list[int] = []
            for md_index in range(start, end + 1):
                regs.extend(float_to_registers(value_map[md_index]))
            addr = md_to_register(start)
            resp = _call_with_unit(self.client.write_registers, addr, regs, unit_id=self.unit_id)
            self._raise_if_error(resp, f"batch write %MD{start}..%MD{end} addr={addr} regs={len(regs)}")

    def read_coil(self, coil_index: int) -> bool:
        resp = _call_with_unit(self.client.read_coils, int(coil_index), 1, unit_id=self.unit_id)
        self._raise_if_error(resp, f"read coil {coil_index}")
        return bool(resp.bits[0])

    def read_coils(self, coil_indices: Iterable[int]) -> dict[int, bool]:
        """Read multiple coils using batched coil reads."""
        result: dict[int, bool] = {}
        for start, end in _contiguous_ranges(coil_indices):
            count = end - start + 1
            resp = _call_with_unit(self.client.read_coils, int(start), count, unit_id=self.unit_id)
            self._raise_if_error(resp, f"batch read coils {start}..{end}")
            bits = list(resp.bits)
            if len(bits) < count:
                raise RuntimeError(f"batch read coils {start}..{end} returned {len(bits)} bits, expected {count}")
            for offset, coil_index in enumerate(range(start, end + 1)):
                result[coil_index] = bool(bits[offset])
        return result

    def write_coil(self, coil_index: int, value: bool) -> None:
        resp = _call_with_unit(self.client.write_coil, int(coil_index), bool(value), unit_id=self.unit_id)
        self._raise_if_error(resp, f"write coil {coil_index}={value}")

    def write_coils_values(self, values: Mapping[int, bool]) -> None:
        """Write multiple coils. Falls back to single-coil writes when needed."""
        if not values:
            return
        value_map = {int(k): bool(v) for k, v in values.items()}
        for start, end in _contiguous_ranges(value_map.keys()):
            bits = [value_map[i] for i in range(start, end + 1)]
            try:
                resp = _call_with_unit(self.client.write_coils, int(start), bits, unit_id=self.unit_id)
                self._raise_if_error(resp, f"batch write coils {start}..{end}")
            except AttributeError:
                for coil_index, value in zip(range(start, end + 1), bits):
                    self.write_coil(coil_index, value)
