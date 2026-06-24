"""
PLC quick tool — read and write coils/registers from command line.

Usage:
    python plc.py read coil 0
    python plc.py read coil 0 10        (read 10 coils starting at 0)
    python plc.py read reg 0
    python plc.py read reg 0 10         (read 10 registers starting at 0)
    python plc.py write coil 0 on
    python plc.py write coil 0 off
    python plc.py write reg 0 42
"""

import sys
from pymodbus.client import ModbusTcpClient

PLC_IP   = "172.16.9.11"
PLC_PORT = 502

c = ModbusTcpClient(PLC_IP, port=PLC_PORT)
c.connect()

args = sys.argv[1:]  # e.g. ["write", "coil", "0", "on"]

if not args:
    print(__doc__)

elif args[0] == "read" and args[1] == "coil":
    addr  = int(args[2])
    count = int(args[3]) if len(args) > 3 else 1
    r = c.read_coils(address=addr, count=count)
    for i, v in enumerate(r.bits[:count]):
        print(f"  coil {addr + i} = {'ON' if v else 'off'}")

elif args[0] == "read" and args[1] == "reg":
    addr  = int(args[2])
    count = int(args[3]) if len(args) > 3 else 1
    r = c.read_holding_registers(address=addr, count=count)
    for i, v in enumerate(r.registers[:count]):
        print(f"  reg {addr + i} = {v}")

elif args[0] == "write" and args[1] == "coil":
    addr  = int(args[2])
    value = args[3].lower() in ("on", "1", "true")
    r = c.write_coil(address=addr, value=value)
    if r.isError():
        print(f"  Failed: {r}")
    else:
        rb = c.read_coils(address=addr, count=1)
        print(f"  coil {addr} = {'ON' if rb.bits[0] else 'off'}")

elif args[0] == "write" and args[1] == "reg":
    addr  = int(args[2])
    value = int(args[3])
    r = c.write_register(address=addr, value=value)
    if r.isError():
        print(f"  Failed: {r}")
    else:
        rb = c.read_holding_registers(address=addr, count=1)
        print(f"  reg {addr} = {rb.registers[0]}")

else:
    print("Unknown command. Run without args to see usage.")

c.close()
