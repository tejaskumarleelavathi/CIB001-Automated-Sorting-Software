"""
test_safety_driver.py — verify FlexiCompactDriver bit decoding live.
Prints only when state changes. Ctrl+C to stop.

Usage:
  python hardware_tests/test_safety_driver.py
"""

import asyncio
import sys
import os
import logging
import time

# Suppress structlog debug noise — we only want our own prints
logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cib.drivers.safety.driver import FlexiCompactDriver


async def main():
    driver = FlexiCompactDriver()

    print("Connecting to Sick Flexi ...")
    await driver.connect()
    print("Connected. Polling every 100ms — prints only on change.\n")
    print(f"  {'TIME':<10} {'STATUS':<20} {'ESTOP':<8} {'LIGHT CURTAIN':<16} {'CONTACTOR':<12} {'RESET AVAIL'}")
    print("  " + "-" * 80)

    prev_key = None

    try:
        while True:
            status = await driver.read_status()

            any_f   = status["any_fault_active"]
            estop   = status["estop_active"]
            lc      = status["light_curtain_tripped"]
            cont    = status["contactor_closed"]
            r_avail = status["reset_available"]

            if any_f:
                if estop:
                    label = "E-STOP ACTIVE"
                elif lc:
                    label = "LIGHT CURTAIN"
                else:
                    label = "FAULT (latched)"
            else:
                label = "OK"

            key = (any_f, estop, lc, cont, r_avail)

            if key != prev_key:
                t = time.strftime("%H:%M:%S")
                print(
                    f"  {t:<10} {label:<20} "
                    f"{'YES' if estop else 'no':<8} "
                    f"{'TRIPPED' if lc else 'clear':<16} "
                    f"{'CLOSED' if cont else 'OPEN':<12} "
                    f"{'YES' if r_avail else 'no'}"
                )
                prev_key = key

            await asyncio.sleep(0.1)

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await driver.disconnect()
        print("\nStopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
