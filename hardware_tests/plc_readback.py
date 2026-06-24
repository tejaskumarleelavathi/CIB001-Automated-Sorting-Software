# plc_readback.py -- read back holding registers and coils from PLC
# Confirms what values are actually stored after writes.
from pymodbus.client import ModbusTcpClient

PLC_IP = "172.16.9.11"

c = ModbusTcpClient(PLC_IP, port=502)
c.connect()

print("Holding registers 0-15:")
r = c.read_holding_registers(address=0, count=16)
if r.isError():
    print(f"  ERROR: {r}")
else:
    for i, v in enumerate(r.registers):
        print(f"  reg[{i}] = {v}")

print("\nCoils 0-7:")
r2 = c.read_coils(address=0, count=8)
if r2.isError():
    print(f"  ERROR: {r2}")
else:
    for i, v in enumerate(r2.bits[:8]):
        print(f"  coil[{i}] = {int(v)}")

c.close()
