#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plc1_logic_test.py

用途：
    该脚本用于在 PLC1 所在命名空间内部测试 OpenPLC 的 Modbus 读写与 PLC 逻辑是否正确。

测试对象：
    ST 程序中的以下变量：
        %MD1   -> PLC2_T1 : REAL
        %QX0.0 -> PLC1_PU1 : BOOL

测试逻辑：
    IF PLC2_T1 < 4.0 THEN
        PLC1_PU1 := TRUE;
    ELSIF PLC2_T1 > 6.3 THEN
        PLC1_PU1 := FALSE;
    END_IF;

使用方式：
    需要在 PLC1 对应命名空间内运行，例如：

        sudo ip netns exec ns-plc1 python3 src/proxy/plc1_logic_test.py

说明：
    1. 脚本默认连接 127.0.0.1:502，因为它假定自己运行在 ns-plc1 内部。
    2. %MD1 对应 Modbus Holding Register 2050，占 2 个寄存器（32 位 REAL）。
    3. %QX0.0 对应 Coil 0。
    4. 为兼容不同版本 pymodbus，脚本同时兼容 unit/slave 参数差异。
"""

from __future__ import annotations

import struct
import sys
import time
import struct
from pymodbus.client.sync import ModbusTcpClient
_USE_SLAVE_KEY = False


PLC_IP = "192.168.1.1"
PLC_PORT = 502
UNIT_ID = 1

MD1_ADDR = 2050      # %MD1 -> holding register 2050,2051
QX0_0_ADDR = 0       # %QX0.0 -> coil 0


def unit_kw(unit_id: int):
    return {"slave": unit_id} if _USE_SLAVE_KEY else {"unit": unit_id}


def float_to_regs(value: float):
    raw = struct.pack(">f", value)
    return [int.from_bytes(raw[0:2], "big"), int.from_bytes(raw[2:4], "big")]


def regs_to_float(registers):
    raw = registers[0].to_bytes(2, "big") + registers[1].to_bytes(2, "big")
    return struct.unpack(">f", raw)[0]


def read_md1(client):
    rr = client.read_holding_registers(MD1_ADDR, 2, **unit_kw(UNIT_ID))
    if rr.isError():
        raise RuntimeError(f"read %MD1 failed: {rr}")
    return regs_to_float(rr.registers)


def write_md1(client, value: float):
    regs = float_to_regs(value)
    rr = client.write_registers(MD1_ADDR, regs, **unit_kw(UNIT_ID))
    if rr.isError():
        raise RuntimeError(f"write %MD1 failed: {rr}")


def read_qx0_0(client):
    rr = client.read_coils(QX0_0_ADDR, 1, **unit_kw(UNIT_ID))
    if rr.isError():
        raise RuntimeError(f"read %QX0.0 failed: {rr}")
    return bool(rr.bits[0])


def run_case(client, value: float, expected: bool):
    print(f"\n[TEST] write %MD1 = {value}")
    write_md1(client, value)

    time.sleep(0.3)  # task0 周期 100ms，留 300ms 让逻辑执行

    md1 = read_md1(client)
    qx = read_qx0_0(client)

    print(f"read %MD1    = {md1}")
    print(f"read %QX0.0  = {qx}")
    print(f"expected     = {expected}")

    if qx != expected:
        print("[FAIL] logic mismatch")
    else:
        print("[PASS] logic ok")


def main():
    client = ModbusTcpClient(PLC_IP, port=PLC_PORT)
    ok = client.connect()
    print(f"connect: {ok}")
    if not ok:
        raise RuntimeError(f"cannot connect to {PLC_IP}:{PLC_PORT}")

    try:
        run_case(client, 3.5, True)   # PLC2_T1 < 4.0 -> PLC1_PU1 = TRUE
        run_case(client, 6.8, False)  # PLC2_T1 > 6.3 -> PLC1_PU1 = FALSE
    finally:
        client.close()


if __name__ == "__main__":
    main()