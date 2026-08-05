"""
test_plc_rawwatch.py — live PLC signal monitor (read-only).

Polls all known PLC coils and discrete inputs every 200ms.
Prints only when any value changes. Ctrl+C to stop.

Confirmed Modbus addresses (from CIB001 PLC Software_Extended.csv, 2026-07-02):

  COILS (FC1 read, 0-based addresses):
    addr 0  WorkLightOn          — 1=work light on,   0=off
    addr 1  Statuslight_DemoMode — 1=demo mode active, 0=normal
    addr 2  (unassigned)
    addr 3  o_PowerOff           — 1=power off sequence running
    addr 4  Statuslight_Set      — trigger: pulse high→low to apply light settings

  DISCRETE INPUTS (FC2 read, read-only physical wiring):
    These are the PLC input signals (air pressure sensor, power button).
    Addresses mapped per coil address 2, 3, 4 in Modbus Mapping.xlsx:
      coil 2 → Air Pressure Okay  (1=OK,   0=warning — display warning to operator)
      coil 3 → Air Pressure Low   (1=OK,   0=fault   — STOP the cell)
      coil 4 → Power Button       (1=being pressed)
    NOTE: The CSV shows i_AirPressureOK, i_AirPressureLow, i_PowerBtn have NO
    Modbus address assigned yet — they may be on discrete inputs (FC2) rather than
    coils (FC1). This script tries both to find which one has live data.

Usage:
  python hardware_tests/test_plc_rawwatch.py
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

COIL_NAMES = {
    0: "WorkLight",
    1: "StatusLightDemo",
    2: "(unassigned)",
    3: "PowerOff",
    4: "StatusLightSet",
}

DISCRETE_INPUT_NAMES = {
    0: "DI-0",
    1: "DI-1",
    2: "DI-2",
    3: "DI-3",
    4: "DI-4",
    5: "DI-5",
    6: "DI-6",
    7: "DI-7",
}

NUM_COILS  = 8
NUM_DI     = 8


def read_all(client):
    coils = [None] * NUM_COILS
    dis   = [None] * NUM_DI

    r = client.read_coils(address=0, count=NUM_COILS)
    if not r.isError():
        coils = list(r.bits[:NUM_COILS])

    r = client.read_discrete_inputs(address=0, count=NUM_DI)
    if not r.isError():
        dis = list(r.bits[:NUM_DI])

    return coils, dis


def fmt_coils(coils):
    parts = []
    for i, v in enumerate(coils):
        name = COIL_NAMES.get(i, f"coil{i}")
        val  = "ON " if v else "off"
        parts.append(f"{name}={val}")
    return "  ".join(parts)


def fmt_dis(dis):
    parts = []
    for i, v in enumerate(dis):
        if v:  # only show ON inputs — quieter
            name = DISCRETE_INPUT_NAMES.get(i, f"di{i}")
            parts.append(f"{name}=ON")
    return ", ".join(parts) if parts else "(all off)"


def main():
    print(f"Connecting to PLC at {PLC_IP}:{PLC_PORT} ...")
    client = ModbusTcpClient(PLC_IP, port=PLC_PORT, timeout=3.0)
    if not client.connect():
        print("ERROR: Could not connect. Check cable and PLC power.")
        return
    print("Connected. Polling every 200ms — prints only on change. Ctrl+C to stop.\n")

    print(f"  {'TIME':<10} {'COILS':<70} {'DISCRETE INPUTS'}")
    print("  " + "-" * 110)

    prev = None
    try:
        while True:
            coils, dis = read_all(client)
            key = (tuple(coils), tuple(dis))
            if key != prev:
                t = time.strftime("%H:%M:%S")
                print(f"  {t:<10} {fmt_coils(coils):<70} {fmt_dis(dis)}")
                prev = key
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
        print("\nStopped.")


if __name__ == "__main__":
    main()
