"""
Orchestrator — the central brain.

Runs the asyncio event loop. Owns one worker coroutine per device.
All device workers put events onto a single queue. The orchestrator
reads from that queue and feeds the state machine. The state machine
decides what to do. The orchestrator executes it.

RULES (enforced by structure, not just comments):
  - This file is the ONLY place that calls driver methods.
  - The UI never calls drivers directly.
  - Device workers never call each other.
  - All decisions go through the state machine.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import structlog

from cib.core.events import (
    AllDevicesReadyEvent,
    ConnectionLostEvent,
    DeviceName,
    FrameReadyEvent,
    CameraID,
    OperatorAbortEvent,
    OperatorAcknowledgeEvent,
    OperatorShutdownEvent,
    OperatorStartEvent,
    OperatorStopEvent,
    PLCEventLogEntry,
    PumpAction,
    PumpActionCompleteEvent,
    RobotClearedFOVEvent,
    RobotInPositionEvent,
    RobotMissionCompleteEvent,
    SafetyFaultEvent,
    SafetyFaultType,
    SafetyStatusEvent,
    SystemEvent,
    VisionEoAResultEvent,
    VisionOverheadResultEvent,
)
from cib.core.state_machine import State, SystemStateMachine
from cib.drivers.base import (
    CameraDriver,
    LightingDriver,
    PLCDriver,
    PumpDriver,
    RobotDriver,
    SafetyDriver,
    StatusLight,
)

log = structlog.get_logger(__name__)

SAFETY_POLL_INTERVAL  = 0.10   # 100ms
PLC_POLL_INTERVAL     = 0.10   # 100ms
ROBOT_POLL_INTERVAL   = 0.20   # 200ms


@dataclass
class DriverSet:
    """All hardware drivers injected into the orchestrator."""
    robot:            RobotDriver
    safety:           SafetyDriver
    plc:              PLCDriver
    pump:             PumpDriver
    camera_overhead:  CameraDriver
    camera_eoa:       CameraDriver
    lighting:         LightingDriver


class Orchestrator:
    """
    Wires together: drivers, state machine, event queue, and UI bridge.

    Instantiate with real drivers for production, mock drivers for development.
    The rest of the codebase never knows the difference.
    """

    def __init__(
        self,
        drivers: DriverSet,
        on_state_change: Callable[[State], None] | None = None,
        on_event: Callable[[SystemEvent], None] | None = None,
        overhead_save_dir: Path | None = None,
        overhead_save_format: str = "png",
        overhead_save_images: bool = True,
        force_sample_detection: bool = False,
    ) -> None:
        self._drivers = drivers
        self._queue: asyncio.Queue[SystemEvent] = asyncio.Queue()
        self._sm = SystemStateMachine(on_state_change=on_state_change)
        self._on_event = on_event     # hook for UI to observe raw events
        self._running = False
        self._cycle_active = False          # prevents concurrent _run_cycle() tasks
        self._loop: asyncio.AbstractEventLoop | None = None   # set in run()
        # Image saving config — passed from main.py via machine_config
        self._overhead_save_dir = overhead_save_dir
        self._overhead_save_format = overhead_save_format
        self._overhead_save_images = overhead_save_images
        # Dev flag: inject a fake centroid when vision finds nothing (empty dish).
        # Lets the pick-place cycle run for testing before bug detection is ready.
        self._force_sample_detection = force_sample_detection

    # ------------------------------------------------------------------
    # Public API — called by UI via bridge (thread-safe)
    # ------------------------------------------------------------------

    def operator_start(self) -> None:
        self._put_event(OperatorStartEvent())

    def operator_stop(self) -> None:
        self._put_event(OperatorStopEvent())

    def operator_abort(self) -> None:
        self._put_event(OperatorAbortEvent())

    def operator_acknowledge(self) -> None:
        """After e-stop reset or protective stop — resume."""
        self._put_event(OperatorAcknowledgeEvent())

    def operator_shutdown(self) -> None:
        """
        Disconnect all hardware cleanly and stop the orchestrator.
        Safe to call from any state. After this, the asyncio loop exits naturally —
        no force-stop needed. Call this before closing the Qt window.
        """
        self._put_event(OperatorShutdownEvent())

    def _put_event(self, event: SystemEvent) -> None:
        """Submit an event from any thread safely."""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
        else:
            self._queue.put_nowait(event)

    @property
    def state(self) -> State:
        return self._sm.current

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Start all device workers and the main event loop.
        Call this from main.py via asyncio.run().
        """
        self._loop = asyncio.get_running_loop()
        self._running = True
        log.info("orchestrator.starting")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._startup())
            tg.create_task(self._safety_poll_loop())
            tg.create_task(self._plc_poll_loop())
            tg.create_task(self._event_loop())

    async def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Startup — connect all devices
    # ------------------------------------------------------------------

    async def _startup(self) -> None:
        log.info("orchestrator.connecting_devices")
        d = self._drivers
        try:
            await asyncio.gather(
                d.robot.connect(),
                d.safety.connect(),
                d.plc.connect(),
                d.pump.connect(),
                d.camera_overhead.connect(),
                d.camera_eoa.connect(),
                d.lighting.connect(),
            )
            # Turn backlight on immediately.
            # set_backlight(True) blocks until all LEDs are at full brightness (0.5s
            # stabilisation built into the driver) — no extra sleep needed here.
            await d.lighting.set_backlight(True)
            log.info("orchestrator.backlight_on")

            await self._queue.put(AllDevicesReadyEvent())
            log.info("orchestrator.all_devices_connected")
        except Exception as exc:
            log.error("orchestrator.startup_failed", error=str(exc))
            await self._queue.put(ConnectionLostEvent(DeviceName.ROBOT, detail=str(exc)))

    # ------------------------------------------------------------------
    # Device poll loops — run forever in background
    # ------------------------------------------------------------------

    async def _safety_poll_loop(self) -> None:
        """Poll Flexi Compact every 100ms. Put safety events on queue."""
        await asyncio.sleep(0.5)    # wait for startup
        while self._running:
            try:
                status = await self._drivers.safety.read_status()
                event = SafetyStatusEvent(
                    any_fault_active=status["any_fault_active"],
                    light_curtain_tripped=status["light_curtain_tripped"],
                    estop_active=status["estop_active"],
                    estop_button_index=status["estop_button_index"],
                    contactor_closed=status["contactor_closed"],
                    reset_available=status["reset_available"],
                )
                await self._queue.put(event)
            except Exception as exc:
                log.error("safety_poll.error", error=str(exc))
                await self._queue.put(
                    ConnectionLostEvent(DeviceName.SAFETY, detail=str(exc))
                )
            await asyncio.sleep(SAFETY_POLL_INTERVAL)

    async def _plc_poll_loop(self) -> None:
        """Poll Logic PLC every 100ms for event log entries."""
        await asyncio.sleep(0.5)
        while self._running:
            try:
                entries = await self._drivers.plc.read_event_log()
                if entries:
                    await self._queue.put(PLCEventLogEntry(entries=entries))
            except Exception as exc:
                log.error("plc_poll.error", error=str(exc))
                await self._queue.put(
                    ConnectionLostEvent(DeviceName.PLC, detail=str(exc))
                )
            await asyncio.sleep(PLC_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Main event loop — state machine + command dispatch
    # ------------------------------------------------------------------

    async def _event_loop(self) -> None:
        """
        Read events from the queue, feed to state machine, dispatch commands.
        This is the single decision-making point for the entire system.
        Exits cleanly when _running is set to False by OperatorShutdownEvent.
        """
        while self._running:
            event = await self._queue.get()

            if not self._running:
                break   # shutdown event already processed, don't handle more

            if self._on_event:
                self._on_event(event)

            await self._handle_event(event)

    async def _handle_event(self, event: SystemEvent) -> None:
        sm = self._sm
        d = self._drivers

        match event:

            # ---- Startup ----
            case AllDevicesReadyEvent():
                sm.all_devices_ready()
                await d.plc.set_status_light(StatusLight.GREEN)

            # ---- Operator commands ----
            case OperatorStartEvent():
                if sm.is_safe_to_start():
                    sm.start()
                    await d.plc.set_status_light(StatusLight.GREEN)
                    # Backlight may be off after a previous STOP — turn it on and wait
                    # for full brightness before the first capture.
                    await d.lighting.set_backlight(True)
                    log.info("orchestrator.backlight_on")
                    if not self._cycle_active:
                        asyncio.create_task(self._run_cycle())

            case OperatorStopEvent():
                if sm.is_running():
                    sm.cycle_stop()
                    await d.plc.set_status_light(StatusLight.AMBER)
                    if not self._cycle_active:
                        # No cycle is running right now (e.g. system was waiting for
                        # samples with no bugs in the dish). Nothing will call
                        # _finish_stop() — do it here immediately.
                        await self._finish_stop(d)
                    # else: backlight stays ON — the active cycle will detect
                    # CYCLE_STOPPING at its next exit point and call _finish_stop().

            case OperatorAbortEvent():
                sm.abort()
                await d.lighting.set_backlight(False)
                await d.robot.move_to_safe_park()
                await d.plc.set_status_light(StatusLight.AMBER)
                log.info("orchestrator.backlight_off")

            case OperatorAcknowledgeEvent():
                if sm.current in {State.PROTECTIVE_STOP}:
                    sm.start()
                    await d.plc.set_status_light(StatusLight.GREEN)
                    if not self._cycle_active:
                        asyncio.create_task(self._run_cycle())
                elif sm.current == State.E_STOP:
                    sm.hardware_reset_complete()
                    await d.plc.set_status_light(StatusLight.AMBER)

            # ---- Shutdown ----
            case OperatorShutdownEvent():
                log.info("orchestrator.shutdown_started")
                # Turn backlight off and send DISCONNECT to lighting controller.
                # This is the clean path — avoids WinError 10054 on next startup.
                try:
                    await d.lighting.set_backlight(False)
                    await d.lighting.disconnect()
                except Exception as exc:
                    log.warning("orchestrator.shutdown_lighting_error", error=str(exc))
                # Stop all loops — _event_loop exits after _handle_event returns,
                # poll loops exit on next iteration (within 100ms).
                self._running = False
                log.info("orchestrator.shutdown_complete")

            # ---- Safety status (100ms poll) ----
            case SafetyStatusEvent():
                await self._evaluate_safety(event)

            # ---- Connection loss ----
            case ConnectionLostEvent():
                log.error("connection_lost", device=event.device.value, detail=event.detail)
                sm.fault()
                await d.plc.set_status_light(StatusLight.RED)

            # ---- PLC event log ----
            case PLCEventLogEntry():
                for entry in event.entries:
                    log.info("plc_event_log", entry=entry)

    async def _evaluate_safety(self, status: SafetyStatusEvent) -> None:
        """Check safety status and trigger state machine transitions if needed."""
        sm = self._sm
        if not status.any_fault_active:
            return

        if sm.current not in {State.RUNNING, State.CYCLE_STOPPING, State.IDLE}:
            return  # already in a fault state

        if status.estop_active:
            log.warning("safety.estop_detected", button=status.estop_button_index)
            await self._drivers.robot.halt()
            sm.safety_fault_estop()
            await self._drivers.plc.set_status_light(StatusLight.RED)

        elif status.light_curtain_tripped:
            log.warning("safety.light_curtain_tripped")
            await self._drivers.robot.halt()
            sm.safety_fault_protective()
            await self._drivers.plc.set_status_light(StatusLight.AMBER)

    # ------------------------------------------------------------------
    # Operating cycle — called as a background task when RUNNING
    # ------------------------------------------------------------------

    async def _finish_stop(self, d: DriverSet) -> None:
        """
        Called at every cycle exit point when state is CYCLE_STOPPING.
        Completes the stop: turns backlight off, parks robot, transitions to IDLE.
        """
        self._sm.cycle_complete()
        await d.lighting.set_backlight(False)
        await d.robot.move_to_safe_park()
        await d.plc.set_status_light(StatusLight.AMBER)
        log.info("orchestrator.stopped")

    async def _run_cycle(self) -> None:
        """
        Execute one pick-and-place cycle.
        Matches the 9-step operating cycle from PRD Section 12.1.
        """
        if not self._sm.is_running():
            return
        if self._cycle_active:
            return   # don't stack concurrent cycles

        self._cycle_active = True
        d = self._drivers
        log.info("cycle.start")

        try:
            # Step 1: Overhead image — backlight is already on (turned on at startup)
            frame = await d.camera_overhead.capture()

            # Step 2: Vision analysis (in thread pool — CPU bound)
            loop = asyncio.get_running_loop()
            overhead_result = await loop.run_in_executor(
                None, self._run_vision_overhead, frame
            )

            if not overhead_result.centroids_robot_xy:
                log.warning("cycle.no_samples_detected")
                if self._sm.current == State.CYCLE_STOPPING:
                    await self._finish_stop(d)
                elif self._sm.is_running():
                    # No samples in dish — wait 2s then retry so STOP can interrupt.
                    # Without the retry, the cycle exits and _cycle_active goes False,
                    # leaving STOP with no active cycle to call _finish_stop().
                    await asyncio.sleep(2.0)
                    if self._sm.is_running():
                        asyncio.create_task(self._run_cycle())
                    elif self._sm.current == State.CYCLE_STOPPING:
                        await self._finish_stop(d)
                return

            # Pick first unvisited centroid
            target_x, target_y = overhead_result.centroids_robot_xy[0]

            # Step 3-4: Robot moves to pick site, EoA image
            await d.robot.move_to_image_site(target_x, target_y)
            eoa_frame = await d.camera_eoa.capture()

            # Step 5: EoA vision analysis
            eoa_result = await loop.run_in_executor(
                None, self._run_vision_eoa, eoa_frame
            )

            # Step 6: Go / no-go
            if not eoa_result.proceed_with_pick:
                log.info("cycle.no_pick_decision")
                await d.robot.move_to_safe_park()
                if self._sm.current == State.CYCLE_STOPPING:
                    await self._finish_stop(d)
                elif self._sm.is_running():
                    asyncio.create_task(self._run_cycle())
                return

            refined_x = eoa_result.refined_x or target_x
            refined_y = eoa_result.refined_y or target_y

            # Step 7-8: Pick and place
            well_x, well_y = self._get_next_well_coords()
            await d.robot.execute_pick_place(refined_x, refined_y, well_x, well_y)
            await d.pump.aspirate(volume_ul=5.0, speed=1.0)
            await d.pump.dispense(volume_ul=5.0, speed=1.0)

            log.info("cycle.pick_complete", pick_x=refined_x, pick_y=refined_y,
                     well_x=well_x, well_y=well_y)

            # Step 9: Queue next cycle or finish stop
            if self._sm.current == State.CYCLE_STOPPING:
                await self._finish_stop(d)
            elif self._sm.is_running():
                asyncio.create_task(self._run_cycle())

        except Exception as exc:
            log.error("cycle.error", error=str(exc))
            self._sm.fault()
            await d.plc.set_status_light(StatusLight.RED)
        finally:
            self._cycle_active = False

    def _run_vision_overhead(self, frame: Any) -> VisionOverheadResultEvent:
        """Runs in thread pool — import here to keep vision isolated."""
        from cib.vision.pipeline import run_overhead_detection
        save_dir = self._overhead_save_dir if self._overhead_save_images else None
        result = run_overhead_detection(
            frame,
            save_dir=save_dir,
            save_format=self._overhead_save_format,
        )
        if self._force_sample_detection and not result.centroids_robot_xy:
            # Dev flag: inject a fake centroid at image centre so the pick-place
            # cycle keeps running even with an empty dish.
            # Remove FORCE_SAMPLE_DETECTION from main.py when real detection is ready.
            log.debug("vision.force_sample_injected",
                      note="FORCE_SAMPLE_DETECTION=True in main.py")
            import dataclasses
            result = dataclasses.replace(
                result,
                centroids_robot_xy=[(100.0, 100.0)],
                sample_count=1,
            )
        return result

    def _run_vision_eoa(self, frame: Any) -> VisionEoAResultEvent:
        from cib.vision.eoa import run_eoa_analysis
        return run_eoa_analysis(frame)

    def _get_next_well_coords(self) -> tuple[float, float]:
        """TODO: implement from plate layout + load order config."""
        return (100.0, 100.0)
