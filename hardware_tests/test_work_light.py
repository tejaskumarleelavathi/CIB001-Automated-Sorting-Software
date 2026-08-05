"""
test_work_light.py — test the cell work light.

Work light Modbus address confirmed from CIB001 PLC Software_Extended.csv:
  WorkLightOn → coil 000001 → pymodbus FC1/FC5 address 0

Usage:
  python hardware_tests/test_work_light.py on      # turn on
  python hardware_tests/test_work_light.py off     # turn off
  python hardware_tests/test_work_light.py blink   # blink 3x to confirm it's working
  python hardware_tests/test_work_light.py         # default: blink
"""

import sys
import os
import time
import logging

logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pymodbus.client import ModbusTcpClient

PLC_IP   = "172.16.9.11"
PLC_PORT = 502

WORK_LIGHT_COIL = 0   # CSV: WorkLightOn at 000001 = pymodbus address 0


def write_work_light(client, on: bool) -> bool:
    r = client.write_coil(address=WORK_LIGHT_COIL, value=on)
    if r.isError():
        print(f"  ERROR writing work light: {r}")
        return False
    rb = client.read_coils(address=WORK_LIGHT_COIL, count=1)
    if rb.isError():
        print(f"  ERROR reading back: {rb}")
        return False
    actual = rb.bits[0]
    state = "ON" if actual else "OFF"
    print(f"  Work light → {state}  (readback confirms)")
    return True


def main():
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "blink"

    print(f"Connecting to PLC at {PLC_IP}:{PLC_PORT} ...")
    client = ModbusTcpClient(PLC_IP, port=PLC_PORT, timeout=3.0)
    if not client.connect():
        print("ERROR: Could not connect. Check cable and PLC power.")
        return
    print("Connected.\n")

    try:
        if cmd == "on":
            write_work_light(client, True)

        elif cmd == "off":
            write_work_light(client, False)

        elif cmd == "blink":
            print("Blinking work light 3x (1s on, 0.5s off) ...")
            for i in range(3):
                write_work_light(client, True)
                time.sleep(1.0)
                write_work_light(client, False)
                time.sleep(0.5)
            print("Done. Work light left OFF.")

        else:
            print(f"Unknown command '{cmd}'. Use: on | off | blink")

    finally:
        client.close()
        print("Disconnected.")


if __name__ == "__main__":
    main()
