# test_status_light.py -- AutomationDirect status light via PLC Modbus TCP
#
# David's register map (addresses TBD - run scan mode first to find base):
#
#   OFFSET  NAME                        VALUES
#   +0      Demo Mode                   0=off, 1=on  (coil or reg)
#   +1      Mode                        0=Off, 1=Steady, 2=Flashing, 3=TwoColorFlash,
#                                       4=50/50, 5=50/50Rotate, 6=Chase, 7=IntensitySweep,
#                                       8=ColorSweep, 9=Sequence, 10=Wave, 11=DoubleWave
#   +2      Animation Pattern           0=Flash, 1=Strobe, 2=ThreePulse, 3=SOS, 4=Random
#   +3      Animation Direction         0=CCW, 1=CW
#   +4      Animation Speed             0=Slow, 1=Medium, 2=Fast
#   +5      Sequence Value              0-255
#   +6      Primary Color               0=Green 1=Red 2=Orange 3=Amber 4=Yellow
#                                       5=LimeGreen 6=SpringGreen 7=Cyan 8=SkyBlue
#                                       9=Blue 10=Violet 11=Magenta 12=Rose 13=White
#   +7      Primary Color Intensity     0=High, 1=Medium, 2=Low, 3=N/A, 4=Off
#   +8      Secondary Color             (same values as Primary Color)
#   +9      Secondary Color Intensity   (same values as Primary Color Intensity)
#
# Usage:
#   python test_status_light.py scan          # find which register addresses control the light
#   python test_status_light.py demo          # blink demo mode on/off
#   python test_status_light.py steady green  # steady green
#   python test_status_light.py flash red     # red flashing
#   python test_status_light.py chase cyan blue  # cyan/blue chase
#   python test_status_light.py off           # turn off
#
# Register map (from Modbus Mapping.xlsx):
#   Coil  0      (00001) Status Light Demo Mode   Write
#   Reg   0      (40001) Status Light Mode         Write
#   Reg   1      (40002) Animation Pattern         Write
#   Reg   2      (40003) Animation Direction       Write
#   Reg   3      (40004) Animation Speed           Write
#   Reg   4      (40005) Sequence Value            Write
#   Reg   5      (40006) Primary Color             Write
#   Reg   6      (40007) Primary Color Intensity   Write
#   Reg   7      (40008) Secondary Color           Write
#   Reg   8      (40009) Secondary Color Intensity Write

import sys
import time
from pymodbus.client import ModbusTcpClient

PLC_IP   = "172.16.9.11"
PLC_PORT = 502

BASE_REG      = 0   # holding register base (40001 = address 0)
DEMO_COIL     = 0   # coil address for demo mode

COLORS = {
    "green": 0, "red": 1, "orange": 2, "amber": 3, "yellow": 4,
    "lime": 5, "spring": 6, "cyan": 7, "sky": 8, "blue": 9,
    "violet": 10, "magenta": 11, "rose": 12, "white": 13,
}

MODES = {
    "off": 0, "steady": 1, "flash": 2, "twocolor": 3,
    "5050": 4, "rotate": 5, "chase": 6, "sweep": 7,
    "colorsweep": 8, "sequence": 9, "wave": 10, "doublewave": 11,
}


def send(fn, *args, **kwargs):
    """Open a fresh connection, run fn(client, *args, **kwargs), close it."""
    c = ModbusTcpClient(PLC_IP, port=PLC_PORT)
    if not c.connect():
        print(f"ERROR: Cannot connect to PLC at {PLC_IP}:{PLC_PORT}")
        sys.exit(1)
    try:
        return fn(c, *args, **kwargs)
    finally:
        c.close()


def wreg(addr, val):
    def _write(c):
        r = c.write_register(address=addr, value=val)
        if r.isError():
            print(f"  FAIL reg[{addr}] = {val}: {r}")
        else:
            print(f"  reg[{addr}] = {val}")
    send(_write)


def wcoil(addr, val):
    def _write(c):
        r = c.write_coil(address=addr, value=val)
        if r.isError():
            print(f"  FAIL coil[{addr}] = {val}: {r}")
        else:
            print(f"  coil[{addr}] = {val}")
    send(_write)


def set_light(mode, primary="green", secondary="red",
              pattern=0, direction=1, speed=1, intensity=0):
    wreg(BASE_REG + 0, MODES.get(mode, 0))       # 40001 Mode
    wreg(BASE_REG + 1, pattern)                   # 40002 Pattern
    wreg(BASE_REG + 2, direction)                 # 40003 Direction
    wreg(BASE_REG + 3, speed)                     # 40004 Speed
    wreg(BASE_REG + 5, COLORS.get(primary, 0))   # 40006 Primary Color
    wreg(BASE_REG + 6, intensity)                 # 40007 Primary Intensity
    wreg(BASE_REG + 7, COLORS.get(secondary, 1)) # 40008 Secondary Color
    wreg(BASE_REG + 8, intensity)                 # 40009 Secondary Intensity


# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "steady"

    if cmd == "off":
        print("Turning status light OFF ...")
        wreg(BASE_REG + 0, 0)

    elif cmd == "demo":
        print("Demo mode ON (coil 0) ...")
        wcoil(DEMO_COIL, True)
        time.sleep(5)
        print("Demo mode OFF ...")
        wcoil(DEMO_COIL, False)

    elif cmd == "steady":
        color = sys.argv[2].lower() if len(sys.argv) > 2 else "green"
        if color not in COLORS:
            print(f"Unknown color '{color}'. Options: {list(COLORS)}")
            sys.exit(1)
        print(f"Steady {color} ...")
        set_light("steady", primary=color)

    elif cmd == "flash":
        color = sys.argv[2].lower() if len(sys.argv) > 2 else "red"
        print(f"Flashing {color} ...")
        set_light("flash", primary=color, pattern=0, speed=1)

    elif cmd == "chase":
        primary   = sys.argv[2].lower() if len(sys.argv) > 2 else "cyan"
        secondary = sys.argv[3].lower() if len(sys.argv) > 3 else "blue"
        print(f"Chase {primary}/{secondary} ...")
        set_light("chase", primary=primary, secondary=secondary, direction=1, speed=1)

    else:
        print("Usage:")
        print("  python test_status_light.py off")
        print("  python test_status_light.py demo")
        print("  python test_status_light.py steady green")
        print("  python test_status_light.py flash red")
        print("  python test_status_light.py chase cyan blue")
