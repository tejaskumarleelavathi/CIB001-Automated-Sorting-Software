"""
baseline_flexi_poll.py -- poll the Flexi Compact safety controller every
100ms (same rate/registers the orchestrator's safety poll loop uses) for a
sustained period, touching NOTHING robot-related at all. Establishes
whether Modbus timeouts happen on their own (pure network/Flexi flakiness)
or only when the robot Remote Ethernet connection is also active.

Usage:
    conda run -n cib_env python hardware_tests/baseline_flexi_poll.py [seconds]
"""

import sys
import time

sys.path.insert(0, ".")

from pymodbus.client import ModbusTcpClient

DURATION_S = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0


def main() -> None:
    client = ModbusTcpClient("172.16.9.12", port=502)
    client.connect()

    n_ok = 0
    n_fail = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < DURATION_S:
        try:
            resp = client.read_input_registers(address=256, count=25)
            if resp.isError():
                n_fail += 1
                print(f"t={time.monotonic()-t0:6.1f}s  MODBUS ERROR RESPONSE: {resp}")
            else:
                n_ok += 1
        except Exception as exc:
            n_fail += 1
            print(f"t={time.monotonic()-t0:6.1f}s  EXCEPTION: {exc}")
        time.sleep(0.1)

    client.close()
    print(f"\nDone. {n_ok} OK, {n_fail} FAILED over {DURATION_S:.0f}s "
          f"(zero robot activity during this test).")


if __name__ == "__main__":
    main()
