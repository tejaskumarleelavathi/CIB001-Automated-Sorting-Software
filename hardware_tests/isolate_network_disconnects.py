"""
hardware_tests/isolate_network_disconnects.py -- staged network-contention
isolation test (2026-08-31). Standalone script, does NOT touch cib/core or
main.py -- reuses the real driver classes directly so anything learned here
carries straight back to production code unchanged.

Goal: find out which device combination on the shared USB-Ethernet adapter
actually causes the disconnects that have been hitting the robot (port 2000)
and Remote Ethernet (port 5000) all session. Staged so each addition is a
single, isolated variable:

  Phase 1 (from launch): robot connect + start sort_server.prg via Remote
           Ethernet (held open, matching the current production sequence)
           + PLC poll (read_event_log) + Flexi poll (read_status). No
           cameras, no pump/pipette. Matches the orchestrator's own
           200ms poll interval for PLC/Flexi (config: poll_interval_ms).

  Phase 2 (you press Enter): additionally connect() both cameras -- POWERED
           and CONNECTED only, never start_streaming()/capture(). Isolates
           whether the mere presence of the cameras on the network (GigE
           Vision devices do their own discovery/heartbeat chatter even
           idle) is enough to cause contention, separate from the bandwidth
           cost of actually streaming frames.

Every poll failure on any channel is printed immediately with a timestamp,
plus a running ok/fail count summary every 2s -- so a disconnect at any
point during either phase is unambiguous about which phase it happened in.

Usage:
    cib_env python hardware_tests/isolate_network_disconnects.py

Type 'stop' + Enter at any time to end cleanly (works during either phase).
Press Enter (empty line) once you want to advance from Phase 1 to Phase 2.
"""

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cib.config.loader import load_machine_config
from cib.drivers.camera.driver import GigECameraDriver
from cib.drivers.plc.driver import LogicPLCDriver
from cib.drivers.robot.driver import EpsonT3BDriver
from cib.drivers.robot_remote.driver import RemoteEthernetDriver
from cib.drivers.safety.driver import FlexiCompactDriver

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "machine_config.yaml")

ROBOT_POLL_INTERVAL_S = 0.5


