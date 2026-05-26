from pymodbus.client import ModbusTcpClient


PLC_IP = "192.168.1.1"
PLC_PORT = 502
WRITE_ADDR = 2050
UNIT_ID = 1

client = ModbusTcpClient(PLC_IP, port=PLC_PORT)
ok = client.connect()
print("connect:", ok)

if ok:
    wr = client.write_registers(WRITE_ADDR, [0x1234, 0x5678], slave=UNIT_ID)
    print("write:", wr)

    rr = client.read_holding_registers(WRITE_ADDR, 2, slave=UNIT_ID)
    print("readback:", rr)
    if not rr.isError():
        print("registers:", rr.registers)

client.close()