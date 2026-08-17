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
from typing import Any, Callable, Iterable, Mapping, Optional

try:  # pymodbus 2.x
    from pymodbus.client.sync import ModbusTcpClient  # type: ignore
except Exception:  # pymodbus 3.x
    from pymodbus.client import ModbusTcpClient  # type: ignore

BASE_MD_REGISTER = 2048
DEFAULT_UNIT_ID = 1
OperationObserver = Callable[[dict[str, Any]], None]


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
    def __init__(
        self,
        host: str,
        port: int = 502,
        unit_id: int = DEFAULT_UNIT_ID,
        timeout: float = 2.0,
        observer: OperationObserver | None = None,
    ):
        self.host = host
        self.port = int(port)
        self.unit_id = int(unit_id)
        self.timeout = float(timeout)
        self._observer = observer
        try:
            self.client = ModbusTcpClient(host, port=self.port, timeout=self.timeout)
        except TypeError:
            self.client = ModbusTcpClient(host, port=self.port)

    def set_observer(self, observer: OperationObserver | None) -> None:
        """Attach an observer for actual Modbus requests made by this endpoint.

        The observer is deliberately best-effort: metric collection must never
        change control behavior.  One callback is emitted for every underlying
        pymodbus request, including each range in a batched operation.
        """
        self._observer = observer

    def _notify_observer(self, payload: dict[str, Any]) -> None:
        if self._observer is None:
            return
        try:
            self._observer(payload)
        except Exception:
            # Quantitative telemetry is observational and must not make an
            # otherwise healthy Modbus exchange fail.
            pass

    def _request(
        self,
        operation: str,
        address: int,
        count: int,
        fn,
        *args,
        description: str,
        validator: Callable[[Any], None] | None = None,
    ):
        wall_start_ns = time.time_ns()
        monotonic_start_ns = time.monotonic_ns()
        response = None
        error: BaseException | None = None
        try:
            response = _call_with_unit(fn, *args, unit_id=self.unit_id)
            self._raise_if_error(response, description)
            if validator is not None:
                validator(response)
            return response
        except BaseException as exc:
            error = exc
            raise
        finally:
            monotonic_end_ns = time.monotonic_ns()
            wall_end_ns = time.time_ns()
            transaction_id = ""
            function_code = ""
            if response is not None:
                transaction_id = getattr(response, "transaction_id", getattr(response, "transactionId", ""))
                function_code = getattr(response, "function_code", "")
            self._notify_observer(
                {
                    "operation": operation,
                    "host": self.host,
                    "port": self.port,
                    "unit_id": self.unit_id,
                    "address": int(address),
                    "count": int(count),
                    "status": "success" if error is None else "error",
                    "error": "" if error is None else str(error),
                    "error_type": "" if error is None else type(error).__name__,
                    "transaction_id": transaction_id,
                    "function_code": function_code,
                    "wall_start_ns": wall_start_ns,
                    "wall_end_ns": wall_end_ns,
                    "monotonic_start_ns": monotonic_start_ns,
                    "monotonic_end_ns": monotonic_end_ns,
                    "latency_ms": (monotonic_end_ns - monotonic_start_ns) / 1_000_000.0,
                }
            )

    def __enter__(self) -> "ModbusEndpoint":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self, retries: int = 1, delay: float = 0.2) -> None:
        wall_start_ns = time.time_ns()
        monotonic_start_ns = time.monotonic_ns()
        error: BaseException | None = None
        try:
            for attempt in range(max(1, retries)):
                if self.client.connect():
                    return
                if attempt + 1 < retries:
                    time.sleep(delay)
            raise ConnectionError(f"Cannot connect to Modbus endpoint {self.host}:{self.port}")
        except BaseException as exc:
            error = exc
            raise
        finally:
            monotonic_end_ns = time.monotonic_ns()
            self._notify_observer({
                "operation": "connect",
                "host": self.host,
                "port": self.port,
                "unit_id": self.unit_id,
                "address": -1,
                "count": 0,
                "status": "success" if error is None else "error",
                "error": "" if error is None else str(error),
                "error_type": "" if error is None else type(error).__name__,
                "transaction_id": "",
                "function_code": "",
                "wall_start_ns": wall_start_ns,
                "wall_end_ns": time.time_ns(),
                "monotonic_start_ns": monotonic_start_ns,
                "monotonic_end_ns": monotonic_end_ns,
                "latency_ms": (monotonic_end_ns - monotonic_start_ns) / 1_000_000.0,
            })

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

    @staticmethod
    def _validate_real_registers(resp: Any, expected: int, op: str) -> None:
        registers = list(getattr(resp, "registers", []) or [])
        if len(registers) < expected:
            raise RuntimeError(f"{op} returned {len(registers)} registers, expected {expected}")
        for offset in range(0, expected, 2):
            registers_to_float(registers[offset : offset + 2])

    @staticmethod
    def _validate_coil_bits(resp: Any, expected: int, op: str) -> None:
        bits = list(getattr(resp, "bits", []) or [])
        if len(bits) < expected:
            raise RuntimeError(f"{op} returned {len(bits)} bits, expected {expected}")

    def write_real_md(self, md_index: int, value: float) -> None:
        addr = md_to_register(md_index)
        regs = float_to_registers(value)
        self._request(
            "write_holding_registers",
            addr,
            len(regs),
            self.client.write_registers,
            addr,
            regs,
            description=f"write %MD{md_index} addr={addr} value={value}",
        )

    def read_real_md(self, md_index: int) -> float:
        addr = md_to_register(md_index)
        resp = self._request(
            "read_holding_registers",
            addr,
            2,
            self.client.read_holding_registers,
            addr,
            2,
            description=f"read %MD{md_index} addr={addr}",
            validator=lambda response: self._validate_real_registers(response, 2, f"read %MD{md_index}"),
        )
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
            resp = self._request(
                "read_holding_registers",
                addr,
                count,
                self.client.read_holding_registers,
                addr,
                count,
                description=f"batch read %MD{start}..%MD{end} addr={addr} count={count}",
                validator=lambda response, expected=count, first=start, last=end: self._validate_real_registers(
                    response, expected, f"batch read %MD{first}..%MD{last}"
                ),
            )
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
            self._request(
                "write_holding_registers",
                addr,
                len(regs),
                self.client.write_registers,
                addr,
                regs,
                description=f"batch write %MD{start}..%MD{end} addr={addr} regs={len(regs)}",
            )

    def read_coil(self, coil_index: int) -> bool:
        resp = self._request(
            "read_coils",
            int(coil_index),
            1,
            self.client.read_coils,
            int(coil_index),
            1,
            description=f"read coil {coil_index}",
            validator=lambda response: self._validate_coil_bits(response, 1, f"read coil {coil_index}"),
        )
        return bool(resp.bits[0])

    def read_coils(self, coil_indices: Iterable[int]) -> dict[int, bool]:
        """Read multiple coils using batched coil reads."""
        result: dict[int, bool] = {}
        for start, end in _contiguous_ranges(coil_indices):
            count = end - start + 1
            resp = self._request(
                "read_coils",
                int(start),
                count,
                self.client.read_coils,
                int(start),
                count,
                description=f"batch read coils {start}..{end}",
                validator=lambda response, expected=count, first=start, last=end: self._validate_coil_bits(
                    response, expected, f"batch read coils {first}..{last}"
                ),
            )
            bits = list(resp.bits)
            if len(bits) < count:
                raise RuntimeError(f"batch read coils {start}..{end} returned {len(bits)} bits, expected {count}")
            for offset, coil_index in enumerate(range(start, end + 1)):
                result[coil_index] = bool(bits[offset])
        return result

    def write_coil(self, coil_index: int, value: bool) -> None:
        self._request(
            "write_coil",
            int(coil_index),
            1,
            self.client.write_coil,
            int(coil_index),
            bool(value),
            description=f"write coil {coil_index}={value}",
        )

    def write_coils_values(self, values: Mapping[int, bool]) -> None:
        """Write multiple coils. Falls back to single-coil writes when needed."""
        if not values:
            return
        value_map = {int(k): bool(v) for k, v in values.items()}
        for start, end in _contiguous_ranges(value_map.keys()):
            bits = [value_map[i] for i in range(start, end + 1)]
            try:
                self._request(
                    "write_coils",
                    int(start),
                    len(bits),
                    self.client.write_coils,
                    int(start),
                    bits,
                    description=f"batch write coils {start}..{end}",
                )
            except AttributeError:
                for coil_index, value in zip(range(start, end + 1), bits):
                    self.write_coil(coil_index, value)
