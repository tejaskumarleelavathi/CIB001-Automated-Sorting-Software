"""
test_status_light_colors.py — cycle through all 13 status light colors.

Cycles every color in steady mode so you can visually verify each one.
Then leaves the light GREEN (system ready).

Usage:
  python hardware_tests/test_status_light_colors.py          # cycle all colors, 1.5s each
  python hardware_tests/test_status_light_colors.py steady green
  python hardware_tests/test_status_light_colors.py flash red
  python hardware_tests/test_status_light_colors.py chase cyan blue
  python hardware_tests/test_status_light_colors.py off
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

# Holding register offsets (base = 0, Modbus 40001)
REG_MODE       = 0
REG_PATTERN    = 1
REG_DIRECTION  = 2
REG_SPEED      = 3
REG_SEQ        = 4
REG_COLOR_PRI  = 5
REG_INTENS_PRI = 6
REG_COLOR_SEC  = 7
REG_INTENS_SEC = 8

COIL_STATUSLIGHT_SET = 4   # Statuslight_Set at 000005 = pymodbus addr 4

COLORS = {
    "green":   0,
    "red":     1,
    "orange":  2,
    "amber":   3,
    "yellow":  4,
    "lime":    5,
    "spring":  6,
    "cyan":    7,
    "sky":     8,
    "blue":    9,
    "violet":  10,
    "magenta": 11,
    "rose":    12,
    "white":   13,
}

MODES = {
    "off":        0,
    "steady":     1,
    "flash":      2,
    "twocolor":   3,
    "5050":       4,
    "rotate":     5,
    "chase":      6,
    "sweep":      7,
    "colorsweep": 8,
    "sequence":   9,
    "wave":       10,
    "doublewave": 11,
}


def connect():
    c = ModbusTcpClient(PLC_IP, port=PLC_PORT, timeout=3.0)
    if not c.connect():
        print(f"ERROR: Cannot connect to PLC at {PLC_IP}:{PLC_PORT}")
        sys.exit(1)
    return c


def wreg(c, addr, val):
    r = c.write_register(address=addr, value=val)
    if r.isError():
        print(f"  WARN: reg[{addr}]={val} failed: {r}")
        return
    # Readback to confirm PLC accepted the value
    rb = c.read_holding_registers(address=addr, count=1)
    actual = rb.registers[0] if not rb.isError() else "?"
    ok = "OK" if actual == val else f"MISMATCH (got {actual})"
    print(f"  reg[{addr}]={val}  readback={actual}  {ok}")


def set_steady(c, color_name, intensity=0):
    col = COLORS.get(color_name.lower())
    if col is None:
        print(f"Unknown color '{color_name}'. Options: {list(COLORS)}")
        return
    wreg(c, REG_MODE,       MODES["steady"])
    wreg(c, REG_COLOR_PRI,  col)
    wreg(c, REG_INTENS_PRI, intensity)
    wreg(c, REG_COLOR_SEC,  col)
    wreg(c, REG_INTENS_SEC, intensity)
    trigger_set(c)


def set_flash(c, color_name, speed=1):
    col = COLORS.get(color_name.lower())
    if col is None:
        print(f"Unknown color '{color_name}'. Options: {list(COLORS)}")
        return
    wreg(c, REG_MODE,       MODES["flash"])
    wreg(c, REG_PATTERN,    0)           # Flash pattern
    wreg(c, REG_SPEED,      speed)
    wreg(c, REG_COLOR_PRI,  col)
    wreg(c, REG_INTENS_PRI, 0)
    wreg(c, REG_COLOR_SEC,  col)
    wreg(c, REG_INTENS_SEC, 0)
    trigger_set(c)


def set_chase(c, primary, secondary, speed=1):
    p = COLORS.get(primary.lower())
    s = COLORS.get(secondary.lower())
    if p is None or s is None:
        print(f"Unknown color. Options: {list(COLORS)}")
        return
    wreg(c, REG_MODE,       MODES["chase"])
    wreg(c, REG_DIRECTION,  1)           # Clockwise
    wreg(c, REG_SPEED,      speed)
    wreg(c, REG_COLOR_PRI,  p)
    wreg(c, REG_INTENS_PRI, 0)
    wreg(c, REG_COLOR_SEC,  s)
    wreg(c, REG_INTENS_SEC, 0)
    trigger_set(c)


def set_off(c):
    wreg(c, REG_MODE, MODES["off"])
    trigger_set(c)


def trigger_set(c):
    """Pulse Statuslight_Set coil high→low to tell PLC to apply the new settings."""
    r1 = c.write_coil(address=COIL_STATUSLIGHT_SET, value=True)
    time.sleep(0.05)
    r2 = c.write_coil(address=COIL_STATUSLIGHT_SET, value=False)
    if r1.isError() or r2.isError():
        print(f"  WARN: Statuslight_Set pulse failed: {r1} / {r2}")
    else:
        print(f"  Statuslight_Set pulsed (coil {COIL_STATUSLIGHT_SET})")


def cycle_all_colors(c, delay=1.5):
    """Cycle through every color in steady mode."""
    print(f"\nCycling all {len(COLORS)} colors ({delay}s each) ...\n")
    for name, code in COLORS.items():
        print(f"  [{code:>2}]  {name:<10}", end="", flush=True)
        set_steady(c, name)
        time.sleep(delay)
        print("  ✓")

    print("\nLeaving light GREEN (system ready).")
    set_steady(c, "green")


def main():
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "cycle"

    c = connect()
    print(f"Connected to PLC at {PLC_IP}:{PLC_PORT}\n")

    try:
        if cmd == "cycle":
            cycle_all_colors(c)

        elif cmd == "steady":
            color = sys.argv[2] if len(sys.argv) > 2 else "green"
            print(f"Steady {color} ...")
            set_steady(c, color)

        elif cmd == "flash":
            color = sys.argv[2] if len(sys.argv) > 2 else "red"
            print(f"Flashing {color} ...")
            set_flash(c, color)

        elif cmd == "chase":
            pri = sys.argv[2] if len(sys.argv) > 2 else "cyan"
            sec = sys.argv[3] if len(sys.argv) > 3 else "blue"
            print(f"Chase {pri}/{sec} ...")
            set_chase(c, pri, sec)

        elif cmd == "off":
            print("Status light OFF ...")
            set_off(c)

        else:
            print(f"Unknown command '{cmd}'.")
            print("Usage: cycle | steady <color> | flash <color> | chase <pri> <sec> | off")
            print(f"Colors: {list(COLORS)}")

    finally:
        c.close()
        print("Done.")


if __name__ == "__main__":
    main()
