"""
test_flexi_reset_ack.py -- verify which holding register in the confirmed
live block (256-267) is the "Reset P. Stop" write bit (Modbus Mapping.xlsx,
sheet "Flexi", "Routing To CPU" -> Byte 0 -> Bit 0), by pulsing each
candidate high then low and checking whether the latched protective-stop
bit (input register 256, bit 10 / "safety_ok") clears.

Only writes to registers already confirmed readable/writable without error
in probe_flexi_reset_ack.py (256, 257, 258, 260, 264) -- no blind guessing
outside that confirmed-live block.

Usage:
    conda run -n cib_env python hardware_tests/test_flexi_reset_ack.py
"""

import time

from pymodbus.client import ModbusTcpClient

FLEXI_IP = "172.16.9.12"
FLEXI_PORT = 502
INPUT_REG_ADDR = 256
CANDIDATES = [256, 257, 258, 260, 264]
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


def main() -> None:
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
        print("\n  safety_ok is already 1 — nothing latched to clear right now.")
        print("  This test needs an active latch to be meaningful. Stopping.")
        c.close()
        return

    for reg in CANDIDATES:
        print(f"\n=== Trying holding register {reg} ===")
        before = read_status(c)
        if before is None:
            continue
        print(f"  before: {decode(before)}")

        r1 = c.write_register(address=reg, value=1)
        if r1.isError():
            print(f"  write 1 failed: {r1}")
            continue
        time.sleep(0.2)
        r0 = c.write_register(address=reg, value=0)
        if r0.isError():
            print(f"  write 0 failed: {r0}")
            continue
        time.sleep(0.3)

        after = read_status(c)
        if after is None:
            continue
        print(f"  after:  {decode(after)}")

        if not safety_ok(before) and safety_ok(after):
            print(f"\n  *** CONFIRMED: holding register {reg} is Reset P. Stop. ***")
            print(f"  safety_ok flipped 0 -> 1 after pulsing register {reg}.")
            c.close()
            return
        else:
            print(f"  no effect from register {reg}")

    print("\n=== RESULT ===")
    print("  None of the candidates in the confirmed-live block cleared the latch.")
    print("  Reset P. Stop is likely NOT in this address range -- ask David directly")
    print("  for the confirmed Modbus address rather than expanding the search blindly.")

    c.close()


if __name__ == "__main__":
    main()
