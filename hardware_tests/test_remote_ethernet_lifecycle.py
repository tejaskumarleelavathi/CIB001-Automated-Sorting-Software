"""
hardware_tests/test_remote_ethernet_lifecycle.py — isolated Remote Ethernet
lifecycle test. Connects ONLY to Remote Ethernet -- no cameras, no PLC, no
Flexi, no robot port-2000 connection -- to test whether the connection
stays stable without any other device competing for the shared
USB-Ethernet adapter.

Real-hardware finding (2026-08-30): running the FULL main.py startup
sequence, RC+ itself showed "!!Error: 1503, Disconnection while executing
a task" and "!!Error: 1504, Communication disconnection between Remote
Ethernet and Controller" a few seconds after Start() -- the exact same
class of error that has plagued the port-2000 robot connection throughout
this whole project (traced then to USB-Ethernet adapter contention with
cameras/PLC/Flexi). This script isolates Remote Ethernet alone to confirm
(or rule out) the same cause here.

Reuses the real cib.drivers.robot_remote.driver.RemoteEthernetDriver
directly -- not a reimplementation -- so anything learned/fixed here
(timing, retry behavior) ports straight back into main.py's
_remote_ethernet_restart_task() unchanged.

Usage:
    cib_env python hardware_tests/test_remote_ethernet_lifecycle.py

Runs the same Stop -> Reset -> wait_until_error_cleared -> wait_until_ready
-> SetMotorsOn -> Start(0) -> wait_until_running sequence the orchestrator
uses, then polls GetStatus once a second, printing every reading, until
you type "stop" + Enter -- at which point it sends Stop()/SetMotorsOff()
and disconnects cleanly.
"""

import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cib.config.loader import load_machine_config
from cib.drivers.robot_remote.driver import RemoteEthernetDriver

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "machine_config.yaml")


def _wait_for_stop_input(stop_event: threading.Event) -> None:
    """Runs in a plain OS thread — a blocking input() here would stall the asyncio loop."""
    while True:
        try:
            line = input()
        except EOFError:
            return
        if line.strip().lower() in ("stop", "s", "quit", "q"):
            stop_event.set()
            return


async def main() -> None:
    cfg = load_machine_config(CONFIG_PATH)
    rr_cfg = cfg.robot_remote
    print(f"Config: robot_remote.enabled={rr_cfg.enabled}, {rr_cfg.ip}:{rr_cfg.port}")
    if not rr_cfg.enabled:
        print("WARNING: robot_remote.enabled is False in machine_config.yaml — "
              "continuing anyway since this script talks to the driver directly.")

    driver = RemoteEthernetDriver(
        ip=rr_cfg.ip,
        port=rr_cfg.port,
        password=rr_cfg.password,
        connect_timeout_s=rr_cfg.connect_timeout_s,
        command_timeout_s=rr_cfg.command_timeout_s,
    )

    stop_event = threading.Event()
    input_thread = threading.Thread(target=_wait_for_stop_input, args=(stop_event,), daemon=True)
    input_thread.start()

    try:
        print(f"Connecting to {rr_cfg.ip}:{rr_cfg.port} ...")
        await driver.connect()
        print("Connected + logged in.")

        print("Stop() (best-effort — clears any wedged prior task) ...")
        try:
            await driver.stop()
        except Exception as exc:
            print(f"  (stop failed, likely nothing was running — {exc})")

        print("Reset() ...")
        await driver.reset()

        print("Waiting for error to clear ...")
        if not await driver.wait_until_error_cleared(timeout_s=8.0):
            print("ERROR: error never cleared. Aborting.")
            return
        print("  error cleared.")

        print("Waiting for Auto/ready ...")
        if not await driver.wait_until_ready(timeout_s=10.0):
            print("ERROR: controller never reported ready. Aborting.")
            return
        print("  ready.")

        print("SetMotorsOn() ...")
        await driver.set_motors_on()

        print("Start(0) — launching Main ...")
        await driver.start(0)

        print("Waiting for Running to confirm ...")
        if not await driver.wait_until_running(timeout_s=8.0):
            print("ERROR: task never confirmed running.")
            return

        print("\n=== Task confirmed running. Polling GetStatus every 1s. ===")
        print("=== Type 'stop' + Enter at any time to stop cleanly. ===\n")

        tick = 0
        while not stop_event.is_set():
            try:
                status = await driver.get_status()
                print(
                    f"[t+{tick:4d}s] running={status.running} error={status.error} "
                    f"error_code={status.error_code} estop={status.estop} "
                    f"safeguard={status.safeguard} serror={status.serror} "
                    f"auto={status.auto}"
                )
            except Exception as exc:
                print(f"[t+{tick:4d}s] CONNECTION LOST — GetStatus failed: {exc}")
                print("This is the moment to check RC+'s own error dialogs/status bar "
                      "for what it logged at this same timestamp.")
                break
            tick += 1
            await asyncio.sleep(1.0)

        if stop_event.is_set():
            print("\nStop requested — sending Stop() + SetMotorsOff() ...")
            try:
                await driver.stop()
                # SetMotorsOff right after Stop can fail with "error 20
                # (Controller is not ready)" -- confirmed on real hardware
                # 2026-08-30 (same class of settle delay as Reset's ~2s
                # error-clear lag). Brief wait before the next command.
                await asyncio.sleep(1.0)
                await driver.set_motors_off()
            except Exception as exc:
                print(f"  (stop/motors-off failed: {exc})")

    finally:
        print("Disconnecting (closing socket, no Logout — see driver.py) ...")
        try:
            await driver.disconnect()
        except Exception as exc:
            print(f"  (disconnect error, ignoring: {exc})")
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
