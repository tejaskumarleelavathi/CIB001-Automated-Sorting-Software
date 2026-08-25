"""
final_start_and_verify.py -- single persistent Remote Ethernet session
(one Login the whole way through, matching the orchestrator's own pattern):
Start(0), confirm running=True, confirm port 2000 answers STATUS with
OK READY, then ONE Logout at the end. No repeated re-logins.

Usage:
    conda run -n cib_env python hardware_tests/final_start_and_verify.py
"""

import asyncio
import socket
import sys

sys.path.insert(0, ".")

from cib.config.loader import load_machine_config
from cib.drivers.robot_remote.driver import RemoteEthernetDriver


def check_port_2000(ip: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3.0)
    try:
        s.connect((ip, 2000))
        s.sendall(b"STATUS\r\n")
        resp = s.recv(1024)
        return resp.decode(errors="replace").strip()
    except Exception as exc:
        return f"FAILED: {exc}"
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
    print("Login: OK (single persistent session)")

    status = await driver.get_status()
    if not status.running:
        await driver.start(0)
        print("Start(0): OK")
        await asyncio.sleep(1.5)
        status = await driver.get_status()

    print(f"GetStatus: running={status.running} ready={status.ready} error={status.error}")

    print("Checking port 2000 (sort_server.prg's own protocol)...")
    result = check_port_2000(rr.ip)
    print(f"Port 2000 STATUS response: {result}")

    await driver.disconnect()
    print("Logout: OK")


if __name__ == "__main__":
    asyncio.run(main())
