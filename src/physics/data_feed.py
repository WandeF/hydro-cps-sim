import sys
import time
import random
import json
from pathlib import Path

from pymodbus.client.sync import ModbusTcpClient
from pymodbus.payload import BinaryPayloadBuilder, Endian

# =====================
# 配置
# =====================
PLC_IP = "127.0.0.1"
PLC_PORT = 502
UNIT_ID = 1
WRITE_INTERVAL = 1.0  # 秒

# OpenPLC:
# %MD0 -> 2048
BASE_ADDR = 2048


def md_to_register(md_index: int) -> int:
    return BASE_ADDR + md_index * 2


def load_sensors(file_path: str):
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {file_path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("JSON 顶层必须是 list")

    sensors = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i} 项不是对象: {item}")

        for key in ("name", "md_index", "min_val", "max_val"):
            if key not in item:
                raise ValueError(f"第 {i} 项缺少字段 {key}: {item}")

        sensor = {
            "name": str(item["name"]),
            "md_index": int(item["md_index"]),
            "min_val": float(item["min_val"]),
            "max_val": float(item["max_val"]),
        }

        if sensor["min_val"] > sensor["max_val"]:
            raise ValueError(
                f"{sensor['name']} 的 min_val 不能大于 max_val"
            )

        sensors.append(sensor)

    sensors.sort(key=lambda x: x["md_index"])
    return sensors


def gen_random_value(name: str, low: float, high: float) -> float:
    # 反馈量先按 0/1 模拟
    if name.endswith("F"):
        return float(random.choice([0, 1]))
    return round(random.uniform(low, high), 3)


def main():
    if len(sys.argv) != 2:
        print("missing sensors.json")
        sys.exit(1)

    sensors_file = sys.argv[1]
    sensors = load_sensors(sensors_file)

    client = ModbusTcpClient(PLC_IP, port=PLC_PORT)

    print(f"[Modbus Writer] Loading sensors from {sensors_file} ...")
    print(f"[Modbus Writer] Loaded {len(sensors)} sensors.")
    print(f"[Modbus Writer] Connecting to {PLC_IP}:{PLC_PORT} ...")

    if not client.connect():
        print("[Modbus Writer] Connection failed.")
        sys.exit(1)

    print("[Modbus Writer] Connected.")

    try:
        while True:
            values = {}
            builder = BinaryPayloadBuilder(
                byteorder=Endian.Big,
                wordorder=Endian.Big
            )

            for sensor in sensors:
                name = sensor["name"]
                md_index = sensor["md_index"]
                low = sensor["min_val"]
                high = sensor["max_val"]

                val = gen_random_value(name, low, high)
                reg_addr = md_to_register(md_index)

                values[name] = (md_index, reg_addr, val)
                builder.add_32bit_float(val)

            registers = builder.to_registers()

            start_md = sensors[0]["md_index"]
            start_addr = md_to_register(start_md)

            rr = client.write_registers(start_addr, registers, unit=UNIT_ID)

            if rr.isError():
                print("[Modbus Writer] Write failed:", rr)
            else:
                print("=" * 72)
                print(f"[Modbus Writer] Batch write success. Start={start_addr}, regs={len(registers)}")
                for name, (md_index, reg_addr, val) in values.items():
                    print(f"{name:10s}  %MD{md_index:<2d}  reg={reg_addr:<4d}  value={val:>6.3f}")

            time.sleep(WRITE_INTERVAL)

    except KeyboardInterrupt:
        print("\n[Modbus Writer] Stopped by user.")
    finally:
        client.close()
        print("[Modbus Writer] Connection closed.")


if __name__ == "__main__":
    main()