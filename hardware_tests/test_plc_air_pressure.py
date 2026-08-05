"""
test_plc_air_pressure.py — find and monitor air pressure signals from PLC.

From Modbus Mapping.xlsx:
  Coil 2  "Air Pressure Okay" — ON = above target pressure  (warn if OFF)
  Coil 3  "Air Pressure Low"  — ON = above minimum pressure (STOP if OFF)

The CSV (CIB001 PLC Software_Extended.csv) shows i_AirPressureOK and
i_AirPressureLow have no Modbus address yet — they may be on coils (FC1)
or discrete inputs (FC2). This script scans BOTH across a wide range and
prints only when anything changes.

HOW TO USE:
  1. Run this script
  2. It will print the current state of all coils and discrete inputs
  3. Adjust air pressure (turn off compressor, bleed line, etc.)
  4. Watch which bit changes — that's the air pressure signal
  5. Ctrl+C to stop

Usage:
  python hardware_tests/test_plc_air_pressure.py
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

SCAN_COILS = 16    # scan coils 0–15
SCAN_DI    = 16    # scan discrete inputs 0–15

# Known coil labels from CSV + Excel mapping
COIL_LABELS = {
    0:  "WorkLightOn",
    1:  "StatusLight_Demo",
    2:  "AirPressureOK?",      # Excel says coil 2 — to confirm
    3:  "AirPressureLow?",     # Excel says coil 3 — to confirm
    4:  "PowerButton?",        # Excel says coil 4 — to confirm
    5:  "StatusLight_Set",
}


def read_all(client):
    coils = [None] * SCAN_COILS
    dis   = [None] * SCAN_DI

    r = client.read_coils(address=0, count=SCAN_COILS)
    if not r.isError():
        coils = list(r.bits[:SCAN_COILS])

    r = client.read_discrete_inputs(address=0, count=SCAN_DI)
    if not r.isError():
        dis = list(r.bits[:SCAN_DI])

    return coils, dis


def print_state(coils, dis, prev_coils, prev_di):
    t = time.strftime("%H:%M:%S")

    # Print changed coils
    for i, (cur, prev) in enumerate(zip(coils, prev_coils)):
        if cur != prev:
            label = COIL_LABELS.get(i, f"coil{i:>2}")
            val   = "ON " if cur else "OFF"
            arrow = "ON→OFF" if prev else "OFF→ON"
            print(f"  {t}  COIL[{i:>2}] {label:<22} {arrow}")

    # Print changed discrete inputs
    for i, (cur, prev) in enumerate(zip(dis, prev_di)):
        if cur != prev:
            arrow = "ON→OFF" if prev else "OFF→ON"
            print(f"  {t}  DI  [{i:>2}] di_{i:<22} {arrow}")


def print_snapshot(coils, dis):
    """Print full current state — called once at startup."""
    print("\n  Current state:")
    print(f"  {'TYPE':<8} {'ADDR':<6} {'LABEL':<24} {'VALUE'}")
    print("  " + "-" * 55)
    for i, v in enumerate(coils):
        label = COIL_LABELS.get(i, f"coil{i}")
        val   = "ON " if v else "off"
        marker = "  ← ACTIVE" if v else ""
        print(f"  {'COIL':<8} {i:<6} {label:<24} {val}{marker}")
    for i, v in enumerate(dis):
        val   = "ON " if v else "off"
        marker = "  ← ACTIVE" if v else ""
        print(f"  {'DI':<8} {i:<6} {'di_' + str(i):<24} {val}{marker}")
    print()


def main():
    print(f"Connecting to PLC at {PLC_IP}:{PLC_PORT} ...")
    client = ModbusTcpClient(PLC_IP, port=PLC_PORT, timeout=3.0)
    if not client.connect():
        print("ERROR: Could not connect. Check cable and PLC power.")
        return

    print("Connected.\n")
    print("  Scanning coils 0-15 (FC1) and discrete inputs 0-15 (FC2).")
    print("  Prints only when something changes.\n")
    print("  >>> NOW: adjust air pressure (cut compressor or bleed line)")
    print("  >>> and watch which bit changes — that's the air pressure signal.\n")

    coils, dis = read_all(client)
    print_snapshot(coils, dis)

    print(f"  {'TIME':<10} {'CHANGE'}")
    print("  " + "-" * 55)

    prev_coils, prev_di = coils[:], dis[:]

    try:
        while True:
            coils, dis = read_all(client)
            if coils != prev_coils or dis != prev_di:
                print_state(coils, dis, prev_coils, prev_di)
                prev_coils, prev_di = coils[:], dis[:]
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
        print("\nStopped.")


if __name__ == "__main__":
    main()
