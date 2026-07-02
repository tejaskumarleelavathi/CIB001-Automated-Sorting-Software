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

SAFETY_POLL_INTERVAL       = 0.10   # 100ms
PLC_POLL_INTERVAL          = 0.10   # 100ms
ROBOT_POLL_INTERVAL        = 0.20   # 200ms
ROBOT_KEEPALIVE_INTERVAL_S = 0.5    # seconds — Epson TCP idle timer fires at ~1 s; 0.5s keeps well under it


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

    # Tolerance (mm) for "same centroid" comparison across frames.
    # Visited centroid tracking — radius-based Euclidean distance check.
    # A centroid is considered visited if it falls within centroid_snap_mm of
    # any previously attempted centroid. Stores actual float coords (not snapped)
    # so the check is not sensitive to grid-cell boundary artefacts.
    # Calibration RMSE = 0.043mm so inter-frame noise is <<0.5mm — same physical
    # spot reliably lands within the radius across cycles.
    #
    # Future: replace simple visited set with a per-centroid state machine:
    #   PICKED     → picked successfully, remove from dish model
    #   DEBRIS     → EoA confirmed not a bug, never revisit
    #   RETRY      → EoA rejected but unsure, retry at end (max 3 attempts)
    #   FAILED     → retried 3 times, no pick, alert operator
    # See architecture/vision.md § Bug Tracking State Machine.

    def __init__(
        self,
        drivers: DriverSet,
        on_state_change: Callable[[State], None] | None = None,
        on_event: Callable[[SystemEvent], None] | None = None,
        overhead_save_dir: Path | None = None,
        overhead_save_format: str = "png",
        overhead_save_images: bool = True,
        eoa_save_dir: Path | None = None,
        force_sample_detection: bool = False,
        view: bool = False,
        centroid_snap_mm: float = 0.5,
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
        # EoA annotated frame save directory (None = don't save)
        self._eoa_save_dir = eoa_save_dir
        # Dev flag: inject a fake centroid when vision finds nothing (empty dish).
        self._force_sample_detection = force_sample_detection
        # VIEW flag: show image popups for 3s during testing.
        self._view = view
        # Visited centroids — cleared at start of each run.
        # Prevents re-trying positions EoA already rejected in this run.
        self._visited_centroids: set[tuple[float, float]] = set()
        self._centroid_snap_mm = centroid_snap_mm
        # Set to True only after _startup() posts AllDevicesReadyEvent.
        # Prevents the keepalive loop from firing while sort_server.prg is
        # still executing HOME (it can take >5 s to re-enter Line Input after
        # motion completes, which would cause a keepalive STATUS timeout and
        # leave a stale "OK READY" in the TCP buffer for the next real command).
        self._startup_complete = False

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
            tg.create_task(self._robot_keepalive_loop())
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
            # Connect cameras, lighting, safety and PLC first — GigE camera
            # discovery takes ~6 seconds. Robot is connected AFTER so it is
            # never left idle on the TCP socket while we wait for cameras.
            # The Epson controller resets idle connections after a few seconds.
            await asyncio.gather(
                d.safety.connect(),
                d.plc.connect(),
                d.pump.connect(),
                d.camera_overhead.connect(),
                d.camera_eoa.connect(),
                d.lighting.connect(),
            )
            # Connect robot now — cameras are ready so we immediately follow
            # up with motion commands and keep the connection active.
            await d.robot.connect()
            # Send robot to safe park position immediately after connecting.
            # This lifts Z if the arm was left at imaging height by a previous
            # run, then moves to P0 (taught in RC+ Jog & Teach).
            await d.robot.move_to_safe_park()
            log.info("orchestrator.robot_homed")
            # Turn backlight on immediately.
            # set_backlight(True) blocks until all LEDs are at full brightness (0.5s
            # stabilisation built into the driver) — no extra sleep needed here.
            await d.lighting.set_backlight(True)
            log.info("orchestrator.backlight_on")
            # Ring light — skipped until digital output 12 is confirmed wired
            # in RC+ 7.0. Sending RING_ON when output 12 is not configured
            # causes sort_server.prg's On 12 to block ~5s then throw a SPEL+
            # error, triggering OnErr → CloseNet → TCP RST → reconnect loop.
            # Re-enable once output 12 is configured in the RC+ I/O settings.
            # await d.robot.ring_light_on()
            log.info("orchestrator.ring_light_skipped",
                     note="Re-enable once RC+ digital output 12 is configured")

            # Stop camera streams BEFORE enabling keepalive.
            # Both cameras stream at ~980 Mbps on the shared USB-Ethernet adapter.
            # If streams are still running when the keepalive first fires, the
            # STATUS packet is drowned in camera traffic, the robot's ~1s TCP idle
            # timer fires before the packet arrives, and the robot sends RST
            # (WinError 10054). Stopping streams here — before startup_complete is
            # set — guarantees the adapter is clear when the keepalive starts.
            await asyncio.gather(
                d.camera_overhead.stop_streaming(),
                d.camera_eoa.stop_streaming(),
            )
            log.info("orchestrator.camera_streams_stopped_idle")

            # Mark startup complete AFTER streams are stopped so the keepalive
            # loop only fires into a clear network (no camera bandwidth contention).
            self._startup_complete = True
            await self._queue.put(AllDevicesReadyEvent())
            log.info("orchestrator.all_devices_connected")
        except Exception as exc:
            log.error("orchestrator.startup_failed", error=str(exc))
            # Best-effort cleanup: stop streams then disconnect cameras so
            # arena_api releases devices. Without this, camera threads leave
            # devices open and the next startup fails to create_device.
            for cam in (d.camera_overhead, d.camera_eoa):
                try:
                    await cam.stop_streaming()
                except Exception:
                    pass
                try:
                    await cam.disconnect()
                except Exception:
                    pass
            try:
                await d.lighting.disconnect()
            except Exception:
                pass
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

    async def _robot_keepalive_loop(self) -> None:
        """
        Send periodic STATUS to prevent sort_server.prg from closing the idle
        TCP connection. sort_server.prg drops the connection after ~5 s of
        inactivity (its Line Input loop times out). This fires every 2.5 s so
        the connection stays alive while the system is in IDLE between cycles.

        Only fires after _startup_complete is set — sort_server.prg can take
        >5 s to re-enter Line Input after HOME motion, which would cause a
        STATUS timeout and leave a stale response in the TCP buffer.

        The driver's keepalive() skips silently if a motion command currently
        holds _cmd_lock, so there is no risk of interleaving with motion.
        """
        await asyncio.sleep(5.0)  # brief delay so startup can begin
        while self._running:
            await asyncio.sleep(ROBOT_KEEPALIVE_INTERVAL_S)
            if not self._startup_complete:
                continue   # don't touch robot until HOME + ring_light_on are done
            try:
                await self._drivers.robot.keepalive()
            except Exception as exc:
                log.debug("robot_keepalive.error", error=str(exc))

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
                    self._visited_centroids.clear()   # fresh run — reset visited set
                    await d.plc.set_status_light(StatusLight.GREEN)
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
                try:
                    await d.robot.ring_light_off()
                except Exception as exc:
                    log.warning("orchestrator.shutdown_ring_light_error", error=str(exc))
                # Send BYE to robot — sort_server.prg does CloseNet + Exit Do cleanly.
                # Without this, Python dropping the socket causes a SPEL+ runtime error
                # which leaves the motor On and trips the Flexi safety system.
                try:
                    await d.robot.disconnect()
                except Exception as exc:
                    log.warning("orchestrator.shutdown_robot_error", error=str(exc))
                # Turn backlight off and send DISCONNECT to lighting controller.
                # This is the clean path — avoids WinError 10054 on next startup.
                try:
                    await d.lighting.set_backlight(False)
                    await d.lighting.disconnect()
                except Exception as exc:
                    log.warning("orchestrator.shutdown_lighting_error", error=str(exc))
                # Destroy camera devices — arena_api holds a GVCP control channel
                # open per camera. Without explicit disconnect, the camera stays
                # "in use" and the next startup fails with GC_ERR_TIMEOUT on
                # create_device. Must always disconnect cameras on shutdown.
                for cam in (d.camera_overhead, d.camera_eoa):
                    try:
                        await cam.disconnect()
                    except Exception:
                        pass
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
        Matches the operating cycle from PRD Section 12.1.

        Bug selection order: bottom-left first in robot XY space.
          Robot coords: x increases toward bottom of dish, y increases toward right.
          Bottom-left = largest x (bottom), smallest y (left).
          Sort: y ascending first, then x descending — gives bottom-left corner first.

        Visited centroid tracking: centroids snapped to the same 0.1mm grid cell as a
          previously attempted location are skipped. Cleared at run start (OperatorStartEvent).
          Prevents infinite retry if EoA rejects the same debris each cycle.
        """
        if not self._sm.is_running():
            return
        if self._cycle_active:
            return   # don't stack concurrent cycles

        self._cycle_active = True
        d = self._drivers
        log.info("cycle.start")

        try:
            # ── Start overhead stream only ────────────────────────────────
            # EoA stream starts later, after the robot has moved to the image
            # site. Running both cameras simultaneously consumes ~980 Mbps on
            # the shared USB-Ethernet adapter, leaving almost no bandwidth for
            # robot TCP traffic. By streaming cameras only when they are
            # actively capturing, each phase has adequate bandwidth:
            #   Overhead capture:  overhead only  (~392 Mbps, Mono8 @ 2 fps)
            #   Robot motion:      no streams     (0 Mbps — robot has full 1G)
            #   EoA capture:       EoA only       (~588 Mbps, BGR8 @ 1 fps)
            await d.camera_overhead.start_streaming()
            log.debug("cycle.overhead_stream_started")

            # ── Step 1: Overhead image ────────────────────────────────────
            # Backlight already on (turned on at startup / OperatorStartEvent).
            # Drain stale frames before the keeper.
            await d.camera_overhead.capture()  # drain stale frame 1
            await d.camera_overhead.capture()  # drain stale frame 2
            frame = await d.camera_overhead.capture()  # fresh stable frame

            # Stop overhead stream now — done capturing.
            # Frees ~392 Mbps on the adapter so robot TCP has full
            # bandwidth during move_to_image_site.
            await d.camera_overhead.stop_streaming()
            log.debug("cycle.overhead_stream_stopped")

            if not self._running:
                return

            # ── Step 2: Overhead vision analysis (thread pool — CPU bound) ─
            loop = asyncio.get_running_loop()
            overhead_result = await loop.run_in_executor(
                None, self._run_vision_overhead, frame
            )

            # ── Filter: skip visited centroids ────────────────────────────
            all_centroids = overhead_result.centroids_robot_xy
            unvisited = [c for c in all_centroids if not self._is_visited(c)]

            if not unvisited:
                if all_centroids:
                    log.info("cycle.all_centroids_visited",
                             total=len(all_centroids),
                             note="All detected bugs already attempted this run")
                else:
                    log.warning("cycle.no_samples_detected")

                if self._sm.current == State.CYCLE_STOPPING:
                    await self._finish_stop(d)
                elif self._sm.is_running():
                    await asyncio.sleep(2.0)
                    if self._sm.is_running():
                        asyncio.create_task(self._run_cycle())
                    elif self._sm.current == State.CYCLE_STOPPING:
                        await self._finish_stop(d)
                return

            # ── Select target: bottom-left first ─────────────────────────
            # Sort: y ascending (leftmost), then x descending (bottommost).
            # First element = bottom-left corner bug.
            unvisited_sorted = sorted(unvisited, key=lambda c: (c[1], -c[0]))
            target_x, target_y = unvisited_sorted[0]
            log.info("cycle.target_selected",
                     target_x=target_x, target_y=target_y,
                     unvisited=len(unvisited), total=len(all_centroids))

            # Mark visited immediately — even if EoA rejects it, we won't retry
            # this exact spot in subsequent cycles (avoids infinite debris loop).
            self._mark_visited(target_x, target_y)

            # ── VIEW: show overhead with target highlighted ────────────────
            if self._view and overhead_result.annotated_frame is not None:
                from cib.vision.pipeline import draw_target_highlight
                # Find pixel centroid for selected target
                try:
                    idx = overhead_result.centroids_robot_xy.index((target_x, target_y))
                    target_px = overhead_result.centroids_px[idx]
                    highlighted = draw_target_highlight(overhead_result.annotated_frame, target_px)
                except (ValueError, IndexError):
                    highlighted = overhead_result.annotated_frame
                await loop.run_in_executor(
                    None, self._show_image, highlighted,
                    f"Overhead — {overhead_result.sample_count} bug(s), targeting ({target_x:.1f}, {target_y:.1f})"
                )

            if not self._running:
                return

            # ── Steps 3–4: Robot moves to image site + backlight off ───────
            # No camera is streaming during robot motion — the adapter is
            # fully free for TCP so keepalive and the motion command itself
            # have the full 1 Gbps link with zero contention.
            # Start backlight OFF and robot motion simultaneously — backlight
            # settles in <1s while robot travels (30–60s at 5% speed).
            await asyncio.gather(
                d.lighting.set_backlight(False),
                d.robot.move_to_image_site(target_x, target_y),
            )
            log.info("orchestrator.backlight_off_for_eoa")

            # Ring light ON — robot DO 12 illuminates specimen for EoA camera.
            # Backlight is already off. Ring light must be on before capturing.
            # Wrapped in try/except: if RC+ digital output 12 is not yet
            # configured the command will fail but we log a warning instead of
            # faulting the whole cycle (image will be dark but system continues).
            try:
                await d.robot.ring_light_on()
                log.info("cycle.ring_light_on")
            except Exception as exc:
                log.warning("cycle.ring_light_on_failed", error=str(exc),
                            note="EoA image will be dark — configure RC+ output 12 and re-run")

            # Robot is now stationary at imaging height — start EoA stream.
            await d.camera_eoa.start_streaming()
            log.debug("cycle.eoa_stream_started")

            # Frame 1: backlight OFF, ring light ON
            eoa_frame_no_backlight = await d.camera_eoa.capture()
            log.debug("cycle.eoa_captured_no_backlight")

            # Frame 2: backlight ON, ring light ON
            # EoA camera runs at 1 fps with a 2-slot buffer (start_stream(2)).
            # Both buffer slots may hold pre-backlight frames when we turn the
            # backlight on. Drain both slots before capturing the keeper so
            # eoa_frame_with_backlight is guaranteed to be a post-backlight frame.
            await d.lighting.set_backlight(True)
            await d.camera_eoa.capture()   # drain slot[0] — pre-backlight
            await d.camera_eoa.capture()   # drain slot[1] — pre-backlight
            eoa_frame_with_backlight = await d.camera_eoa.capture()
            log.debug("cycle.eoa_captured_with_backlight")
            # Leave backlight ON — overhead camera needs it for the next cycle.

            # Save both raw frames to their respective directories.
            await loop.run_in_executor(
                None, self._save_eoa_frames,
                eoa_frame_no_backlight, eoa_frame_with_backlight
            )

            # Done capturing — stop EoA stream and ring light.
            await d.camera_eoa.stop_streaming()
            log.debug("cycle.eoa_stream_stopped")

            # Ring light OFF — done capturing. Turn off before robot moves.
            try:
                await d.robot.ring_light_off()
                log.info("cycle.ring_light_off")
            except Exception as exc:
                log.warning("cycle.ring_light_off_failed", error=str(exc))

            # ── Step 5: EoA vision analysis ───────────────────────────────
            # Run analysis on no_backlight frame (ring light only — best for specimen detection).
            eoa_result = await loop.run_in_executor(
                None, self._run_vision_eoa, eoa_frame_no_backlight, target_x, target_y
            )

            # ── VIEW: show EoA annotated image ────────────────────────────
            if self._view and eoa_result.frame_path:
                await loop.run_in_executor(
                    None, self._show_image_path, eoa_result.frame_path, "EoA Analysis"
                )

            # ── Step 6: Go / no-go decision ───────────────────────────────
            log.info("cycle.eoa_classification",
                     label=eoa_result.label,
                     proceed=eoa_result.proceed_with_pick,
                     reason=eoa_result.reason,
                     body_center_px=eoa_result.body_center_px or "n/a",
                     offset_from_center_px=eoa_result.offset_from_center_px or "n/a",
                     refined_x=f"{eoa_result.refined_x:.3f}mm" if eoa_result.refined_x is not None else "no EoA cal yet",
                     refined_y=f"{eoa_result.refined_y:.3f}mm" if eoa_result.refined_y is not None else "no EoA cal yet",
                     annotated_image=eoa_result.frame_path or "not saved")
            if not eoa_result.proceed_with_pick:
                log.info("cycle.eoa_no_pick",
                         label=eoa_result.label,
                         reason=eoa_result.reason,
                         note="EoA rejected — going home, will try next bug")
                await d.robot.move_to_safe_park()
                log.info("orchestrator.robot_returning_backlight_already_on")
                if self._sm.current == State.CYCLE_STOPPING:
                    await self._finish_stop(d)
                elif self._sm.is_running():
                    asyncio.create_task(self._run_cycle())
                return

            # ── Steps 7–8: Pick and place ─────────────────────────────────
            refined_x = eoa_result.refined_x or target_x
            refined_y = eoa_result.refined_y or target_y

            # TODO: when full pick-place is implemented, execute here.
            # For now: go home after EoA to test imaging pipeline end-to-end.
            log.info("cycle.eoa_confirmed_bug",
                     refined_x=refined_x, refined_y=refined_y,
                     note="Pick-place not yet implemented — returning home")
            await d.robot.move_to_safe_park()
            log.info("orchestrator.robot_returning_backlight_already_on")

            # ── Step 9: Queue next cycle or finish stop ───────────────────
            if self._sm.current == State.CYCLE_STOPPING:
                await self._finish_stop(d)
            elif self._sm.is_running():
                asyncio.create_task(self._run_cycle())

        except Exception as exc:
            err_str = str(exc)
            # Camera start_stream failures on USB-Ethernet adapters are
            # transient — don't FAULT, just retry the cycle after a delay.
            if "start_stream" in err_str or "GC_ERR_TIMEOUT" in err_str or "get_buffer" in err_str.lower():
                log.warning("cycle.camera_error_retrying", error=err_str[:120])
                if self._sm.current == State.CYCLE_STOPPING:
                    await self._finish_stop(d)
                elif self._sm.is_running():
                    await asyncio.sleep(3.0)
                    if self._sm.is_running():
                        asyncio.create_task(self._run_cycle())
            else:
                log.error("cycle.error", error=str(exc))
                self._sm.fault()
                await d.plc.set_status_light(StatusLight.RED)
                try:
                    await d.lighting.set_backlight(False)
                except Exception:
                    pass
        finally:
            self._cycle_active = False
            # Stop streams so the adapter is free for robot TCP keepalive.
            for cam in (d.camera_overhead, d.camera_eoa):
                try:
                    await cam.stop_streaming()
                except Exception:
                    pass
            # Best-effort ring light off — in case cycle aborted mid-EoA.
            try:
                await d.robot.ring_light_off()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Centroid selection helpers
    # ------------------------------------------------------------------

    def _is_visited(self, centroid: tuple[float, float]) -> bool:
        """
        Return True if this centroid is within centroid_snap_mm of any
        previously attempted centroid (Euclidean distance check).
        """
        cx, cy = centroid
        r2 = self._centroid_snap_mm ** 2
        return any(
            (cx - vx) ** 2 + (cy - vy) ** 2 <= r2
            for vx, vy in self._visited_centroids
        )

    def _mark_visited(self, x: float, y: float) -> None:
        """Record a centroid as visited for this run (stores actual coords, not snapped)."""
        self._visited_centroids.add((float(x), float(y)))
        log.debug("cycle.centroid_marked_visited", x=x, y=y,
                  total_visited=len(self._visited_centroids))

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

    def _run_vision_eoa(self, frame: Any, robot_x: float, robot_y: float) -> VisionEoAResultEvent:
        from cib.vision.eoa import run_eoa_analysis
        return run_eoa_analysis(
            frame,
            save_dir=self._eoa_save_dir,
            robot_xy=(robot_x, robot_y),
        )

    def _save_eoa_frames(self, frame_no_backlight, frame_with_backlight) -> None:
        """Save both raw EoA frames to their respective subdirectories."""
        import cv2
        from datetime import datetime

        if self._eoa_save_dir is None:
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]

        dir_no_bl = self._eoa_save_dir / "backlight_off"
        dir_with_bl = self._eoa_save_dir / "backlight_on"
        dir_no_bl.mkdir(parents=True, exist_ok=True)
        dir_with_bl.mkdir(parents=True, exist_ok=True)

        path_no_bl = dir_no_bl / f"eoa_no_backlight_{ts}.jpg"
        path_with_bl = dir_with_bl / f"eoa_with_backlight_{ts}.jpg"

        cv2.imwrite(str(path_no_bl), frame_no_backlight)
        cv2.imwrite(str(path_with_bl), frame_with_backlight)
        log.info("cycle.eoa_frames_saved",
                 no_backlight=str(path_no_bl),
                 with_backlight=str(path_with_bl))

    def _get_next_well_coords(self) -> tuple[float, float]:
        """TODO: implement from plate layout + load order config."""
        return (100.0, 100.0)

    # ------------------------------------------------------------------
    # VIEW helpers — image display for testing (VIEW=True in main.py)
    # ------------------------------------------------------------------
    # NOTE: cv2.imshow/waitKey deadlocks on Windows when PyQt owns the
    # main thread message pump. Images are saved to disk instead; use
    # the saved file paths for review.

    def _show_image(self, img: Any, title: str) -> None:
        """Save image to disk and open with Windows default viewer (VIEW mode).

        Uses os.startfile() — launches a separate Photos/viewer process so
        there is no conflict with the PyQt6 main thread message pump.
        """
        import os
        import tempfile
        from datetime import datetime
        import cv2

        h, w = img.shape[:2]
        log.info("view.image", title=title, shape=f"{w}x{h}")

        save_dir = self._overhead_save_dir or Path(tempfile.gettempdir())
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        out_path = save_dir / f"overhead_target_{ts}.png"
        try:
            cv2.imwrite(str(out_path), img, [cv2.IMWRITE_PNG_COMPRESSION, 3])
            log.info("view.target_image_saved", path=str(out_path))
            os.startfile(str(out_path))
        except Exception as exc:
            log.warning("view.image_open_failed", error=str(exc))

    def _show_image_path(self, path: str, title: str) -> None:
        """Open a saved image with Windows default viewer (VIEW mode)."""
        import os
        if not path:
            return
        log.info("view.image_saved", title=title, path=path)
        try:
            os.startfile(path)
        except Exception as exc:
            log.warning("view.image_open_failed", path=path, error=str(exc))
