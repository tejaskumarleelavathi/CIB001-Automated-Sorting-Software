"""
test_remote_ethernet_reset.py -- read status, call reset() (clears a latched
alarm, does NOT move the robot or touch motor power), read status again.
Does NOT call set_motors_on/off(), start(), stop(), or home().

Usage:
    conda run -n cib_env python hardware_tests/test_remote_ethernet_reset.py
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
        print(f"GetStatus (before reset): {before}")
    except Exception as exc:
        print(f"GetStatus FAILED: {exc}")
        await driver.disconnect()
        return

    try:
        await driver.reset()
        print("Reset: OK")
    except Exception as exc:
        print(f"Reset FAILED: {exc}")

    try:
        after = await driver.get_status()
        print(f"GetStatus (after reset): {after}")
    except Exception as exc:
        print(f"GetStatus FAILED: {exc}")

    await driver.disconnect()
    print("Logout: OK")


if __name__ == "__main__":
    asyncio.run(main())
