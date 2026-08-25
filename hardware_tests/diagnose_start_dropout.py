"""
diagnose_start_dropout.py -- Start(0), then for ~20s poll GetStatus (Remote
Ethernet) AND probe TCP port 2000 (sort_server.prg's own listener) once per
second, printing both side by side with a timestamp. Read-only after the
single Start() call -- no reset/motors/home/stop.

Usage:
    conda run -n cib_env python hardware_tests/diagnose_start_dropout.py
"""

import asyncio
import socket
import sys
import time

sys.path.insert(0, ".")

from cib.config.loader import load_machine_config
from cib.drivers.robot_remote.driver import RemoteEthernetDriver


def probe_port_2000(ip: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect((ip, 2000))
        return "OPEN"
    except Exception as exc:
        return f"refused ({exc.__class__.__name__})"
    finally:
        s.close()


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

    status = await driver.get_status()
    print(f"t=0.0s  GetStatus (before start): {status}")

    t_start = time.monotonic()
    await driver.start(0)
    print(f"t={time.monotonic()-t_start:.1f}s  Start(0): OK")

    for i in range(25):
        elapsed = time.monotonic() - t_start
        status = await driver.get_status()
        port_state = probe_port_2000(rr.ip)
        print(
            f"t={elapsed:5.1f}s  running={status.running!s:5}  "
            f"ready={status.ready!s:5}  error={status.error!s:5}  "
            f"code={status.error_code}  port2000={port_state}"
        )
        await asyncio.sleep(1.0)

    await driver.disconnect()
    print("Logout: OK")


if __name__ == "__main__":
    asyncio.run(main())
