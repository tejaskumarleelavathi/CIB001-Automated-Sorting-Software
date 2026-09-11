"""
bitscan_reset_register.py -- test every BIT POSITION (not just bit 0)
within one or more candidate Flexi holding registers, pulsing each bit
individually and checking whether the latched safety_ok bit clears.

Why: the Safety Designer offline report (2026-09-11) confirms "Reset
Motor" is bit 0 of a single byte in the "Network to CPU" gateway data
object -- but comparing the report's read-side ("CPU to Network") byte/bit
table against our driver's own EMPIRICALLY-confirmed bit map (built by
watching register 256 while actually pressing buttons, 2026-07-02)
suggests this device byte-swaps: reading "Byte 1" occupies the LOW 8 bits
of its register and "Byte 0" occupies the HIGH 8 bits, not the other way
round. If the write side has the same swap, "Reset Motor" (bit 0 of ITS
byte) could sit at absolute bit 8 (value 256) of whatever register it's
in, not bit 0 (value 1) -- the only value every previous test has tried.

Rather than guess which swap convention applies, this pulses EVERY bit
(0-15) individually at each candidate register -- covers "no swap",
"byte swap", and anything else, without needing to be right about the
convention first.

Defaults to register 256 only (the single most likely candidate: it's the
same number already used for the read side, just in the separate
holding-register address space -- a common device-design symmetry).
Widen with --start/--end if 256 comes back empty.

Usage:
    conda run -n cib_env python hardware_tests/bitscan_reset_register.py
    conda run -n cib_env python hardware_tests/bitscan_reset_register.py --start 256 --end 260

Only meaningful while a fault is actually latched (safety_ok=0) -- trip the
light curtain first, then run this from another terminal/machine.
"""

import argparse
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


def try_bit(c: ModbusTcpClient, reg: int, bit: int) -> bool:
    """Pulse ONE bit within one register (write value=1<<bit, then 0).
    Returns True if the latch cleared."""
    value = 1 << bit
    before = read_status(c)
    if before is None:
        return False

    r1 = c.write_register(address=reg, value=value)
    if r1.isError():
        print(f"  reg {reg} bit {bit} (value {value}): write failed ({r1}) -- skipping")
        return False
    time.sleep(0.2)
    r0 = c.write_register(address=reg, value=0)
    if r0.isError():
        print(f"  reg {reg} bit {bit}: write-back-to-0 failed ({r0}) -- "
              f"register may be stuck at {value}, check manually")
        return False
    time.sleep(0.3)

    after = read_status(c)
    if after is None:
        return False

    if not safety_ok(before) and safety_ok(after):
        print(f"\n  *** CONFIRMED: register {reg}, bit {bit} (value {value}) "
              f"is Reset Motor / Reset P. Stop ***")
        print(f"  before: {decode(before)}")
        print(f"  after:  {decode(after)}")
        return True

    print(f"  reg {reg} bit {bit:2d} (value {value:5d}): no effect")
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=int, default=256)
    ap.add_argument("--end", type=int, default=256, help="inclusive")
    args = ap.parse_args()

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

    n_regs = args.end - args.start + 1
    print(f"\nBit-scanning register(s) {args.start}-{args.end} "
          f"({n_regs} register(s) x 16 bits = {n_regs * 16} pulses max) ...\n")

    found = False
    for reg in range(args.start, args.end + 1):
        print(f"--- register {reg} ---")
        for bit in range(16):
            if try_bit(c, reg, bit):
                found = True
                break
            status = read_status(c)
            if status is not None and safety_ok(status):
                print("\n  Latch cleared on its own mid-scan (someone pressed the "
                      "blue button?) -- stopping, nothing left to test against.")
                found = True   # not a real find, but nothing more to do
                break
        if found:
            break

    if not found:
        print(f"\n=== RESULT ===\n"
              f"  No bit in register(s) {args.start}-{args.end} cleared the latch.\n"
              f"  Combined with coils (fully unsupported) and the earlier 256-300 "
              f"whole-register sweep (value=1 only), this is now a genuinely "
              f"exhaustive test of register {args.start}"
              + (f"-{args.end}" if n_regs > 1 else "")
              + f" at every bit position. If this comes back empty, the exact "
                f"register number is very likely something only SICK's own "
                f"Modbus interface spec for the FLX3-CPUC200 gateway would "
                f"give you -- worth asking David if he already has that "
                f"document, rather than continuing to guess addresses.")

    c.close()


if __name__ == "__main__":
    main()