class Tally:
    """ok/fail counters for one polled channel, plus the last error seen."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = 0
        self.fail = 0
        self.last_error: str | None = None

    def record_ok(self) -> None:
        self.ok += 1

    def record_fail(self, exc: Exception) -> None:
        self.fail += 1
        self.last_error = str(exc)
        print(f"[t+{_elapsed():7.1f}s] {self.name} POLL FAILED: {exc}")

    def summary(self) -> str:
        return f"{self.name}={self.ok}ok/{self.fail}fail"


_start_time = time.monotonic()


def _elapsed() -> float:
    return time.monotonic() - _start_time


def _wait_for_line(prompt: str) -> str:
    """Blocking input() -- only ever called from the background OS thread."""
    try:
        return input(prompt)
    except EOFError:
        return ""


def _input_thread(phase2_event: threading.Event, stop_event: threading.Event) -> None:
    """
    Runs in a plain OS thread so blocking input() never stalls the asyncio
    loop driving the poll tasks. First line (any content, including empty)
    advances to Phase 2. After that, watches for a stop keyword.
    """
    _wait_for_line("")
    phase2_event.set()
    while True:
        line = _wait_for_line("")
        if line.strip().lower() in ("stop", "s", "quit", "q"):
            stop_event.set()
            return


async def _robot_poll_loop(robot: EpsonT3BDriver, tally: Tally, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            await robot.keepalive()
            if not await robot.is_ready():
                raise RuntimeError("keepalive succeeded but is_ready() is False")
            tally.record_ok()
        except Exception as exc:
            tally.record_fail(exc)
        await asyncio.sleep(ROBOT_POLL_INTERVAL_S)


async def _plc_poll_loop(plc: LogicPLCDriver, tally: Tally, interval_s: float, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            await plc.read_event_log()
            tally.record_ok()
        except Exception as exc:
            tally.record_fail(exc)
        await asyncio.sleep(interval_s)


async def _flexi_poll_loop(flexi: FlexiCompactDriver, tally: Tally, interval_s: float, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            await flexi.read_status()
            tally.record_ok()
        except Exception as exc:
            tally.record_fail(exc)
        await asyncio.sleep(interval_s)


async def _summary_loop(tallies: list[Tally], stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        await asyncio.sleep(2.0)
        print(f"[t+{_elapsed():7.1f}s] " + "  ".join(t.summary() for t in tallies))


async def main() -> None:
    cfg = load_machine_config(CONFIG_PATH)

    print("=== Phase 1: robot + Remote Ethernet start + PLC poll + Flexi poll ===")
    print("    (no cameras, no pump/pipette)\n")

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
    plc = LogicPLCDriver(ip=cfg.plc.ip, port=cfg.plc.port)
    flexi = FlexiCompactDriver(ip=cfg.flexi.ip, port=cfg.flexi.port)

    plc_interval_s = cfg.plc.poll_interval_ms / 1000.0
    flexi_interval_s = cfg.flexi.poll_interval_ms / 1000.0

    # Remote Ethernet MUST start the task before robot.connect() (port 2000)
    # can succeed -- sort_server.prg's own OpenNet/WaitNet on port 2000 only
    # exists once its task is actually running. Connecting port 2000 first
    # (the original bug here) hits a bare ConnectionRefused, since nothing
    # is listening yet -- confirmed 2026-08-31 on real hardware.
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

    # Retry with backoff (matches orchestrator._startup()'s own
    # 2026-08-30 finding): wait_until_running confirms the Remote Ethernet
    # status flag, but the task may still need a moment to execute past
    # its own Motor On/Speed/AccelS setup lines and reach OpenNet -- a
    # single unretried connect() here can race that.
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

    print(f"Connecting PLC at {cfg.plc.ip}:{cfg.plc.port} ...")
    await plc.connect()
    print("  PLC connected.")

    print(f"Connecting Flexi at {cfg.flexi.ip}:{cfg.flexi.port} ...")
    await flexi.connect()
    print("  Flexi connected.")

    robot_tally = Tally("robot")
    plc_tally = Tally("plc")
    flexi_tally = Tally("flexi")
    overhead_tally: Tally | None = None
    eoa_tally: Tally | None = None

    stop_event = threading.Event()
    phase2_event = threading.Event()
    threading.Thread(target=_input_thread, args=(phase2_event, stop_event), daemon=True).start()

    print("\n=== Polling started (robot every 0.5s, PLC/Flexi every "
          f"{plc_interval_s:.1f}s) ===")
    print("=== Press Enter to add the 2 cameras (Phase 2, connect only, no streaming) ===")
    print("=== Type 'stop' + Enter at any time to end cleanly ===\n")

    tasks = [
        asyncio.create_task(_robot_poll_loop(robot, robot_tally, stop_event)),
        asyncio.create_task(_plc_poll_loop(plc, plc_tally, plc_interval_s, stop_event)),
        asyncio.create_task(_flexi_poll_loop(flexi, flexi_tally, flexi_interval_s, stop_event)),
        asyncio.create_task(_summary_loop([robot_tally, plc_tally, flexi_tally], stop_event)),
    ]

    overhead_cam: GigECameraDriver | None = None
    eoa_cam: GigECameraDriver | None = None

    try:
        while not stop_event.is_set():
            if phase2_event.is_set() and overhead_cam is None:
                print(f"\n[t+{_elapsed():7.1f}s] === Phase 2: connecting cameras "
                      "(power+connect only, no streaming) ===\n")
                overhead_cam = GigECameraDriver(
                    serial=cfg.overhead_camera.serial,
                    target_ip=cfg.overhead_camera.ip,
                    exposure_us=cfg.overhead_camera.exposure_us,
                    gain_db=cfg.overhead_camera.gain_db,
                    pixel_format=cfg.overhead_camera.pixel_format,
                    max_fps=getattr(cfg.overhead_camera, "max_fps", None),
                    name="overhead",
                )
                eoa_cam = GigECameraDriver(
                    serial=cfg.eoa_camera.serial,
                    target_ip=cfg.eoa_camera.ip,
                    exposure_us=cfg.eoa_camera.exposure_us,
                    gain_db=cfg.eoa_camera.gain_db,
                    pixel_format=cfg.eoa_camera.pixel_format,
                    max_fps=getattr(cfg.eoa_camera, "max_fps", None),
                    name="eoa",
                )
                try:
                    await overhead_cam.connect()
                    print(f"[t+{_elapsed():7.1f}s]   overhead camera connected (not streaming).")
                    if stop_event.is_set():
                        print(f"[t+{_elapsed():7.1f}s]   stop requested mid-Phase-2 -- "
                              "skipping eoa camera connect.\n")
                    else:
                        await eoa_cam.connect()
                        print(f"[t+{_elapsed():7.1f}s]   eoa camera connected (not streaming).\n")
                except Exception as exc:
                    print(f"[t+{_elapsed():7.1f}s]   CAMERA CONNECT FAILED: {exc}\n")
            await asyncio.sleep(0.2)
    finally:
        stop_event.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        print(f"\n=== Stopped at t+{_elapsed():.1f}s. Final tallies: ===")
        for t in (robot_tally, plc_tally, flexi_tally):
            print(f"  {t.summary()}" + (f"  (last error: {t.last_error})" if t.fail else ""))

        print("\nDisconnecting everything ...")
        for name, coro in (
            ("overhead camera", overhead_cam.disconnect() if overhead_cam else None),
            ("eoa camera", eoa_cam.disconnect() if eoa_cam else None),
            ("flexi", flexi.disconnect()),
            ("plc", plc.disconnect()),
        ):
            if coro is None:
                continue
            try:
                await coro
            except Exception as exc:
                print(f"  ({name} disconnect error, ignoring: {exc})")

        print("Stopping robot program via Remote Ethernet and powering off motors ...")
        try:
            await robot_remote.stop()
            await asyncio.sleep(1.0)
            await robot_remote.set_motors_off()
        except Exception as exc:
            print(f"  (stop/motors-off failed: {exc})")
        try:
            await robot_remote.disconnect()
        except Exception as exc:
            print(f"  (robot_remote disconnect error, ignoring: {exc})")
        try:
            await robot.disconnect()
        except Exception as exc:
            print(f"  (robot disconnect error, ignoring: {exc})")

        print("Done.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Typing 'stop' while a camera connect() is still mid-flight (GigE
        # discovery/handshake takes a few seconds) leaves that one await
        # uncancellable until it finishes -- if you Ctrl+C out of impatience
        # during that window, this is where it lands. Cleanup in main()'s
        # finally block already ran (see its own "Done." print) by the time
        # this is reached -- this is just suppressing the noisy re-raise.
        print("\nInterrupted.")
