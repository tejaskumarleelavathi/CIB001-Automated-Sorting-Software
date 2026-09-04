"""
hardware_tests/isolate_robot_only.py -- narrowest isolation test
(2026-08-31): robot connect + Remote Ethernet start only, forever. No PLC,
no Flexi, no cameras, no pump/pipette -- the bare minimum needed to have
the robot running at all. No main-code changes -- reuses the real driver
classes directly.

If this still disconnects on its own, that rules out PLC/Flexi/cameras
entirely and points squarely at the robot controller (172.16.9.15) itself
or its own link/cable/adapter port -- worth comparing against
isolate_robot_plc_flexi.py's result (same test + PLC/Flexi polling) to see
whether adding those two changes anything.

Usage:
    cib_env python hardware_tests/isolate_robot_only.py

Runs until you press Ctrl+C, at which point it disconnects cleanly and
prints a summary: whether the robot ever disconnected, and if so, exactly
when (elapsed + wall clock) and for how long each outage lasted.
"""

import asyncio
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cib.config.loader import load_machine_config
from cib.drivers.robot.driver import EpsonT3BDriver
from cib.drivers.robot_remote.driver import RemoteEthernetDriver

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "machine_config.yaml")
ROBOT_POLL_INTERVAL_S = 0.5

_start_time = time.monotonic()


def _elapsed() -> float:
    return time.monotonic() - _start_time


def _wall() -> str:
    return time.strftime("%H:%M:%S")


class DisconnectTracker:
    """
    ok/fail counts plus a log of distinct outage episodes (start/end,
    elapsed + wall clock) -- the ok/fail counts alone don't tell you WHEN
    something dropped or for how long.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = 0
        self.fail = 0
        self._down = False
        self.episodes: list[dict] = []

    def record_ok(self) -> None:
        self.ok += 1
        if self._down:
            ep = self.episodes[-1]
            ep["end_t"] = _elapsed()
            ep["end_wall"] = _wall()
            self._down = False
            print(f"[t+{_elapsed():7.1f}s] {self.name} RECOVERED "
                  f"(was down {ep['end_t'] - ep['start_t']:.1f}s)")

    def record_fail(self, exc: Exception) -> None:
        self.fail += 1
        if not self._down:
            self._down = True
            self.episodes.append({
                "start_t": _elapsed(), "start_wall": _wall(),
                "end_t": None, "end_wall": None, "error": str(exc),
            })
            print(f"[t+{_elapsed():7.1f}s] {self.name} DISCONNECTED: {exc}")
        else:
            print(f"[t+{_elapsed():7.1f}s] {self.name} still down: {exc}")

    def summary_lines(self) -> list[str]:
        lines = [f"{self.name}: {self.ok}ok / {self.fail}fail"]
        if not self.episodes:
            lines.append(f"  never disconnected.")
            return lines
        lines.append(f"  disconnected {len(self.episodes)} time(s):")
        for i, ep in enumerate(self.episodes, 1):
            if ep["end_t"] is None:
                lines.append(
                    f"    {i}) t+{ep['start_t']:.1f}s ({ep['start_wall']}) "
                    f"-- still down when stopped"
                )
            else:
                dur = ep["end_t"] - ep["start_t"]
                lines.append(
                    f"    {i}) t+{ep['start_t']:.1f}s ({ep['start_wall']}) -> "
                    f"t+{ep['end_t']:.1f}s ({ep['end_wall']})  [down {dur:.1f}s]"
                )
        return lines


async def _robot_poll_loop(robot: EpsonT3BDriver, tracker: DisconnectTracker, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await robot.keepalive()
            if not await robot.is_ready():
                raise RuntimeError("keepalive succeeded but is_ready() is False")
            tracker.record_ok()
        except Exception as exc:
            tracker.record_fail(exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=ROBOT_POLL_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


async def _summary_loop(tracker: DisconnectTracker, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            print(f"[t+{_elapsed():7.1f}s] {tracker.name}={tracker.ok}ok/{tracker.fail}fail")


async def main() -> None:
    cfg = load_machine_config(CONFIG_PATH)

    print("=== robot + Remote Ethernet start ONLY (no PLC, no Flexi, no cameras) ===\n")

    robot = EpsonT3BDriver(
        ip=cfg.robot.ip, port=cfg.robot.port,
        connect_timeout_s=cfg.robot.connect_timeout_s,
        motion_timeout_s=getattr(cfg.robot, "motion_timeout_s", 120.0),
    )
    robot_remote = RemoteEthernetDriver(
        ip=cfg.robot_remote.ip, port=cfg.robot_remote.port,
        password=cfg.robot_remote.password,
        connect_timeout_s=cfg.robot_remote.connect_timeout_s,
        command_timeout_s=cfg.robot_remote.command_timeout_s,
    )

    # Remote Ethernet must start the task before robot.connect() (port 2000)
    # can succeed -- sort_server.prg's own OpenNet/WaitNet only exists once
    # its task is running.
    print(f"Connecting Remote Ethernet at {cfg.robot_remote.ip}:{cfg.robot_remote.port} "
          "and starting sort_server.prg (held open, matching production) ...")
    await robot_remote.connect()
    try:
        await robot_remote.stop()
    except Exception as exc:
        print(f"  (stop failed, likely nothing was running -- {exc})")
    await robot_remote.reset()
    if not await robot_remote.wait_until_error_cleared(timeout_s=8.0):
        print("ERROR: error never cleared on Remote Ethernet. Aborting.")
        return
    if not await robot_remote.wait_until_ready(timeout_s=10.0):
        print("ERROR: controller never reported ready. Aborting.")
        return
    await robot_remote.set_motors_on()
    await robot_remote.start(0)
    if not await robot_remote.wait_until_running(timeout_s=8.0):
        print("ERROR: task never confirmed running. Aborting.")
        return
    print("  sort_server.prg confirmed running.")

    print(f"Connecting robot at {cfg.robot.ip}:{cfg.robot.port} ...")
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            await robot.connect()
            last_exc = None
            break
        except RuntimeError as exc:
            last_exc = exc
            if attempt < 2:
                print(f"  (connect attempt {attempt + 1} failed, retrying in 2s -- {exc})")
                await asyncio.sleep(2.0)
    if last_exc is not None:
        raise last_exc
    print("  robot connected.")

    robot_t = DisconnectTracker("robot")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(stop_event.set))

    print(f"\n=== Polling started (robot every {ROBOT_POLL_INTERVAL_S}s) ===")
    print("=== Press Ctrl+C to stop and get a disconnect summary ===\n")

    tasks = [
        asyncio.create_task(_robot_poll_loop(robot, robot_t, stop_event)),
        asyncio.create_task(_summary_loop(robot_t, stop_event)),
    ]

    await stop_event.wait()
    await asyncio.gather(*tasks, return_exceptions=True)

    print(f"\n=== Stopped at t+{_elapsed():.1f}s ({_wall()}). Summary: ===")
    for line in robot_t.summary_lines():
        print(f"  {line}")

    print("\nStopping robot program via Remote Ethernet and powering off motors ...")
    try:
        await robot_remote.stop()
        await asyncio.sleep(1.0)
        await robot_remote.set_motors_off()
    except Exception as exc:
        print(f"  (stop/motors-off failed: {exc})")
    for name, coro in (("robot_remote", robot_remote.disconnect()), ("robot", robot.disconnect())):
        try:
            await coro
        except Exception as exc:
            print(f"  ({name} disconnect error, ignoring: {exc})")

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
