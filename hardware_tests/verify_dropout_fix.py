"""
verify_dropout_fix.py -- Start(0), open a TCP connection to port 2000 and
send NOTHING before closing it (reproducing the exact failure found by
diagnose_start_dropout.py), then probe port 2000 every 2s for ~30s to see
whether sort_server.prg's listener recovers on its own now that TCP/IP
port 201's SetNet Timeout is set to 15s (was 0 = never).

Usage:
    conda run -n cib_env python hardware_tests/verify_dropout_fix.py
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


def kill_connection_silently(ip: str) -> None:
    """Connect and disconnect without sending anything -- the exact failure mode."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect((ip, 2000))
        print("  (silent probe connected, closing without sending anything)")
    except Exception as exc:
        print(f"  (silent probe FAILED to even connect: {exc})")
    finally:
        s.close()


async def main() -> None:
    cfg = load_machine_config("config/machine_config.yaml")
    rr = cfg.robot_remote
    driver = RemoteEthernetDriver(
        ip=rr.ip, port=rr.port, password=rr.password,
        connect_timeout_s=rr.connect_timeout_s,
        command_timeout_s=rr.command_timeout_s,
    )

    await driver.connect()
    print("Login: OK")

    status = await driver.get_status()
    if not status.running:
        print("Starting Main[0] ...")
        await driver.start(0)
        await asyncio.sleep(1.0)

    print("Reproducing the failure: connect to port 2000, send nothing, close.")
    kill_connection_silently(rr.ip)

    t0 = time.monotonic()
    for i in range(16):
        elapsed = time.monotonic() - t0
        port_state = probe_port_2000(rr.ip)
        print(f"t={elapsed:5.1f}s  port2000={port_state}")
        if port_state == "OPEN":
            print("\nRECOVERED -- server accepted a new connection.")
            break
        await asyncio.sleep(2.0)
    else:
        print("\nNOT RECOVERED within 30s.")

    await driver.disconnect()
    print("Logout: OK")


if __name__ == "__main__":
    asyncio.run(main())
