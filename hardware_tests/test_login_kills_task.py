"""
test_login_kills_task.py -- isolate whether a fresh Remote Ethernet Login
(while Main is already running) is what stops the task, independent of
anything happening on port 2000 (no port-2000 traffic at all in this test).

Sequence:
  1. Login, Start(0) if not already running, confirm running=True, Logout.
  2. Wait 5s (no port 2000 traffic, no remote-ethernet connection at all).
  3. Fresh Login, GetStatus, Logout. Compare running before/after.

Usage:
    conda run -n cib_env python hardware_tests/test_login_kills_task.py
"""

import asyncio
import sys
import time

sys.path.insert(0, ".")

from cib.config.loader import load_machine_config
from cib.drivers.robot_remote.driver import RemoteEthernetDriver


async def login_and_status(rr) -> bool:
    driver = RemoteEthernetDriver(
        ip=rr.ip, port=rr.port, password=rr.password,
        connect_timeout_s=rr.connect_timeout_s,
        command_timeout_s=rr.command_timeout_s,
    )
    await driver.connect()
    status = await driver.get_status()
    if not status.running:
        print("  starting Main[0] ...")
        await driver.start(0)
        await asyncio.sleep(1.0)
        status = await driver.get_status()
    await driver.disconnect()
    return status.running


async def main() -> None:
    cfg = load_machine_config("config/machine_config.yaml")
    rr = cfg.robot_remote

    print("--- Login #1 ---")
    running1 = await login_and_status(rr)
    print(f"running after Login #1: {running1}")

    print("\nWaiting 5s with NO remote-ethernet connection, NO port-2000 traffic...")
    await asyncio.sleep(5.0)

    print("\n--- Login #2 (fresh) ---")
    driver = RemoteEthernetDriver(
        ip=rr.ip, port=rr.port, password=rr.password,
        connect_timeout_s=rr.connect_timeout_s,
        command_timeout_s=rr.command_timeout_s,
    )
    t0 = time.monotonic()
    await driver.connect()
    status_immediately_after_login = await driver.get_status()
    print(f"running immediately after Login #2 ({time.monotonic()-t0:.2f}s): {status_immediately_after_login.running}")
    await driver.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
