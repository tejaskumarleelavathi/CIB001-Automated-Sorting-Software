"""
try_reset_reg.py -- pulse ONE candidate holding register (1 then 0) and
report whether the Flexi's latched safety_ok bit (input register 256, bit
10) clears. Run once per candidate address so each attempt is visible on
its own before trying the next.

Usage:
    conda run -n cib_env python hardware_tests/try_reset_reg.py <register>
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


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: try_reset_reg.py <register>")
        return
    reg = int(sys.argv[1])

    c = ModbusTcpClient(FLEXI_IP, port=FLEXI_PORT, timeout=3.0)
    if not c.connect():
        print(f"Could not connect to {FLEXI_IP}:{FLEXI_PORT}")
        return

    r = c.read_input_registers(address=INPUT_REG_ADDR, count=1)
    if r.isError():
        print(f"read failed: {r}")
        c.close()
        return
    before = r.registers[0]
    print(f"before: {decode(before)}")

    if safety_ok(before):
        print("safety_ok already 1 -- nothing latched to clear, test not meaningful right now.")
        c.close()
        return

    r1 = c.write_register(address=reg, value=1)
    print(f"write reg {reg} = 1: {'OK' if not r1.isError() else r1}")
    time.sleep(0.2)
    r0 = c.write_register(address=reg, value=0)
    print(f"write reg {reg} = 0: {'OK' if not r0.isError() else r0}")
    time.sleep(0.3)

    r = c.read_input_registers(address=INPUT_REG_ADDR, count=1)
    if r.isError():
        print(f"read failed: {r}")
        c.close()
        return
    after = r.registers[0]
    print(f"after:  {decode(after)}")

    if not safety_ok(before) and safety_ok(after):
        print(f"\n*** CONFIRMED: register {reg} is Reset P. Stop ***")
    else:
        print(f"\nno effect from register {reg}")

    c.close()


if __name__ == "__main__":
    main()
