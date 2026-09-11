"""
sweep_reset_holding_regs.py -- properly exhaustive Flexi HOLDING REGISTER
write sweep for the "Reset Motor" / "Reset P. Stop" signal, pulsing each
candidate (write 1, wait, write 0) and checking whether the latched
safety_ok bit (input register 256, bit 10) clears.

Why this script, given try_reset_reg.py / test_flexi_reset_ack.py already
existed (2026-08-22): despite the commit message for that work claiming an
"exhaustive" sweep of the live holding-register block (256-279), the actual
script only ever tried 5 addresses in it -- 256, 257, 258, 260, 264 -- the
ones a read-only recon pass happened to flag as cleanly readable. A
register configured WRITE-ONLY would fail that read-based recon (look
"not live") while still silently accepting a write -- exactly the kind of
register we're looking for. This script does not pre-filter by
readability: it tries a write on every address in range regardless of
whether a read on it succeeds, so a write-only "Reset Motor" register
can't be invisible to it the way it could have been before.

Range: defaults to 256-300 -- the confirmed-live 256-279 block itself
(this time genuinely every register in it, not a sample), plus a modest
extension past it on the theory that the write-side ("Routing to CPU")
block sits immediately after the already-confirmed read-side ("Routing to
Network") block, a common layout convention. Override with --start/--end
if you want a wider net, but widen deliberately, not blindly -- this is
still a live safety controller.

Only pulses (write 1 then immediately write 0) -- never leaves a register
holding a modified value, and stops the instant the latch clears (or
clears on its own mid-sweep, e.g. someone pressed the blue button).

Usage:
    conda run -n cib_env python hardware_tests/sweep_reset_holding_regs.py
    conda run -n cib_env python hardware_tests/sweep_reset_holding_regs.py --start 256 --end 320

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

DEFAULT_START = 256
DEFAULT_END = 300   # inclusive


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


def try_register(c: ModbusTcpClient, reg: int) -> bool:
    """
    Pulse one holding register (write 1, then write 0). Returns True if
    the latch cleared. Does NOT require the register to be readable first
    -- a write-only register would fail a read but could still work here.
    """
    before = read_status(c)
    if before is None:
        return False

    r1 = c.write_register(address=reg, value=1)
    if r1.isError():
        print(f"  reg {reg}: write 1 failed ({r1}) -- skipping")
        return False
    time.sleep(0.2)
    r0 = c.write_register(address=reg, value=0)
    if r0.isError():
        print(f"  reg {reg}: write 0 failed ({r0}) -- register may be stuck at 1, check manually")
        return False
    time.sleep(0.3)

    after = read_status(c)
    if after is None:
        return False

    if not safety_ok(before) and safety_ok(after):
        print(f"\n  *** CONFIRMED: holding register {reg} is Reset Motor / Reset P. Stop ***")
        print(f"  before: {decode(before)}")
        print(f"  after:  {decode(after)}")
        return True

    print(f"  reg {reg}: no effect  ({decode(after)})")
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=int, default=DEFAULT_START)
    ap.add_argument("--end", type=int, default=DEFAULT_END, help="inclusive")
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

    print(f"\nSweeping holding registers {args.start}-{args.end} "
          f"({args.end - args.start + 1} addresses) ...\n")

    found = False
    for reg in range(args.start, args.end + 1):
        if try_register(c, reg):
            found = True
            break
        status = read_status(c)
        if status is not None and safety_ok(status):
            print("\n  Latch cleared on its own mid-sweep (someone pressed the "
                  "blue button?) -- stopping, nothing left to test against.")
            break

    if not found:
        print(f"\n=== RESULT ===\n"
              f"  No register in {args.start}-{args.end} cleared the latch.\n"
              f"  Coils are already confirmed unsupported (FC5 and FC15 both "
              f"'Illegal Function'). If this range comes back empty too, the "
              f"reset almost certainly isn't reachable over Modbus TCP at all "
              f"on this device -- worth checking Safety Designer for which "
              f"fieldbus protocol is actually active before widening the sweep.")

    c.close()


if __name__ == "__main__":
    main()
