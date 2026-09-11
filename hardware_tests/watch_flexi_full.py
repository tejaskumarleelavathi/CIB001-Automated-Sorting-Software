"""
watch_flexi_full.py -- live, change-on-delta dump of EVERY bit in Flexi
input register 256, using the FULL bit map cross-validated 2026-09-11
against the Safety Designer offline report's own logic diagram (not just
the subset cib/drivers/safety/driver.py currently decodes).

DO NOT run this at the same time as main.py. The Safety Designer offline
report says this Flexi's Modbus server "Supported connections: 1" -- it
only accepts ONE client at a time. Running this alongside the real app
steals that slot, so the app's own safety-driver connect() fails, which
(since it's gathered together with the PLC/pump connects early in
_startup()) can cascade into blocking everything after it, including the
robot -- confirmed live 2026-09-11 ("the robot never connects when I run
both"). This script is for standalone use with the app NOT running. To get
this same bit-level detail WHILE the app is running, use the app's own
log instead -- cib/drivers/safety/driver.py's read_status() now decodes
and logs enable_system/edm_okay/p_stop_reset_ready on every poll, no
second connection needed.

Why this exists: after fixing curtain-flicker debouncing, CONTINUE still
failed to release the P-Stop latch even with the curtain provably clear
for 2+ seconds. The debounce fix addressed one real problem but evidently
not the only one blocking this. Guessing a third cause without data would
just be another guess -- this tool shows the actual state of the specific
bits the reset mechanism depends on, live, so a failed attempt can be
diagnosed from what actually happened instead of inferred.

Key bits to watch during a failed reset attempt:
  bit  8 (enable_system)      -- from the report: "E-Stop/Sys Enabled".
                                  This is the E-STOP side's own Reset V1
                                  block's output. Per the Safety Designer
                                  logic diagram, P-Stop's Reset V1 (the one
                                  our "Reset Motor" pulse targets) can only
                                  release once THIS is already 1. If it's
                                  0, no amount of curtain-clearing or
                                  pulsing "Reset Motor" can work -- this
                                  needs a genuine physical e-stop button
                                  press + blue reset button cycle first,
                                  completely independent of the curtain.
  bit 10 (p_stop_enabled)      -- what the driver currently calls
                                  "safety_ok"/"any_fault_active". Literally
                                  "does the motor have power" -- this is
                                  what we're actually trying to get to 1.
  bit 11 (p_stop_reset_ready)  -- the report's "P. Stop Reset Required".
                                  Best current guess at meaning: the P-Stop
                                  Reset V1 block is in a state where it can
                                  accept a reset attempt -- NOT necessarily
                                  that Release (Enable System AND curtain
                                  clear) is satisfied at this instant.
  bit  2 (light_curtain_clear) -- already tracked by the driver.
  bit  4 (edm_okay)            -- External Device Monitoring feedback.
                                  Feeds "Enable System" too (block 6's
                                  Release condition) -- if this is ever 0,
                                  Enable System can't latch either.

Usage:
    conda run -n cib_env python hardware_tests/watch_flexi_full.py

Prints one line per state CHANGE only (like test_safety_driver.py), so a
quiet period between changes just means nothing new happened. Ctrl+C to
stop. Read-only -- never writes anything.
"""

import time

from pymodbus.client import ModbusTcpClient

FLEXI_IP = "172.16.9.12"
FLEXI_PORT = 502
INPUT_REG_ADDR = 256
HEARTBEAT_MASK = 0x8000

# Global bit -> (short name, 1-meaning). Cross-validated 2026-09-11 against
# the Safety Designer offline report's "Routing to Network" byte/bit table,
# corrected for this device's byte-swap (Byte 1 -> low 8 bits, Byte 0 ->
# high 8 bits -- confirmed by Heartbeat landing at bit 15, matching the
# driver's independently rawwatch-confirmed heartbeat position).
BITS: dict[int, tuple[str, str]] = {
    0:  ("system_estop_ok",      "1=button not pressed"),
    1:  ("robot_pendant_estop_ok", "1=button not pressed"),
    2:  ("light_curtain_clear",  "1=beam intact"),
    3:  ("reset_button",         "1=being pressed"),
    4:  ("edm_okay",             "1=external device monitoring OK"),
    5:  ("unknown_b1.5", ""),
    6:  ("unknown_b1.6", ""),
    7:  ("unknown_b1.7", ""),
    8:  ("enable_system",        "1=E-Stop side's own Reset V1 is Enabled -- "
                                  "REQUIRED before P-Stop's Reset V1 can ever release"),
    9:  ("estop_reset_required", "1=e-stop reset is being requested/awaited"),
    10: ("p_stop_enabled",       "1=motor actually has power (== old 'safety_ok')"),
    11: ("p_stop_reset_ready",   "1=P-Stop's Reset V1 will accept a reset attempt"),
    12: ("unknown_b0.4", ""),
    13: ("unknown_b0.5", ""),
    14: ("unknown_b0.6", ""),
    15: ("heartbeat (masked out below)", ""),
}


def decode(word: int) -> dict[str, int]:
    w = word & ~HEARTBEAT_MASK
    return {name: (w >> bit) & 1 for bit, (name, _) in BITS.items() if bit != 15}


def format_line(bits: dict[str, int]) -> str:
    return "  ".join(f"{name}={val}" for name, val in bits.items())


def main() -> None:
    c = ModbusTcpClient(FLEXI_IP, port=FLEXI_PORT, timeout=3.0)
    if not c.connect():
        print(f"Could not connect to {FLEXI_IP}:{FLEXI_PORT}")
        return

    print("Watching Flexi register 256, full bit map (change-on-delta). Ctrl+C to stop.\n")
    print("Key bits: enable_system (8), p_stop_enabled (10), p_stop_reset_ready (11), "
          "light_curtain_clear (2), edm_okay (4)\n")

    last: dict[str, int] | None = None
    try:
        while True:
            r = c.read_input_registers(address=INPUT_REG_ADDR, count=1)
            if r.isError():
                print(f"  read failed: {r}")
                time.sleep(0.5)
                continue
            bits = decode(r.registers[0])
            if bits != last:
                ts = time.strftime("%H:%M:%S")
                print(f"[{ts}] {format_line(bits)}")
                last = bits
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        c.close()


if __name__ == "__main__":
    main()
