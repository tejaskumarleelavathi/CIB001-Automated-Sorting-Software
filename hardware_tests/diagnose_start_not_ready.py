"""
diagnose_start_not_ready.py -- run Stop -> Reset -> SetMotorsOn -> Start,
printing the FULL GetStatus after every single step, so we can see exactly
what flag/error is actually blocking Start with error 20 (Controller is
not ready). Also tries Start a second time after a longer pause, in case
it's a slower settling time than 5s.

Usage:
    conda run -n cib_env python hardware_tests/diagnose_start_not_ready.py
"""

import asyncio
import sys

sys.path.insert(0, ".")

from cib.config.loader import load_machine_config
from cib.drivers.robot_remote.driver import RemoteEthernetDriver, RemoteEthernetError


async def main() -> None:
    cfg = load_machine_config("config/machine_config.yaml")
    rr = cfg.robot_remote
    driver = RemoteEthernetDriver(
        ip=rr.ip, port=rr.port, password=rr.password,
        connect_timeout_s=rr.connect_timeout_s,
        command_timeout_s=rr.command_timeout_s,
    )

    await driver.connect()
    print("Login: OK\n")

    async def show(label: str) -> None:
        status = await driver.get_status()
        print(f"[{label}] {status}")

    await show("initial")

    try:
        await driver.stop()
        print("Stop: OK")
    except Exception as exc:
        print(f"Stop: FAILED {exc}")
    await show("after stop")

    try:
        await driver.reset()
        print("Reset: OK")
    except Exception as exc:
        print(f"Reset: FAILED {exc}")
    await show("after reset")

    try:
        await driver.set_motors_on()
        print("SetMotorsOn: OK")
    except Exception as exc:
        print(f"SetMotorsOn: FAILED {exc}")
    await show("after motors_on (immediately)")

    for wait_s in (1, 2, 3):
        await asyncio.sleep(1.0)
        await show(f"after motors_on (+{wait_s}s)")

    print("\nAttempting Start(0) now...")
    try:
        await driver.start(0)
        print("Start: OK")
    except RemoteEthernetError as exc:
        print(f"Start: FAILED {exc}")
    await show("after start attempt")

    await driver.disconnect()
    print("\nLogout: OK")


if __name__ == "__main__":
    asyncio.run(main())
