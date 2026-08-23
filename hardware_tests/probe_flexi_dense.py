"""
probe_flexi_dense.py -- dense READ-ONLY sweep of holding registers on the
Flexi to find any live block other than the already-known 256-267 range
(which was proven to have no effect on safety_ok when written to).

Read-only. No writes. Steps through every 8 registers from 0 to 8192.

Usage:
    conda run -n cib_env python hardware_tests/probe_flexi_dense.py
"""

from pymodbus.client import ModbusTcpClient

FLEXI_IP = "172.16.9.12"
FLEXI_PORT = 502


def main() -> None:
    c = ModbusTcpClient(FLEXI_IP, port=FLEXI_PORT, timeout=3.0)
    if not c.connect():
        print(f"Could not connect to {FLEXI_IP}:{FLEXI_PORT}")
        return

    print("Dense holding-register sweep (FC3), step=8, 0-8192, read-only...\n")
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

    print(f"\nLive blocks found at: {live_blocks}")
    c.close()


if __name__ == "__main__":
    main()
