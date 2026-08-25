"""
test_remote_ethernet_motors_on.py -- read status, call set_motors_on(),
read status again. Does NOT call start(), stop(), or home() -- this only
energizes the motors, it does not run the program or move the robot.

Usage:
    conda run -n cib_env python hardware_tests/test_remote_ethernet_motors_on.py
"""

import asyncio
import sys

sys.path.insert(0, ".")

from cib.config.loader import load_machine_config
from cib.drivers.robot_remote.driver import RemoteEthernetDriver


async def main() -> None:
    cfg = load_machine_config("config/machine_config.yaml")
    rr = cfg.robot_remote
    print(f"Connecting to Remote Ethernet at {rr.ip}:{rr.port} ...")

    driver = RemoteEthernetDriver(
        ip=rr.ip, port=rr.port, password=rr.password,
        connect_timeout_s=rr.connect_timeout_s,
        command_timeout_s=rr.command_timeout_s,
    )

    try:
        await driver.connect()
        print("Login: OK")
    except Exception as exc:
        print(f"Login FAILED: {exc}")
        return

    try:
        before = await driver.get_status()
        print(f"GetStatus (before motors on): {before}")
    except Exception as exc:
        print(f"GetStatus FAILED: {exc}")
        await driver.disconnect()
        return

    try:
        await driver.set_motors_on()
        print("SetMotorsOn: OK")
    except Exception as exc:
        print(f"SetMotorsOn FAILED: {exc}")

    try:
        after = await driver.get_status()
        print(f"GetStatus (after motors on): {after}")
    except Exception as exc:
        print(f"GetStatus FAILED: {exc}")

    await driver.disconnect()
    print("Logout: OK")


if __name__ == "__main__":
    asyncio.run(main())
