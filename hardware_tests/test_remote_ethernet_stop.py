"""
test_remote_ethernet_stop.py -- read status, call stop() (stops the running
task so RC+'s Setup menu unlocks), read status again.

Usage:
    conda run -n cib_env python hardware_tests/test_remote_ethernet_stop.py
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

    await driver.connect()
    print("Login: OK")

    before = await driver.get_status()
    print(f"GetStatus (before stop): {before}")

    try:
        await driver.stop()
        print("Stop: OK")
    except Exception as exc:
        print(f"Stop FAILED: {exc}")

    after = await driver.get_status()
    print(f"GetStatus (after stop): {after}")

    await driver.disconnect()
    print("Logout: OK")


if __name__ == "__main__":
    asyncio.run(main())
