"""
try_reset_coil.py -- pulse ONE candidate Flexi COIL (1 then 0) and report
whether the latched safety_ok bit (input register 256, bit 10) clears.

Why coils, not holding registers: the 2026-08-22 investigation (see
architecture/safety.md OQ-23, and git history on this file's siblings)
exhaustively pulsed every writable HOLDING register in the Flexi's live
block (256-279) -- none of them cleared the latch.

2026-09-11: opened the actual Safety Designer project (SICK Flexi Compact
CIB001.sdp2) and confirmed via the logic editor + Fieldbus > Routing to CPU
page that the real, live signal is named "Bit 0 - Reset Motor" -- byte 0,
bit 0 of the "Routing to CPU" output data block, OR'd with the physical
reset button to feed the protective-stop Reset V1 block. That structure
maps onto Modbus coil address 0 by the obvious byte/bit convention -- but
a first attempt using FC5 (Write Single Coil) got exception code 1
("Illegal Function": this device does not implement FC5 AT ALL, for any
address -- not a wrong-address problem).

This version tries FC5 first, and if that gets "Illegal Function"
specifically, automatically falls back to FC15 (Write Multiple Coils --
same coil, sent as a single-element list) before giving up on that coil.
Some Modbus servers implement FC15 but not FC5 for the exact same address.

Usage:
    conda run -n cib_env python hardware_tests/try_reset_coil.py <coil_addr>
    conda run -n cib_env python hardware_tests/try_reset_coil.py 0
    conda run -n cib_env python hardware_tests/try_reset_coil.py --sweep 0 7

Only meaningful while a fault is actually latched (safety_ok=0) -- trip the
light curtain first, then run this from another terminal/machine.
"""

import sys
import time

from pymodbus.client import ModbusTcpClient

FLEXI_IP = "172.16.9.12"
FLEXI_PORT = 502
INPUT_REG_ADDR = 256
HEARTBEAT_MASK = 0x8000


def decode(word: int) -> str:
    w = word & ~HEARTBEAT_MASK
    bit = lambda n: (w >> n) & 1
    return (
        f"raw=0x{w:04X}  estop_btn_ok={bit(0)} light_curtain_ok={bit(2)} "
        f"reset_btn={bit(3)} contactor_closed={bit(8)} reset_avail={bit(9)} "
        f"safety_ok={bit(10)}"
    )


def safety_ok(word: int) -> bool:
    return bool(((word & ~HEARTBEAT_MASK) >> 10) & 1)


def read_status(c: ModbusTcpClient) -> int | None:
    r = c.read_input_registers(address=INPUT_REG_ADDR, count=1)
    if r.isError():
        print(f"  read failed: {r}")
        return None
    return r.registers[0]


def _is_illegal_function(resp) -> bool:
    """True if resp is a Modbus exception response with code 1 (Illegal
    Function) -- the device doesn't implement this function code at all,
    as opposed to code 2 (Illegal Data Address, i.e. wrong address)."""
    code = getattr(resp, "exception_code", None)
    return code == 1


def _write_coil_fc5_or_fc15(c: ModbusTcpClient, coil: int, value: bool) -> tuple[bool, str]:
    """
    Try FC5 (Write Single Coil) first; if that specifically comes back
    "Illegal Function" (not a different error), retry the same coil via
    FC15 (Write Multiple Coils, one-element list). Returns (ok, which_fc).
    """
    r = c.write_coil(address=coil, value=value)
    if not r.isError():
        return True, "FC5"
    if _is_illegal_function(r):
        r2 = c.write_coils(address=coil, values=[value])
        if not r2.isError():
            return True, "FC15"
        return False, f"FC5 Illegal Function, FC15 also failed: {r2}"
    return False, f"FC5 failed (not Illegal Function): {r}"


def try_coil(c: ModbusTcpClient, coil: int) -> bool:
    """Pulse one coil high then low. Returns True if the latch cleared."""
    print(f"\n=== Trying coil {coil} ===")
    before = read_status(c)
    if before is None:
        return False
    print(f"  before: {decode(before)}")

    ok1, how1 = _write_coil_fc5_or_fc15(c, coil, True)
    if not ok1:
        print(f"  write coil {coil}=1 failed: {how1}")
        return False
    print(f"  wrote coil {coil}=1 via {how1}")
    time.sleep(0.2)
    ok0, how0 = _write_coil_fc5_or_fc15(c, coil, False)
    if not ok0:
        print(f"  write coil {coil}=0 failed: {how0}")
        return False
    time.sleep(0.3)

    after = read_status(c)
    if after is None:
        return False
    print(f"  after:  {decode(after)}")

    if not safety_ok(before) and safety_ok(after):
        print(f"\n  *** CONFIRMED: coil {coil} is Reset P. Stop -- safety_ok flipped 0 -> 1 ***")
        return True
    print(f"  no effect from coil {coil}")
    return False


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return

    c = ModbusTcpClient(FLEXI_IP, port=FLEXI_PORT, timeout=3.0)
    if not c.connect():
        print(f"Could not connect to {FLEXI_IP}:{FLEXI_PORT}")
        return

    status = read_status(c)
    if status is None:
        c.close()
        return
    print(f"=== Starting state ===\n  {decode(status)}")
    if safety_ok(status):
        print("\n  safety_ok is already 1 -- nothing latched to clear right now.")
        print("  Trip the light curtain (or e-stop) first, then re-run this.")
        c.close()
        return

    if sys.argv[1] == "--sweep":
        lo, hi = int(sys.argv[2]), int(sys.argv[3])
        for coil in range(lo, hi + 1):
            if try_coil(c, coil):
                break
            status = read_status(c)
            if status is not None and safety_ok(status):
                print("\n  Latch cleared on its own mid-sweep (someone pressed the "
                      "blue button?) -- stopping, nothing left to test against.")
                break
    else:
        try_coil(c, int(sys.argv[1]))

    c.close()


if __name__ == "__main__":
    main()
