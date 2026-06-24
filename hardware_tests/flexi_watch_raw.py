# flexi_watch_raw.py -- watch raw register values from Sick Flexi for any changes
# Reads input registers 2000-2007 every 200ms and prints any register that changes.
# Do physical operations (press e-stop, block light curtain, press reset)
# and watch which register number changes value.
#
# Usage: python flexi_watch_raw.py

import time
from pymodbus.client import ModbusTcpClient

FLEXI_IP = "172.16.9.12"
START    = 2000
COUNT    = 8

c = ModbusTcpClient(FLEXI_IP, port=502)
c.connect()

prev = None
print(f"Watching input registers {START}-{START+COUNT-1} @ {FLEXI_IP}")
print("Do physical actions (press e-stop, block light curtain, press reset button)")
print("Any register that changes will be highlighted. Ctrl+C to stop.\n")

try:
    while True:
        r = c.read_input_registers(address=START, count=COUNT)
        if r.isError():
            print(f"Read error: {r}")
            time.sleep(0.5)
            continue

        cur = r.registers[:COUNT]

        if prev is None:
            prev = cur
            header = "  " + "  ".join(f"reg{START+i:04d}" for i in range(COUNT))
            print(header)
            vals = "  " + "  ".join(f"0x{v:04X} " for v in cur)
            print(vals)
            print()
        else:
            changed = [i for i in range(COUNT) if cur[i] != prev[i]]
            if changed:
                print(f"CHANGE at t={time.strftime('%H:%M:%S')}:")
                for i in changed:
                    print(f"  reg{START+i} : 0x{prev[i]:04X} -> 0x{cur[i]:04X}  "
                          f"(bits: {prev[i]:016b} -> {cur[i]:016b})")
                prev = cur
                print()

        time.sleep(0.2)

except KeyboardInterrupt:
    print("\nStopped.")
finally:
    c.close()
