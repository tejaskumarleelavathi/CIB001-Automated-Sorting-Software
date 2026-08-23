"""
probe_flexi_reset_ack.py -- READ-ONLY exploration to help locate the Modbus
write address for the Flexi's "Reset P. Stop" bit (Modbus Mapping.xlsx,
sheet "Flexi", "Routing To CPU" -> Byte 0 -> Bit 0).

Does NOT write anything. Confirmed read side is input registers at address
256 (PROC_ADDR in cib/drivers/safety/driver.py, baseline 0x0517). This
script reads holding registers, coils, and input registers across a wide
range of candidate addresses so a human can compare the results against
what the Flexi Soft / Ewon configuration says the "Routing To CPU" block
actually lives at, before anyone attempts a write test.

Usage:
    conda run -n cib_env python hardware_tests/probe_flexi_reset_ack.py
"""

from pymodbus.client import ModbusTcpClient

FLEXI_IP = "172.16.9.12"
FLEXI_PORT = 502

# Confirmed read side, for a sanity-check baseline comparison.
KNOWN_INPUT_REG_ADDR = 256
KNOWN_INPUT_REG_COUNT = 25

# Candidate bases to read from, read-only. Includes probe_flexi.py's
# original list plus addresses near the known input-register block (256),
# since "outputs" and "inputs" are often adjacent or mirrored in these
# Ewon/Modbus-gateway setups.
CANDIDATE_BASES = [0, 1, 2, 4, 8, 16, 32, 64, 100, 128, 200, 256, 257, 258,
                   260, 264, 280, 281, 282, 300, 400, 500, 1000, 2000, 4096, 8192]


def main() -> None:
    c = ModbusTcpClient(FLEXI_IP, port=FLEXI_PORT, timeout=3.0)
    if not c.connect():
        print(f"Could not connect to {FLEXI_IP}:{FLEXI_PORT}")
        return

    print(f"=== Sanity check: known input-register baseline ===")
    r = c.read_input_registers(address=KNOWN_INPUT_REG_ADDR, count=1)
    if not r.isError():
        print(f"  inreg[{KNOWN_INPUT_REG_ADDR}] = 0x{r.registers[0]:04X} "
              f"(expect 0x0517 if system is fully safe/idle right now)")
    else:
        print(f"  inreg[{KNOWN_INPUT_REG_ADDR}]: ERROR {r}")

    print(f"\n=== Holding Registers (FC3) — read-only ===")
    for start in CANDIDATE_BASES:
        r = c.read_holding_registers(address=start, count=4)
        if not r.isError():
            print(f"  hreg[{start}-{start+3}] = {[f'0x{v:04X}' for v in r.registers]}")
        else:
            print(f"  hreg[{start}+4]: {r}")

    print(f"\n=== Coils (FC1) — read-only ===")
    for start in CANDIDATE_BASES:
        r = c.read_coils(address=start, count=8)
        if not r.isError():
            vals = [int(b) for b in r.bits[:8]]
            print(f"  coil[{start}-{start+7}] = {vals}")
        else:
            print(f"  coil[{start}+8]: {r}")

    print(f"\n=== Discrete Inputs (FC2) — read-only ===")
    for start in CANDIDATE_BASES:
        r = c.read_discrete_inputs(address=start, count=8)
        if not r.isError():
            vals = [int(b) for b in r.bits[:8]]
            print(f"  disc[{start}-{start+7}] = {vals}")
        else:
            print(f"  disc[{start}+8]: {r}")

    print(f"\n=== Input Registers (FC4) near the known block ===")
    for start in [0, 100, 200, 232, 240, 250, 256, 264, 280, 281, 282, 300, 400]:
        r = c.read_input_registers(address=start, count=4)
        if not r.isError():
            print(f"  inreg[{start}-{start+3}] = {[f'0x{v:04X}' for v in r.registers]}")
        else:
            print(f"  inreg[{start}+4]: {r}")

    c.close()
    print("\n=== Done — no writes were performed ===")


if __name__ == "__main__":
    main()
