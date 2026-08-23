"""
probe_plc_readonly.py -- READ-ONLY reconnaissance of the System PLC
(172.16.9.11) to look for anything related to a safety/P.Stop reset,
before considering any write test. No writes performed.

Usage:
    conda run -n cib_env python hardware_tests/probe_plc_readonly.py
"""

from pymodbus.client import ModbusTcpClient

PLC_IP = "172.16.9.11"
PLC_PORT = 502


def main() -> None:
    c = ModbusTcpClient(PLC_IP, port=PLC_PORT, timeout=3.0)
    if not c.connect():
        print(f"Could not connect to {PLC_IP}:{PLC_PORT}")
        return

    print(f"=== Input Registers (FC4) -- read-only, 0-25 ===")
    r = c.read_input_registers(address=0, count=25)
    if not r.isError():
        print(f"  inreg[0-24] = {r.registers}")
    else:
        print(f"  {r}")

    print(f"\n=== Holding Registers (FC3) -- read-only, known named ones (40001-40009 => 0-8) ===")
    r = c.read_holding_registers(address=0, count=16)
    if not r.isError():
        print(f"  hreg[0-15] = {r.registers}")
    else:
        print(f"  {r}")

    print(f"\n=== Coils (FC1) -- read-only, 0-16 ===")
    r = c.read_coils(address=0, count=16)
    if not r.isError():
        vals = [int(b) for b in r.bits[:16]]
        print(f"  coil[0-15] = {vals}")
    else:
        print(f"  {r}")

    print(f"\n=== Dense holding-register sweep, step=8, 0-8192, read-only ===")
    live_blocks = []
    addr = 0
    while addr < 8192:
        r = c.read_holding_registers(address=addr, count=8)
        if not r.isError():
            nonzero = any(v != 0 for v in r.registers)
            marker = "  <-- NONZERO" if nonzero else ""
            print(f"  hreg[{addr}-{addr+7}] = {r.registers}{marker}")
            live_blocks.append(addr)
        addr += 8

    print(f"\nLive holding-register blocks found at: {live_blocks}")

    print(f"\n=== Dense input-register sweep, step=8, 0-8192, read-only ===")
    live_in_blocks = []
    addr = 0
    while addr < 8192:
        r = c.read_input_registers(address=addr, count=8)
        if not r.isError():
            print(f"  inreg[{addr}-{addr+7}] = {r.registers}")
            live_in_blocks.append(addr)
        addr += 8
    print(f"\nLive input-register blocks found at: {live_in_blocks}")

    c.close()


if __name__ == "__main__":
    main()
