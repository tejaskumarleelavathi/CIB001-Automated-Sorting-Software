"""
test_remote_ethernet_start.py -- read status, call start(0) (runs Main[0] --
sort_server.prg's TCP listener; opens the PICK/DROP socket server, does not
itself move the robot), read status again. Does NOT call home() or
stop() -- leaves the program running so sort_server.prg's own client can
connect afterward.

Usage:
    conda run -n cib_env python hardware_tests/test_remote_ethernet_start.py
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
        print(f"GetStatus (before start): {before}")
    except Exception as exc:
        print(f"GetStatus FAILED: {exc}")
        await driver.disconnect()
        return

    try:
        await driver.start(0)
        print("Start(0): OK")
    except Exception as exc:
        print(f"Start(0) FAILED: {exc}")

    try:
        after = await driver.get_status()
        print(f"GetStatus (after start): {after}")
    except Exception as exc:
        print(f"GetStatus FAILED: {exc}")

    await driver.disconnect()
    print("Logout: OK -- program left running (Remote Ethernet connection closed, Main[0] keeps executing)")


if __name__ == "__main__":
    asyncio.run(main())
