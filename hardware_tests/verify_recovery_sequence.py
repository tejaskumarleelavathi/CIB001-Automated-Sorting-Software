"""
verify_recovery_sequence.py -- runs the EXACT sequence
Orchestrator._try_remote_ethernet_recovery() now uses (Stop -> Reset ->
wait_until_error_cleared -> wait_until_ready -> SetMotorsOn -> Start),
end to end, then confirms port 2000 actually answers STATUS/OK READY.

Usage:
    conda run -n cib_env python hardware_tests/verify_recovery_sequence.py
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
        return s.recv(1024).decode(errors="replace").strip()
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
    print("Login: OK")

    try:
        await driver.stop()
    except Exception as exc:
        print(f"Stop warning (non-fatal): {exc}")
    print("Stop: done")

    await driver.reset()
    print("Reset: OK")

    error_cleared = await driver.wait_until_error_cleared(timeout_s=8.0)
    print(f"wait_until_error_cleared: {error_cleared}")
    if not error_cleared:
        print("ABORTING — error never cleared")
        await driver.disconnect()
        return

    ready = await driver.wait_until_ready(timeout_s=10.0)
    print(f"wait_until_ready (auto): {ready}")
    if not ready:
        print("ABORTING — never became ready/auto")
        await driver.disconnect()
        return

    await driver.set_motors_on()
    print("SetMotorsOn: OK")

    await driver.start(0)
    print("Start: OK")

    await asyncio.sleep(3.0)
    result = check_port_2000(rr.ip)
    print(f"Port 2000 STATUS response: {result}")

    await driver.disconnect()
    print("Logout: OK")


if __name__ == "__main__":
    asyncio.run(main())
