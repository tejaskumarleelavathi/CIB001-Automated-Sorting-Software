"""
test_robot_connect_vs_flexi.py -- polls the Flexi safety controller
continuously in a background thread (same call the safety driver makes),
while in the main thread repeatedly opening/closing the robot Remote
Ethernet connection (Login/GetStatus/Logout) every few seconds. If Flexi
polls fail specifically around the moments the robot connection opens,
that's real evidence for "opening the robot link disrupts Flexi" -- if
failures (if any) are scattered independent of that timing, it isn't.

Usage:
    conda run -n cib_env python hardware_tests/test_robot_connect_vs_flexi.py
"""

import asyncio
import sys
import threading
import time

sys.path.insert(0, ".")

from pymodbus.client import ModbusTcpClient

from cib.config.loader import load_machine_config
from cib.drivers.robot_remote.driver import RemoteEthernetDriver

DURATION_S = 60.0
_stop = False
_events: list[tuple[float, str]] = []
_lock = threading.Lock()


def log_event(msg: str) -> None:
    with _lock:
        _events.append((time.monotonic(), msg))
    print(f"t={time.monotonic()-_t0:6.2f}s  {msg}")


def flexi_poll_thread() -> None:
    client = ModbusTcpClient("172.16.9.12", port=502)
    client.connect()
    while not _stop:
        try:
            resp = client.read_input_registers(address=256, count=25)
            if resp.isError():
                log_event(f"FLEXI ERROR RESPONSE: {resp}")
        except Exception as exc:
            log_event(f"FLEXI EXCEPTION: {exc}")
        time.sleep(0.1)
    client.close()


async def robot_connect_cycle_loop(cfg) -> None:
    rr = cfg.robot_remote
    while not _stop:
        await asyncio.sleep(3.0)
        if _stop:
            break
        driver = RemoteEthernetDriver(
            ip=rr.ip, port=rr.port, password=rr.password,
            connect_timeout_s=rr.connect_timeout_s,
            command_timeout_s=rr.command_timeout_s,
        )
        try:
            log_event("ROBOT: opening Remote Ethernet connection...")
            await driver.connect()
            log_event("ROBOT: connected, querying status...")
            await driver.get_status()
            log_event("ROBOT: closing connection...")
            await driver.disconnect()
            log_event("ROBOT: closed")
        except Exception as exc:
            log_event(f"ROBOT: connect cycle FAILED: {exc}")


async def main() -> None:
    global _stop, _t0
    cfg = load_machine_config("config/machine_config.yaml")

    t = threading.Thread(target=flexi_poll_thread, daemon=True)
    _t0 = time.monotonic()
    t.start()

    robot_task = asyncio.create_task(robot_connect_cycle_loop(cfg))
    await asyncio.sleep(DURATION_S)
    _stop = True
    await robot_task
    t.join(timeout=2.0)

    n_flexi_errors = sum(1 for _, msg in _events if "FLEXI" in msg)
    print(f"\nDone. {len(_events)} total events, {n_flexi_errors} Flexi errors, "
          f"over {DURATION_S:.0f}s with robot connect/disconnect cycles every 3s.")


if __name__ == "__main__":
    asyncio.run(main())
